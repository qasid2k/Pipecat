// The behaviours the old page had, checked on the React one (IMP-011 is a pure
// port: anything that differs is a regression).
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App, HISTORY_REFRESH_DELAY_MS } from "./App";
import { CallDetail } from "./components/CallDetail";
import { FakeSocket, installFakeFetch, installFakeSocket, liveState } from "./test/fakes";
import { RECONNECT_MS } from "./useLive";

const ROW = {
  call_id: "abcdef12-0000", started_at: "2026-09-29T08:00:00Z", ended_at: null, duration_s: 42,
  caller_id: "100", persona: "Sarah", end_reason: "", cause: "caller hung up", transferred_to: "billing",
};

async function flush() {
  await act(async () => { await Promise.resolve(); await Promise.resolve(); });
}

beforeEach(() => { vi.useFakeTimers(); installFakeSocket(); });
afterEach(() => { cleanup(); vi.useRealTimers(); vi.unstubAllGlobals(); });

describe("live connection", () => {
  it("shows connecting, then live, then the pushed state", async () => {
    installFakeFetch({ "/history": { calls: [] } });
    render(<App />);
    expect(screen.getByText("connecting…")).toBeTruthy();
    act(() => { FakeSocket.latest().open(); FakeSocket.latest().push(liveState([{ call_id: "c1" }])); });
    expect(screen.getByText("live")).toBeTruthy();
    expect(screen.getByText("techbridge")).toBeTruthy();
    expect(screen.getByText("c1")).toBeTruthy(); // call id column (first 8 chars)
    expect(screen.getByText("up 1:05")).toBeTruthy();
  });

  it("says disconnected and reconnects after a close", () => {
    installFakeFetch({ "/history": { calls: [] } });
    render(<App />);
    act(() => { FakeSocket.latest().open(); FakeSocket.latest().close(); });
    expect(screen.getByText("disconnected")).toBeTruthy();
    expect(FakeSocket.all.length).toBe(1);
    act(() => { vi.advanceTimersByTime(RECONNECT_MS); });
    expect(FakeSocket.all.length).toBe(2);
  });

  it("marks failed/turned-away/dropped counters as warnings only when non-zero", () => {
    installFakeFetch({ "/history": { calls: [] } });
    const { container } = render(<App />);
    act(() => { FakeSocket.latest().push(liveState()); });
    const warned = [...container.querySelectorAll(".stat.warn small")].map((e) => e.textContent);
    expect(warned).toEqual(["failed"]);
  });
});

describe("recent calls", () => {
  it("loads on open and reloads ~1.5 s after a live call ends, not at once", async () => {
    const asked = installFakeFetch({ "/history": { calls: [ROW] } });
    render(<App />);
    await flush();
    expect(asked.filter((u) => u.startsWith("/history?")).length).toBe(1);

    act(() => { FakeSocket.latest().push(liveState([{ call_id: "c1" }])); });
    act(() => { FakeSocket.latest().push(liveState([])); }); // c1 ended
    await flush();
    expect(asked.filter((u) => u.startsWith("/history?")).length).toBe(1);

    await act(async () => { vi.advanceTimersByTime(HISTORY_REFRESH_DELAY_MS); });
    await flush();
    expect(asked.filter((u) => u.startsWith("/history?")).length).toBe(2);
  });

  it("applies filters on submit and clears them", async () => {
    const asked = installFakeFetch({ "/history": { calls: [ROW] } });
    const { container } = render(<App />);
    await flush();
    fireEvent.change(container.querySelector("#f-outcome")!, { target: { value: "transferred" } });
    fireEvent.change(container.querySelector("#f-caller")!, { target: { value: "10" } });
    fireEvent.submit(container.querySelector("form.filters")!);
    await flush();
    expect(asked[asked.length - 1]).toBe("/history?limit=50&outcome=transferred&caller=10");
    fireEvent.click(screen.getByText("Clear"));
    await flush();
    expect(asked[asked.length - 1]).toBe("/history?limit=50");
  });

  it("opens the call detail when a row is clicked", async () => {
    installFakeFetch({
      "/history?": { calls: [ROW] },
      "/history/": { call: ROW, transcript: [{ speaker: "agent", text: "Hi, this is Sarah." }], transcript_source: "conversation" },
    });
    render(<App />);
    await flush();
    fireEvent.click(screen.getByText("billing"));
    await flush();
    expect(document.querySelector("#call-detail")).toBeTruthy();
    expect(screen.getByText("Hi, this is Sarah.")).toBeTruthy();
  });

  it("says why when the history cannot be loaded", async () => {
    installFakeFetch({});
    render(<App />);
    await flush();
    expect(screen.getByText(/Call history is unavailable/)).toBeTruthy();
  });
});

describe("call detail", () => {
  async function show(source: "conversation" | "turns" | "none", transcript: { speaker: string; text: string }[]) {
    installFakeFetch({ "/history/": { call: ROW, transcript, transcript_source: source } });
    const { container } = render(<CallDetail callId="abcdef12-0000" />);
    await flush();
    return container;
  }

  it("shows the conversation as a chat, agent right and caller left", async () => {
    const c = await show("conversation", [
      { speaker: "agent", text: "Hi, this is Sarah." }, { speaker: "caller", text: "Billing please." },
    ]);
    expect([...c.querySelectorAll(".msg")].map((m) => m.className)).toEqual(["msg agent", "msg caller"]);
    expect(screen.getByText("billing")).toBeTruthy(); // transferred to
  });

  it("says when only the caller's side exists", async () => {
    await show("turns", [{ speaker: "caller", text: "hello" }]);
    expect(screen.getByText(/Only the caller's side/)).toBeTruthy();
  });

  it("says when there is no transcript", async () => {
    await show("none", []);
    expect(screen.getByText(/No transcript was recorded/)).toBeTruthy();
  });

  it("does not render markup from caller data", async () => {
    const c = await show("conversation", [{ speaker: "caller", text: "<img src=x onerror=alert(1)>" }]);
    expect(c.querySelector("img")).toBeNull();
  });
});
