import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";
import { mockApi } from "../test/mockApi";
import { InboxPage } from "./InboxPage";

const question = {
  id: "q1", project: "demo", kind: "gate", title: "Review the plan", detail: "Stage: architect", created: "2026-11-06T01:00:00Z",
};
const todo = {
  id: "t1", project: "demo", title: "Add the Apollo API key", detail: "Raised by the Developer on T3",
  source: "developer", created: "2026-11-06T01:05:00Z", can_mark_done: true,
};
const system = {
  id: "system-GitHub token", project: null, title: "GitHub token", detail: "GITHUB_TOKEN is not set.",
  source: "system", created: null, can_mark_done: false,
};

function open(todos: unknown[] = [todo, system], questions: unknown[] = [question]) {
  const api = mockApi({
    "GET /api/inbox": questions,
    "GET /api/todos": { todos },
    "POST /api/projects/demo/todos/t1/done": { ok: true },
  });
  render(<MemoryRouter><InboxPage /></MemoryRouter>);
  return api;
}

describe("the inbox", () => {
  it("shows decisions and, apart from them, the things only you can do", async () => {
    open();

    expect(await screen.findByText("Review the plan")).toBeInTheDocument();
    expect(await screen.findByText("Add the Apollo API key")).toBeInTheDocument();
    expect(screen.getByText(/Raised by the Developer on T3/)).toBeInTheDocument();
    expect(screen.getByText("GitHub token")).toBeInTheDocument();
  });

  it("marks an item done, but offers no button for one that clears itself", async () => {
    const api = open();
    await screen.findByText("Add the Apollo API key");

    expect(screen.getAllByRole("button", { name: "Mark done" })).toHaveLength(1);
    await userEvent.click(screen.getByRole("button", { name: "Mark done" }));

    await waitFor(() => expect(api.posts()).toHaveLength(1));
    expect(api.posts()[0]?.url).toBe("/api/projects/demo/todos/t1/done");
  });

  it("says when there is nothing to do", async () => {
    open([], []);

    expect(await screen.findByText("Nothing is waiting for you.")).toBeInTheDocument();
  });
});
