import type { Note, RawTriple } from "./types"

/**
 * A small synthetic "vault". The notes are plain unstructured text with NO
 * [[links]] between them — the concept graph is entirely inferred from content.
 *
 * The data is designed to exercise every stage:
 *  - entity resolution: "Sarah" / "Sarah K." / "Sarah Khan", "PromptlyBI" /
 *    "Promptly BI" / "the BI project", "Raj" / "Raj P."
 *  - contradictions: auth migration has two deadlines; Sarah reports to two
 *    different managers; Raj is in two locations.
 *  - link prediction: people & projects that share neighbors but aren't linked.
 */
export const SAMPLE_NOTES: Note[] = [
  {
    id: "n1",
    title: "Auth migration kickoff",
    content:
      "Sarah is leading the auth migration. She owns the whole effort end to end. " +
      "The auth migration is scheduled for March 15. Sarah reports to Mei Lin on this one. " +
      "We're using Postgres for the new identity store.",
    lastEdited: "2024-02-01T09:00:00Z",
    extractionStatus: "done",
  },
  {
    id: "n2",
    title: "SSO planning sync",
    content:
      "The new SSO rollout is blocked until the auth migration finishes — it depends on it directly. " +
      "Sarah K. is working on the SSO rollout as well. The SSO rollout uses Redis for session caching.",
    lastEdited: "2024-02-08T14:30:00Z",
    extractionStatus: "done",
  },
  {
    id: "n3",
    title: "Team changes",
    content:
      "Sarah is now also covering the SSO rollout while Raj is out. " +
      "Heads up: Sarah Khan now reports to Raj Patel after the reorg. " +
      "Raj is located in Berlin for the quarter.",
    lastEdited: "2024-02-20T11:00:00Z",
    extractionStatus: "done",
  },
  {
    id: "n4",
    title: "Deadline slip",
    content:
      "Bad news: the auth migration is scheduled for April 2 now, it slipped three weeks. " +
      "This pushes the SSO rollout too since it depends on the auth migration.",
    lastEdited: "2024-03-05T16:45:00Z",
    extractionStatus: "done",
  },
  {
    id: "n5",
    title: "PromptlyBI overview",
    content:
      "PromptlyBI is our analytics product. The dashboard redesign is part of PromptlyBI. " +
      "PromptlyBI uses Postgres for its warehouse and Redis for query caching. " +
      "Raj P. works on PromptlyBI.",
    lastEdited: "2024-02-12T10:15:00Z",
    extractionStatus: "done",
  },
  {
    id: "n6",
    title: "BI project notes",
    content:
      "Mei Lin owns the BI project. The billing service depends on the auth migration before it can ship. " +
      "The billing service is part of Promptly BI. Mei is located in London.",
    lastEdited: "2024-02-25T13:20:00Z",
    extractionStatus: "done",
  },
  {
    id: "n7",
    title: "Raj location update",
    content:
      "Raj Patel is now located in London after relocating. Raj P. is still on the SSO rollout. " +
      "Raj works on the dashboard redesign too.",
    lastEdited: "2024-03-10T08:00:00Z",
    extractionStatus: "done",
  },
  {
    id: "n8",
    title: "Infra dependencies",
    content:
      "The dashboard redesign uses Postgres directly now. " +
      "The billing service uses Redis for rate limiting. Tom works on the billing service.",
    lastEdited: "2024-03-12T17:30:00Z",
    extractionStatus: "done",
  },
]

let _id = 0
const t = (
  subjectText: string,
  relation: RawTriple["relation"],
  objectText: string,
  confidence: number,
  sourceQuote: string,
  sourceNoteId: string,
  extractedAt: string,
): RawTriple => ({
  id: `t${++_id}`,
  subjectText,
  relation,
  objectText,
  confidence,
  sourceQuote,
  sourceNoteId,
  extractedAt,
})

/**
 * Pre-extracted triples for the sample vault — what an ontology-constrained LLM
 * would return for these notes. Subjects/objects are RAW mentions (not yet
 * normalized); entity resolution collapses them downstream.
 */
