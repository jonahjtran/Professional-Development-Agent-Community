import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { mockApi } from "../test/mockApi";
import { ProjectTasks } from "./ProjectTasks";

const task = (over: Record<string, unknown>) => ({
  id: "T1", title: "Create the table", state: "ready", waiting_on: [], pr_url: null, can_kill: true, kill_blocked: null, ...over,
});

function board(over: Record<string, unknown> = {}) {
  return {
    slug: "demo", status: "running", alive: true, can_pause: true, can_resume: false,
    agent: { stage: "developer", label: "Developer", state: "running" },
    tasks: [
      task({ id: "T1", title: "Create the table", state: "done", can_kill: false, kill_blocked: "it is finished" }),
      task({ id: "T2", title: "Add the API", state: "working", can_kill: true }),
      task({ id: "T3", title: "Add the page", state: "waiting", waiting_on: ["T2"], can_kill: true }),
      task({ id: "T4", title: "Wire the form", state: "waiting", waiting_on: ["T3"], can_kill: false, kill_blocked: "T5 depends on it: stop that first" }),
    ],
    ...over,
  };
}

function open(reply: unknown = board(), extra: Record<string, unknown> = {}) {
  const api = mockApi({
    "GET /api/projects/demo/tasks": reply,
    "POST /api/projects/demo/pause": { ok: true },
    "POST /api/projects/demo/resume": { ok: true },
    "POST /api/projects/demo/tasks/T2/kill": { ok: true, state: "cancelled" },
    "POST /api/projects/demo/tasks/T3/kill": { ok: true, state: "stopping" },
    ...extra,
  });
  render(<ProjectTasks slug="demo" />);
  return api;
}

describe("a project's tasks", () => {
  it("lists the agent and the tasks of this project with their states", async () => {
    open();

    expect(await screen.findByText("Add the API")).toBeInTheDocument();
    expect(screen.getByText(/Developer/)).toBeInTheDocument();
    expect(screen.getByText("Add the page")).toBeInTheDocument();
    expect(screen.getByText(/waiting for T2/)).toBeInTheDocument();
    expect(screen.getByText(/working now/)).toBeInTheDocument();
  });

  it("asks for only this project's tasks", async () => {
    const api = open();
    await screen.findByText("Add the API");

    expect(api.calls.some((c) => c.url === "/api/projects/demo/tasks")).toBe(true);
    expect(api.calls.some((c) => c.url === "/api/tasks")).toBe(false);
  });

  it("shows nothing for a finished project", async () => {
    const api = mockApi({ "GET /api/projects/demo/tasks": board({ status: "done", alive: false, can_pause: false, tasks: [] }) });
    const { container } = render(<ProjectTasks slug="demo" />);

    await waitFor(() => expect(api.calls.some((c) => c.url === "/api/projects/demo/tasks")).toBe(true));
    await waitFor(() => expect(container.textContent).toBe(""));
  });

  it("pauses the project from the task that is running, after asking", async () => {
    const api = open();
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
    const row = (await screen.findByText("Add the API")).closest("li") as HTMLElement;

    await userEvent.click(within(row).getByRole("button", { name: "Pause" }));

    expect(confirm).toHaveBeenCalledWith(expect.stringContaining("T2"));
    await waitFor(() => expect(api.posts().map((p) => p.url)).toEqual(["/api/projects/demo/pause"]));
    expect(screen.getAllByRole("button", { name: "Pause" })).toHaveLength(1);
  });

  it("resumes the same task of a paused project", async () => {
    const api = open(board({ status: "halted", alive: false, can_pause: false, can_resume: true, agent: { stage: "developer", label: "Developer", state: "paused" } }));
    const row = (await screen.findByText("Add the API")).closest("li") as HTMLElement;

    await userEvent.click(within(row).getByRole("button", { name: "Resume" }));

    await waitFor(() => expect(api.posts().map((p) => p.url)).toEqual(["/api/projects/demo/resume"]));
  });

  it("stops a task after asking, naming it, and says it is removed", async () => {
    const api = open();
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
    const row = (await screen.findByText("Add the API")).closest("li") as HTMLElement;

    await userEvent.click(within(row).getByRole("button", { name: "Stop" }));

    expect(confirm).toHaveBeenCalledWith(expect.stringMatching(/Stop T2.*removed/s));
    await waitFor(() => expect(api.posts().map((p) => p.url)).toEqual(["/api/projects/demo/tasks/T2/kill"]));
  });

  it("stops nothing if you cancel", async () => {
    const api = open();
    vi.spyOn(window, "confirm").mockReturnValue(false);
    const row = (await screen.findByText("Add the API")).closest("li") as HTMLElement;

    await userEvent.click(within(row).getByRole("button", { name: "Stop" }));

    expect(api.posts()).toHaveLength(0);
  });

  it("explains why a task cannot be stopped instead of offering the button", async () => {
    open();
    const row = (await screen.findByText("Wire the form")).closest("li") as HTMLElement;

    expect(within(row).queryByRole("button", { name: "Stop" })).not.toBeInTheDocument();
    expect(within(row).getByText(/T5 depends on it/)).toBeInTheDocument();
  });

  it("does not list finished tasks, only how many are done", async () => {
    open();

    expect(await screen.findByText("Add the API")).toBeInTheDocument();
    expect(screen.queryByText("Create the table")).not.toBeInTheDocument();
    expect(screen.getByText(/1 of 4 tasks done/)).toBeInTheDocument();
  });

  it("lists no stopped tasks at all", async () => {
    open();
    await screen.findByText("Add the API");

    expect(screen.queryByText(/Cancelled|cancelled/)).not.toBeInTheDocument();
  });

  it("tells you when a stop is waiting for the run to halt", async () => {
    open(board(), { "POST /api/projects/demo/tasks/T3/kill": { ok: true, state: "stopping" } });
    vi.spyOn(window, "confirm").mockReturnValue(true);
    const row = (await screen.findByText("Add the page")).closest("li") as HTMLElement;

    await userEvent.click(within(row).getByRole("button", { name: "Stop" }));

    expect(await screen.findByText(/will be removed when the run stops/i)).toBeInTheDocument();
  });

  it("puts Pause on the agent when no task is running, such as the Architect", async () => {
    open(board({ agent: { stage: "architect", label: "Architect", state: "running" }, tasks: [] }));

    expect(await screen.findByRole("button", { name: "Pause" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Stop|Prioritize/ })).not.toBeInTheDocument();
  });

  it("shows why the server refused", async () => {
    open(board(), {
      "POST /api/projects/demo/tasks/T2/kill": { status: 409, body: { error: "T2 cannot be stopped: it is finished" } },
    });
    vi.spyOn(window, "confirm").mockReturnValue(true);
    const row = (await screen.findByText("Add the API")).closest("li") as HTMLElement;

    await userEvent.click(within(row).getByRole("button", { name: "Stop" }));

    expect(await screen.findByText(/it is finished/)).toBeInTheDocument();
  });
});
