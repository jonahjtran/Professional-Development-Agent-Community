import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { mockApi } from "../test/mockApi";
import { ChatPanel } from "./ChatPanel";

const repos = {
  repos: [
    { id: "studio", label: "Agent Studio (this app)" },
    { id: "demo", label: "demo" },
  ],
  cap_usd: 1,
};

const reply = [
  { type: "session", id: "s1" },
  { type: "step", text: "Reading permissions.py" },
  { type: "text", delta: "It decides " },
  { type: "text", delta: "what is allowed." },
  { type: "done", html: "<p>It decides <strong>what</strong> is allowed.</p>", cost_usd: 0.02, session_cost_usd: 0.02, proposals: [] },
];

const past = [
  { id: "c1", title: "Why did QA fail?", repo: "studio", project: "demo", updated: "2026-11-08T12:00:00+00:00", turns: 2, cost_usd: 0.03 },
  { id: "c2", title: "What is the Developer doing?", repo: "studio", project: "demo", updated: "2026-11-07T12:00:00+00:00", turns: 4, cost_usd: 0.1 },
];

const stored = {
  id: "c1", title: "Why did QA fail?", repo: "studio", tier: "fast", project: "demo", cost_usd: 0.03,
  turns: [
    { role: "you", text: "Why did QA fail?" },
    { role: "assistant", text: "Because", html: "<p>Because <em>gitleaks</em> is missing.</p>", steps: ["Reading qa_report.md"], proposals: [] },
  ],
};

function open(routes: Record<string, unknown> = {}, request: { prompt?: string; question?: string } | null = null) {
  const api = mockApi({
    "GET /api/chat/repos": repos,
    "GET /api/chats?project=demo": { chats: [] },
    "POST /api/chat": { stream: reply },
    ...routes,
  });
  const onClose = vi.fn();
  render(<ChatPanel project="demo" request={request} onClose={onClose} />);
  return { ...api, onClose };
}

async function send(text: string) {
  await userEvent.type(await screen.findByLabelText("Message"), text);
  await userEvent.click(screen.getByRole("button", { name: "Send" }));
}

