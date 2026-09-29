// The web app's behaviours. IMP-012 redesigned the look; every behaviour the
// IMP-011 tests checked is still checked here, against the new screens.
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
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

function go(route: string) {
  act(() => {
    location.hash = route;
    window.dispatchEvent(new HashChangeEvent("hashchange"));
  });
}

beforeEach(() => {
  vi.useFakeTimers();
  installFakeSocket();
  location.hash = "";
});
afterEach(() => { cleanup(); vi.useRealTimers(); vi.unstubAllGlobals(); });

describe("shell and navigation", () => {
  it("opens on Live and marks it as the current page", () => {
    installFakeFetch({});
    render(<App />);
    const nav = screen.getAllByRole("navigation")[0];
    expect(within(nav).getByText("Live").closest("a")!.getAttribute("aria-current")).toBe("page");
    expect(screen.getByRole("heading", { level: 1 }).textContent).toBe("Live");
  });

  it("follows the hash to Calls and Agents, and back", () => {
    installFakeFetch({ "/history": { calls: [] } });
    render(<App />);
    go("#/calls");
    expect(screen.getByRole("heading", { level: 1 }).textContent).toBe("Calls");
    go("#/agents");
    expect(screen.getByRole("heading", { level: 1 }).textContent).toBe("Agents");
    go("#/live");
    expect(screen.getByRole("heading", { level: 1 }).textContent).toBe("Live");
  });

  it("sends an unknown page to Live", () => {
    installFakeFetch({});
    location.hash = "#/nope";
    render(<App />);
    expect(screen.getByRole("heading", { level: 1 }).textContent).toBe("Live");
  });

  it("opens and closes the small-screen menu", () => {
    installFakeFetch({});
    render(<App />);
    const button = screen.getByRole("button", { name: /menu/i });
    expect(button.getAttribute("aria-expanded")).toBe("false");
    fireEvent.click(button);
    expect(button.getAttribute("aria-expanded")).toBe("true");
  });
});

describe("live connection", () => {
  it("shows connecting, then live, then the pushed state", () => {
    installFakeFetch({});
    render(<App />);
    expect(screen.getAllByText("Connecting…").length).toBeGreaterThan(0);
    act(() => { FakeSocket.latest().open(); FakeSocket.latest().push(liveState([{ call_id: "c1" }])); });
    expect(screen.getAllByText("Live").length).toBeGreaterThan(1); // nav + status
    expect(screen.getAllByText("techbridge").length).toBe(2); // sidebar + small-screen bar
    expect(screen.getByText("c1")).toBeTruthy(); // call id column
    expect(screen.getByText("Up 1:05")).toBeTruthy();
  });

  it("shows a skeleton until the first push arrives", () => {
    installFakeFetch({});
    render(<App />);
    expect(document.querySelector('[aria-busy="true"]')).toBeTruthy();
    act(() => { FakeSocket.latest().push(liveState()); });
    expect(document.querySelector('[aria-busy="true"]')).toBeNull();
  });

  it("warns that numbers may be stale while offline, and reconnects", () => {
    installFakeFetch({});
    render(<App />);
    act(() => { FakeSocket.latest().open(); FakeSocket.latest().push(liveState()); FakeSocket.latest().close(); });
    expect(screen.getByRole("status").textContent).toMatch(/offline/i);
    expect(FakeSocket.all.length).toBe(1);
    act(() => { vi.advanceTimersByTime(RECONNECT_MS); });
    expect(FakeSocket.all.length).toBe(2);
  });

  it("flags problems only when there are some", () => {
    installFakeFetch({});
    render(<App />);
    act(() => { FakeSocket.latest().push(liveState()); }); // 1 failed, 0 turned away, audio clean
    expect(document.querySelectorAll('[data-warn="true"]').length).toBe(1);
    expect(screen.getByText("Audio OK")).toBeTruthy();
  });
});

describe("agent cards", () => {
  it("shows each agent free or on a call, with the caller and a ticking duration", () => {
    installFakeFetch({});
    render(<App />);
    act(() => { FakeSocket.latest().push(liveState([{ call_id: "c1", persona: "Sarah" }])); });
    const sarah = screen.getByRole("group", { name: /Sarah/ });
    const alex = screen.getByRole("group", { name: /Alex/ });
    expect(within(sarah).getByText("On call")).toBeTruthy();
    expect(within(sarah).getByText("100")).toBeTruthy();
    expect(within(alex).getByText("Free")).toBeTruthy();
    const before = within(sarah).getByTestId("duration").textContent;
    act(() => { vi.advanceTimersByTime(3000); });
    expect(within(sarah).getByTestId("duration").textContent).not.toBe(before);
  });
});

describe("calls page", () => {
  it("loads on open and reloads ~1.5 s after a live call ends, not at once", async () => {
    const asked = installFakeFetch({ "/history": { calls: [ROW] } });
    location.hash = "#/calls";
    render(<App />);
    await flush();
    const count = () => asked.filter((u) => u.startsWith("/history?")).length;
    expect(count()).toBe(1);

    act(() => { FakeSocket.latest().push(liveState([{ call_id: "c1" }])); });
    act(() => { FakeSocket.latest().push(liveState([])); }); // c1 ended
    await flush();
    expect(count()).toBe(1);

    await act(async () => { vi.advanceTimersByTime(HISTORY_REFRESH_DELAY_MS); });
    await flush();
    expect(count()).toBe(2);
  });

  it("applies filters on submit and clears them", async () => {
    const asked = installFakeFetch({ "/history": { calls: [ROW] } });
    location.hash = "#/calls";
    const { container } = render(<App />);
    await flush();
    fireEvent.change(container.querySelector("#f-outcome")!, { target: { value: "transferred" } });
    fireEvent.change(container.querySelector("#f-caller")!, { target: { value: "10" } });
    fireEvent.submit(container.querySelector("form")!);
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
    location.hash = "#/calls";
    render(<App />);
    await flush();
    fireEvent.click(screen.getByText("billing"));
    await flush();
    expect(document.querySelector("#call-detail")).toBeTruthy();
    expect(screen.getByText("Hi, this is Sarah.")).toBeTruthy();
  });

  it("says why when the history cannot be loaded", async () => {
    installFakeFetch({});
    location.hash = "#/calls";
    render(<App />);
    await flush();
    expect(screen.getByText(/Call history is unavailable/)).toBeTruthy();
  });
});

describe("agents page", () => {
  it("lists every agent with its status", () => {
    installFakeFetch({});
    location.hash = "#/agents";
    render(<App />);
    act(() => { FakeSocket.latest().push(liveState([{ call_id: "c1", persona: "Sarah" }])); });
    const rows = screen.getAllByRole("row").slice(1).map((r) => r.textContent);
    expect(rows.length).toBe(3);
    expect(rows.find((r) => r!.includes("Sarah"))).toMatch(/On call/);
    expect(rows.find((r) => r!.includes("Alex"))).toMatch(/Free/);
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
    expect([...c.querySelectorAll("[data-speaker]")].map((m) => m.getAttribute("data-speaker")))
      .toEqual(["agent", "caller"]);
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
