import { describe, expect, it } from "vitest";
import { historyQuery } from "./api";
import { hhmmss } from "./format";
import { NO_FILTERS } from "./types";

describe("hhmmss", () => {
  it("formats minutes and hours like the old page", () => {
    expect(hhmmss(0)).toBe("0:00");
    expect(hhmmss(75)).toBe("1:15");
    expect(hhmmss(3725)).toBe("1:02:05");
    expect(hhmmss(-5)).toBe("0:00");
  });
});

describe("historyQuery", () => {
  it("asks for the plain list when nothing is filtered", () => {
    expect(historyQuery(NO_FILTERS)).toBe("limit=50");
  });

  it("sends only the filters that are set, trimmed", () => {
    const q = new URLSearchParams(
      historyQuery({ ...NO_FILTERS, persona: "Sarah", outcome: "transferred", caller: " 100 ", since: "2026-09-01" }),
    );
    expect(Object.fromEntries(q)).toEqual({
      limit: "50", persona: "Sarah", outcome: "transferred", caller: "100", since: "2026-09-01",
    });
  });
});
