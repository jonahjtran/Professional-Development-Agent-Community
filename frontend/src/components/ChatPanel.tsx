import { useEffect, useRef, useState } from "react";
import { ApiError, get, post, postStream } from "../api";
import { useLoad } from "../hooks";
import type { ChatEvent, ChatRepos, ChatSummary, ProposalView, StoredChat } from "../types";
import type { ChatRequest } from "./ChatContext";
import { Banner, Button, Card, Loading, Segmented, money } from "./ui";

interface Turn {
  role: "you" | "assistant";
  text: string; // plain text while streaming, and for what you typed
  html?: string; // server-rendered Markdown, set when the answer is complete
  steps: string[];
  proposals: ProposalView[];
  error?: string;
  stopped?: boolean; // you pressed Stop: what is shown is what it had written
}

const lastKey = (project: string) => `studio.chat.last.${project}`;

function remember(project: string, id: string | null) {
  try {
    if (id === null) localStorage.removeItem(lastKey(project));
    else localStorage.setItem(lastKey(project), id);
  } catch {
    // a browser that will not keep it: the chat still works, it is just not remembered
  }
}

function remembered(project: string): string | null {
  try {
    return localStorage.getItem(lastKey(project));
  } catch {
    return null;
  }
}

type Tier = "strong" | "fast";

function ProposalCard({ proposal, onChange }: { proposal: ProposalView; onChange: (p: ProposalView) => void }) {
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);

  async function decide(action: "confirm" | "dismiss") {
    setBusy(true);
    setProblem(null);
    try {
      const reply = await post<{ result?: string }>(`/api/projects/${proposal.project}/proposals/${proposal.id}/${action}`);
      onChange({
        ...proposal,
        status: action === "confirm" ? "confirmed" : "dismissed",
        result: action === "confirm" ? (reply.result ?? "Done.") : "Dismissed.",
      });
    } catch (e) {
      setProblem(e instanceof ApiError ? e.message : String(e));
      setBusy(false);
    }
  }

  return (
    <div className={`proposal ${proposal.status}`} role="group" aria-label={`Proposal for ${proposal.project}`}>
      <div className="head">{proposal.summary}</div>
      {proposal.status === "pending" ? (
        <>
          <div className="muted">Waiting for you: nothing has happened yet.</div>
          <div className="actions">
            <Button disabled={busy} onClick={() => void decide("confirm")}>Confirm</Button>
            <Button tone="secondary" disabled={busy} onClick={() => void decide("dismiss")}>Dismiss</Button>
          </div>
        </>
      ) : (
        <div className="muted">{proposal.result ?? proposal.status}</div>
      )}
      {problem && <div className="muted">{problem}</div>}
    </div>
  );
}

