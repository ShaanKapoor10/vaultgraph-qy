"""
Turning very large documents into knowledge the graph can hold.

WHY THIS EXISTS
---------------
Everything upstream of here assumes a note is small. The notes in this system
average 1,304 characters and the largest is 3,699. A one-hour meeting
transcript is 50,000-75,000 -- roughly twenty times the biggest thing the
pipeline has ever been handed.

That is not a matter of degree. `extraction._is_too_large` marks an oversized
note as a PERMANENT failure, with the message "split the note, or raise the
tier", because the extraction prompt carries the entire ontology alongside the
note and a long note tips the request past the provider's per-minute allowance
on its own. A transcript cannot traverse the existing path at all; it fails
before a single triple is read.

So this is a stage in front of the pipeline, not a bigger note.

    transcript
      |- segment    speaker-aware chunks that fit a token budget
      |- comprehend one LLM pass per chunk -> typed records + prose
      |- assemble   artifacts stored directly; prose becomes NOTES
      `- notes flow into the EXISTING extract -> resolve -> graph

WHAT IS STORED, AND WHY BOTH
----------------------------
A transcript yields two different kinds of knowledge, and collapsing them
loses one of them:

  ARTIFACTS  decisions, action items, risks, open questions -- typed rows with
             an owner, a date, a rationale. These answer the questions an
             organisation actually asks ("what did we decide about pricing?",
             "what are my action items?") and they must stay structured. The
             ontology has no vocabulary for any of them: its 18 relations
             contain no `decided`, no `action_item`, no `attended`, so pushing
             them through extraction alone degrades every one to `related_to`
             and destroys exactly the business meaning worth keeping.

  PROSE      a summary note per chunk, which flows into the existing pipeline
             unchanged and becomes entities and relations in the graph. This is
             what keeps transcripts searchable alongside everything else, and
             what gives /ask real sentences to cite.

STORAGE AUTHORITY
-----------------
Following the split declared in stores/base.py, applied to this data:

  SOURCE   `transcripts` -- the raw text as submitted. Cannot be recomputed.
           If the only copy is lost it is gone, exactly like a note.

  DERIVED  `transcript_chunks` and `meeting_artifacts` -- both are a function
           of the transcript plus a model. Rebuildable by re-running ingestion,
           which costs LLM calls and time, never information.

The one place that boundary will move: the moment a human can EDIT an action
item -- reassign it, mark it done -- that edit is source data and a rebuild
would destroy it. When that arrives it needs its own table rather than a
mutable column here. Written down now because this is precisely the kind of
distinction that rots into a comment nobody trusts.

DELIBERATELY NOT ON THE GraphStore CONTRACT
-------------------------------------------
This module owns its own tables and its own store. Adding transcript methods to
GraphStore would force SQLite, Postgres AND Neo4j to implement them, and Neo4j
has no business holding raw transcripts -- it is the engine for the derived
graph. The isolation guarantees are kept the same way the rest of the system
keeps them: every row carries `workspace_id`, and the store is bound to one
workspace at construction rather than trusting callers to filter.
"""

from __future__ import annotations

__all__ = ["segment", "comprehend", "assemble", "store"]
