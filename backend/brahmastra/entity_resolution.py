"""
Stage 4 — Entity Resolution.

Algorithm:
1. Collect all unique entity mentions from raw_triples (subject_text + object_text).
2. Compute pairwise similarity using a cascade of heuristics:
     a. Exact match (after normalisation)               → sim = 1.0
     b. Token-subset match (one name is subset of other) → sim = 0.9
     c. Acronym expansion                               → sim = 0.88
     d. Jaro-Winkler string distance                    → sim if ≥ threshold
     e. Cosine similarity of sentence-transformers embeddings → sim if ≥ threshold
3. Feed candidate pairs (sim ≥ MERGE_THRESHOLD) into Union-Find.
4. Each component becomes an entity cluster; the canonical name is the
   longest / most-specific mention in the cluster.
5. Write canonical_map + entity_clusters to SQLite.

sentence-transformers is imported lazily — if unavailable (e.g. first run
before the model downloads), heuristics-only mode is used automatically.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
from typing import Any, Collection, Iterable

# Quiet the noisy HF / transformers output ("unauthenticated requests to HF Hub",
# "Loading weights 100%") emitted when the sentence-transformers model loads.
os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
os.environ.setdefault("TRANSFORMERS_VERBOSITY", "error")
os.environ.setdefault("TRANSFORMERS_NO_ADVISORY_WARNINGS", "1")
for _noisy in ("transformers", "sentence_transformers", "huggingface_hub"):
    logging.getLogger(_noisy).setLevel(logging.ERROR)

from brahmastra import db, entity_confirm

# ---------------------------------------------------------------------------
# Thresholds
# ---------------------------------------------------------------------------

JARO_THRESHOLD = 0.92          # jellyfish jaro_winkler_similarity
EMBEDDING_THRESHOLD = 0.82     # cosine similarity of sentence-transformer embeddings
MERGE_THRESHOLD = 0.85         # minimum sim to merge two mentions


# ---------------------------------------------------------------------------
# Text normalisation
# ---------------------------------------------------------------------------

def _normalise(text: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace."""
    text = text.lower()
    text = re.sub(r"[^\w\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _tokens(text: str) -> set[str]:
    return set(_normalise(text).split())


# ---------------------------------------------------------------------------
# Contrast guard — block merging entities distinguished only by antonym tokens
# (e.g. "Brahmastra backend" vs "Brahmastra frontend" embed at ~0.94 but are
#  distinct entities). Without this, embedding similarity over-merges them.
# ---------------------------------------------------------------------------

_CONTRAST_GROUPS: list[set[str]] = [
    {"backend", "frontend"},
    {"client", "server"},
    {"input", "output"},
    {"read", "write"},
    {"source", "target"},
    {"public", "private"},
    {"internal", "external"},
    {"dev", "development", "prod", "production", "staging", "test"},
    {"request", "response"},
    {"get", "set", "post", "put", "delete", "patch"},
    {"open", "close"},
    {"start", "stop", "end"},
    {"min", "max"},
    {"upload", "download"},
    {"encode", "decode"},
]


# A name that DENIES something is not a longer way of saying it.
#
# Found in the live graph, not imagined:
#
#     0.952   "Do not move the release to April 15th"
#          == "Move the release to April 15th"
#
# Merged into one entity. The pair-difference rule below could not see it,
# because it fires only when exactly one token differs on EACH side and here
# one side simply has two extra words. So the two halves of a reversed
# decision became the same node in a graph whose entire job is recording what
# was decided.
#
# Same failure consolidate.py records for STATEMENTS -- "embeddings place a
# sentence and its negation almost on top of each other, because they share
# every content word". This is the third layer to pay for it. Anywhere
# embedding similarity decides that two things are THE SAME, assume it cannot
# tell a thing from its opposite, and check.
# DELIBERATELY WIDER THAN consolidate.POLARITY_SENSITIVE, which leaves bare
# "no" out because "there is no runbook" is a positive assertion OF a risk and
# splitting it from its own paraphrase would cost recall. That reasoning is
# about STATEMENTS being scored for overlap. These are entity NAMES, where the
# costs run the other way: a spurious extra node is visible in the graph and
# mergeable by hand, and a fused assertion-and-denial is not visible at all.
_NEGATORS = frozenset({
    "no", "not", "never", "cannot", "cant", "wont", "dont", "doesnt", "didnt",
    "without", "excluding", "except", "instead", "rather",
})


def _negation_differs(ta: set[str], tb: set[str]) -> bool:
    """True when one name negates and the other does not."""
    return bool(ta & _NEGATORS) != bool(tb & _NEGATORS)


# Two things whose names an embedding cannot tell apart, and a rule can.
#
# From the live graph, read-only over 907 mentions:
#
#     0.910   backend/brahmastra/ingest/memo.py == backend/brahmastra/memo.py
#     0.914   function run_pipeline             == run_full_pipeline function
#
# Two different files fused into one node; two different functions fused into
# another. Neither is a threshold problem -- "pipeline.py"/"file pipeline.py"
# scores 0.888 and is RIGHT while the memo pair scores 0.910 and is wrong, so
# no cut separates them. But both are decidable without a model, because a
# path and an identifier are structured text rather than prose, and the
# structure says which is which.
#
# These are deliberately narrow. They answer "are these provably two things?"
# and never "are these the same thing?" -- everything they do not recognise
# falls through to the similarity cascade exactly as before.

_PATHISH = re.compile(r"[\w./-]+\.[A-Za-z]{1,5}$")
_IDENTIFIER = re.compile(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b")


def _path_of(name: str) -> str | None:
    """The file path inside a mention, if the mention is about a file."""
    for token in name.split():
        token = token.strip("`'\"(),")
        if _PATHISH.match(token):
            return token.replace("\\", "/").lower()
    return None


def _different_files(a: str, b: str) -> bool:
    """
    True when both names denote files and they are provably NOT the same file.

    Same basename, different directory. "app/page.tsx" and "page.tsx" are one
    file written two ways, because one path ends with the other; "src/a/util.py"
    and "src/b/util.py" are two files, because neither does.
    """
    pa, pb = _path_of(a), _path_of(b)
    if not pa or not pb or pa == pb:
        return False
    if pa.split("/")[-1] != pb.split("/")[-1]:
        return False          # different basenames: the cascade can judge it
    long, short = (pa, pb) if len(pa) >= len(pb) else (pb, pa)
    # One path being a tail of the other is the same file named more fully.
    return not long.endswith("/" + short) and long != short


def _different_identifiers(a: str, b: str) -> bool:
    """
    True when both names carry snake_case identifiers and none is shared.

    "function run_pipeline" and "run_pipeline" share one, so they are the same
    function described twice. "run_pipeline" and "run_full_pipeline" share
    none, so they are two functions whose names merely resemble each other --
    which is exactly the case Jaro-Winkler and cosine both get wrong.

    A KNOWN OVERREACH, kept deliberately. "brahmastra_add_note" and "add_note"
    are refused although they are plausibly one tool named short and long. The
    obvious fix -- allow it when one identifier is a suffix of the other -- was
    tried and rejected, because "mcp_server" is a suffix of "test_mcp_server"
    and those are a module and its test, which are two files. No rule over
    identifier text alone separates a namespacing prefix from a qualifying one.

    So the trade is taken on purpose, in the direction this system argues for
    everywhere else: a refusal costs a visible duplicate node a reader can
    merge, a wrong merge costs an invisible fusion nobody can see. On the live
    corpus the shape does not occur -- all ten refusals this rule produces
    there are correct.
    """
    ia = set(_IDENTIFIER.findall(a.lower()))
    ib = set(_IDENTIFIER.findall(b.lower()))
    if not ia or not ib:
        return False
    return not (ia & ib)


def is_distinct(a: str, b: str) -> bool:
    """Provably two things. See _different_files and _different_identifiers."""
    return _different_files(a, b) or _different_identifiers(a, b)


def _is_contrasting(a: str, b: str) -> bool:
    """
    True if a and b look like opposites rather than variants of one name.

    Two rules, and they catch different shapes. The pair rule handles names of
    the same length differing by one known antonym ("backend" / "frontend").
    The negation rule handles one name denying what the other asserts, which
    the pair rule structurally cannot see.

    Deliberately asymmetric in cost: a guard that fires too eagerly leaves two
    nodes where one would do, which a reader can see and merge. A guard that
    misses fuses an assertion with its denial into a single entity, which
    nobody can see at all.
    """
    ta, tb = _tokens(a), _tokens(b)
    if _negation_differs(ta, tb):
        return True

    only_a, only_b = ta - tb, tb - ta
    if len(only_a) == 1 and len(only_b) == 1:
        x, y = next(iter(only_a)), next(iter(only_b))
        for grp in _CONTRAST_GROUPS:
            if x in grp and y in grp:
                return True
    return False


# ---------------------------------------------------------------------------
# Acronym detection
# ---------------------------------------------------------------------------

def _is_acronym_of(short: str, full: str) -> bool:
    """Return True if `short` (uppercased) is an acronym formed from `full`."""
    if not short.isupper() or len(short) < 2:
        return False
    words = [w for w in _normalise(full).split() if w]
    if len(words) != len(short):
        return False
    return all(w[0] == c for w, c in zip(words, short.lower()))


# ---------------------------------------------------------------------------
# Pairwise heuristic similarity
# ---------------------------------------------------------------------------

def _heuristic_sim(a: str, b: str) -> tuple[float, str]:
    """
    Return (similarity, method_name).
    Returns (0.0, "none") if no heuristic triggers.
    """
    na, nb = _normalise(a), _normalise(b)

    # 1. Exact after normalisation
    if na == nb:
        return 1.0, "exact"

    # 2. Token subset (one is fully contained in the other)
    ta, tb = _tokens(a), _tokens(b)
    if ta and tb:
        if ta.issubset(tb) or tb.issubset(ta):
            score = min(len(ta), len(tb)) / max(len(ta), len(tb))
            if score >= 0.5:      # avoid merging single-token names too aggressively
                return 0.9 * score + 0.1, "token_subset"

    # 3. Acronym expansion
    if _is_acronym_of(a, b) or _is_acronym_of(b, a):
        return 0.88, "acronym"

    # 4. Jaro-Winkler
    try:
        import jellyfish
        jw = jellyfish.jaro_winkler_similarity(na, nb)
        if jw >= JARO_THRESHOLD:
            return float(jw), "jaro_winkler"
    except ImportError:
        pass

    return 0.0, "none"


# ---------------------------------------------------------------------------
# Embedding-based similarity (lazy load)
# ---------------------------------------------------------------------------

def _get_embedder():
    """
    Shared loader (brahmastra.embeddings) so the weights load once per process
    rather than once here and again for semantic search. Also fixes the cache
    path, which used to be relative and landed in backend/backend/.cache.
    """
    from brahmastra.embeddings import get_model
    return get_model()


def _embedding_sim(mentions: list[str]) -> dict[tuple[str, str], float]:
    """
    Compute pairwise cosine similarity for all mention pairs.
    Returns a dict {(a, b): sim} for pairs whose sim ≥ EMBEDDING_THRESHOLD.
    Returns {} if sentence-transformers unavailable.
    """
    model = _get_embedder()
    if model is None or len(mentions) < 2:
        return {}

    try:
        import numpy as np

        embeddings = model.encode(mentions, normalize_embeddings=True)
        # Cosine similarity = dot product when embeddings are L2-normalised
        sim_matrix = embeddings @ embeddings.T

        result: dict[tuple[str, str], float] = {}
        n = len(mentions)
        for i in range(n):
            for j in range(i + 1, n):
                s = float(sim_matrix[i, j])
                if s >= EMBEDDING_THRESHOLD:
                    result[(mentions[i], mentions[j])] = s
        return result
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Union-Find
# ---------------------------------------------------------------------------

class _UnionFind:
    def __init__(self, items: list[str]) -> None:
        self._parent: dict[str, str] = {x: x for x in items}
        self._rank: dict[str, int] = {x: 0 for x in items}

    def find(self, x: str) -> str:
        while self._parent[x] != x:
            self._parent[x] = self._parent[self._parent[x]]  # path compression
            x = self._parent[x]
        return x

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return
        if self._rank[ra] < self._rank[rb]:
            ra, rb = rb, ra
        self._parent[rb] = ra
        if self._rank[ra] == self._rank[rb]:
            self._rank[ra] += 1

    def components(self) -> list[list[str]]:
        groups: dict[str, list[str]] = {}
        for x in self._parent:
            root = self.find(x)
            groups.setdefault(root, []).append(x)
        return list(groups.values())


# ---------------------------------------------------------------------------
# Canonical name selection
# ---------------------------------------------------------------------------

# Bump to re-key every cluster at once. Only a change to what IDENTIFIES a
# cluster needs it -- not a change to how one is named, scored or rendered.
CLUSTER_ID_VERSION = "1"


def cluster_id_for(mentions: Iterable[str]) -> str:
    """
    A cluster's id, derived from WHO IS IN IT rather than from where it landed.

    It used to be `f"c{i:04d}"` -- the cluster's position in the list
    UnionFind happened to return. Measured on the live graph, 970 triples and
    901 clusters, by running the resolver twice in two processes over
    BYTE-IDENTICAL input:

        cluster ids naming the same members    2 of 901
        derived ids naming the same members  901 of 901

    Two, out of nine hundred and one. The mentions are collected into a SET,
    and set iteration order for strings depends on the hash seed, which differs
    per process -- so the positions were never stable, and every pipeline run
    rewrote the entire canonical map for no reason at all. Growing the corpus
    was no better: clustering 70% of the notes and then all of them left 1 of
    667 positional ids intact, against 645 of 667 derived ones.

    cocoindex names this exactly: ids not derived from the data mean every
    reprocessing run produces different ids for the same data, so the target
    churns -- old rows deleted, identical rows inserted under new keys.

    The id changes when the MEMBERSHIP changes, which is correct and is the
    same rule cluster summaries already use: a cluster that gained a mention is
    not the cluster that existed before, and nothing downstream should assume
    it is.
    """
    h = hashlib.sha256()
    h.update(CLUSTER_ID_VERSION.encode("utf-8"))
    h.update(bytes([0]))
    for mention in sorted(mentions):
        h.update(mention.encode("utf-8", "replace"))
        h.update(bytes([0]))
    return "c" + h.hexdigest()[:12]


_WORD = re.compile(r"[A-Za-z0-9]+")


def _words(name: str) -> set[str]:
    return {w.lower() for w in _WORD.findall(name)}


def _more_specific(existing: str, candidate: str) -> bool:
    """
    Is `candidate` the same name as `existing`, stated more fully?

    True only for a STRICT word-superset: "Sarah" -> "Sarah Chen",
    "pipeline.py" -> "file pipeline.py". Not for a respelling, a plural or a
    reordering, all of which are the same name written differently and are
    exactly what pinning exists to stop flapping between.

    This is the one escape hatch in the PINNED policy, and it is narrow on
    purpose. Checked against all eight clusters where pinning actually changed
    the answer on the live corpus -- it promotes none of them -- while fixing
    the case pinning alone gets wrong: a cluster first seen as "Sarah" that
    later gains "Sarah Chen" should take the fuller name. The eight are
    measured; this case is reasoned, because corpus growth did not produce one.
    """
    return _words(existing) < _words(candidate)


def pinned_enabled() -> bool:
    """
    ON unless switched off. ENTITY_PINNED=0 goes back to naming by heuristic
    alone, which is also how a name frozen by mistake gets re-picked.
    """
    return os.environ.get("ENTITY_PINNED", "").strip() != "0"


def _pick_canonical(mentions: list[str],
                    existing: Collection[str] = ()) -> str:
    """
    Pick the best canonical name from a cluster:
    - Prefer title-cased names (likely proper nouns).
    - Among those, pick the longest (most specific).
    - Break ties by the name itself, so the answer does not depend on luck.

    THE TIE-BREAK IS NOT COSMETIC. `max(pool, key=len)` returns the first
    longest item in ITERATION ORDER, and that order comes from a set, so it
    varies per process. Two runs over byte-identical input gave 14 of 980
    mentions a different canonical name:

        'function run_pipeline'   vs  'run_pipeline function'
        'Apollo Project'          vs  'Apollo project'
        'function _ask'           vs  '_ask function'

    Every one of those is a graph that renamed an entity because a hash seed
    changed. Sorting the tie makes the choice arbitrary but STABLE, which is
    the property that was missing -- there is no reason to prefer either
    spelling of "run_pipeline", and every reason to keep answering with the
    same one.

    PINNED: A NAME THAT ALREADY WON KEEPS WINNING
    ---------------------------------------------
    `existing` is what the LAST run called things. A member that was already a
    canonical name stays canonical, which is cocoindex's PINNED policy and the
    half of entity resolution this system did not have.

    Measured by simulating corpus growth on the live graph -- cluster 70% of
    the notes, take that run's canonical map as `existing`, then cluster all of
    them:

        renames after growth, heuristic alone   9 of 727 mentions
        renames after growth, pinned            0

    The heuristic renames because it re-runs a popularity contest every time
    the cluster gains a member, and "longest title-cased" is a poor judge of
    which name a person means:

        Shaan Kapoor      ->  ShaanKapoor10          a person, renamed to a handle
        CocoIndex         ->  Cocoindex              correct casing, lost
        2026-08-12        ->  2026-08-18             a DIFFERENT DATE
        embedding model   ->  embeddings.get_model
        decision          ->  decisions

    Pinning kept the left-hand name in all eight clusters where it differed
    from the heuristic, and produced no rename anywhere else.

    WHAT IS NOT COPIED. cocoindex's PINNED also says two existing canonicals
    never merge. That rule does not survive the trip: here a name is canonical
    the moment it appears, because a lone mention is its own cluster, so
    "never merge two existings" would refuse nearly every merge. The
    equivalent case -- a cluster containing several former canonicals -- did
    not occur once during that growth, so it falls back to the heuristic among
    them rather than to a rule nothing has tested.
    """
    if existing and pinned_enabled():
        kept = [m for m in mentions if m in existing]
        if kept:
            held = max(kept, key=lambda m: (len(m), m))
            fuller = [m for m in mentions if _more_specific(held, m)]
            if not fuller:
                return held
            return max(fuller, key=lambda m: (len(m), m))

    titled = [m for m in mentions if m and m[0].isupper()]
    pool = titled if titled else mentions
    return max(pool, key=lambda m: (len(m), m))


# ---------------------------------------------------------------------------
# Edge list for the entity resolution panel
# ---------------------------------------------------------------------------

def _build_merge_edges(
    mentions: list[str],
    heuristic_pairs: list[tuple[str, str, float, str]],
    embedding_pairs: dict[tuple[str, str], float],
) -> list[dict[str, Any]]:
    """Collect all pairs that were merged, with their similarity and method."""
    edges = []
    seen: set[tuple[str, str]] = set()

    for a, b, sim, method in heuristic_pairs:
        key = (min(a, b), max(a, b))
        if key not in seen:
            seen.add(key)
            edges.append({"a": a, "b": b, "similarity": round(sim, 3), "method": method})

    for (a, b), sim in embedding_pairs.items():
        key = (min(a, b), max(a, b))
        if key not in seen:
            seen.add(key)
            edges.append({"a": a, "b": b, "similarity": round(sim, 3), "method": "embedding"})

    return edges


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def run_resolution() -> dict[str, Any]:
    """
    Resolve entity mentions across all raw triples.
    Writes canonical_map + entity_clusters to SQLite.
    Returns a summary dict.
    """
    triples = db.get_all_triples()
    if not triples:
        return {"clusters": 0, "mentions": 0, "merge_edges": 0, "embedding_used": False}

    # 1. Collect unique mentions
    raw_mentions: set[str] = set()
    for t in triples:
        if t["subject_text"]:
            raw_mentions.add(t["subject_text"].strip())
        if t["object_text"]:
            raw_mentions.add(t["object_text"].strip())

    mentions = [m for m in raw_mentions if m]
    uf = _UnionFind(mentions)
    # Reported, not merely applied. A guard whose effect is invisible is one
    # nobody can audit, and this one is the difference between a graph that
    # says two things and a graph that says one.
    refused: set[tuple[str, str]] = set()

    # 2. Heuristic pairs
    heuristic_merged: list[tuple[str, str, float, str]] = []
    n = len(mentions)
    for i in range(n):
        for j in range(i + 1, n):
            a, b = mentions[i], mentions[j]
            sim, method = _heuristic_sim(a, b)
            # SIMILARITY FIRST, then the guard -- so a refusal means "this
            # would have merged and was stopped", not "these two were never
            # going to merge anyway". The other order counted 16 refusals on a
            # six-pair probe that only ever had 4 real merges to stop, which
            # is precisely the kind of number that reads as work being done.
            if sim < MERGE_THRESHOLD:
                continue
            # BOTH paths are guarded, not only the embedding one: four of the
            # ten merges this refuses on the live corpus came from
            # Jaro-Winkler, which scored "brahmastra_search_entities" against
            # "brahmastra_search_notes" at 0.951. A string metric is if
            # anything MORE confident about names differing by a few
            # characters than an embedding is.
            if _is_contrasting(a, b) or is_distinct(a, b):
                refused.add((a, b))
                continue
            uf.union(a, b)
            heuristic_merged.append((a, b, sim, method))

    # 3. Embedding pairs (skip antonym/contrast pairs that embed deceptively high)
    raw_embedding_pairs = _embedding_sim(mentions)
    embedding_pairs = {}
    for (a, b), sim in raw_embedding_pairs.items():
        if _is_contrasting(a, b) or is_distinct(a, b):
            refused.add((a, b))
            continue
        embedding_pairs[(a, b)] = sim
    # OPT-IN second opinion, off by default. See entity_confirm.py for the
    # four-run measurement that kept it off: it reliably prevents two wrong
    # merges here and reliably costs one or two right ones, and the ones it
    # costs change between runs at temperature 0.
    #
    # Only the EMBEDDING candidates are put to it. The heuristic ones -- exact
    # match after normalisation, token subset, acronym -- are precise by
    # construction, and paying a model to re-confirm "pipeline.py" against
    # "pipeline.py" would be spending the budget where there is no doubt.
    #
    # A pair nobody answered for keeps the behaviour it had before the judge
    # existed. An outage must not silently change the shape of the graph.
    judged = {"asked": 0, "refused": 0, "unanswered": 0}
    if embedding_pairs and entity_confirm.enabled():
        candidates = list(embedding_pairs)
        verdicts, unanswered = entity_confirm.confirm(candidates)
        judged["asked"] = len(candidates)
        judged["unanswered"] = len(unanswered)
        for pair, same in verdicts.items():
            if not same:
                embedding_pairs.pop(pair, None)
                refused.add(pair)
                judged["refused"] += 1

    embedding_used = bool(embedding_pairs)
    for (a, b), sim in embedding_pairs.items():
        uf.union(a, b)

    # 4. Build cluster list
    components = uf.components()
    clusters: list[dict[str, Any]] = []
    # WHAT THE LAST RUN CALLED THINGS. Read here rather than passed in,
    # because `replace_canonical_map` below overwrites it -- so this is the
    # only moment the previous answer still exists. An empty map (a first run,
    # or a deliberate reset) means every name is new and the heuristic decides,
    # which is also how a name frozen by mistake gets re-picked.
    try:
        established = frozenset(db.get_canonical_map().values())
    except Exception:
        established = frozenset()

    renamed: list[dict[str, str]] = []
    for component in components:
        canonical = _pick_canonical(component, established)
        # A cluster holding more than one former canonical is two established
        # entities being merged. It did not happen once while this was
        # measured, so it is REPORTED rather than decided by an untested rule.
        contested = sorted(m for m in component if m in established)
        if len(contested) > 1:
            renamed.append({"canonical": canonical,
                            "absorbed": ", ".join(m for m in contested
                                                  if m != canonical)})
        clusters.append({
            "cluster_id": cluster_id_for(component),
            "canonical_name": canonical,
            "mentions": sorted(component),
        })
    # Sorted so the LIST is stable too, not only the ids in it. Set iteration
    # decides what order UnionFind hands these back in, and a caller comparing
    # two runs should not have to sort them itself.
    clusters.sort(key=lambda c: c["cluster_id"])

    # 5. Persist
    db.replace_canonical_map(clusters)

    merge_edges = _build_merge_edges(mentions, heuristic_merged, embedding_pairs)

    return {
        "clusters": len(clusters),
        "mentions": len(mentions),
        "merge_edges": len(merge_edges),
        "refused_merges": len(refused),
        "judged": judged,
        "pinned": pinned_enabled() and bool(established),
        # Established names that absorbed another established name. Reported
        # because a merge of two things the graph already had names for is the
        # one event somebody should actually look at.
        "absorbed_canonicals": renamed,
        "embedding_used": embedding_used,
        "details": {
            "clusters": clusters,
            "merge_edges": merge_edges,
            "refused_merges": [{"a": a, "b": b} for a, b in sorted(refused)],
        },
    }