function when(iso: string): string {
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? "" : date.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

/**
 * The chat sidebar of a project: the conversation, a history of past chats of this project, and a
 * way to start a new one. A past chat can be opened and continued.
 */
export function ChatPanel({ project, request, onClose }: { project: string; request: ChatRequest | null; onClose: () => void }) {
  const { data: repos } = useLoad<ChatRepos>("/api/chat/repos");
  const history = useLoad<{ chats: ChatSummary[] }>(`/api/chats?project=${project}`);
  const [view, setView] = useState<"chat" | "history">("chat");
  const [repo, setRepo] = useState("");
  const [tier, setTier] = useState<Tier>("strong");
  const [turns, setTurns] = useState<Turn[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [cost, setCost] = useState(0);
  const [problem, setProblem] = useState<string | null>(null);
  const [started, setStarted] = useState(false);
  const session = useRef<string | null>(null);
  const writing = useRef<AbortController | null>(null);
  const context = useRef<{ project: string; question_id: string } | null>(null);

  function newChat() {
    session.current = null;
    context.current = null;
    setStarted(false);
    setTurns([]);
    setCost(0);
    setInput("");
    setProblem(null);
    setView("chat");
    remember(project, null);
  }

  // Coming back to a project: pick up the chat you were in.
  useEffect(() => {
    const id = remembered(project);
    if (request || id === null) return;
    void openPast(id, true);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // A page asked the chat to start from something (for example an open question).
  useEffect(() => {
    if (!request) return;
    newChat();
    setInput(request.prompt ?? "");
    context.current = request.question ? { project, question_id: request.question } : null;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [request]);

  const chosenRepo = repo || (repos?.repos.some((r) => r.id === project) ? project : "studio");

  function patchLast(change: (t: Turn) => Turn) {
    setTurns((all) => all.map((t, i) => (i === all.length - 1 ? change(t) : t)));
  }

  async function send() {
    const message = input.trim();
    if (!message || busy) return;
    setInput("");
    setBusy(true);
    setStarted(true);
    setTurns((all) => [
      ...all,
      { role: "you", text: message, steps: [], proposals: [] },
      { role: "assistant", text: "", steps: [], proposals: [] },
    ]);
    const controller = new AbortController();
    writing.current = controller;
    const body = {
      message,
      repo: chosenRepo,
      tier,
      session_id: session.current,
      context: session.current ? null : context.current,
      project,
    };
    try {
      await postStream<ChatEvent>("/api/chat", body, (event) => {
        if (event.type === "session") {
          session.current = event.id;
          remember(project, event.id);
        } else if (event.type === "step") patchLast((t) => ({ ...t, steps: [...t.steps, event.text] }));
        else if (event.type === "text") patchLast((t) => ({ ...t, text: t.text + event.delta }));
        else if (event.type === "error") patchLast((t) => ({ ...t, error: event.message }));
        else {
          setCost(event.session_cost_usd);
          patchLast((t) => ({ ...t, html: event.html, proposals: event.proposals }));
        }
      }, controller.signal);
      context.current = null;
      history.reload();
    } catch (e) {
      if (controller.signal.aborted) {
        context.current = null;
        patchLast((t) => ({ ...t, stopped: true }));
        history.reload();
      } else {
        patchLast((t) => ({ ...t, error: e instanceof ApiError ? e.message : String(e) }));
      }
    } finally {
      writing.current = null;
      setBusy(false);
    }
  }

  function stop() {
    writing.current?.abort();
  }

  async function openPast(id: string, quiet = false) {
    setProblem(null);
    try {
      const chat = await get<StoredChat>(`/api/chats/${id}`);
      session.current = chat.id;
      context.current = null;
      setRepo(chat.repo);
      setTier(chat.tier);
      setCost(chat.cost_usd);
      setStarted(true);
      setInput("");
      setTurns(
        chat.turns.map((t) => ({
          role: t.role,
          text: t.text,
          html: t.html ?? undefined,
          steps: t.steps ?? [],
          proposals: t.proposals ?? [],
          error: t.error,
          stopped: t.stopped,
        })),
      );
      remember(project, chat.id);
      setView("chat");
    } catch (e) {
      if (quiet) remember(project, null); // the remembered chat is gone: start fresh, say nothing
      else setProblem(e instanceof ApiError ? e.message : String(e));
    }
  }

  async function removePast(chat: ChatSummary) {
    if (!window.confirm(`Delete the chat “${chat.title}”? This cannot be undone.`)) return;
    setProblem(null);
    try {
      await post(`/api/chats/${chat.id}/delete`);
      if (session.current === chat.id) newChat();
      history.reload();
    } catch (e) {
      setProblem(e instanceof ApiError ? e.message : String(e));
    }
  }

  function updateProposal(turn: number, next: ProposalView) {
    setTurns((all) => all.map((t, i) => (i === turn ? { ...t, proposals: t.proposals.map((p) => (p.id === next.id ? next : p)) } : t)));
  }

  if (repos === null) return <Loading what="the chat" />;

  return (
    <div className="chat-panel">
      <div className="chat-head">
        <strong>Chat</strong>
        <div className="chat-tools">
          <Button tone="secondary" onClick={newChat}>New chat</Button>
          <Button
            tone="secondary"
            aria-pressed={view === "history"}
            onClick={() => {
              if (view === "chat") history.reload();
              setView(view === "chat" ? "history" : "chat");
            }}
          >
            History
          </Button>
          <button type="button" className="chat-close" aria-label="Collapse chat" title="Collapse chat" onClick={onClose}>»</button>
        </div>
      </div>
      {problem && <Banner>{problem}</Banner>}

      {view === "history" ? (
        <div className="chat-history">
          {history.data === null ? (
            <Loading what="past chats" />
          ) : history.data.chats.length === 0 ? (
            <p className="muted">No past chats yet. Your conversations in {project} are kept here.</p>
          ) : (
            history.data.chats.map((c) => (
              <div key={c.id} className="chat-past">
                <button type="button" className="chat-past-open" aria-label={`Open chat: ${c.title}`} onClick={() => void openPast(c.id)}>
                  <span className="title">{c.title || "Untitled chat"}</span>
                  <span className="muted">{when(c.updated)} · {Math.ceil(c.turns / 2)} {c.turns <= 2 ? "message" : "messages"}</span>
                </button>
                <button type="button" className="chat-past-delete" aria-label={`Delete chat: ${c.title}`} onClick={() => void removePast(c)}>
                  Delete
                </button>
              </div>
            ))
          )}
        </div>
      ) : (
        <>
          <div className="chat-bar">
            <label className="field">
              Ask about
              <select value={chosenRepo} disabled={started} onChange={(e) => setRepo(e.target.value)}>
                {repos.repos.map((r) => <option key={r.id} value={r.id}>{r.label}</option>)}
              </select>
            </label>
            <Segmented<Tier>
              name="tier"
              value={tier}
              onChange={setTier}
              options={[{ value: "strong", label: "Strong" }, { value: "fast", label: "Fast" }]}
            />
            <span className="muted">This chat: {money(cost)} of {money(repos.cap_usd)}</span>
          </div>

          <div className="chat-log" aria-live="polite">
            {turns.length === 0 && (
              <p className="muted">
                Ask about the code or this project. It can read, and pause a project; anything bigger is a proposal you
                confirm. Try “why did QA fail?” or “what is the Developer doing?”.
              </p>
            )}
            {turns.map((t, i) => (
              <div key={i} className={`turn ${t.role}`}>
                {t.role === "you" ? (
                  <div className="bubble-you">{t.text}</div>
                ) : (
                  <Card>
                    {t.steps.length > 0 && <div className="steps muted">{t.steps.map((s, k) => <div key={k}>{s}</div>)}</div>}
                    {t.html !== undefined ? (
                      <div className="markdown" dangerouslySetInnerHTML={{ __html: t.html }} />
                    ) : (
                      <div className="streaming">{t.text}{busy && i === turns.length - 1 && !t.error && !t.stopped ? "…" : ""}</div>
                    )}
                    {t.stopped && <div className="muted">Stopped</div>}
                    {t.error && <Banner>{t.error}</Banner>}
                    {t.proposals.map((p) => (
                      <ProposalCard key={p.id} proposal={p} onChange={(next) => updateProposal(i, next)} />
                    ))}
                  </Card>
                )}
              </div>
            ))}
          </div>

          <form
            className="chat-input"
            onSubmit={(e) => {
              e.preventDefault();
              void send();
            }}
          >
            <label className="field">
              Message
              <textarea
                value={input}
                rows={3}
                onChange={(e) => setInput(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" && !e.shiftKey) {
                    e.preventDefault(); // while it is answering, the draft waits: Stop it or wait
                    if (!busy) void send();
                  }
                }}
              />
            </label>
            {busy ? (
              <Button type="button" tone="destructive" onClick={stop}>Stop</Button>
            ) : (
              <Button type="submit" disabled={input.trim() === ""}>Send</Button>
            )}
          </form>
        </>
      )}
    </div>
  );
}
