import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { describe, expect, it } from "vitest";
import { mockApi } from "../test/mockApi";
import { TeamPage } from "./TeamPage";

const agenda = [
  { id: "T1", title: "Create the table", state: "done", waiting_on: [], pr_url: "https://x/pull/1" },
  { id: "T2", title: "Add the API", state: "working", waiting_on: [], pr_url: null },
  { id: "T3", title: "Build the page", state: "waiting", waiting_on: ["T2"], pr_url: null },
  { id: "T4", title: "Write the README", state: "ready", waiting_on: [], pr_url: null },
];

function person(stage: string, name: string, status: string, doing: string, extra: Record<string, unknown> = {}) {
  return { stage, name, status, doing, waiting_on: null, task: null, agenda: [], notes: [], recent: [], model: null, backend: null, model_source: null, ...extra };
}

const agents = [
  person("product_owner", "Product Owner", "done", "Finished: wrote the product requirements"),
  person("architect", "Architect", "done", "Finished: designed the architecture"),
  person("developer", "Developer", "working", "T2: Add the API. Running tests", {
    task: { id: "T2", title: "Add the API" },
    model: "claude-sonnet-5-5", backend: "claude", model_source: "running",
    agenda,
    notes: ["T1: Built the table", "Gap: No index", "Needs you: Add the Apollo key"],
    recent: ["Running tests", "Editing page.tsx"],
  }),
  person("qa", "QA", "upcoming", "Waiting for the Developer to finish", { waiting_on: "Developer" }),
  person("career_coach", "Career coach", "upcoming", "Waiting for the QA to finish", { waiting_on: "QA" }),
];

function open(reply: unknown = { agents }) {
  const api = mockApi({ "GET /api/projects/demo/activity": reply });
  render(
    <MemoryRouter initialEntries={["/p/demo/team"]}>
      <Routes><Route path="/p/:slug/team" element={<TeamPage />} /></Routes>
    </MemoryRouter>,
  );
  return api;
}

describe("the team page, with models", () => {
  it("shows the exact model each agent is using and where it runs", async () => {
    open({
      agents: agents.map((a) =>
        a.stage === "qa" ? { ...a, model: "claude-haiku-5-5", backend: "claude", model_source: "configured" } : a,
      ),
    });

    const dev = await screen.findByRole("button", { name: /Developer/ });
    expect(within(dev).getByText("claude-sonnet-5-5")).toBeInTheDocument();
    expect(within(screen.getByRole("button", { name: /QA/ })).getByText("claude-haiku-5-5")).toBeInTheDocument();
  });

  it("marks a local model as local, and says whether it is running now", async () => {
    open({
      agents: agents.map((a) =>
        a.stage === "developer" ? { ...a, model: "qwen3-coder:27b", backend: "local", model_source: "running" } : a,
      ),
    });

    const dev = await screen.findByRole("button", { name: /Developer/ });
    expect(within(dev).getByText("qwen3-coder:27b")).toBeInTheDocument();
    expect(within(dev).getByText("local")).toBeInTheDocument();
    expect(within(dev).getByText(/running now/)).toBeInTheDocument();
  });

  it("says when the model shown is only configured, not yet used", async () => {
    open({
      agents: agents.map((a) =>
        a.stage === "qa" ? { ...a, model: "claude-haiku-5-5", backend: "claude", model_source: "configured" } : a,
      ),
    });

    const qa = await screen.findByRole("button", { name: /QA/ });
    expect(within(qa).getByText(/will use/)).toBeInTheDocument();
  });

  it("shows no model line for an agent whose model is unknown", async () => {
    open();

    const owner = await screen.findByRole("button", { name: /Product Owner/ });
    expect(within(owner).queryByText(/claude/)).not.toBeInTheDocument();
  });
});

describe("the team page", () => {
  it("shows every agent as a person with a bubble saying what they are doing", async () => {
    open();

    expect(await screen.findByText("T2: Add the API. Running tests")).toBeInTheDocument();
    expect(screen.getByText("Waiting for the Developer to finish")).toBeInTheDocument();
    expect(screen.getByText("Finished: wrote the product requirements")).toBeInTheDocument();
    for (const name of ["Product Owner", "Architect", "Developer", "QA", "Career coach"]) {
      expect(screen.getByRole("button", { name: new RegExp(name) })).toBeInTheDocument();
    }
  });

  it("marks each person with their state, for color and for screen readers", async () => {
    open();
    await screen.findByText("T2: Add the API. Running tests");

    expect(screen.getByRole("button", { name: /Developer.*working/ })).toHaveClass("working");
    expect(screen.getByRole("button", { name: /QA.*upcoming/ })).toHaveClass("upcoming");
  });

  it("opens a person's notes, agenda and recent steps when you click them", async () => {
    open();
    await userEvent.click(await screen.findByRole("button", { name: /Developer/ }));

    const panel = screen.getByRole("region", { name: /Developer's notes/ });
    expect(within(panel).getByText("T1: Built the table")).toBeInTheDocument();
    expect(within(panel).getByText("Gap: No index")).toBeInTheDocument();
    expect(within(panel).getByText("Needs you: Add the Apollo key")).toBeInTheDocument();
    expect(within(panel).getByText("Running tests")).toBeInTheDocument();
    expect(within(panel).getByText(/Build the page/)).toBeInTheDocument();
    expect(within(panel).getByText(/waiting on T2/)).toBeInTheDocument();
  });

  it("says when a person has nothing yet", async () => {
    open();
    await userEvent.click(await screen.findByRole("button", { name: /QA/ }));

    expect(screen.getByText("No notes yet.")).toBeInTheDocument();
  });

  it("switches to a board of the Developer's tasks", async () => {
    open();
    await screen.findByText("T2: Add the API. Running tests");

    await userEvent.click(screen.getByLabelText("Board"));

    const todo = screen.getByRole("region", { name: "To do" });
    const working = screen.getByRole("region", { name: "Working" });
    const done = screen.getByRole("region", { name: "Done" });
    expect(within(todo).getByText(/Build the page/)).toBeInTheDocument();
    expect(within(todo).getByText(/waiting on T2/)).toBeInTheDocument();
    expect(within(todo).getByText(/Write the README/)).toBeInTheDocument();
    expect(within(working).getByText(/Add the API/)).toBeInTheDocument();
    expect(within(done).getByRole("link", { name: /Create the table/ })).toHaveAttribute("href", "https://x/pull/1");
  });

  it("puts the task under review in its own column when the Developer waits for you", async () => {
    const waiting = agents.map((a) => (a.stage === "developer" ? { ...a, status: "waiting_for_you" } : a));
    open({ agents: waiting });
    await screen.findByText("T2: Add the API. Running tests");

    await userEvent.click(screen.getByLabelText("Board"));

    expect(within(screen.getByRole("region", { name: "In review" })).getByText(/Add the API/)).toBeInTheDocument();
  });

  it("explains a missing task list", async () => {
    open({ agents: agents.map((a) => ({ ...a, agenda: [] })) });
    await screen.findByText("T2: Add the API. Running tests");

    await userEvent.click(screen.getByLabelText("Board"));

    expect(screen.getByText(/No tasks yet/)).toBeInTheDocument();
  });

  it("explains an error", async () => {
    open({ status: 404, body: { error: "no such project" } });

    expect(await screen.findByText("no such project")).toBeInTheDocument();
  });
});
