import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import { ChatContext } from "../components/ChatContext";
import { mockApi } from "../test/mockApi";
import { ProjectPage } from "./ProjectPage";

const stages = [
  { stage: "product_owner", label: "Product Owner", state: "finished", spent: 0.5, cap: 1.5, fraction: 0.33, visits: 1, returned: false },
  { stage: "developer", label: "Developer", state: "current", spent: 2.9, cap: 3, fraction: 0.97, visits: 1, returned: false },
];

function project(over: Record<string, unknown> = {}, summary: Record<string, unknown> = {}) {
  return {
    starting: false,
    summary: { slug: "demo", idea: "An idea", stage: "developer", status: "running", detail: "", cost_usd: 3.4, cap_usd: 20, pending: 0, alive: true, ...summary },
    stages, warn_fraction: 0.8, helper: { explain_usd: 0.002, chat_usd: 0 }, autonomy: "standard", todos: [], journey: { path: [], going_back: null }, can_send_back: false, developer: { backend: "claude", source: "default", model: "claude-sonnet-5-5", billing: "api", billing_source: "default" }, gates: [], links: [], artifacts: [], pending: [], ...over,
  };
}

const asked = vi.fn();

function open(body: unknown, extra: Record<string, unknown> = {}) {
  const api = mockApi({ "GET /api/projects/demo": body, ...extra });
  render(
    <ChatContext.Provider value={{ isOpen: false, request: null, open: asked, close: () => {}, toggle: () => {} }}>
      <MemoryRouter initialEntries={["/p/demo"]}>
        <Routes><Route path="/p/:slug" element={<ProjectPage />} /></Routes>
      </MemoryRouter>
    </ChatContext.Provider>,
  );
  return api;
}

