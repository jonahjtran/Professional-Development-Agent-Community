import { useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { ApiError, post } from "../api";
import { useChat } from "../components/ChatContext";
import { Workbench } from "../components/Workbench";
import { Banner, Button, Card, Group, Loading, Rendered, Section, Segmented, SwitchRow } from "../components/ui";
import { useLoad } from "../hooks";
import type {
  CapRequest,
  ExplanationReply,
  GateRequest,
  OpenQuestion,
  Question,
  RuleOptionsReply,
} from "../types";

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
  const chat = useChat();
  return (
    <>
      <p className="back"><Link to="/inbox">‹ Inbox</Link> · <Link to={`/p/${q.project}`}>{q.project}</Link></p>
      <h1>{q.title}</h1>
      <p className="hint">
        <Button
          tone="secondary"
          onClick={() =>
            chat.open({ prompt: "What is this asking me to decide, and what should I check before I do?", question: q.id })
          }
        >
          Ask the assistant about this
        </Button>
      </p>
      {q.refused && <Banner>Your last answer was refused: {q.refused}. Please answer again.</Banner>}
      {error && <Banner>{error}</Banner>}
      {q.kind === "gate" && <GateForm q={q} submit={submit} busy={busy} />}
      {q.kind === "cap" && <CapForm request={q.request as unknown as CapRequest} detail={q.detail} submit={submit} busy={busy} />}
      {q.kind === "merge" && <MergeForm urls={(q.request as { pr_urls: string[] }).pr_urls} submit={submit} busy={busy} />}
      {q.kind === "tool" && <ToolForm project={q.project} id={q.id} request={q.request as { tool: string; summary: string; reason: string }} submit={submit} busy={busy} />}
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

function Explanation({ project, id }: { project: string; id: string }) {
  const { data, error } = useLoad<ExplanationReply>(`/api/projects/${project}/questions/${id}/explanation`);
  if (error) return <p className="muted">No explanation available: {error.message}</p>;
  if (data === null) return <p className="muted">Working out what this does…</p>;
  if (data.explanation === null) return <p className="muted">{data.error ?? "No explanation available."}</p>;
  const e = data.explanation;
  return (
    <Card>
      <p><strong>{e.what}</strong> <span className={`pill risk-${e.risk}`}>{e.risk} risk</span></p>
      <p><span className="muted">Why it wants this:</span> {e.why}</p>
      <p><span className="muted">What it could change:</span> {e.effect}</p>
      {e.unsure && <p className="muted">The explainer is not sure about this one. Read the request itself.</p>}
      <p className="hint">Advice only, written by a model. You decide.</p>
    </Card>
  );
}

type Remember = "none" | "project" | "global";

function ToolForm({ project, id, request, submit, busy }: FormProps & { project: string; id: string; request: { tool: string; summary: string; reason: string } }) {
  const [message, setMessage] = useState("");
  const [remember, setRemember] = useState<Remember>("none");
  const [modes, setModes] = useState<string[]>([]);
  const { data: options } = useLoad<RuleOptionsReply>(`/api/projects/${project}/questions/${id}/rule-options`);
  const canRemember = options?.available === true;

  function allow() {
    const fields: Record<string, string> = { allow: "yes", message };
    if (canRemember && remember !== "none") {
      fields.remember = remember;
      fields.rule_modes = (options?.parts ?? []).map((_, i) => modes[i] ?? "exact").join(",");
    }
    void submit(fields);
  }

  return (
    <>
      <p>The Developer wants to use <strong>{request.tool}</strong>:</p>
      <pre>{request.summary}</pre>
      <Explanation project={project} id={id} />
      <p className="muted">Why you are being asked: {request.reason}</p>
      {options && !options.available && options.reason && <p className="hint">{options.reason}</p>}
      {canRemember && (
        <div className="choices">
          <span className="muted">Next time</span>
          <Segmented<Remember>
            name="remember"
            value={remember}
            onChange={setRemember}
            options={[
              { value: "none", label: "Allow once" },
              { value: "project", label: "Always in this project" },
              { value: "global", label: "Always everywhere" },
            ]}
          />
          {remember !== "none" &&
            options.parts.map((part, i) => (
              <label key={part.label} className="field">
                {part.label}
                <select value={modes[i] ?? "exact"} onChange={(e) => setModes((m) => Object.assign([...m], { [i]: e.target.value }))}>
                  <option value="exact">only exactly this</option>
                  {part.any_label && <option value="any">{part.any_label}</option>}
                </select>
              </label>
            ))}
          {remember !== "none" && (
            <p className="hint">Saved rules never override a refusal, and you can revoke them on the Rules page.</p>
          )}
        </div>
      )}
      <label className="field">
        If you deny, tell the agent why (optional)
        <input type="text" value={message} onChange={(e) => setMessage(e.target.value)} />
      </label>
      <div className="actions">
        <Button disabled={busy} onClick={allow}>{remember === "none" || !canRemember ? "Allow once" : "Allow and remember"}</Button>
        <Button tone="destructive" disabled={busy} onClick={() => void submit({ allow: "no", message })}>Deny</Button>
      </div>
    </>
  );
}
