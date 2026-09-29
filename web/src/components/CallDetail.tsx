import { useEffect, useRef, useState } from "react";
import { fetchCallDetail } from "../api";
import { hhmmss } from "../format";
import type { CallDetailBody } from "../types";

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
    panel.current?.scrollIntoView?.({ behavior: "smooth", block: "start" });
    return () => { current = false; };
  }, [callId]);

  const c = body?.call;
  const facts: [string, string][] = c
    ? [
        ["started", new Date(c.started_at).toLocaleString()],
        ["length", hhmmss(c.duration_s || 0)],
        ["agent", c.persona || ""],
        ["caller", c.caller_id || ""],
        ["how it ended", c.cause || c.end_reason || ""],
        ["transferred to", c.transferred_to || "—"],
      ]
    : [];

  return (
    <div className="panel" id="call-detail" ref={panel} style={{ marginTop: 16 }}>
      <h2>Call <span className="mono">{callId.slice(0, 8)}</span></h2>
      <div className="facts">
        {facts.map(([k, v]) => <div key={k}><small>{k}</small>{v}</div>)}
      </div>
      <div className="chat">
        {body?.transcript.map((t, i) => (
          <div key={i} className={`msg ${t.speaker === "agent" ? "agent" : "caller"}`}>
            <small>{t.speaker === "agent" ? c?.persona || "agent" : "caller"}</small>
            {t.text}
          </div>
        ))}
      </div>
      <div className="empty">
        {error ? `Could not load this call: ${error}` : body ? NOTES[body.transcript_source] : "Loading…"}
      </div>
    </div>
  );
}
