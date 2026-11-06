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
    stages, warn_fraction: 0.8, gates: [], links: [], artifacts: [], pending: [], ...over,
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

  it("has no Resume button while the run is alive, and no Halt once it is done", async () => {
    open(project());
    await screen.findByText("An idea");
    expect(screen.queryByRole("button", { name: "Resume" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Halt/ })).toBeInTheDocument();
  });

  it("no halt button for a finished project", async () => {
    open(project({}, { status: "done", alive: false }));
    await screen.findByText("An idea");
    expect(screen.queryByRole("button", { name: /Halt/ })).not.toBeInTheDocument();
  });

  it("asks before halting, and does nothing if you cancel", async () => {
    const api = open(project(), { "POST /api/projects/demo/halt": { ok: true } });
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);

    await userEvent.click(await screen.findByRole("button", { name: /Halt/ }));
    expect(confirm).toHaveBeenCalled();
    expect(api.posts()).toHaveLength(0);

    confirm.mockReturnValue(true);
    await userEvent.click(screen.getByRole("button", { name: /Halt/ }));
    await waitFor(() => expect(api.posts().map((p) => p.url)).toEqual(["/api/projects/demo/halt"]));
  });

  it("shows the server's reason when a halt is refused", async () => {
    open(project(), { "POST /api/projects/demo/halt": { status: 409, body: { error: "demo is already done." } } });
    vi.spyOn(window, "confirm").mockReturnValue(true);

    await userEvent.click(await screen.findByRole("button", { name: /Halt/ }));

    expect(await screen.findByText("demo is already done.")).toBeInTheDocument();
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
