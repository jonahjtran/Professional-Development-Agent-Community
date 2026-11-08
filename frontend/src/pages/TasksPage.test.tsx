import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import { mockApi } from "../test/mockApi";
import { TasksPage } from "./TasksPage";

const task = (over: Record<string, unknown>) => ({
  id: "T1", title: "Create the table", state: "ready", waiting_on: [], pr_url: null, can_kill: true, kill_blocked: null,
  can_prioritize: over.state === "ready" || over.state === "waiting", priority: null, ...over,
});

function board(over: Record<string, unknown> = {}) {
  return {
    projects: [
      {
        slug: "demo", status: "running", alive: true, can_pause: true, pending: 0,
        agent: { stage: "developer", label: "Developer", state: "running" },
        tasks: [
          task({ id: "T1", title: "Create the table", state: "done", can_kill: false, kill_blocked: "it is finished" }),
          task({ id: "T2", title: "Add the API", state: "working", can_kill: true }),
          task({ id: "T3", title: "Add the page", state: "waiting", waiting_on: ["T2"], can_kill: true }),
          task({ id: "T4", title: "Wire the form", state: "waiting", waiting_on: ["T3"], can_kill: false, kill_blocked: "T5 depends on it: cancel that first" }),
        ],
        cancelled: [{ id: "H1-1", title: "Make it all pass", ts: "2026-11-07T10:00:00Z" }],
        ...over,
      },
    ],
  };
}

function open(reply: unknown = board(), extra: Record<string, unknown> = {}) {
  const api = mockApi({
    "GET /api/tasks": reply,
    "POST /api/projects/demo/pause": { ok: true },
    "POST /api/projects/demo/tasks/T2/kill": { ok: true, state: "cancelled" },
    "POST /api/projects/demo/tasks/T3/kill": { ok: true, state: "stopping" },
    "POST /api/projects/demo/tasks/T3/prioritize": { ok: true, state: "asked" },
    ...extra,
  });
  render(<MemoryRouter><TasksPage /></MemoryRouter>);
  return api;
}

