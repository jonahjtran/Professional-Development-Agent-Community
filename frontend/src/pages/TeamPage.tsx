import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { Banner, Card, Loading, Segmented } from "../components/ui";
import { useLoad } from "../hooks";
import type { AgendaItem, AgentActivity } from "../types";

const FACE: Record<string, string> = {
  product_owner: "🧭",
  architect: "📐",
  developer: "💻",
  qa: "🔍",
  career_coach: "🎓",
};

const STATUS_WORD: Record<AgentActivity["status"], string> = {
  done: "done",
  working: "working",
  waiting_for_you: "waiting for you",
  paused: "paused",
  stopped: "stopped",
  upcoming: "upcoming",
};

const SOURCE_WORD = {
  running: "running now",
  used: "used last",
  configured: "will use",
} as const;

function Person({ agent, open, onOpen }: { agent: AgentActivity; open: boolean; onOpen: () => void }) {
  return (
    <button
      type="button"
      className={`person ${agent.status.replace("_", "-")} ${agent.status} ${open ? "open" : ""}`}
      aria-label={`${agent.name}, ${STATUS_WORD[agent.status]}`}
      aria-pressed={open}
      onClick={onOpen}
    >
      <span className="bubble">{agent.doing}</span>
      <span className="avatar" aria-hidden="true">{FACE[agent.stage] ?? "🙂"}</span>
      <span className="desk">
        <strong>{agent.name}</strong>
        <span className="muted">{STATUS_WORD[agent.status]}</span>
        {agent.model && (
          <span className="model">
            <code>{agent.model}</code>
            {agent.backend === "local" && <span className="pill waiting">local</span>}
            <span className="muted">{SOURCE_WORD[agent.model_source ?? "configured"]}</span>
          </span>
        )}
      </span>
    </button>
  );
}

function Notes({ agent }: { agent: AgentActivity }) {
  return (
    <section className="notes" aria-label={`${agent.name}'s notes`}>
      <h2>{agent.name}</h2>
      {agent.agenda.length > 0 && (
        <>
          <h3>Agenda</h3>
          <ul className="plain">
            {agent.agenda.map((t) => (
              <li key={t.id}>
                <strong>{t.id}</strong> {t.title} <span className="muted">· {agendaWord(t)}</span>
              </li>
            ))}
          </ul>
        </>
      )}
      <h3>Notes</h3>
      {agent.notes.length === 0 ? (
        <p className="muted">No notes yet.</p>
      ) : (
        <ul className="plain">{agent.notes.map((n, i) => <li key={i}>{n}</li>)}</ul>
      )}
      {agent.recent.length > 0 && (
        <>
          <h3>Latest steps</h3>
          <ul className="plain">{agent.recent.map((r, i) => <li key={i}>{r}</li>)}</ul>
        </>
      )}
    </section>
  );
}

function agendaWord(t: AgendaItem): string {
  if (t.state === "waiting") return `waiting on ${t.waiting_on.join(", ")}`;
  return t.state;
}

function Card_({ t }: { t: AgendaItem }) {
  const label = (
    <>
      <strong>{t.id}</strong> {t.title}
    </>
  );
  return (
    <li className={`task ${t.state}`}>
      {t.pr_url ? <a href={t.pr_url}>{label}</a> : label}
      {t.state === "waiting" && <div className="muted">waiting on {t.waiting_on.join(", ")}</div>}
    </li>
  );
}

function Board({ developer }: { developer: AgentActivity | undefined }) {
  const tasks = developer?.agenda ?? [];
  if (tasks.length === 0) return <p className="muted">No tasks yet. The Architect writes the task list first.</p>;
  const reviewing = developer?.status === "waiting_for_you";
  const columns: { name: string; items: AgendaItem[] }[] = [
    { name: "To do", items: tasks.filter((t) => t.state === "ready" || t.state === "waiting") },
    { name: "Working", items: tasks.filter((t) => t.state === "working" && !reviewing) },
    { name: "In review", items: tasks.filter((t) => t.state === "working" && reviewing) },
    { name: "Done", items: tasks.filter((t) => t.state === "done") },
  ];
  return (
    <div className="board">
      {columns.map((c) => (
        <section key={c.name} className="column" aria-label={c.name}>
          <h3>{c.name} <span className="muted">{c.items.length}</span></h3>
          <ul className="plain">{c.items.map((t) => <Card_ key={t.id} t={t} />)}</ul>
        </section>
      ))}
    </div>
  );
}

export function TeamPage() {
  const { slug = "" } = useParams();
  const { data, error } = useLoad<{ agents: AgentActivity[] }>(`/api/projects/${slug}/activity`, 3000);
  const [view, setView] = useState<"office" | "board">("office");
  const [openStage, setOpenStage] = useState<string | null>(null);

  if (error) return <Banner>{error.message}</Banner>;
  if (data === null) return <Loading what="the team" />;
  const opened = data.agents.find((a) => a.stage === openStage);

  return (
    <>
      <p className="back"><Link to={`/p/${slug}`}>‹ {slug}</Link></p>
      <h1>Team</h1>
      <p className="lede">What each agent is doing right now. Click a person for their notes and agenda.</p>
      <Segmented
        name="team-view"
        value={view}
        onChange={setView}
        options={[
          { value: "office", label: "Office" },
          { value: "board", label: "Board" },
        ]}
      />
      {view === "office" ? (
        <>
          <div className="office">
            {data.agents.map((a) => (
              <Person
                key={a.stage}
                agent={a}
                open={a.stage === openStage}
                onOpen={() => setOpenStage(a.stage === openStage ? null : a.stage)}
              />
            ))}
          </div>
          {opened && <Card><Notes agent={opened} /></Card>}
        </>
      ) : (
        <Board developer={data.agents.find((a) => a.stage === "developer")} />
      )}
    </>
  );
}