describe("the project page", () => {
  it("shows spend against caps, the decisions and the documents", async () => {
    open(project({ artifacts: ["prd.md"], gates: [{ ts: "2026-11-05T12:00:00Z", stage: "architect", outcome: "approve", comments: "looks good" }] }));

    expect((await screen.findAllByText("Product Owner")).length).toBeGreaterThan(0);
    expect(screen.getByText("$3.40 of $20.00")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "prd.md" })).toHaveAttribute("href", "/p/demo/a/prd.md");
    expect(screen.getByText("looks good")).toBeInTheDocument();
    expect(screen.getAllByRole("meter").length).toBe(3);
  });

  it("shows only the agent that is active, not the journey", async () => {
    const step = (stage: string, label: string, direction: string, reason: string) => ({ stage, label, direction, reason, ts: "2026-11-07T12:00:00Z" });
    open(project({
      journey: {
        path: [step("product_owner", "Product Owner", "forward", "start"), step("architect", "Architect", "back", "QA failed")],
        going_back: { stage: "architect", label: "Architect", reason: "QA failed", came_from: "QA" },
      },
    }));

    expect(await screen.findByText(/Active agent/)).toHaveTextContent("Active agent: Developer");
    expect(screen.queryByRole("list", { name: "Journey" })).not.toBeInTheDocument();
    expect(screen.queryByText(/Back at the/)).not.toBeInTheDocument();
    expect(screen.queryByText("will run again")).not.toBeInTheDocument();
    expect(screen.queryByRole("listitem", { name: "" })).not.toBeInTheDocument();
  });

  it("says when every agent is finished", async () => {
    open(project({}, { status: "done", stage: "career_coach", alive: false }));

    expect(await screen.findByText(/Active agent/)).toHaveTextContent("No agent is active: the project is finished");
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

    await userEvent.click(await screen.findByRole("button", { name: /Ask the assistant/ }));

    expect(asked).toHaveBeenCalledWith({ prompt: expect.stringContaining("Why is demo paused") });
  });

  it("lets you collapse and open the pull requests, decisions and documents sections", async () => {
    open(project({
      links: [{ label: "Developer PR", url: "https://github.com/x/y/pull/1" }],
      gates: [{ ts: "2026-11-01T10:00:00Z", stage: "product_owner", outcome: "approve", comments: "Looks right" }],
      artifacts: ["prd.md"],
    }));

    for (const title of ["Pull requests", "Your decisions", "Documents"]) {
      const toggle = await screen.findByRole("button", { name: title });
      expect(toggle).toHaveAttribute("aria-expanded", "true");
    }
    expect(screen.getByText("Looks right")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Your decisions" }));
    expect(screen.getByRole("button", { name: "Your decisions" })).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByText("Looks right")).not.toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Your decisions" }));
    expect(screen.getByText("Looks right")).toBeInTheDocument();
  });

  it("collapses the pull requests section without hiding the others", async () => {
    open(project({ links: [{ label: "Developer PR", url: "https://github.com/x/y/pull/1" }], artifacts: ["prd.md"] }));

    await userEvent.click(await screen.findByRole("button", { name: "Pull requests" }));

    expect(screen.queryByText("Developer PR")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Documents" })).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByText("prd.md")).toBeInTheDocument();
  });

  it("lets you send a finished project back to an agent with an instruction", async () => {
    const done = project(
      {
        can_send_back: true,
        stages: ["product_owner", "architect", "developer", "qa", "career_coach"].map((id) => ({
          stage: id, label: id, state: "done", spent: 0, cap: 1, fraction: 0, visits: 1, returned: false,
        })),
      },
      { status: "done", alive: false },
    );
    const api = open(done, { "POST /api/projects/demo/send-back": { ok: true } });

    await userEvent.selectOptions(await screen.findByLabelText("Send it back to"), "architect");
    await userEvent.type(screen.getByLabelText("What should change?"), "Store events in Postgres");
    await userEvent.click(screen.getByRole("button", { name: "Send back and continue" }));

    await waitFor(() => expect(api.posts().map((p) => p.body)).toEqual([{ to: "architect", comment: "Store events in Postgres" }]));
  });

  it("offers only the agents the project has reached", async () => {
    open(project({ can_send_back: true, stages: [
      { stage: "product_owner", label: "Product Owner", state: "finished", spent: 0, cap: 1, fraction: 0, visits: 1, returned: false },
      { stage: "architect", label: "Architect", state: "finished", spent: 0, cap: 1, fraction: 0, visits: 1, returned: false },
      { stage: "developer", label: "Developer", state: "current", spent: 0, cap: 1, fraction: 0, visits: 1, returned: false },
      { stage: "qa", label: "QA", state: "upcoming", spent: 0, cap: 1, fraction: 0, visits: 0, returned: false },
    ] }, { status: "halted", alive: false }));

    const select = await screen.findByLabelText("Send it back to");

    const options = within(select).getAllByRole("option").map((o) => o.textContent);
    expect(options).toEqual(["Let the coordinator decide (recommended)", "Product Owner", "Architect", "Developer"]);
  });

  it("lets the coordinator choose the agent by default and tells you where it went and why", async () => {
    const api = open(project({ can_send_back: true }, { status: "done", alive: false }), {
      "POST /api/projects/demo/send-back": {
        ok: true, agent: "architect", label: "Architect", reason: "It is a big change.", routed: true,
      },
    });

    await userEvent.type(await screen.findByLabelText("What should change?"), "Make it pass the PRD");
    await userEvent.click(screen.getByRole("button", { name: "Send back and continue" }));

    await waitFor(() => expect(api.posts().map((p) => p.body)).toEqual([{ to: "auto", comment: "Make it pass the PRD" }]));
    expect(await screen.findByText(/Sent to the Architect/)).toBeInTheDocument();
    expect(screen.getByText(/It is a big change\./)).toBeInTheDocument();
  });

  it("needs an instruction before it will send anything", async () => {
    const api = open(project({ can_send_back: true }, { status: "halted", alive: false }));
    await screen.findByLabelText("What should change?");

    expect(screen.getByRole("button", { name: "Send back and continue" })).toBeDisabled();
    expect(api.posts()).toHaveLength(0);
  });

  it("tells you to pause first while the project is running", async () => {
    open(project({ can_send_back: false }));

    expect(await screen.findByText(/Pause the project first/)).toBeInTheDocument();
    expect(screen.queryByLabelText("What should change?")).not.toBeInTheDocument();
  });

  it("shows why the server refused to send it back", async () => {
    open(project({ can_send_back: true }, { status: "halted", alive: false }), {
      "POST /api/projects/demo/send-back": { status: 409, body: { error: "demo is already running. Pause it first." } },
    });
    await userEvent.type(await screen.findByLabelText("What should change?"), "Change it");

    await userEvent.click(screen.getByRole("button", { name: "Send back and continue" }));

    expect(await screen.findByText("demo is already running. Pause it first.")).toBeInTheDocument();
  });

  it("has no Developer model or billing controls: those are set once, in Models", async () => {
    open(project());
    await screen.findByText("An idea");

    expect(screen.queryByText("Developer model")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Local model")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("My subscription")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Follow the default")).not.toBeInTheDocument();
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
  describe("raising a cap", () => {
    it("offers every agent and the project, and sends the amount for the one chosen", async () => {
      const api = open(project({}, { status: "halted", alive: false }), { "POST /api/projects/demo/raise-cap": { ok: true } });

      const choose = await screen.findByLabelText("Raise the cap of");
      const options = within(choose).getAllByRole("option").map((o) => o.textContent);
      expect(options).toEqual(["Project", "Product Owner", "Developer"]);
      await userEvent.selectOptions(choose, "product_owner");
      await userEvent.type(screen.getByLabelText("Add dollars"), "2.5");
      await userEvent.click(screen.getByRole("button", { name: "Raise cap" }));

      await waitFor(() => expect(api.posts().map((p) => p.body)).toEqual([{ scope: "product_owner", amount: 2.5 }]));
    });

    it("says to pause first while the run is going", async () => {
      open(project());

      expect(await screen.findByText(/Pause the project to raise a cap/)).toBeInTheDocument();
      expect(screen.queryByRole("button", { name: "Raise cap" })).toBeNull();
    });

    it("refuses an amount that is not a positive number without calling the server", async () => {
      const api = open(project({}, { status: "halted", alive: false }));

      await userEvent.type(await screen.findByLabelText("Add dollars"), "lots");
      await userEvent.click(screen.getByRole("button", { name: "Raise cap" }));

      expect(await screen.findByText(/positive number of dollars/)).toBeInTheDocument();
      expect(api.posts()).toEqual([]);
    });
  });

  it("shows this project's own tasks with Pause and Stop", async () => {
    const tasks = {
      slug: "demo", status: "running", alive: true, can_pause: true, can_resume: false,
      agent: { stage: "developer", label: "Developer", state: "running" },
      tasks: [{ id: "T2", title: "Add the API", state: "working", waiting_on: [], pr_url: null, can_kill: true, kill_blocked: null }],
    };
    open(project(), { "GET /api/projects/demo/tasks": tasks });

    const row = (await screen.findByText("Add the API")).closest("li") as HTMLElement;

    expect(within(row).getByRole("button", { name: "Pause" })).toBeInTheDocument();
    expect(within(row).getByRole("button", { name: "Stop" })).toBeInTheDocument();
  });
});
