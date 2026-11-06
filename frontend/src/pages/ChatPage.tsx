import { useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { ApiError, post, postStream } from "../api";
import { Banner, Button, Card, Loading, Segmented, money } from "../components/ui";
import { useLoad } from "../hooks";
import type { ChatEvent, ChatRepos, ProposalView } from "../types";

interface Turn {
  role: "you" | "assistant";
  text: string; // plain text while streaming, and for what you typed
  html?: string; // server-rendered Markdown, set when the answer is complete
  steps: string[];
  proposals: ProposalView[];
  error?: string;
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
        <div className="muted">{proposal.result}</div>
      )}
      {problem && <div className="muted">{problem}</div>}
    </div>
  );
}

export function ChatPage() {
  const [params] = useSearchParams();
  const { data: repos } = useLoad<ChatRepos>("/api/chat/repos");
  const [repo, setRepo] = useState("studio");
  const [tier, setTier] = useState<Tier>("strong");
  const [turns, setTurns] = useState<Turn[]>([]);
  const [input, setInput] = useState(params.get("prompt") ?? "");
  const [busy, setBusy] = useState(false);
  const [cost, setCost] = useState(0);
  const session = useRef<string | null>(null);
  const context = useRef<{ project: string; question_id: string } | null>(
    params.get("project") && params.get("question")
      ? { project: params.get("project") ?? "", question_id: params.get("question") ?? "" }
      : null,
  );

  function patchLast(change: (t: Turn) => Turn) {
    setTurns((all) => all.map((t, i) => (i === all.length - 1 ? change(t) : t)));
  }

  async function send() {
    const message = input.trim();
    if (!message || busy) return;
    setInput("");
    setBusy(true);
    setTurns((all) => [
      ...all,
      { role: "you", text: message, steps: [], proposals: [] },
      { role: "assistant", text: "", steps: [], proposals: [] },
    ]);
    const body = { message, repo, tier, session_id: session.current, context: session.current ? null : context.current };
    try {
      await postStream<ChatEvent>("/api/chat", body, (event) => {
        if (event.type === "session") session.current = event.id;
        else if (event.type === "step") patchLast((t) => ({ ...t, steps: [...t.steps, event.text] }));
        else if (event.type === "text") patchLast((t) => ({ ...t, text: t.text + event.delta }));
        else if (event.type === "error") patchLast((t) => ({ ...t, error: event.message }));
        else {
          setCost(event.session_cost_usd);
          patchLast((t) => ({ ...t, html: event.html, proposals: event.proposals }));
        }
      });
      context.current = null;
    } catch (e) {
      patchLast((t) => ({ ...t, error: e instanceof ApiError ? e.message : String(e) }));
    } finally {
      setBusy(false);
    }
  }

  function newChat() {
    session.current = null;
    setTurns([]);
    setCost(0);
  }

  function updateProposal(turn: number, next: ProposalView) {
    setTurns((all) => all.map((t, i) => (i === turn ? { ...t, proposals: t.proposals.map((p) => (p.id === next.id ? next : p)) } : t)));
  }

  if (repos === null) return <Loading what="the chat" />;

  return (
    <>
      <h1>Chat</h1>
      <p className="lede">
        Ask about the code or your projects. It can read, and pause a project. Anything bigger is a proposal you confirm.
      </p>
      <div className="chat-bar">
        <label className="field">
          Ask about
          <select value={repo} disabled={session.current !== null} onChange={(e) => setRepo(e.target.value)}>
            {repos.repos.map((r) => <option key={r.id} value={r.id}>{r.label}</option>)}
          </select>
        </label>
        <Segmented<Tier>
          name="tier"
          value={tier}
          onChange={setTier}
          options={[{ value: "strong", label: "Strong" }, { value: "fast", label: "Fast" }]}
        />
        <Button tone="secondary" onClick={newChat}>New chat</Button>
        <span className="muted">This chat: {money(cost)} of {money(repos.cap_usd)}</span>
      </div>

      <div className="chat-log" aria-live="polite">
        {turns.length === 0 && <p className="muted">Try “why did QA fail?”, “where is the budget check?” or “what is the Developer doing?”.</p>}
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
                  <div className="streaming">{t.text}{busy && i === turns.length - 1 && !t.error ? "…" : ""}</div>
                )}
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
                e.preventDefault();
                void send();
              }
            }}
          />
        </label>
        <Button type="submit" disabled={busy || input.trim() === ""}>Send</Button>
      </form>
    </>
  );
}
