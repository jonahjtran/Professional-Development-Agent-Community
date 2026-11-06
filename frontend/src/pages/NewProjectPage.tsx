import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { ApiError, post } from "../api";
import { Banner, Button, Card, Group, Section, Segmented, SwitchRow } from "../components/ui";
import type { NewProjectFields } from "../types";

const blank: NewProjectFields = {
  name: "",
  idea: "",
  track: "engineering",
  agent: "full",
  visibility: "private",
  public_ok: false,
};

interface Preview { clone: string; fields: NewProjectFields }

export function NewProjectPage() {
  const navigate = useNavigate();
  const [fields, setFields] = useState<NewProjectFields>(blank);
  const [problems, setProblems] = useState<string[]>([]);
  const [preview, setPreview] = useState<Preview | null>(null);
  const [busy, setBusy] = useState(false);
  const set = <K extends keyof NewProjectFields>(key: K, value: NewProjectFields[K]) =>
    setFields({ ...fields, [key]: value });

  async function review() {
    setBusy(true);
    setProblems([]);
    try {
      setPreview(await post<Preview>("/api/new/preview", fields));
    } catch (e) {
      setProblems(e instanceof ApiError ? (e.problems.length ? e.problems : [e.message]) : [String(e)]);
    } finally {
      setBusy(false);
    }
  }

  async function start() {
    setBusy(true);
    try {
      const done = await post<{ slug: string }>("/api/new/start", fields);
      navigate(`/p/${done.slug}`);
    } catch (e) {
      setPreview(null);
      setProblems(e instanceof ApiError ? (e.problems.length ? e.problems : [e.message]) : [String(e)]);
      setBusy(false);
    }
  }

  if (preview) {
    const f = preview.fields;
    return (
      <>
        <h1>Confirm</h1>
        <Card>
          <h3>{f.name}</h3>
          <p>{f.idea}</p>
          <p className="muted">Track: {f.track}</p>
        </Card>
        {f.agent === "full" ? (
          <Section title="This will, on your behalf">
            <Card>
              <ul>
                <li>create a <strong>{f.visibility}</strong> GitHub repository <code>{f.name}</code> on your account, using your own <code>gh</code> login;</li>
                <li>clone it to <code>{preview.clone}</code>;</li>
                <li>start the agents, which stop at a gate for your approval after every stage and open pull requests you merge yourself;</li>
                <li>spend real money on the Claude API, up to the project cap you configured.</li>
              </ul>
            </Card>
          </Section>
        ) : (
          <Section title="This is a practice run">
            <Card><p>It uses the free dummy agent. Nothing is created on GitHub and nothing is spent.</p></Card>
          </Section>
        )}
        <div className="actions">
          <Button disabled={busy} onClick={() => void start()}>Create and start</Button>
          <Button tone="secondary" onClick={() => setPreview(null)}>Back</Button>
        </div>
      </>
    );
  }

  return (
    <>
      <h1>New project</h1>
      {problems.map((p) => <Banner key={p}>{p}</Banner>)}
      <label className="field">
        Name (also the repository name)
        <input type="text" value={fields.name} placeholder="my-project" onChange={(e) => set("name", e.target.value)} />
      </label>
      <label className="field">
        The idea, in a paragraph
        <textarea rows={6} value={fields.idea} onChange={(e) => set("idea", e.target.value)} />
      </label>

      <div className="choices">
        <span className="muted">Track</span>
        <Segmented
          name="track"
          value={fields.track}
          onChange={(v) => set("track", v)}
          options={[
            { value: "engineering", label: "engineering" },
            { value: "consulting", label: "consulting" },
            { value: "both", label: "both" },
          ]}
        />
      </div>

      <div className="choices">
        <span className="muted">How to run it</span>
        <Segmented
          name="agent"
          value={fields.agent}
          onChange={(v) => set("agent", v)}
          options={[{ value: "full", label: "Full" }, { value: "echo", label: "Practice" }]}
        />
        <p className="hint">
          <strong>Full</strong>: real agents, a new GitHub repository and pull requests; it costs money, up to the
          project cap. <strong>Practice</strong>: the free dummy agent, and nothing is created anywhere.
        </p>
      </div>

      <div className="choices">
        <span className="muted">Repository visibility</span>
        <Segmented
          name="visibility"
          value={fields.visibility}
          onChange={(v) => set("visibility", v)}
          options={[{ value: "private", label: "Private" }, { value: "public", label: "Public" }]}
        />
        <div style={{ marginTop: 10 }}>
          <Group>
            <SwitchRow label="I want it public" checked={fields.public_ok} onChange={(on) => set("public_ok", on)} />
          </Group>
        </div>
      </div>

      <div className="actions">
        <Button disabled={busy} onClick={() => void review()}>Review</Button>
        <Link to="/">Cancel</Link>
      </div>
    </>
  );
}
