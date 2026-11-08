import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";
import { mockApi } from "../test/mockApi";
import { ModelsPage } from "./ModelsPage";

const agent = (id: string, name: string, model: string, backend = "claude", note = "") => ({ id, name, backend, model, note });

function models(over: Record<string, unknown> = {}) {
  return {
    developer: { default: "claude", claude_model: "claude-sonnet-5-5", local_model: "qwen3-coder:27b-instruct-q4_K_M", endpoint: "http://localhost:11434" },
    agents: [
      agent("product_owner", "Product Owner", "claude-opus-5-5"),
      agent("architect", "Architect", "claude-opus-5-5"),
      agent("developer", "Developer", "claude-sonnet-5-5", "claude", "Chosen per project or as the default; applies from the next task."),
      agent("qa", "QA", "claude-haiku-5-5"),
      agent("career_coach", "Career coach", "claude-opus-5-5"),
      agent("chat", "Chat assistant", "claude-opus-5-5"),
      agent("explanations", "Approval explanations", "claude-haiku-5-5"),
    ],
    billing: { default: "api", sign_in: null },
    local: { ok: true, reachable: true, installed: true, context: 131072, detail: "qwen3-coder is installed and loaded (context 131072).", fix: "" },
    ...over,
  };
}

function open(body: unknown = models(), extra: Record<string, unknown> = {}) {
  const api = mockApi({
    "GET /api/models": body,
    "POST /api/models/developer": { ok: true },
    "POST /api/models/developer-billing": { ok: true },
    ...extra,
  });
  render(<MemoryRouter><ModelsPage /></MemoryRouter>);
  return api;
}

describe("the models page", () => {
  it("shows which model every agent uses, by its exact id", async () => {
    open();

    const row = await screen.findByRole("row", { name: /Developer/ });
    expect(within(row).getByText("claude-sonnet-5-5")).toBeInTheDocument();
    expect(screen.getByRole("row", { name: /Product Owner/ })).toHaveTextContent("claude-opus-5-5");
    expect(screen.getByRole("row", { name: /QA/ })).toHaveTextContent("claude-haiku-5-5");
    expect(screen.getByRole("row", { name: /Approval explanations/ })).toHaveTextContent("claude-haiku-5-5");
  });

  it("labels a local model as local and Claude models as Claude", async () => {
    open(models({
      developer: { default: "local", claude_model: "claude-sonnet-5-5", local_model: "qwen3-coder:27b", endpoint: "http://localhost:11434" },
      agents: [agent("developer", "Developer", "qwen3-coder:27b", "local")],
    }));

    const row = await screen.findByRole("row", { name: /Developer/ });
    expect(within(row).getByText("qwen3-coder:27b")).toBeInTheDocument();
    expect(within(row).getByText("Local")).toBeInTheDocument();
    expect(screen.getByLabelText(/Local/)).toBeChecked();
  });

  it("switches the default Developer model, and says it applies from the next task", async () => {
    const api = open();
    expect(await screen.findByLabelText(/Claude/)).toBeChecked();

    await userEvent.click(screen.getByLabelText(/Local/));

    await waitFor(() => expect(api.posts().map((p) => p.body)).toEqual([{ backend: "local" }]));
    expect(await screen.findByText("Saved. It applies from the next task.")).toBeInTheDocument();
  });

  it("shows the local server's state when it is ready", async () => {
    open();

    expect(await screen.findByText(/installed and loaded/)).toBeInTheDocument();
    expect(screen.getByText("Ready")).toBeInTheDocument();
  });

  it("shows what to do when the local server is not ready", async () => {
    open(models({
      local: { ok: false, reachable: false, installed: false, context: null, detail: "Ollama is not answering at http://localhost:11434.", fix: "Start it with `OLLAMA_CONTEXT_LENGTH=65536 ollama serve`." },
    }));

    expect(await screen.findByText(/Ollama is not answering/)).toBeInTheDocument();
    expect(screen.getByText(/OLLAMA_CONTEXT_LENGTH=65536 ollama serve/)).toBeInTheDocument();
    expect(screen.getByText("Not ready")).toBeInTheDocument();
  });

  it("checks the local server again on request", async () => {
    const api = open();
    await screen.findByText("Ready");

    await userEvent.click(screen.getByRole("button", { name: "Check again" }));

    await waitFor(() => expect(api.calls.filter((c) => c.url === "/api/models")).toHaveLength(2));
  });

  it("shows an error from the server", async () => {
    open({ status: 500, body: { error: "boom" } });

    expect(await screen.findByText("boom")).toBeInTheDocument();
  });

  it("says the Developer is paid for with API credits by default", async () => {
    open();

    expect(await screen.findByLabelText("API credits")).toBeChecked();
    expect(screen.getByLabelText("My subscription")).not.toBeChecked();
  });

  it("switches the Developer to your subscription and says how it differs", async () => {
    const api = open();

    await userEvent.click(await screen.findByLabelText("My subscription"));

    await waitFor(() => expect(api.posts().map((p) => p.body)).toEqual([{ billing: "subscription" }]));
    expect(screen.getByText(/ordinary individual use/i)).toBeInTheDocument();
    expect(screen.getByText(/estimates/i)).toBeInTheDocument();
    expect(screen.getByText(/planning agents.*API/i)).toBeInTheDocument();
  });

  it("shows whether you are signed in when a subscription is in use", async () => {
    open(models({ billing: { default: "subscription", sign_in: { ok: true, detail: "signed in with your Claude account", fix: "", skipped: false } } }));

    expect(await screen.findByText("signed in with your Claude account")).toBeInTheDocument();
    expect(screen.getByLabelText("My subscription")).toBeChecked();
  });

  it("shows how to sign in when you are not", async () => {
    open(models({ billing: { default: "subscription", sign_in: { ok: false, detail: "Claude Code is not signed in to a Claude account.", fix: "Run `claude auth login` and sign in.", skipped: false } } }));

    expect(await screen.findByText(/not signed in to a Claude account/)).toBeInTheDocument();
    expect(screen.getByText(/claude auth login/)).toBeInTheDocument();
  });

  it("shows nothing about sign-in while API credits are in use", async () => {
    open();

    await screen.findByLabelText("API credits");
    expect(screen.queryByText(/signed in/i)).not.toBeInTheDocument();
  });
});
