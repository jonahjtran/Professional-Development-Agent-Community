import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import { mockApi } from "../test/mockApi";
import { QuestionPage } from "./QuestionPage";

const gate = {
  status: "open", id: "abc", project: "demo", kind: "gate", title: "Review: the plan", detail: "",
  request: {
    stage: "career_coach", title: "Review", summary: "x", cost_usd: 1.5,
    items: [{ id: "readme", label: "Replace README" }, { id: "website", label: "Website entry" }],
    questions: ["How many users?", "Built alone?"],
  },
  summary_html: "<h1>Report</h1>", refused: null, workbench: null,
};

const workbench = {
  fit: [{ track: "tech_consulting", decision: "include", reason: "AI implementation." }],
  changes: [{
    action: "add", section: "Projects", entry: "Agent Studio", reason: "Shows control.",
    replaces: "Old project",
    bullets: [{ text: "Passed 9 checks", evidence: [{ id: "qa", type: "qa", description: "9 of 9 checks passed" }] }],
    latex: "\\resumeProject",
  }],
  website: null, website_reason: "No measured result yet.",
  readme_html: "<h1>README</h1>", case_study_html: "<h1>Case study</h1>",
};

function open(question: unknown, extra: Record<string, unknown> = {}) {
  const api = mockApi({
    "GET /api/projects/demo/questions/abc": question,
    "POST /api/projects/demo/questions/abc": { ok: true },
    ...extra,
  });
  render(
    <MemoryRouter initialEntries={["/p/demo/q/abc"]}>
      <Routes>
        <Route path="/p/:slug/q/:id" element={<QuestionPage />} />
        <Route path="/inbox" element={<div>the inbox</div>} />
      </Routes>
    </MemoryRouter>,
  );
  return api;
}

describe("a gate question", () => {
  it("approves with every item on, the answers given, and goes back to the inbox", async () => {
    const api = open(gate);
    await screen.findByText("Questions for you");

    await userEvent.type(screen.getByLabelText("How many users?"), "40 students");
    await userEvent.click(screen.getByRole("button", { name: "Approve" }));

    await screen.findByText("the inbox");
    expect(api.posts()[0]?.body).toEqual({
      fields: { outcome: "approve", comments: "", answer0: "40 students" },
      items: ["readme", "website"],
    });
  });

  it("leaves out an item that is switched off", async () => {
    const api = open(gate);
    await userEvent.click(await screen.findByRole("switch", { name: "Website entry" }));

    await userEvent.click(screen.getByRole("button", { name: "Approve" }));

    await waitFor(() => expect(api.posts()).toHaveLength(1));
    expect((api.posts()[0]?.body as { items: string[] }).items).toEqual(["readme"]);
  });

  it("requests changes with the comment, and rejects with halt only if asked", async () => {
    const api = open(gate);
    await userEvent.type(await screen.findByLabelText(/Comments/), "tighten the bullets");

    await userEvent.click(screen.getByRole("switch", { name: /pause the project/ }));
    await userEvent.click(screen.getByRole("button", { name: "Reject" }));

    await waitFor(() => expect(api.posts()).toHaveLength(1));
    expect((api.posts()[0]?.body as { fields: Record<string, string> }).fields).toEqual({
      outcome: "reject", comments: "tighten the bullets", halt: "on",
    });
  });

  it("rejects without halting unless the switch is on", async () => {
    const api = open(gate);

    await userEvent.click(await screen.findByRole("button", { name: "Reject" }));

    await waitFor(() => expect(api.posts()).toHaveLength(1));
    expect((api.posts()[0]?.body as { fields: Record<string, string> }).fields).toEqual({
      outcome: "reject", comments: "",
    });
  });

  it("shows the server's reason and stays on the page when the answer is refused", async () => {
    open(gate, {
      "POST /api/projects/demo/questions/abc": { status: 400, body: { error: "say what should change" } },
    });

    await userEvent.click(await screen.findByRole("button", { name: "Request changes" }));

    expect(await screen.findByText("say what should change")).toBeInTheDocument();
    expect(screen.queryByText("the inbox")).not.toBeInTheDocument();
  });

  it("tells you when your earlier answer was refused", async () => {
    open({ ...gate, refused: "unknown items: x" });

    expect(await screen.findByText(/Your last answer was refused: unknown items: x/)).toBeInTheDocument();
  });

  it("shows the career workbench with each bullet beside its evidence", async () => {
    open({ ...gate, workbench });

    expect(await screen.findByText("Fit with your goals")).toBeInTheDocument();
    expect(screen.getByText("• Passed 9 checks")).toBeInTheDocument();
    expect(screen.getByText(/9 of 9 checks passed/)).toBeInTheDocument();
    expect(screen.getByText("Replaces: Old project")).toBeInTheDocument();
    expect(screen.getByText(/No website entry\. No measured result yet\./)).toBeInTheDocument();
  });

  it("copies the LaTeX snippet", async () => {
    const writeText = vi.fn(() => Promise.resolve());
    vi.stubGlobal("navigator", { ...navigator, clipboard: { writeText } });
    open({ ...gate, workbench });

    await userEvent.click(await screen.findByText("LaTeX for Overleaf"));
    await userEvent.click(screen.getByRole("button", { name: "Copy" }));

    expect(writeText).toHaveBeenCalledWith("\\resumeProject");
    expect(await screen.findByRole("button", { name: "Copied" })).toBeInTheDocument();
  });
});