describe("the tasks page", () => {
  it("lists each project's agent and its tasks with their states", async () => {
    open();

    expect(await screen.findByText("demo")).toBeInTheDocument();
    expect(screen.getByText(/Developer/)).toBeInTheDocument();
    expect(screen.getByText("Add the API")).toBeInTheDocument();
    expect(screen.getByText("Add the page")).toBeInTheDocument();
    expect(screen.getByText(/waiting for T2/)).toBeInTheDocument();
    expect(screen.getByText("Make it all pass")).toBeInTheDocument();
  });

  it("says there is nothing to do when no project is active", async () => {
    open({ projects: [] });

    expect(await screen.findByText(/No active projects/)).toBeInTheDocument();
  });

  it("pauses a project after asking", async () => {
    const api = open();
    vi.spyOn(window, "confirm").mockReturnValue(true);
    await screen.findByText("demo");

    await userEvent.click(screen.getByRole("button", { name: "Pause" }));

    await waitFor(() => expect(api.posts().map((p) => p.url)).toEqual(["/api/projects/demo/pause"]));
  });

  it("kills a task after asking, naming the task", async () => {
    const api = open();
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
    await screen.findByText("Add the API");

    const row = screen.getByText("Add the API").closest("li") as HTMLElement;
    await userEvent.click(within(row).getByRole("button", { name: "Kill" }));

    expect(confirm).toHaveBeenCalledWith(expect.stringContaining("T2"));
    await waitFor(() => expect(api.posts().map((p) => p.url)).toEqual(["/api/projects/demo/tasks/T2/kill"]));
  });

  it("kills nothing if you cancel", async () => {
    const api = open();
    vi.spyOn(window, "confirm").mockReturnValue(false);
    const row = (await screen.findByText("Add the API")).closest("li") as HTMLElement;

    await userEvent.click(within(row).getByRole("button", { name: "Kill" }));

    expect(api.posts()).toHaveLength(0);
  });

  it("explains why a task cannot be killed instead of offering the button", async () => {
    open();
    const row = (await screen.findByText("Wire the form")).closest("li") as HTMLElement;

    expect(within(row).queryByRole("button", { name: "Kill" })).not.toBeInTheDocument();
    expect(within(row).getByText(/T5 depends on it/)).toBeInTheDocument();
  });

  it("does not list finished tasks, only how many are done", async () => {
    open();

    expect(await screen.findByText("Add the API")).toBeInTheDocument();
    expect(screen.queryByText("Create the table")).not.toBeInTheDocument();
    expect(screen.getByText(/1 of 4 tasks done/)).toBeInTheDocument();
  });

  it("tells you when a kill is waiting for the run to stop", async () => {
    open();
    vi.spyOn(window, "confirm").mockReturnValue(true);
    const row = (await screen.findByText("Add the page")).closest("li") as HTMLElement;

    await userEvent.click(within(row).getByRole("button", { name: "Kill" }));

    expect(await screen.findByText(/will be cancelled when the run stops/i)).toBeInTheDocument();
  });

  it("gives the Architect no task controls: only Pause", async () => {
    open(board({ agent: { stage: "architect", label: "Architect", state: "running" }, tasks: [] }));

    const header = (await screen.findByText("demo")).closest("section") as HTMLElement;

    expect(within(header).getByRole("button", { name: "Pause" })).toBeInTheDocument();
    expect(within(header).queryByRole("button", { name: /Kill|Prioritize/ })).not.toBeInTheDocument();
  });

  it("shows why the server refused", async () => {
    open(board(), {
      "POST /api/projects/demo/tasks/T2/kill": { status: 409, body: { error: "T2 cannot be cancelled: it is finished" } },
    });
    vi.spyOn(window, "confirm").mockReturnValue(true);
    const row = (await screen.findByText("Add the API")).closest("li") as HTMLElement;

    await userEvent.click(within(row).getByRole("button", { name: "Kill" }));

    expect(await screen.findByText(/it is finished/)).toBeInTheDocument();
  });

  it("asks the Architect to prioritize a task, with your reason, and says the Architect decides", async () => {
    const api = open();
    vi.spyOn(window, "prompt").mockReturnValue("The demo is Friday");
    const row = (await screen.findByText("Add the page")).closest("li") as HTMLElement;

    await userEvent.click(within(row).getByRole("button", { name: "Prioritize" }));

    await waitFor(() => expect(api.posts().map((p) => p.url)).toEqual(["/api/projects/demo/tasks/T3/prioritize"]));
    expect(api.posts()[0]?.body).toEqual({ reason: "The demo is Friday" });
    expect(await screen.findByText(/The Architect decides the order/)).toBeInTheDocument();
  });

  it("asks for nothing if you cancel the reason prompt", async () => {
    const api = open();
    vi.spyOn(window, "prompt").mockReturnValue(null);
    const row = (await screen.findByText("Add the page")).closest("li") as HTMLElement;

    await userEvent.click(within(row).getByRole("button", { name: "Prioritize" }));

    expect(api.posts()).toHaveLength(0);
  });

  it("offers no prioritize button on the working task or the Architect's row", async () => {
    open();
    const working = (await screen.findByText("Add the API")).closest("li") as HTMLElement;

    expect(within(working).queryByRole("button", { name: "Prioritize" })).not.toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: "Prioritize" })).toHaveLength(2); // T3 and T4, both waiting
  });

  it("shows what the Architect decided on a request", async () => {
    open(board({
      tasks: [
        task({ id: "T2", title: "Add the API", state: "ready", priority: { status: "moved", reason: "demo", answer: "T2 has no dependencies." } }),
        task({ id: "T3", title: "Add the page", state: "ready", priority: { status: "kept", reason: "", answer: "T3 needs T2 first." } }),
        task({ id: "T4", title: "Wire the form", state: "ready", priority: { status: "asked", reason: "now", answer: "" } }),
        task({ id: "T5", title: "Polish", state: "ready", priority: { status: "not_applied", reason: "", answer: "The order put T5 before T4." } }),
      ],
    }));

    expect(await screen.findByText(/The Architect moved this up: T2 has no dependencies\./)).toBeInTheDocument();
    expect(screen.getByText(/The Architect kept the order: T3 needs T2 first\./)).toBeInTheDocument();
    expect(screen.getByText(/Waiting for the Architect to decide/)).toBeInTheDocument();
    expect(screen.getByText(/Not applied: The order put T5 before T4\./)).toBeInTheDocument();
  });
});
