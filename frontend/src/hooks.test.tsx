import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Layout } from "./components/Layout";
import { mockApi } from "./test/mockApi";

const todo = {
  id: "t1", project: "demo", title: "Add the Apollo key", detail: "On T3", source: "developer",
  created: "2026-11-06T01:00:00Z", can_mark_done: true,
};
const question = { id: "q1", project: "demo", kind: "gate", title: "Review the plan", detail: "", created: "x" };

const made: { title: string; options?: NotificationOptions }[] = [];

beforeEach(() => {
  made.length = 0;
  document.title = "Agent Studio";
  class FakeNotification {
    static permission = "granted";
    static requestPermission = vi.fn();
    onclick: (() => void) | null = null;
    constructor(title: string, options?: NotificationOptions) {
      made.push({ title, options });
    }
  }
  vi.stubGlobal("Notification", FakeNotification);
});

afterEach(() => vi.unstubAllGlobals());

/** Replies `first` on the first poll and `later` on every poll after. */
function polls(first: unknown, later: unknown) {
  let calls = 0;
  return () => (calls++ === 0 ? first : later);
}

function show() {
  render(<MemoryRouter><Layout /></MemoryRouter>);
}

describe("attention", () => {
  it("counts questions and action items together in the nav and the tab title", async () => {
    mockApi({ "GET /api/inbox": [question], "GET /api/todos": { todos: [todo] } });
    show();

    expect(await screen.findByText("(2)")).toBeInTheDocument();
    await waitFor(() => expect(document.title).toBe("(2) Action needed · Agent Studio"));
  });

  it("marks the badge red only when something needs you personally", async () => {
    mockApi({ "GET /api/inbox": [question], "GET /api/todos": { todos: [] } });
    show();
    const badge = await screen.findByText("(1)");
    expect(badge).not.toHaveClass("alert");
    expect(document.title).toBe("(1) Agent Studio");
  });

  it("shows a red badge for an action item", async () => {
    mockApi({ "GET /api/inbox": [], "GET /api/todos": { todos: [todo] } });
    show();

    expect(await screen.findByText("(1)")).toHaveClass("alert");
  });

  it("raises a browser notification, titled differently, for a new action item", async () => {
    mockApi({ "GET /api/inbox": [], "GET /api/todos": polls({ todos: [] }, { todos: [todo] }) });
    vi.useFakeTimers({ shouldAdvanceTime: true });
    show();
    await screen.findByRole("navigation");

    await vi.advanceTimersByTimeAsync(5100);

    await waitFor(() => expect(made.map((n) => n.title)).toEqual(["You need to do something"]));
    expect(made[0]?.options?.body).toBe("demo: Add the Apollo key");
    vi.useRealTimers();
  });

  it("does not announce what was already open when the page loaded", async () => {
    mockApi({ "GET /api/inbox": [], "GET /api/todos": { todos: [todo] } });
    vi.useFakeTimers({ shouldAdvanceTime: true });
    show();
    await screen.findByText("(1)");

    await vi.advanceTimersByTimeAsync(11000);

    expect(made).toEqual([]);
    vi.useRealTimers();
  });
});