describe("the chat sidebar", () => {
  it("sends a message and shows the streamed answer, its steps and the cost", async () => {
    const api = open();

    await send("what does permissions.py do?");

    expect(await screen.findByText("Reading permissions.py")).toBeInTheDocument();
    expect(await screen.findByText("what")).toBeInTheDocument(); // the final, rendered Markdown
    expect(screen.getByText("what does permissions.py do?")).toBeInTheDocument();
    expect(screen.getByText(/\$0\.02 of \$1\.00/)).toBeInTheDocument();
    const call = api.posts().find((p) => p.url === "/api/chat");
    expect(call?.body).toEqual({ message: "what does permissions.py do?", repo: "demo", tier: "strong", session_id: null, context: null, project: "demo" });
  });

  it("continues the same chat with the session id, and New chat starts over", async () => {
    const api = open();
    await send("first");
    await screen.findByText("what");

    await send("second");
    await waitFor(() => expect(api.posts().filter((p) => p.url === "/api/chat")).toHaveLength(2));
    expect((api.posts()[1]?.body as { session_id: string }).session_id).toBe("s1");

    await userEvent.click(screen.getByRole("button", { name: "New chat" }));
    await send("third");
    await waitFor(() => expect(api.posts().filter((p) => p.url === "/api/chat")).toHaveLength(3));
    expect((api.posts()[2]?.body as { session_id: string | null }).session_id).toBeNull();
  });

  it("lets you pick the repository and the model", async () => {
    const api = open();
    await userEvent.selectOptions(await screen.findByLabelText("Ask about"), "studio");
    await userEvent.click(screen.getByLabelText("Fast"));

    await send("hi");

    await waitFor(() => expect(api.posts().length).toBeGreaterThan(0));
    expect(api.posts()[0]?.body).toMatchObject({ repo: "studio", tier: "fast" });
  });

  it("shows an error from the assistant", async () => {
    open({ "POST /api/chat": { stream: [{ type: "error", message: "This chat reached its $1.00 cap. Start a new chat to continue." }] } });

    await send("more");

    expect(await screen.findByText(/reached its \$1\.00 cap/)).toBeInTheDocument();
  });

  it("shows a proposal as a card that nothing has done yet, and confirms it on click", async () => {
    const proposal = { id: "p1", project: "demo", action: "set_autonomy", summary: "Set demo to autonomous autonomy", status: "pending" };
    const api = open({
      "POST /api/chat": { stream: [{ type: "session", id: "s1" }, { type: "done", html: "<p>Proposed.</p>", cost_usd: 0.01, session_cost_usd: 0.01, proposals: [proposal] }] },
      "POST /api/projects/demo/proposals/p1/confirm": { ok: true, result: "Autonomy set to autonomous." },
    });
    await send("make demo autonomous");

    const card = await screen.findByRole("group", { name: /Proposal/ });
    expect(within(card).getByText("Set demo to autonomous autonomy")).toBeInTheDocument();
    expect(within(card).getByText(/Waiting for you/)).toBeInTheDocument();
    await userEvent.click(within(card).getByRole("button", { name: "Confirm" }));

    expect(await screen.findByText("Autonomy set to autonomous.")).toBeInTheDocument();
    expect(api.posts().map((p) => p.url)).toContain("/api/projects/demo/proposals/p1/confirm");
    expect(within(card).queryByRole("button", { name: "Confirm" })).not.toBeInTheDocument();
  });

  it("dismisses a proposal", async () => {
    const proposal = { id: "p1", project: "demo", action: "resume_project", summary: "Resume demo", status: "pending" };
    const api = open({
      "POST /api/chat": { stream: [{ type: "done", html: "<p>ok</p>", cost_usd: 0, session_cost_usd: 0, proposals: [proposal] }] },
      "POST /api/projects/demo/proposals/p1/dismiss": { ok: true },
    });
    await send("resume it");

    await userEvent.click(await screen.findByRole("button", { name: "Dismiss" }));

    await waitFor(() => expect(api.posts().map((p) => p.url)).toContain("/api/projects/demo/proposals/p1/dismiss"));
    expect(await screen.findByText(/Dismissed/)).toBeInTheDocument();
  });

  it("shows why a confirmation failed", async () => {
    const proposal = { id: "p1", project: "demo", action: "answer_question", summary: "Approve it", status: "pending" };
    open({
      "POST /api/chat": { stream: [{ type: "done", html: "<p>ok</p>", cost_usd: 0, session_cost_usd: 0, proposals: [proposal] }] },
      "POST /api/projects/demo/proposals/p1/confirm": { status: 409, body: { error: "This question is no longer open." } },
    });
    await send("approve");

    await userEvent.click(await screen.findByRole("button", { name: "Confirm" }));

    expect(await screen.findByText("This question is no longer open.")).toBeInTheDocument();
  });

  it("starts from a question: prefilled, with that question as context", async () => {
    const api = open({}, { prompt: "What is this asking?", question: "q1" });
    const box = await screen.findByLabelText("Message");
    expect(box).toHaveValue("What is this asking?");

    await userEvent.click(screen.getByRole("button", { name: "Send" }));

    await waitFor(() => expect(api.posts().length).toBeGreaterThan(0));
    expect(api.posts()[0]?.body).toMatchObject({ context: { project: "demo", question_id: "q1" } });
  });

  it("does not send an empty message", async () => {
    const api = open();
    await screen.findByLabelText("Message");

    expect(screen.getByRole("button", { name: "Send" })).toBeDisabled();
    expect(api.posts()).toHaveLength(0);
  });

  it("defaults to asking about the project you are in", async () => {
    open();

    expect(await screen.findByLabelText("Ask about")).toHaveValue("demo");
  });

  it("falls back to this app when the project has no clone to read", async () => {
    open({ "GET /api/chat/repos": { repos: [{ id: "studio", label: "Agent Studio (this app)" }], cap_usd: 1 } });

    expect(await screen.findByLabelText("Ask about")).toHaveValue("studio");
  });

  it("closes from its own button", async () => {
    const { onClose } = open();

    await userEvent.click(await screen.findByRole("button", { name: "Close chat" }));

    expect(onClose).toHaveBeenCalled();
  });
});

