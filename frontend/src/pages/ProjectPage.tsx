import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { ApiError, post } from "../api";
import {
  Banner,
  Button,
  Card,
  Group,
  Item,
  Loading,
  Meter,
  money,
  Section,
  CollapsibleSection,
  Segmented,
  Steps,
  StatusPill,
} from "../components/ui";
import { ProjectTasks } from "../components/ProjectTasks";
import { TodoRow } from "../components/TodoRow";
import { useLoad } from "../hooks";
import type { Project, StartingProject } from "../types";

export function ProjectPage() {
  const { slug = "" } = useParams();
  const { data, error, reload } = useLoad<Project | StartingProject>(`/api/projects/${slug}`, 3000);
  const [problem, setProblem] = useState<string | null>(null);
  const [sendTo, setSendTo] = useState("");
  const [sendNote, setSendNote] = useState("");
  const [sentNotice, setSentNotice] = useState<string | null>(null);
  const [raiseScope, setRaiseScope] = useState("project");
  const [raiseAmount, setRaiseAmount] = useState("");

  if (error) {
    return (
      <>
        <Link className="back" to="/">‹ Projects</Link>
        <Banner>{error.status === 404 ? "There is no such project." : error.message}</Banner>
      </>
    );
  }
  if (data === null) return <Loading what="the project" />;
  if (data.starting) {
    return (
      <>
        <h1>Starting {data.slug}…</h1>
        <p className="lede">
          This page updates until the run has saved its first state. If it stays here, the log
          below says why.
        </p>
        <pre>{data.log}</pre>
      </>
    );
  }

  const { summary, stages, warn_fraction: warn } = data;
  const active = summary.status === "running" || summary.status === "awaiting_human";

  async function act(action: "pause" | "resume") {
    if (
      action === "pause" &&
      !window.confirm(`Pause ${slug}? It stops using credits before its next step. Nothing is lost, and you can resume at any time.`)
    )
      return;
    setProblem(null);
    try {
      await post(`/api/projects/${slug}/${action}`);
      reload();
    } catch (e) {
      setProblem(e instanceof ApiError ? e.message : String(e));
    }
  }

  const reached = stages.filter((s) => s.state !== "upcoming" || s.visits > 0);

  async function sendBack() {
    setProblem(null);
    try {
      const sent = await post<{ label: string; reason: string }>(`/api/projects/${slug}/send-back`, {
        to: sendTo || "auto",
        comment: sendNote.trim(),
      });
      setSentNotice(`Sent to the ${sent.label}. ${sent.reason}`);
      setSendNote("");
      reload();
    } catch (e) {
      setProblem(e instanceof ApiError ? e.message : String(e));
    }
  }

  async function raiseCap() {
    const amount = Number(raiseAmount);
    if (raiseAmount.trim() === "" || !Number.isFinite(amount) || amount <= 0) {
      setProblem("Enter a positive number of dollars to add.");
      return;
    }
    setProblem(null);
    try {
      await post(`/api/projects/${slug}/raise-cap`, { scope: raiseScope, amount });
      setRaiseAmount("");
      reload();
    } catch (e) {
      setProblem(e instanceof ApiError ? e.message : String(e));
    }
  }

  async function setAutonomy(level: string) {
    setProblem(null);
    try {
      await post(`/api/projects/${slug}/autonomy`, { level });
      reload();
    } catch (e) {
      setProblem(e instanceof ApiError ? e.message : String(e));
    }
  }

  return (
    <>
      <Link className="back" to="/">‹ Projects</Link>
      <h1>{summary.slug} <StatusPill status={summary.status} /></h1>
      <p className="lede">{summary.idea}</p>

      {problem && <Banner>{problem}</Banner>}
      {sentNotice && <Banner>{sentNotice}</Banner>}
      {data.pending.map((q) => (
        <Banner key={q.id}>
          <Link to={`/p/${slug}/q/${q.id}`}><strong>{q.title}</strong></Link> — {q.detail}
        </Banner>
      ))}
      {summary.detail && data.pending.length === 0 && <Banner>{summary.detail}</Banner>}
      {data.todos.length > 0 && (
        <Section title="Action needed: only you can do these">
          <Group>
            {data.todos.map((t) => (
              <TodoRow key={t.id} todo={t} onChange={reload} />
            ))}
          </Group>
        </Section>
      )}
      {summary.status === "halted" && (
        <Banner>
          Paused. Nothing is using credits. <Button onClick={() => void act("resume")}>Resume</Button>{" "}
          <Link to={`/chat?project=${slug}&prompt=${encodeURIComponent(`Why is ${slug} paused, and what should I do next?`)}`}>
            Ask the assistant about this
          </Link>
        </Banner>
      )}
      {active && !summary.alive && (
        <Banner>
          The run is not active (it stopped, or it was never started from here).{" "}
          <Button onClick={() => void act("resume")}>Resume</Button>
          <div className="muted">
            or <code>studio resume {slug} --agent full --ui</code>
          </div>
        </Banner>
      )}

      {data.journey.going_back && (
        <Banner>
          Back at the <strong>{data.journey.going_back.label}</strong>: {data.journey.going_back.reason}
          {data.journey.going_back.came_from && <> (from {data.journey.going_back.came_from})</>}. The agents after it
          will run again.
        </Banner>
      )}
      <Steps stages={stages} />
      {data.journey.path.length > 0 && (
        <Section title="Journey">
          <ol className="journey" aria-label="Journey">
            {data.journey.path.map((j, i) => (
              <li key={`${j.stage}-${i}`} className={j.direction} title={j.reason}>
                {j.direction === "back" ? "↩ " : ""}
                {j.label}
              </li>
            ))}
          </ol>
        </Section>
      )}
      {active && (
        <Button onClick={() => void act("pause")}>Pause</Button>
      )}

      <Section title="Change something">
        {data.can_send_back ? (
          <Card>
            <label className="field">
              Send it back to
              <select value={sendTo} onChange={(e) => setSendTo(e.target.value)}>
                <option value="">Let the coordinator decide (recommended)</option>
                {reached.map((s) => (
                  <option key={s.stage} value={s.stage}>{s.label}</option>
                ))}
              </select>
            </label>
            <label className="field">
              What should change?
              <textarea value={sendNote} rows={3} onChange={(e) => setSendNote(e.target.value)} />
            </label>
            <Button disabled={sendNote.trim() === ""} onClick={() => void sendBack()}>Send back and continue</Button>
            <p className="hint">
              Works on a paused or finished project. Tell the coordinator what you want changed and it
              picks the right agent and says why; or choose an agent yourself. You review the result,
              and the agents after it run again. This spends credits.
            </p>
          </Card>
        ) : (
          <Card>
            <p className="muted">Pause the project first (or wait until it is done) to send it back to an agent.</p>
          </Card>
        )}
      </Section>

      <Section title="Approvals">
        <Card>
          <Segmented
            name="autonomy"
            value={data.autonomy}
            onChange={(level) => void setAutonomy(level)}
            options={[
              { value: "cautious", label: "Cautious" },
              { value: "standard", label: "Standard" },
              { value: "autonomous", label: "Autonomous" },
            ]}
          />
          <p className="hint">
            Cautious ignores your saved rules. Standard uses them. Autonomous also allows installing what the
            lockfile already lists. None of them can allow what is always refused.
          </p>
        </Card>
      </Section>

      <Section title="Tasks">
        <ProjectTasks slug={slug} />
      </Section>

      <Section title="Spend">
        <Card>
          <Meter label="Project" spent={summary.cost_usd} cap={summary.cap_usd} warn={warn} strong />
          {stages.map((s) => (
            <Meter key={s.stage} label={s.label} spent={s.spent} cap={s.cap} warn={warn} />
          ))}
          <p className="hint">The marker shows the {Math.round(warn * 100)}% warning point.</p>
          {summary.alive || summary.status === "running" ? (
            <p className="hint">Pause the project to raise a cap.</p>
          ) : (
            <>
              <label className="field">
                Raise the cap of
                <select value={raiseScope} onChange={(e) => setRaiseScope(e.target.value)}>
                  <option value="project">Project</option>
                  {stages.map((s) => (
                    <option key={s.stage} value={s.stage}>{s.label}</option>
                  ))}
                </select>
              </label>
              <label className="field">
                Add dollars
                <input type="text" inputMode="decimal" value={raiseAmount} onChange={(e) => setRaiseAmount(e.target.value)} />
              </label>
              <Button tone="secondary" onClick={() => void raiseCap()}>Raise cap</Button>
            </>
          )}
          {data.helper.explain_usd + data.helper.chat_usd > 0 && (
            <p className="hint">
              Helper calls (explanations and chat), not counted against the caps:{" "}
              {money(data.helper.explain_usd + data.helper.chat_usd, 3)}
            </p>
          )}
        </Card>
      </Section>

      {data.links.length > 0 && (
        <CollapsibleSection title="Pull requests">
          <Group>
            {data.links.map((l) => (
              <Item key={l.url} href={l.url}>
                <div className="head">{l.label}</div>
                <div className="meta">{l.url}</div>
              </Item>
            ))}
          </Group>
        </CollapsibleSection>
      )}

      <CollapsibleSection title="Your decisions">
        {data.gates.length === 0 ? (
          <Card><span className="muted">No decisions yet.</span></Card>
        ) : (
          <div className="table">
            <table>
              <thead>
                <tr><th>When</th><th>Stage</th><th>Decision</th><th>Comment</th></tr>
              </thead>
              <tbody>
                {data.gates.map((g) => (
                  <tr key={g.ts + g.stage + g.outcome}>
                    <td>{new Date(g.ts).toLocaleString([], { dateStyle: "short", timeStyle: "short" })}</td>
                    <td>{g.stage.replace("_", " ")}</td>
                    <td>{g.outcome.replace("_", " ")}</td>
                    <td>{g.comments}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </CollapsibleSection>

      <CollapsibleSection title="Documents">
        <Group>
          {data.artifacts.length === 0 && <Item><span className="muted">Nothing produced yet.</span></Item>}
          {data.artifacts.map((name) => (
            <Item key={name} to={`/p/${slug}/a/${name}`}>{name}</Item>
          ))}
        </Group>
      </CollapsibleSection>

      <Section title="Activity">
        <Group>
          <Item to={`/p/${slug}/team`}>
            <div className="head">Team</div>
            <div className="meta">Who is doing what right now, their notes and agenda, and a task board.</div>
          </Item>
          <Item to={`/p/${slug}/trace`}>
            <div className="head">Open the trace</div>
            <div className="meta">Every model call, tool call and decision.</div>
          </Item>
        </Group>
      </Section>
    </>
  );
}