describe("the other questions", () => {
  it("answers a budget pause by raising it, abandoning or approving once", async () => {
    const api = open({
      status: "open", id: "abc", project: "demo", kind: "cap", title: "Limit reached", detail: "80% of the cap",
      request: { kind: "budget_cap", stage: "developer", detail: "80% of the cap", can_approve_anyway: true, unit: "usd" },
      summary_html: "", refused: null, workbench: null,
    });
    await userEvent.type(await screen.findByLabelText(/Raise by dollars/), "5");
    expect(screen.getByRole("button", { name: /Approve anyway/ })).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Raise" }));

    await waitFor(() => expect(api.posts()).toHaveLength(1));
    expect(api.posts()[0]?.body).toEqual({ fields: { action: "raise", amount: "5" }, items: [] });
  });

  it("hides 'approve anyway' when the pause does not allow it", async () => {
    open({
      status: "open", id: "abc", project: "demo", kind: "cap", title: "Limit reached", detail: "d",
      request: { kind: "revision_cap", stage: "architect", detail: "d", can_approve_anyway: false, unit: "revisions" },
      summary_html: "", refused: null, workbench: null,
    });

    await screen.findByLabelText(/Raise by revisions/);

    expect(screen.queryByRole("button", { name: /Approve anyway/ })).not.toBeInTheDocument();
  });

  it("lists the pull requests to merge and answers yes or no", async () => {
    const api = open({
      status: "open", id: "abc", project: "demo", kind: "merge", title: "Merge your pull requests", detail: "",
      request: { pr_urls: ["https://github.com/me/p/pull/1"] },
      summary_html: "", refused: null, workbench: null,
    });
    const link = await screen.findByRole("link", { name: "https://github.com/me/p/pull/1" });
    expect(link).toHaveAttribute("rel", expect.stringContaining("noopener"));

    await userEvent.click(screen.getByRole("button", { name: /I merged them/ }));

    await waitFor(() => expect(api.posts()).toHaveLength(1));
    expect(api.posts()[0]?.body).toEqual({ fields: { merged: "yes" }, items: [] });
  });

  it("shows what the Developer wants to run as plain text and answers allow or deny", async () => {
    const api = open({
      status: "open", id: "abc", project: "demo", kind: "tool", title: "Permission needed: Bash", detail: "",
      request: { tool: "Bash", summary: "pip install <x>", reason: "installs a package" },
      summary_html: "", refused: null, workbench: null,
    });
    expect(await screen.findByText("pip install <x>")).toBeInTheDocument();
    await userEvent.type(screen.getByLabelText(/tell the agent why/), "use the stdlib");

    await userEvent.click(screen.getByRole("button", { name: "Deny" }));

    await waitFor(() => expect(api.posts()).toHaveLength(1));
    expect(api.posts()[0]?.body).toEqual({ fields: { allow: "no", message: "use the stdlib" }, items: [] });
  });
});

