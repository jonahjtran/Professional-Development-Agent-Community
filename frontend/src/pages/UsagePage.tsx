import { useState } from "react";
import { Link } from "react-router-dom";
import { ApiError, post } from "../api";
import { Banner, Button, Card, Loading, Pill, Section } from "../components/ui";
import { useLoad } from "../hooks";
import type { TaskUsage, UsageReply, UsageWindow } from "../types";

const WINDOW_WORDS: Record<string, string> = {
  five_hour: "the 5-hour window",
  seven_day: "the weekly window",
  seven_day_opus: "the weekly Opus window",
  seven_day_sonnet: "the weekly Sonnet window",
};

const percent = (fraction: number): string => `${Math.round(fraction * 100)}%`;

function ago(seconds: number): string {
  if (seconds < 60) return "just now";
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes} minute${minutes === 1 ? "" : "s"} ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 48) return `${hours} hour${hours === 1 ? "" : "s"} ago`;
  return `${Math.round(hours / 24)} days ago`;
}

function resets(iso: string | null): string {
  if (iso === null) return "";
  return `resets ${new Date(iso).toLocaleString([], { weekday: "short", hour: "numeric", minute: "2-digit" })}`;
}

function state(window: UsageWindow): { text: string; tone: string } | null {
  if (window.status === "rejected") return { text: "Limit reached", tone: "halted" };
  if (window.status === "allowed_warning") return { text: "Close to the limit", tone: "waiting" };
  return null;
}

/** What a task or project used of each window, in the server's own whole percentages. */
function used(change: Record<string, number>): string[] {
  return Object.entries(change)
    .filter(([, fraction]) => Math.round(fraction * 100) > 0)
    .map(([name, fraction]) => `+${percent(fraction)} of ${WINDOW_WORDS[name] ?? name}`);
}

function Used({ change }: { change: Record<string, number> }) {
  const lines = used(change);
  if (lines.length === 0) return <span className="muted">no change measured</span>;
  return <>{lines.map((line) => <div key={line}>{line}</div>)}</>;
}

function tokens(t: TaskUsage["tokens"]): string {
  return `${t.input.toLocaleString()} in, ${t.output.toLocaleString()} out`;
}

export function UsagePage() {
  const { data, error, reload } = useLoad<UsageReply>("/api/usage", 15000);
  const [problem, setProblem] = useState<string | null>(null);
  const [checking, setChecking] = useState(false);
  const [fresh, setFresh] = useState<UsageReply["plan"]>(null);

  async function checkNow() {
    setProblem(null);
    setChecking(true);
    try {
      const reply = await post<{ plan: UsageReply["plan"] }>("/api/usage/refresh");
      setFresh(reply.plan);
      reload();
    } catch (e) {
      setProblem(e instanceof ApiError ? e.message : String(e));
    } finally {
      setChecking(false);
    }
  }

  if (error) return <Banner>{error.message}</Banner>;
  if (data === null) return <Loading what="the usage" />;
  // Show whichever reading is newer: the one just checked, or the last the server kept.
  const plan =
    fresh !== null && (data.plan === null || fresh.as_of >= data.plan.as_of) ? fresh : data.plan;

  return (
    <>
      <h1>Plan usage</h1>
      <p className="lede">
        How much of your Claude plan the Developer has used. These are the limits Claude Code itself
        reports (the same as <code>/usage</code> there), not estimates made here.
      </p>
      {problem && <Banner>{problem}</Banner>}

      <Section title="Your plan right now">
        <Card>
          {plan === null ? (
            <p className="muted">
              No plan usage has been measured yet. Run a Developer task with the subscription, or check now.
            </p>
          ) : (
            <>
              <ul className="usage-windows">
                {plan.windows.map((w) => {
                  const flag = state(w);
                  return (
                    <li key={w.name} className="usage-window">
                      <div className="usage-head">
                        <span>{w.label}</span>
                        <strong>{percent(w.utilization)}</strong>
                      </div>
                      <div className="meter" role="img" aria-label={`${w.label} ${percent(w.utilization)} used`}>
                        <div className="meter-fill" style={{ width: `${Math.min(100, w.utilization * 100)}%` }} />
                      </div>
                      <div className="meta">
                        {resets(w.resets_at)} {flag && <Pill tone={flag.tone}>{flag.text}</Pill>}
                      </div>
                    </li>
                  );
                })}
              </ul>
              {plan.overage.status && (
                <p className="hint">
                  Pay-as-you-go overage is {plan.overage.in_use ? "in use" : "not in use"} ({plan.overage.status}).
                </p>
              )}
              <p className="hint">Read {ago(plan.age_seconds)}, as reported by Anthropic.</p>
            </>
          )}
          <Button disabled={checking} onClick={() => void checkNow()}>Check now</Button>
          <p className="hint">Checking sends one tiny message from your plan.</p>
        </Card>
      </Section>

      {data.projects.map((project) => (
        <Section key={project.slug} title={project.slug}>
          <Card>
            <section>
              <p>
                <Link to={`/p/${project.slug}`}>{project.slug}</Link>: {project.totals.tasks} task
                {project.totals.tasks === 1 ? "" : "s"}, {tokens(project.totals.tokens)}
                {project.totals.tokens.cache_read > 0 && `, ${project.totals.tokens.cache_read.toLocaleString()} cached`}
              </p>
              <Used change={project.totals.change} />
            </section>
            <div className="table">
              <table>
                <thead>
                  <tr>
                    <th>Task</th>
                    <th>Input</th>
                    <th>Output</th>
                    <th>Cached</th>
                    <th>Used of your plan</th>
                  </tr>
                </thead>
                <tbody>
                  {project.tasks.map((t) => (
                    <tr key={`${t.task_id}-${t.ts}`}>
                      <td>
                        {t.task_id}
                        <div className="meta">{t.billing === "api" ? "API credits" : "subscription"}</div>
                      </td>
                      <td>{t.tokens.input.toLocaleString()}</td>
                      <td>{t.tokens.output.toLocaleString()}</td>
                      <td>{t.tokens.cache_read.toLocaleString()}</td>
                      <td><Used change={t.change} /></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Card>
        </Section>
      ))}

      <p className="hint">
        Anthropic reports whole percentages, so a small task may show no change; the token counts are exact.
        A task's share is measured from the reading before it to the one after, and a window that reset in
        between is left out. Dollar figures elsewhere in Studio are still estimates under a subscription.
      </p>
    </>
  );
}