export const SAMPLE_TRIPLES: RawTriple[] = [
  // n1
  t("Sarah", "owns", "the auth migration", 0.95, "Sarah is leading the auth migration. She owns the whole effort end to end.", "n1", "2024-02-01T09:05:00Z"),
  t("the auth migration", "scheduled_for", "March 15", 0.92, "The auth migration is scheduled for March 15.", "n1", "2024-02-01T09:05:00Z"),
  t("Sarah", "reports_to", "Mei Lin", 0.88, "Sarah reports to Mei Lin on this one.", "n1", "2024-02-01T09:05:00Z"),
  t("the auth migration", "uses", "Postgres", 0.8, "We're using Postgres for the new identity store.", "n1", "2024-02-01T09:05:00Z"),
  // n2
  t("the SSO rollout", "depends_on", "the auth migration", 0.94, "The new SSO rollout is blocked until the auth migration finishes — it depends on it directly.", "n2", "2024-02-08T14:35:00Z"),
  t("Sarah K.", "works_on", "the SSO rollout", 0.9, "Sarah K. is working on the SSO rollout as well.", "n2", "2024-02-08T14:35:00Z"),
  t("the SSO rollout", "uses", "Redis", 0.85, "The SSO rollout uses Redis for session caching.", "n2", "2024-02-08T14:35:00Z"),
  // n3
  t("Sarah", "works_on", "the SSO rollout", 0.87, "Sarah is now also covering the SSO rollout while Raj is out.", "n3", "2024-02-20T11:05:00Z"),
  t("Sarah Khan", "reports_to", "Raj Patel", 0.86, "Sarah Khan now reports to Raj Patel after the reorg.", "n3", "2024-02-20T11:05:00Z"),
  t("Raj", "located_in", "Berlin", 0.84, "Raj is located in Berlin for the quarter.", "n3", "2024-02-20T11:05:00Z"),
  // n4
  t("the auth migration", "scheduled_for", "April 2", 0.93, "the auth migration is scheduled for April 2 now, it slipped three weeks.", "n4", "2024-03-05T16:50:00Z"),
  t("the SSO rollout", "depends_on", "the auth migration", 0.9, "This pushes the SSO rollout too since it depends on the auth migration.", "n4", "2024-03-05T16:50:00Z"),
  // n5
  t("the dashboard redesign", "part_of", "PromptlyBI", 0.91, "The dashboard redesign is part of PromptlyBI.", "n5", "2024-02-12T10:20:00Z"),
  t("PromptlyBI", "uses", "Postgres", 0.88, "PromptlyBI uses Postgres for its warehouse and Redis for query caching.", "n5", "2024-02-12T10:20:00Z"),
  t("PromptlyBI", "uses", "Redis", 0.88, "PromptlyBI uses Postgres for its warehouse and Redis for query caching.", "n5", "2024-02-12T10:20:00Z"),
  t("Raj P.", "works_on", "PromptlyBI", 0.86, "Raj P. works on PromptlyBI.", "n5", "2024-02-12T10:20:00Z"),
  // n6
  t("Mei Lin", "owns", "the BI project", 0.89, "Mei Lin owns the BI project.", "n6", "2024-02-25T13:25:00Z"),
  t("the billing service", "depends_on", "the auth migration", 0.9, "The billing service depends on the auth migration before it can ship.", "n6", "2024-02-25T13:25:00Z"),
  t("the billing service", "part_of", "Promptly BI", 0.87, "The billing service is part of Promptly BI.", "n6", "2024-02-25T13:25:00Z"),
  t("Mei", "located_in", "London", 0.85, "Mei is located in London.", "n6", "2024-02-25T13:25:00Z"),
  // n7
  t("Raj Patel", "located_in", "London", 0.9, "Raj Patel is now located in London after relocating.", "n7", "2024-03-10T08:05:00Z"),
  t("Raj P.", "works_on", "the SSO rollout", 0.85, "Raj P. is still on the SSO rollout.", "n7", "2024-03-10T08:05:00Z"),
  t("Raj", "works_on", "the dashboard redesign", 0.84, "Raj works on the dashboard redesign too.", "n7", "2024-03-10T08:05:00Z"),
  // n8
  t("the dashboard redesign", "uses", "Postgres", 0.83, "The dashboard redesign uses Postgres directly now.", "n8", "2024-03-12T17:35:00Z"),
  t("the billing service", "uses", "Redis", 0.83, "The billing service uses Redis for rate limiting.", "n8", "2024-03-12T17:35:00Z"),
  t("Tom", "works_on", "the billing service", 0.82, "Tom works on the billing service.", "n8", "2024-03-12T17:35:00Z"),
]
