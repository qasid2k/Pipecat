import { useEffect, useRef, useState } from "react";
import { fetchCallDetail } from "../api";
import { hhmmss } from "../format";
import type { CallDetailBody } from "../types";
import { Card } from "./Shell";

// What the server's `transcript_source` means for the person reading it.
const NOTES: Record<CallDetailBody["transcript_source"], string> = {
  conversation: "",
  turns: "Only the caller's side is available: this call's conversation file is missing.",
  none: "No transcript was recorded for this call.",
};

/** One finished call: its facts, and the conversation as a chat (caller left,
 *  agent right). Shown when a row in Recent calls is clicked. */
export function CallDetail({ callId }: { callId: string }) {
  const [body, setBody] = useState<CallDetailBody | null>(null);
  const [error, setError] = useState<string | null>(null);
  const panel = useRef<HTMLDivElement>(null);

  useEffect(() => {
    let current = true; // ignore a slow answer for a call no longer selected
    setBody(null);
    setError(null);
    fetchCallDetail(callId)
      .then((b) => { if (current) setBody(b); })
      .catch((e: Error) => { if (current) setError(e.message); });
    panel.current?.scrollIntoView?.({ behavior: "smooth", block: "nearest" });
    return () => { current = false; };
  }, [callId]);

  const c = body?.call;
  const facts: [string, React.ReactNode][] = c
    ? [
        ["Started", new Date(c.started_at).toLocaleString()],
        ["Length", hhmmss(c.duration_s || 0)],
        ["Agent", c.persona || "—"],
        ["Caller", <span className="font-mono">{c.caller_id || "—"}</span>],
        ["How it ended", c.cause || c.end_reason || "—"],
        ["Transferred to", c.transferred_to || "—"],
      ]
    : [];
  const note = error ? `Could not load this call: ${error}` : body ? NOTES[body.transcript_source] : "Loading…";

  return (
    <div id="call-detail" ref={panel}>
      <Card title={`Call ${callId.slice(0, 8)}`}>
        <dl className="mb-4 grid grid-cols-2 gap-x-4 gap-y-3 text-sm">
          {facts.map(([k, v]) => (
            <div key={k}>
              <dt className="text-xs text-muted">{k}</dt>
              <dd className="mt-0.5">{v}</dd>
            </div>
          ))}
        </dl>
        <div className="flex flex-col gap-2">
          {body?.transcript.map((t, i) => {
            const agent = t.speaker === "agent";
            return (
              <div
                key={i}
                data-speaker={agent ? "agent" : "caller"}
                className={`max-w-[85%] rounded-2xl px-3.5 py-2 text-sm ${
                  agent ? "self-end rounded-br-sm bg-brand-soft" : "self-start rounded-bl-sm border border-border bg-surface-2"
                }`}
              >
                <div className="mb-0.5 text-xs font-medium text-muted">{agent ? c?.persona || "Agent" : "Caller"}</div>
                {t.text}
              </div>
            );
          })}
        </div>
        {note && <p className={`pt-4 text-center text-sm ${error ? "text-bad" : "text-muted"}`}>{note}</p>}
      </Card>
    </div>
  );
}
