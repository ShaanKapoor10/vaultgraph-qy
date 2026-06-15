import { Dashboard } from "@/components/dashboard"
import { SAMPLE_NOTES, SAMPLE_TRIPLES } from "@/lib/sample-notes"

export default function Page() {
  return <Dashboard initialNotes={SAMPLE_NOTES} initialTriples={SAMPLE_TRIPLES} />
}
