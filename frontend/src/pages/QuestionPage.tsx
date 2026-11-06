import { useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { ApiError, post } from "../api";
import { Workbench } from "../components/Workbench";
import { Banner, Button, Card, Loading, Rendered, Section, SwitchRow, Group } from "../components/ui";
import { useLoad } from "../hooks";
import type { CapRequest, GateRequest, OpenQuestion, Question } from "../types";

type Submit = (fields: Record<string, string>, items?: string[]) => Promise<void>;

function useSubmit(slug: string, id: string): { submit: Submit; error: string | null; busy: boolean } {
  const navigate = useNavigate();
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const submit: Submit = async (fields, items = []) => {
    setBusy(true);
    setError(null);
    try {
      await post(`/api/projects/${slug}/questions/${id}`, { fields, items });
      navigate("/inbox");
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
      setBusy(false);
    }
  };
  return { submit, error, busy };
}

export function QuestionPage() {
  const { slug = "", id = "" } = useParams();
  const { data, error } = useLoad<Question>(`/api/projects/${slug}/questions/${id}`);

  if (error) {
    return (
      <>
        <Banner>
          {error.status === 404 ? "This question is no longer open: it was answered, or the run moved on." : error.message}
        </Banner>
        <p><Link to="/inbox">‹ Inbox</Link></p>
      </>
    );
  }
  if (data === null) return <Loading what="the question" />;
  if (data.status === "answered") {
    return (
      <>
        <Banner>This question was already answered.</Banner>
        <p><Link to={`/p/${slug}`}>‹ {slug}</Link></p>
      </>
    );
  }
  return <Body q={data} />;
}

function Body({ q }: { q: OpenQuestion }) {
  const { submit, error, busy } = useSubmit(q.project, q.id);
  return (
    <>
      <p className="back"><Link to="/inbox">‹ Inbox</Link> · <Link to={`/p/${q.project}`}>{q.project}</Link></p>
      <h1>{q.title}</h1>
      {q.refused && <Banner>Your last answer was refused: {q.refused}. Please answer again.</Banner>}
      {error && <Banner>{error}</Banner>}
      {q.kind === "gate" && <GateForm q={q} submit={submit} busy={busy} />}
      {q.kind === "cap" && <CapForm request={q.request as unknown as CapRequest} detail={q.detail} submit={submit} busy={busy} />}
      {q.kind === "merge" && <MergeForm urls={(q.request as { pr_urls: string[] }).pr_urls} submit={submit} busy={busy} />}
      {q.kind === "tool" && <ToolForm request={q.request as { tool: string; summary: string; reason: string }} submit={submit} busy={busy} />}
    </>
  );
}

interface FormProps { submit: Submit; busy: boolean }

export function GateForm({ q, submit, busy }: FormProps & { q: OpenQuestion }) {
  const request = q.request as unknown as GateRequest;
  const [ticked, setTicked] = useState<Record<string, boolean>>(
    Object.fromEntries(request.items.map((i) => [i.id, true])),
  );
  const [answers, setAnswers] = useState<string[]>(request.questions.map(() => ""));
  const [comments, setComments] = useState("");
  const [halt, setHalt] = useState(false);

  function decide(outcome: "approve" | "request_changes" | "reject") {
    const fields: Record<string, string> = { outcome, comments };
    if (outcome === "reject" && halt) fields.halt = "on";
    answers.forEach((a, n) => {
      if (a.trim()) fields[`answer${n}`] = a;
    });
    void submit(fields, request.items.filter((i) => ticked[i.id]).map((i) => i.id));
  }

  return (
    <>
      <p className="muted">Spent so far ${request.cost_usd.toFixed(2)}</p>
      {q.workbench ? (
        <>
          <Workbench wb={q.workbench} />
          <details>
            <summary>The plain-text version the terminal shows</summary>
            <Rendered html={q.summary_html} />
          </details>
        </>
      ) : (
        <Rendered html={q.summary_html} />
      )}

      {request.questions.length > 0 && (
        <Section title="Questions for you">
          <p className="hint">
            Answer what you know and leave the rest blank. Your answers become evidence the next
            draft can use.
          </p>
          {request.questions.map((question, n) => (
            <label className="field" key={question}>
              {question}
              <input
                type="text"
                value={answers[n] ?? ""}
                onChange={(e) => setAnswers(answers.map((a, i) => (i === n ? e.target.value : a)))}
              />
            </label>
          ))}
        </Section>
      )}

      {request.items.length > 0 && (
        <Section title="What to approve">
          <p className="hint">Each switched-on item is carried out when you approve. Switch off anything you want skipped.</p>
          <Group>
            {request.items.map((item) => (
              <SwitchRow
                key={item.id}
                label={item.label}
                checked={ticked[item.id] ?? false}
                onChange={(on) => setTicked({ ...ticked, [item.id]: on })}
              />
            ))}
          </Group>
        </Section>
      )}

      <Section title="Your decision">
        <label className="field">
          Comments (for “request changes”, say what should change)
          <textarea rows={4} value={comments} onChange={(e) => setComments(e.target.value)} />
        </label>
        <div className="actions">
          <Button disabled={busy} onClick={() => decide("approve")}>Approve</Button>
          <Button tone="secondary" disabled={busy} onClick={() => decide("request_changes")}>Request changes</Button>
          <Button tone="destructive" disabled={busy} onClick={() => decide("reject")}>Reject</Button>
        </div>
        <Group>
          <SwitchRow label="With reject: pause the project instead of restarting this stage" checked={halt} onChange={setHalt} />
        </Group>
      </Section>
    </>
  );
}

function CapForm({ request, detail, submit, busy }: FormProps & { request: CapRequest; detail: string }) {
  const [amount, setAmount] = useState("");
  return (
    <>
      <Card>{detail}</Card>
      <p>Raising adds to the limit in config.yaml for this project only.</p>
      <div className="actions">
        {request.can_approve_anyway && (
          <Button disabled={busy} onClick={() => void submit({ action: "approve_anyway" })}>Approve anyway (this once)</Button>
        )}
        <Button tone="destructive" disabled={busy} onClick={() => void submit({ action: "abandon" })}>Stop here and pause the project</Button>
      </div>
      <label className="field">
        Raise by {request.unit === "revisions" ? "revisions" : "dollars"}
        <input type="text" value={amount} inputMode="decimal" onChange={(e) => setAmount(e.target.value)} />
      </label>
      <div className="actions">
        <Button tone="secondary" disabled={busy} onClick={() => void submit({ action: "raise", amount })}>Raise</Button>
      </div>
    </>
  );
}

function MergeForm({ urls, submit, busy }: FormProps & { urls: string[] }) {
  return (
    <>
      <p>QA checks the finished project, so each of these pull requests must be merged into main first.</p>
      <Card>
        {urls.map((url) => (
          <div key={url}><a href={url} rel="noopener noreferrer" target="_blank">{url}</a></div>
        ))}
      </Card>
      <div className="actions">
        <Button disabled={busy} onClick={() => void submit({ merged: "yes" })}>I merged them: check again</Button>
        <Button tone="destructive" disabled={busy} onClick={() => void submit({ merged: "no" })}>Give up for now and pause</Button>
      </div>
    </>
  );
}

function ToolForm({ request, submit, busy }: FormProps & { request: { tool: string; summary: string; reason: string } }) {
  const [message, setMessage] = useState("");
  return (
    <>
      <p>The Developer wants to use <strong>{request.tool}</strong>:</p>
      <pre>{request.summary}</pre>
      <p className="muted">Why you are being asked: {request.reason}</p>
      <label className="field">
        If you deny, tell the agent why (optional)
        <input type="text" value={message} onChange={(e) => setMessage(e.target.value)} />
      </label>
      <div className="actions">
        <Button disabled={busy} onClick={() => void submit({ allow: "yes", message })}>Allow once</Button>
        <Button tone="destructive" disabled={busy} onClick={() => void submit({ allow: "no", message })}>Deny</Button>
      </div>
    </>
  );
}