describe("an explained approval", () => {
  const tool = {
    status: "open", id: "abc", project: "demo", kind: "tool", title: "Permission needed: Bash", detail: "",
    request: { tool: "Bash", summary: "npm install", reason: "not on the allowed list" },
    summary_html: "", refused: null, workbench: null,
  };

  it("shows what the request does and how risky it looks, as advice", async () => {
    open(tool, {
      "GET /api/projects/demo/questions/abc/explanation": {
        explanation: {
          what: "Installs the project's libraries.", why: "The task needs them to run tests.",
          effect: "Writes node_modules.", risk: "low", unsure: false,
        },
        error: null, cost_usd: 0.001,
      },
    });

    expect(await screen.findByText("Installs the project's libraries.")).toBeInTheDocument();
    expect(screen.getByText(/The task needs them/)).toBeInTheDocument();
    expect(screen.getByText(/Writes node_modules/)).toBeInTheDocument();
    expect(screen.getByText("low risk")).toBeInTheDocument();
    expect(screen.getByText(/advice only/i)).toBeInTheDocument();
    expect(screen.getByText("npm install")).toBeInTheDocument();
  });

  it("says when the explainer is unsure, and still offers the decision", async () => {
    open(tool, {
      "GET /api/projects/demo/questions/abc/explanation": {
        explanation: { what: "Runs a script.", why: "Unclear.", effect: "Unknown.", risk: "high", unsure: true },
        error: null, cost_usd: 0,
      },
    });

    expect(await screen.findByText(/not sure/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Allow once" })).toBeInTheDocument();
  });

  it("falls back to the raw request when the explanation is unavailable", async () => {
    open(tool, {
      "GET /api/projects/demo/questions/abc/explanation": {
        explanation: null, error: "The explanation service is unavailable.", cost_usd: 0,
      },
    });

    expect(await screen.findByText("The explanation service is unavailable.")).toBeInTheDocument();
    expect(screen.getByText("npm install")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Deny" })).toBeInTheDocument();
  });
});

describe("remembering an approval", () => {
  const tool = {
    status: "open", id: "abc", project: "demo", kind: "tool", title: "Permission needed: Bash", detail: "",
    request: { tool: "Bash", summary: "npx next lint --fix", reason: "not on the allowed list" },
    summary_html: "", refused: null, workbench: null,
  };
  const noExplanation = {
    "GET /api/projects/demo/questions/abc/explanation": { explanation: null, error: "off", cost_usd: 0 },
  };
  const options = {
    available: true, reason: null,
    parts: [{ label: "npx next lint --fix", exact: ["npx", "next", "lint", "--fix"], any_label: "npx next …any arguments" }],
  };

  it("allows once by default, without asking to remember", async () => {
    const api = open(tool, { ...noExplanation, "GET /api/projects/demo/questions/abc/rule-options": options });
    await screen.findByText("npx next lint --fix");

    await userEvent.click(await screen.findByRole("button", { name: "Allow once" }));

    await waitFor(() => expect(api.posts()).toHaveLength(1));
    expect(api.posts()[0]?.body).toEqual({ fields: { allow: "yes", message: "" }, items: [] });
  });

  it("can remember the wider pattern for this project", async () => {
    const api = open(tool, { ...noExplanation, "GET /api/projects/demo/questions/abc/rule-options": options });
    await userEvent.click(await screen.findByLabelText("Always in this project"));
    await userEvent.selectOptions(screen.getByLabelText("npx next lint --fix"), "any");

    await userEvent.click(screen.getByRole("button", { name: "Allow and remember" }));

    await waitFor(() => expect(api.posts()).toHaveLength(1));
    expect(api.posts()[0]?.body).toEqual({
      fields: { allow: "yes", message: "", remember: "project", rule_modes: "any" }, items: [],
    });
  });

  it("can remember exactly this command everywhere", async () => {
    const api = open(tool, { ...noExplanation, "GET /api/projects/demo/questions/abc/rule-options": options });
    await userEvent.click(await screen.findByLabelText("Always everywhere"));

    await userEvent.click(screen.getByRole("button", { name: "Allow and remember" }));

    await waitFor(() => expect(api.posts()).toHaveLength(1));
    expect(api.posts()[0]?.body).toEqual({
      fields: { allow: "yes", message: "", remember: "global", rule_modes: "exact" }, items: [],
    });
  });

  it("explains why a command cannot be remembered, and still allows it once", async () => {
    open(tool, {
      ...noExplanation,
      "GET /api/projects/demo/questions/abc/rule-options": {
        available: false, reason: "This cannot be remembered: git push.", parts: [],
      },
    });

    expect(await screen.findByText(/cannot be remembered/)).toBeInTheDocument();
    expect(screen.queryByLabelText("Always in this project")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Allow once" })).toBeInTheDocument();
  });
});

describe("asking the assistant", () => {
  it("offers to ask about this question, carrying the question as context", async () => {
    open(gate);

    const link = await screen.findByRole("link", { name: /Ask the assistant/ });

    const url = new URL(link.getAttribute("href") ?? "", "http://x");
    expect(url.pathname).toBe("/chat");
    expect(url.searchParams.get("project")).toBe("demo");
    expect(url.searchParams.get("question")).toBe("abc");
  });
});

describe("a question that is gone", () => {
  it("says it was answered, or that it is no longer open", async () => {
    open({ status: "answered", id: "abc", project: "demo" });
    expect(await screen.findByText("This question was already answered.")).toBeInTheDocument();
  });

  it("explains a 404", async () => {
    open({ status: 404, body: { error: "gone" } });
    const banner = await screen.findByRole("status");
    expect(within(banner).getByText(/no longer open/)).toBeInTheDocument();
  });
});
