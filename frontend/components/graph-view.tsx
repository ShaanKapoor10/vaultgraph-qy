"use client"

import { useEffect, useMemo, useRef, useState } from "react"
import {
  forceSimulation,
  forceLink,
  forceManyBody,
  forceCenter,
  forceCollide,
  type Simulation,
} from "d3-force"
import type { ConceptGraph, ConceptCluster, CentralEntity } from "@/lib/types"
import { clusterColor, RELATION_LABEL } from "@/lib/viz"

interface SimNode {
  id: string
  cluster: number
  radius: number
  x?: number
  y?: number
  fx?: number | null
  fy?: number | null
}
interface SimLink {
  source: SimNode | string
  target: SimNode | string
  relation: keyof typeof RELATION_LABEL
}

interface GraphViewProps {
  graph: ConceptGraph
  clusters: ConceptCluster[]
  central: CentralEntity[]
  selected: string | null
  onSelect: (id: string | null) => void
}

const clamp = (v: number, lo: number, hi: number) => Math.min(hi, Math.max(lo, v))

export function GraphView({ graph, clusters, central, selected, onSelect }: GraphViewProps) {
  const wrapRef = useRef<HTMLDivElement>(null)
  const svgRef = useRef<SVGSVGElement>(null)
  const [size, setSize] = useState({ w: 800, h: 560 })
  const [, force] = useState(0) // re-render trigger
  const simRef = useRef<Simulation<SimNode, SimLink> | null>(null)
  const nodesRef = useRef<SimNode[]>([])
  const linksRef = useRef<SimLink[]>([])
  const draggingRef = useRef<string | null>(null)

  // viewport transform (pan + zoom)
  const [view, setView] = useState({ x: 0, y: 0, k: 1 })
  const viewRef = useRef(view)
  useEffect(() => {
    viewRef.current = view
  }, [view])
  // background-pan state
  const panRef = useRef<{ sx: number; sy: number; ox: number; oy: number } | null>(null)
  const movedRef = useRef(false)

  // cluster lookup + centrality lookup
  const clusterOf = useMemo(() => {
    const m = new Map<string, number>()
    clusters.forEach((c) => c.members.forEach((mm) => m.set(mm, c.id)))
    return m
  }, [clusters])

  const scoreOf = useMemo(() => {
    const m = new Map<string, number>()
    let max = 0
    central.forEach((c) => {
      m.set(c.entity, c.score)
      max = Math.max(max, c.score)
    })
    return { m, max: max || 1 }
  }, [central])

  // Resize observer
  useEffect(() => {
    if (!wrapRef.current) return
    const el = wrapRef.current
    const ro = new ResizeObserver(() => {
      setSize({ w: el.clientWidth, h: Math.max(420, el.clientHeight) })
    })
    ro.observe(el)
    setSize({ w: el.clientWidth, h: Math.max(420, el.clientHeight) })
    return () => ro.disconnect()
  }, [])

  // Build + run simulation when the graph data changes
  useEffect(() => {
    const nodes: SimNode[] = graph.nodes.map((n) => {
      const s = scoreOf.m.get(n.id) ?? 0
      return {
        id: n.id,
        cluster: clusterOf.get(n.id) ?? 0,
        radius: 7 + (s / scoreOf.max) * 16,
      }
    })
    const nodeIndex = new Map(nodes.map((n) => [n.id, n]))
    // unique undirected links for layout
    const seen = new Set<string>()
    const links: SimLink[] = []
    for (const e of graph.edges) {
      const key = [e.source, e.target].sort().join("|")
      if (seen.has(key)) continue
      seen.add(key)
      links.push({
        source: nodeIndex.get(e.source)!,
        target: nodeIndex.get(e.target)!,
        relation: e.relation,
      })
    }
    nodesRef.current = nodes
    linksRef.current = links

    const sim = forceSimulation<SimNode>(nodes)
      .force(
        "link",
        forceLink<SimNode, SimLink>(links)
          .id((d) => d.id)
          .distance(90)
          .strength(0.4),
      )
      .force("charge", forceManyBody().strength(-340))
      .force("center", forceCenter(size.w / 2, size.h / 2))
      .force("collide", forceCollide<SimNode>().radius((d) => d.radius + 8))
      .on("tick", () => force((v) => v + 1))

    simRef.current = sim
    sim.alpha(1).restart()
    return () => {
      sim.stop()
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [graph, clusterOf, scoreOf])

  // keep center updated on resize
  useEffect(() => {
    const sim = simRef.current
    if (!sim) return
    sim.force("center", forceCenter(size.w / 2, size.h / 2))
    sim.alpha(0.3).restart()
  }, [size])

  const nodes = nodesRef.current
  const links = linksRef.current

  // neighbors of selected for highlight
  const neighborSet = useMemo(() => {
    if (!selected) return null
    const s = new Set<string>([selected])
    for (const e of graph.edges) {
      if (e.source === selected) s.add(e.target)
      if (e.target === selected) s.add(e.source)
    }
    return s
  }, [selected, graph.edges])

  // screen → world coords (accounting for pan + zoom)
  const toWorld = (clientX: number, clientY: number) => {
    const rect = svgRef.current?.getBoundingClientRect()
    const v = viewRef.current
    const sx = clientX - (rect?.left ?? 0)
    const sy = clientY - (rect?.top ?? 0)
    return { x: (sx - v.x) / v.k, y: (sy - v.y) / v.k }
  }

  // Wheel zoom toward cursor — native listener so we can preventDefault (passive:false)
  useEffect(() => {
    const svg = svgRef.current
    if (!svg) return
    const onWheel = (e: WheelEvent) => {
      e.preventDefault()
      const rect = svg.getBoundingClientRect()
      const sx = e.clientX - rect.left
      const sy = e.clientY - rect.top
      setView((v) => {
        const k = clamp(v.k * (1 - e.deltaY * 0.0012), 0.2, 4)
        const wx = (sx - v.x) / v.k
        const wy = (sy - v.y) / v.k
        return { k, x: sx - wx * k, y: sy - wy * k }
      })
    }
    svg.addEventListener("wheel", onWheel, { passive: false })
    return () => svg.removeEventListener("wheel", onWheel)
  }, [])

  // node drag start
  const onNodePointerDown = (e: React.PointerEvent, id: string) => {
    e.stopPropagation()
    draggingRef.current = id
    ;(e.target as Element).setPointerCapture?.(e.pointerId)
    simRef.current?.alphaTarget(0.3).restart()
  }

  // background pan start
  const onBgPointerDown = (e: React.PointerEvent) => {
    panRef.current = { sx: e.clientX, sy: e.clientY, ox: view.x, oy: view.y }
    movedRef.current = false
    ;(e.currentTarget as Element).setPointerCapture?.(e.pointerId)
  }

  const onPointerMove = (e: React.PointerEvent) => {
    const id = draggingRef.current
    if (id) {
      const { x, y } = toWorld(e.clientX, e.clientY)
      const n = nodes.find((nn) => nn.id === id)
      if (n) {
        n.fx = x
        n.fy = y
      }
      return
    }
    const pan = panRef.current
    if (pan) {
      const dx = e.clientX - pan.sx
      const dy = e.clientY - pan.sy
      if (Math.abs(dx) > 3 || Math.abs(dy) > 3) movedRef.current = true
      setView((v) => ({ ...v, x: pan.ox + dx, y: pan.oy + dy }))
    }
  }

  const onPointerUp = () => {
    const id = draggingRef.current
    if (id) {
      const n = nodes.find((nn) => nn.id === id)
      if (n) {
        n.fx = null
        n.fy = null
      }
      draggingRef.current = null
      simRef.current?.alphaTarget(0)
      return
    }
    if (panRef.current) {
      const wasClick = !movedRef.current
      panRef.current = null
      if (wasClick) onSelect(null) // click on empty space deselects
    }
  }

  const resetView = () => setView({ x: 0, y: 0, k: 1 })
  const zoomBy = (factor: number) =>
    setView((v) => {
      const k = clamp(v.k * factor, 0.2, 4)
      const cx = size.w / 2
      const cy = size.h / 2
      const wx = (cx - v.x) / v.k
      const wy = (cy - v.y) / v.k
      return { k, x: cx - wx * k, y: cy - wy * k }
    })

  return (
    <div ref={wrapRef} className="relative h-full w-full">
      <svg
        ref={svgRef}
        width={size.w}
        height={size.h}
        className={`block touch-none ${panRef.current ? "cursor-grabbing" : "cursor-grab"}`}
        onPointerDown={onBgPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
        onPointerLeave={onPointerUp}
      >
        <defs>
          <marker id="arrow" viewBox="0 -5 10 10" refX="22" refY="0" markerWidth="6" markerHeight="6" orient="auto">
            <path d="M0,-4L8,0L0,4" className="fill-muted-foreground/50" />
          </marker>
        </defs>

        <g transform={`translate(${view.x},${view.y}) scale(${view.k})`}>
        {/* edges */}
        <g>
          {links.map((l, i) => {
            const s = l.source as SimNode
            const t = l.target as SimNode
            if (s.x == null || t.x == null) return null
            const active = !neighborSet || (neighborSet.has(s.id) && neighborSet.has(t.id))
            return (
              <line
                key={i}
                x1={s.x}
                y1={s.y}
                x2={t.x}
                y2={t.y}
                strokeWidth={1.2}
                className={active ? "stroke-muted-foreground/45" : "stroke-muted-foreground/10"}
                markerEnd="url(#arrow)"
              />
            )
          })}
        </g>

        {/* nodes */}
        <g>
          {nodes.map((n) => {
            if (n.x == null) return null
            const dimmed = neighborSet && !neighborSet.has(n.id)
            const isSel = selected === n.id
            return (
              <g
                key={n.id}
                transform={`translate(${n.x},${n.y})`}
                className="cursor-pointer"
                opacity={dimmed ? 0.25 : 1}
                onPointerDown={(e) => onNodePointerDown(e, n.id)}
                onClick={(e) => {
                  e.stopPropagation()
                  onSelect(n.id === selected ? null : n.id)
                }}
              >
                <circle
                  r={n.radius}
                  fill={clusterColor(n.cluster)}
                  className={isSel ? "stroke-foreground" : "stroke-background"}
                  strokeWidth={isSel ? 2.5 : 1.5}
                />
                <text
                  y={n.radius + 13}
                  textAnchor="middle"
                  className="pointer-events-none fill-foreground/85 font-mono text-[10px]"
                >
                  {n.id.length > 22 ? n.id.slice(0, 21) + "…" : n.id}
                </text>
              </g>
            )
          })}
        </g>
        </g>
      </svg>

      {/* zoom / pan controls */}
      <div className="absolute right-3 top-3 flex flex-col gap-1">
        <button
          onClick={() => zoomBy(1.25)}
          className="flex h-7 w-7 items-center justify-center rounded-md border border-border bg-card/80 font-mono text-sm text-foreground/80 backdrop-blur hover:bg-card"
          title="Zoom in"
        >
          +
        </button>
        <button
          onClick={() => zoomBy(0.8)}
          className="flex h-7 w-7 items-center justify-center rounded-md border border-border bg-card/80 font-mono text-sm text-foreground/80 backdrop-blur hover:bg-card"
          title="Zoom out"
        >
          −
        </button>
        <button
          onClick={resetView}
          className="flex h-7 w-7 items-center justify-center rounded-md border border-border bg-card/80 font-mono text-[9px] uppercase text-foreground/80 backdrop-blur hover:bg-card"
          title="Reset view"
        >
          ⟳
        </button>
      </div>

      {/* legend */}
      <div className="pointer-events-none absolute bottom-3 left-3 flex flex-col gap-1 rounded-md border border-border bg-card/80 p-2.5 backdrop-blur">
        <span className="mb-0.5 font-mono text-[10px] uppercase tracking-wider text-muted-foreground">Clusters</span>
        {clusters.slice(0, 5).map((c) => (
          <div key={c.id} className="flex items-center gap-2">
            <span className="h-2.5 w-2.5 rounded-full" style={{ backgroundColor: clusterColor(c.id) }} />
            <span className="font-mono text-[11px] text-foreground/80">
              {c.members.length} node{c.members.length === 1 ? "" : "s"}
            </span>
          </div>
        ))}
      </div>
    </div>
  )
}