describe("chat history", () => {
  async function showHistory() {
    await userEvent.click(await screen.findByRole("button", { name: "History" }));
  }

  it("lists the past chats of this project, newest first, with a way to start a new one", async () => {
    open({ "GET /api/chats?project=demo": { chats: past } });
    await showHistory();

    const rows = await screen.findAllByRole("button", { name: /^Open chat:/ });

    expect(rows.map((r) => r.textContent)).toEqual([expect.stringContaining("Why did QA fail?"), expect.stringContaining("What is the Developer doing?")]);
    expect(screen.getByRole("button", { name: "New chat" })).toBeInTheDocument();
  });

  it("says so when there are no past chats", async () => {
    open();
    await showHistory();

    expect(await screen.findByText(/No past chats yet/)).toBeInTheDocument();
  });

  it("opens a past chat with its answers, and keeps talking in it", async () => {
    const api = open({ "GET /api/chats?project=demo": { chats: past }, "GET /api/chats/c1": stored });
    await showHistory();
    await userEvent.click(await screen.findByRole("button", { name: /Open chat: Why did QA fail/ }));

    expect(await screen.findByText("gitleaks")).toBeInTheDocument();
    expect(screen.getByText("Reading qa_report.md")).toBeInTheDocument();
    expect(screen.getByText(/\$0\.03 of \$1\.00/)).toBeInTheDocument();
    await send("and the fix?");

    await waitFor(() => expect(api.posts().some((p) => p.url === "/api/chat")).toBe(true));
    expect(api.posts()[0]?.body).toMatchObject({ session_id: "c1", repo: "studio", tier: "fast", project: "demo" });
  });

  it("locks the repository and model of a chat that has begun", async () => {
    open({ "GET /api/chats?project=demo": { chats: past }, "GET /api/chats/c1": stored });
    await showHistory();
    await userEvent.click(await screen.findByRole("button", { name: /Open chat: Why did QA fail/ }));
    await screen.findByText("gitleaks");

    expect(screen.getByLabelText("Ask about")).toBeDisabled();
  });

  it("New chat clears the conversation and starts without a session", async () => {
    const api = open({ "GET /api/chats?project=demo": { chats: past }, "GET /api/chats/c1": stored });
    await showHistory();
    await userEvent.click(await screen.findByRole("button", { name: /Open chat: Why did QA fail/ }));
    await screen.findByText("gitleaks");

    await userEvent.click(screen.getByRole("button", { name: "New chat" }));
    expect(screen.queryByText("gitleaks")).toBeNull();
    await send("fresh");

    await waitFor(() => expect(api.posts().some((p) => p.url === "/api/chat")).toBe(true));
    expect(api.posts()[0]?.body).toMatchObject({ session_id: null });
  });

  it("deletes a past chat after you confirm", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(true);
    const api = open({ "GET /api/chats?project=demo": { chats: past }, "POST /api/chats/c2/delete": { ok: true } });
    await showHistory();

    await userEvent.click(await screen.findByRole("button", { name: /Delete chat: What is the Developer doing/ }));

    await waitFor(() => expect(api.posts().map((p) => p.url)).toEqual(["/api/chats/c2/delete"]));
  });

  it("tells you when a chat cannot be opened", async () => {
    open({ "GET /api/chats?project=demo": { chats: past }, "GET /api/chats/c1": { status: 404, body: { error: "there is no such chat" } } });
    await showHistory();

    await userEvent.click(await screen.findByRole("button", { name: /Open chat: Why did QA fail/ }));

    expect(await screen.findByText("there is no such chat")).toBeInTheDocument();
  });
});
