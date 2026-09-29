// The shapes the Python API sends. Kept in one place so a server change shows
// up as a type error here instead of a blank cell on a supervisor's screen.

export interface LiveCall {
  call_id: string;
  persona: string;
  caller_id: string;
  started_at: string; // ISO; durations are ticked in the browser from this
}

export interface LiveState {
  tenant: string;
  pool: {
    capacity: number;
    free: number;
    busy: number;
    free_agents: string[];
    busy_agents: string[];
  };
  calls: LiveCall[];
  counters: {
    uptime_s: number;
    calls_total: number;
    calls_rejected_total: number;
    calls_failed_total: number;
    transfers_total: number;
    frames_dropped_total: number;
    pacer_slips_total: number;
  };
}

/** One finished call, as `/history` returns it. */
export interface CallRow {
  call_id: string;
  started_at: string;
  ended_at: string | null;
  duration_s: number;
  caller_id: string | null;
  persona: string | null;
  end_reason: string | null;
  cause: string | null;
  transferred_to: string | null;
}

export interface Filters {
  since: string;
  until: string;
  persona: string;
  outcome: "" | "transferred" | "handled";
  caller: string;
}

export const NO_FILTERS: Filters = { since: "", until: "", persona: "", outcome: "", caller: "" };

export interface TranscriptLine {
  speaker: "caller" | "agent";
  text: string;
}

/** `/history/<id>`. `transcript_source` says how complete the words are. */
export interface CallDetailBody {
  call: CallRow;
  transcript: TranscriptLine[];
  transcript_source: "conversation" | "turns" | "none";
}
