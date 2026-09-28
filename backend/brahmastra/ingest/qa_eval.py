"""
Where does a session's knowledge get lost on its way into the graph?

Step 0 of the grounded-transcript plan (docs/ROADMAP.md). `evaluate.py` scores
what comprehension FINDS; this follows the same session all the way through,
because what a person gets back is an answer from the graph, not a list of
findings:

    raw transcript -> items and points -> the part notes -> graph triples -> /ask

STAGE COVERAGE: for each labelled fact in the case, is it still present at each
stage? Matched with the evaluator's own meaning matcher, so "present" means the
same thing it means everywhere else here.

ANSWERS: each question in the case's QA set is asked two ways --
    graph   the product's own `/ask` (rag.answer_question) over the graph the
            pipeline built from this session alone
    raw     the same model answering from the best-matching raw transcript
            parts, retrieved by embedding -- what a statement- or passage-
            grounded design could reach
and graded by a DIFFERENT model against the expected answer, so a model is
never marking its own work.

Run it in a scratch store, which is the only safe place for it -- it ingests,
runs the pipeline and writes a graph:

    python -m brahmastra.scratch --db data/eval-private/qa-<case>.db \\
        -m brahmastra.ingest.qa_eval CASE.json QA.json

A persistent --db keeps the LLM memo between runs, so a second run over the
same session costs only the questions.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from typing import Any

DEFAULT_JUDGE = "groq:qwen/qwen3.8-27b"
RAW_PASSAGES = 3

JUDGE_PROMPT = """You grade an answer against the expected answer to a question about a
meeting or class. Correct means it states the substance of the expected answer
-- the same facts, names, numbers or dates. Extra correct detail is fine. A
partial answer that misses a key fact, a wrong fact, or "I don't know" is NOT
correct.

Return JSON only: {"correct": true or false, "why": "one short sentence"}"""

RAW_PROMPT = """Answer the question using ONLY the transcript excerpts given. If they do
not contain the answer, say you don't know. Be brief and specific."""


def _sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+|\n+", text or "")
    return [p.strip(" -•\t") for p in parts if len(p.strip()) > 12]


def stage_coverage(labels: list[dict[str, Any]], stages: dict[str, list[str]]) -> dict[str, Any]:
    """Which labels are still present, stage by stage."""
    from brahmastra.ingest.evaluate import _matcher, for_kind

    every = [l["statement"] for l in labels] + [t for texts in stages.values() for t in texts]
    compare, threshold = _matcher(every)
    out: dict[str, Any] = {}
    for stage, texts in stages.items():
        present, lost = [], []
        for label in labels:
            fn = for_kind(compare, label["kind"])
            best = max((fn(label["statement"], t) for t in texts), default=0.0)
            (present if best >= threshold else lost).append(label["statement"])
        out[stage] = {"present": len(present), "of": len(labels), "lost": lost}
    return out


def _judge(question: str, expected: str, answer: str, judge_model: str) -> dict[str, Any]:
    from brahmastra.llm import chat, using_model

    with using_model(judge_model):
        raw = chat(JUDGE_PROMPT,
                   f"Question: {question}\nExpected answer: {expected}\nAnswer given: {answer}",
                   json_mode=True, temperature=0.0, max_tokens=400)
    try:
        data = json.loads(raw[raw.find("{"): raw.rfind("}") + 1])
        return {"correct": bool(data.get("correct")), "why": str(data.get("why") or "")}
    except Exception:                                          # noqa: BLE001
        return {"correct": False, "why": f"unparseable grade: {raw[:80]!r}"}


def _raw_answer(question: str, passages: list[str]) -> str:
    from brahmastra.embeddings import embed
    from brahmastra.llm import chat

    vectors = embed([question] + passages)
    q, rest = vectors[0], vectors[1:]
    ranked = sorted(range(len(passages)),
                    key=lambda i: -sum(a * b for a, b in zip(q, rest[i])))[:RAW_PASSAGES]
    context = "\n\n---\n\n".join(passages[i] for i in sorted(ranked))
    return chat(RAW_PROMPT, f"Transcript excerpts:\n\n{context}\n\nQuestion: {question}",
                temperature=0.0, max_tokens=1200)


