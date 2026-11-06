import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import { mockApi } from "../test/mockApi";
import { ProjectPage } from "./ProjectPage";

const stages = [
  { stage: "product_owner", label: "Product Owner", state: "done", spent: 0.5, cap: 1.5, fraction: 0.33 },
  { stage: "developer", label: "Developer", state: "current", spent: 2.9, cap: 3, fraction: 0.97 },
];

function project(over: Record<string, unknown> = {}, summary: Record<string, unknown> = {}) {
  return {
    starting: false,
    summary: { slug: "demo", idea: "An idea", stage: "developer", status: "running", detail: "", cost_usd: 3.4, cap_usd: 20, pending: 0, alive: true, ...summary },
    stages, warn_fraction: 0.8, helper: { explain_usd: 0.002, chat_usd: 0 }, autonomy: "standard", todos: [], gates: [], links: [], artifacts: [], pending: [], ...over,
  };
}

function open(body: unknown, extra: Record<string, unknown> = {}) {
  const api = mockApi({ "GET /api/projects/demo": body, ...extra });
  render(
    <MemoryRouter initialEntries={["/p/demo"]}>
      <Routes><Route path="/p/:slug" element={<ProjectPage />} /></Routes>
    </MemoryRouter>,
  );
  return api;
}

describe("the project page", () => {
  it("shows the timeline, spend against caps and the documents", async () => {
    open(project({ artifacts: ["prd.md"], gates: [{ ts: "2026-11-05T12:00:00Z", stage: "architect", outcome: "approve", comments: "looks good" }] }));

    expect((await screen.findAllByText("Product Owner")).length).toBeGreaterThan(0);
    expect(screen.getByText("$3.40 of $20.00")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "prd.md" })).toHaveAttribute("href", "/p/demo/a/prd.md");
    expect(screen.getByText("looks good")).toBeInTheDocument();
    expect(screen.getAllByRole("meter").length).toBe(3);
  });

  it("links straight to an open question", async () => {
    open(project({ pending: [{ id: "q1", project: "demo", kind: "gate", title: "Review: QA", detail: "Spent so far $3.40", created: "" }] }));

    const link = await screen.findByRole("link", { name: "Review: QA" });

    expect(link).toHaveAttribute("href", "/p/demo/q/q1");
  });

  it("offers Resume only for a run that is not active", async () => {
    const api = open(project({}, { alive: false }), { "POST /api/projects/demo/resume": { ok: true } });

    await userEvent.click(await screen.findByRole("button", { name: "Resume" }));

    await waitFor(() => expect(api.posts().map((p) => p.url)).toEqual(["/api/projects/demo/resume"]));
  });

  it("has no Resume button while the run is alive, but can be paused", async () => {
    open(project());
    await screen.findByText("An idea");
    expect(screen.queryByRole("button", { name: "Resume" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Pause/ })).toBeInTheDocument();
  });

  it("no pause button for a finished project", async () => {
    open(project({}, { status: "done", alive: false }));
    await screen.findByText("An idea");
    expect(screen.queryByRole("button", { name: /Pause/ })).not.toBeInTheDocument();
  });

  it("asks before pausing, and does nothing if you cancel", async () => {
    const api = open(project(), { "POST /api/projects/demo/pause": { ok: true } });
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);

    await userEvent.click(await screen.findByRole("button", { name: /Pause/ }));
    expect(confirm).toHaveBeenCalled();
    expect(api.posts()).toHaveLength(0);

    confirm.mockReturnValue(true);
    await userEvent.click(screen.getByRole("button", { name: /Pause/ }));
    await waitFor(() => expect(api.posts().map((p) => p.url)).toEqual(["/api/projects/demo/pause"]));
  });

  it("shows the server's reason when a pause is refused", async () => {
    open(project(), { "POST /api/projects/demo/pause": { status: 409, body: { error: "demo is already done." } } });
    vi.spyOn(window, "confirm").mockReturnValue(true);

    await userEvent.click(await screen.findByRole("button", { name: /Pause/ }));

    expect(await screen.findByText("demo is already done.")).toBeInTheDocument();
  });

  it("shows helper spend apart from the caps", async () => {
    open(project());
    expect(await screen.findByText(/Helper calls.*\$0\.002/)).toBeInTheDocument();
  });

  it("lists what only you can do on this project, and marks it done", async () => {
    const todo = {
      id: "t1", project: "demo", title: "Add the Apollo API key", detail: "Raised by the Developer on T3",
      source: "developer", created: "2026-11-06T01:05:00Z", can_mark_done: true,
    };
    const api = open(project({ todos: [todo] }), { "POST /api/projects/demo/todos/t1/done": { ok: true } });

    expect(await screen.findByText("Add the Apollo API key")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Mark done" }));

    await waitFor(() => expect(api.posts().map((p) => p.url)).toEqual(["/api/projects/demo/todos/t1/done"]));
  });

  it("offers to ask the assistant about a paused project", async () => {
    open(project({}, { status: "halted", alive: false }));

    const link = await screen.findByRole("link", { name: /Ask the assistant/ });

    expect(link.getAttribute("href")).toContain("/chat?project=demo");
  });

  it("links to the team page", async () => {
    open(project());

    expect(await screen.findByRole("link", { name: /Team/ })).toHaveAttribute("href", "/p/demo/team");
  });

  it("shows the autonomy level and changes it", async () => {
    const api = open(project(), { "POST /api/projects/demo/autonomy": { ok: true } });

    expect(await screen.findByLabelText("Standard")).toBeChecked();
    await userEvent.click(screen.getByLabelText("Autonomous"));

    await waitFor(() => expect(api.posts().map((p) => p.body)).toEqual([{ level: "autonomous" }]));
  });

  it("shows a paused project as paused, with Resume and no Pause", async () => {
    const api = open(project({}, { status: "halted", alive: false }), {
      "POST /api/projects/demo/resume": { ok: true },
    });

    expect(await screen.findByText("paused")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Pause/ })).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Resume" }));
    await waitFor(() => expect(api.posts().map((p) => p.url)).toEqual(["/api/projects/demo/resume"]));
  });

  it("shows the log while a new project is still starting", async () => {
    open({ starting: true, slug: "demo", log: "Starting...\n<script>x</script>" });

    expect(await screen.findByText("Starting demo…")).toBeInTheDocument();
    expect(screen.getByText(/<script>x<\/script>/)).toBeInTheDocument(); // shown as text, not run
  });

  it("says there is no such project", async () => {
    mockApi({ "GET /api/projects/demo": { status: 404, body: { error: "no such project" } } });
    render(
      <MemoryRouter initialEntries={["/p/demo"]}>
        <Routes><Route path="/p/:slug" element={<ProjectPage />} /></Routes>
      </MemoryRouter>,
    );

    expect(await screen.findByText("There is no such project.")).toBeInTheDocument();
  });
});
