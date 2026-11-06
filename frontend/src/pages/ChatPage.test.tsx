import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";
import { mockApi } from "../test/mockApi";
import { ChatPage } from "./ChatPage";

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

function open(routes: Record<string, unknown> = {}, url = "/chat") {
  const api = mockApi({
    "GET /api/chat/repos": repos,
    "POST /api/chat": { stream: reply },
    "GET /api/proposals?status=pending": { proposals: [] },
    ...routes,
  });
  render(<MemoryRouter initialEntries={[url]}><ChatPage /></MemoryRouter>);
  return api;
}

async function send(text: string) {
  await userEvent.type(await screen.findByLabelText("Message"), text);
  await userEvent.click(screen.getByRole("button", { name: "Send" }));
}

describe("the chat page", () => {
  it("sends a message and shows the streamed answer, its steps and the cost", async () => {
    const api = open();

    await send("what does permissions.py do?");

    expect(await screen.findByText("Reading permissions.py")).toBeInTheDocument();
    expect(await screen.findByText("what")).toBeInTheDocument(); // the final, rendered Markdown
    expect(screen.getByText("what does permissions.py do?")).toBeInTheDocument();
    expect(screen.getByText(/\$0\.02 of \$1\.00/)).toBeInTheDocument();
    const call = api.posts().find((p) => p.url === "/api/chat");
    expect(call?.body).toEqual({ message: "what does permissions.py do?", repo: "studio", tier: "strong", session_id: null, context: null });
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
    await userEvent.selectOptions(await screen.findByLabelText("Ask about"), "demo");
    await userEvent.click(screen.getByLabelText("Fast"));

    await send("hi");

    await waitFor(() => expect(api.posts().length).toBeGreaterThan(0));
    expect(api.posts()[0]?.body).toMatchObject({ repo: "demo", tier: "fast" });
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
    const api = open({}, "/chat?project=demo&question=q1&prompt=What%20is%20this%20asking%3F");
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
});