def run(case_path: Path, qa_path: Path, judge_model: str = DEFAULT_JUDGE,
        arms: tuple[str, ...] = ("graph", "raw")) -> dict[str, Any]:
    from brahmastra import db
    from brahmastra.ingest.assemble import process_transcript
    from brahmastra.ingest.segment import segment
    from brahmastra.ingest.store import Transcript, get_ingest_store
    from brahmastra.pipeline import run_pipeline
    from brahmastra.rag import answer_question

    case = json.loads(case_path.read_text(encoding="utf-8"))
    questions = json.loads(qa_path.read_text(encoding="utf-8"))
    name = case.get("name") or case_path.stem

    store = get_ingest_store()
    existing = [t for t in store.list_transcripts(limit=200) if t["title"] == name]
    tid = existing[0]["id"] if existing else store.create_transcript(
        Transcript("", name, case["transcript"], mode=case.get("mode")))

    started = time.perf_counter()
    ingest = process_transcript(tid, store=store)
    if ingest.get("status") != "ok":
        # Scoring a half-read session measures the quota, not the design.
        raise RuntimeError(f"ingestion was not clean ({ingest.get('status')}): "
                           f"{(ingest.get('errors') or ingest.get('degraded'))!r}"[:600])
    pipeline = run_pipeline(full=False)

    chunks = segment(case["transcript"])
    artifacts = [a for a in store.get_artifacts(transcript_id=tid, limit=100_000)
                 if not a.get("superseded_by")]
    notes = [n for n in db.get_notes() if n["id"].startswith(tid)]
    triples = [t for t in db.get_all_triples()
               if str(t.get("source_note_id") or "").startswith(tid)]
    stages = {
        "raw": [t.text for c in chunks for t in c.turns],
        "items": [a["statement"] for a in artifacts],
        "notes": [s for n in notes for s in _sentences(n["content"])],
        "triples": [f"{t['subject_text']} {t['relation'].replace('_', ' ')} {t['object_text']}"
                    for t in triples],
    }
    coverage = stage_coverage(case["expected"], stages)

    passages = [c.text for c in chunks]
    graded: list[dict[str, Any]] = []
    for q in questions:
        row: dict[str, Any] = {"kind": q.get("kind", "item"), "question": q["question"]}
        for arm in arms:
            try:
                answer = (answer_question(q["question"])["answer"] if arm == "graph"
                          else _raw_answer(q["question"], passages))
            except Exception as exc:                           # noqa: BLE001
                answer = f"(failed: {type(exc).__name__}: {exc})"[:300]
            row[arm] = {"answer": answer, **_judge(q["question"], q["answer"], answer, judge_model)}
        graded.append(row)

    summary: dict[str, Any] = {}
    for arm in arms:
        for kind in ("item", "detail"):
            rows = [r for r in graded if r["kind"] == kind]
            summary[f"{arm}:{kind}"] = f"{sum(r[arm]['correct'] for r in rows)}/{len(rows)}"
    return {
        "case": name, "mode": case.get("mode") or "meeting",
        "ingest_status": ingest.get("status"), "pipeline_status": pipeline.get("status"),
        "notes": len(notes), "triples": len(triples), "seconds": round(time.perf_counter() - started),
        "coverage": {k: f"{v['present']}/{v['of']}" for k, v in coverage.items()},
        "lost": {k: v["lost"] for k, v in coverage.items()},
        "answers": summary, "graded": graded,
    }


# The scratch runner loads NO .env (BRAHMASTRA_NO_DOTENV), so storage cannot
# reach production -- and so no provider either. This takes back ONLY what an
# LLM call needs. Found the hard way: the first run ingested and built a graph
# with no model at all, and failed only when the judge was asked.
_LLM_VARS = re.compile(r"^(GROQ_API_KEYS?|LLM_PROVIDER|GROQ_DEFAULT_MODEL|OLLAMA_\w+|"
                       r"RESOLUTION_LLM_MODEL|ENTITY_CONFIRM)$")


def load_llm_env(path: Path | None = None) -> list[str]:
    import os

    path = path or Path(__file__).resolve().parents[2] / ".env"
    loaded = []
    for line in path.read_text(encoding="utf-8").splitlines():
        name, sep, value = line.strip().partition("=")
        if sep and _LLM_VARS.match(name.strip()) and not os.environ.get(name.strip()):
            os.environ[name.strip()] = value.strip().strip('"').strip("'")
            loaded.append(name.strip())
    return loaded


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("case", type=Path)
    parser.add_argument("qa", type=Path)
    parser.add_argument("--judge", default=DEFAULT_JUDGE)
    parser.add_argument("--out", type=Path, default=None, help="write the full result as JSON")
    args = parser.parse_args(argv)

    load_llm_env()
    result = run(args.case, args.qa, args.judge)
    if args.out:
        args.out.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    w = lambda s: sys.stdout.buffer.write((str(s) + "\n").encode("utf-8"))
    w(f"\n{result['case']} ({result['mode']}): ingest {result['ingest_status']}, "
      f"pipeline {result['pipeline_status']}, {result['notes']} notes, {result['triples']} triples")
    w(f"  facts present by stage: {result['coverage']}")
    w(f"  answers correct: {result['answers']}")
    for stage in ("notes", "triples"):
        for lost in result["lost"][stage][:8]:
            w(f"  lost by {stage}: {lost[:90]}")
    for r in result["graded"]:
        marks = " ".join(f"{arm}={'Y' if r[arm]['correct'] else 'n'}" for arm in ("graph", "raw") if arm in r)
        w(f"  [{r['kind']:6}] {marks}  {r['question'][:70]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
