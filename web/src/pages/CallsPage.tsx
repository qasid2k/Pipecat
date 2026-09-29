import { MousePointerClick } from "lucide-react";
import { CallDetail } from "../components/CallDetail";
import { RecentCalls } from "../components/RecentCalls";
import { Card, PageHeader } from "../components/Shell";

interface Props {
  agents: string[];
  refreshKey: number;
  selected: string | null;
  onSelect: (callId: string) => void;
}

/** Finished calls: the searchable list, and the selected call beside it
 *  (below it on narrow screens). A fuller redesign is IMP-013. */
export function CallsPage({ agents, refreshKey, selected, onSelect }: Props) {
  return (
    <>
      <PageHeader title="Calls" subtitle="Finished calls, newest first. Click one to read it." />
      <div className="grid grid-cols-1 gap-6 xl:grid-cols-5">
        <div className="xl:col-span-3">
          <RecentCalls agents={agents} refreshKey={refreshKey} selected={selected} onSelect={onSelect} />
        </div>
        <div className="xl:col-span-2">
          {selected ? (
            <CallDetail callId={selected} />
          ) : (
            <Card>
              <div className="flex flex-col items-center gap-2 py-10 text-center text-sm text-muted">
                <MousePointerClick size={22} aria-hidden="true" />
                Select a call to read the conversation
              </div>
            </Card>
          )}
        </div>
      </div>
    </>
  );
}
