import { useState } from "react";
import type { Evidence, Workbench as WorkbenchData } from "../types";
import { Card, Pill, Rendered, Section } from "./ui";

function EvidenceLines({ evidence }: { evidence: Evidence[] }) {
  return (
    <>
      {evidence.map((e) => (
        <div className="evidence" key={e.id}>
          ↳ <code>{e.id}</code> ({e.type}): {e.description}
        </div>
      ))}
    </>
  );
}

function CopyButton({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <button
      type="button"
      className="copy"
      onClick={() => {
        void navigator.clipboard.writeText(text).then(() => {
          setCopied(true);
          setTimeout(() => setCopied(false), 1500);
        });
      }}
    >
      {copied ? "Copied" : "Copy"}
    </button>
  );
}

/** The career plan laid out for a decision: each claim beside the evidence behind it. */
export function Workbench({ wb }: { wb: WorkbenchData }) {
  return (
    <>
      <Section title="Fit with your goals">
        {wb.fit.map((f) => (
          <Card key={f.track}>
            <strong>{f.track.replace("_", " ")}</strong>{" "}
            <Pill tone={f.decision === "include" ? "done" : ""}>{f.decision}</Pill>
            <div>{f.reason}</div>
          </Card>
        ))}
      </Section>

      <Section title="Resume">
        {wb.changes.length === 0 && <p className="muted">No resume change is proposed.</p>}
        {wb.changes.map((c, n) => (
          <Card key={`${c.entry}-${n}`}>
            <strong>{c.action.toUpperCase()}: {c.entry}</strong>{" "}
            <span className="muted">({c.section})</span>
            <div>{c.reason}</div>
            {c.replaces && <div className="muted">Replaces: {c.replaces}</div>}
            {c.bullets.map((b) => (
              <div key={b.text} style={{ margin: "10px 0" }}>
                <div>• {b.text}</div>
                <EvidenceLines evidence={b.evidence} />
              </div>
            ))}
            {c.latex && (
              <details>
                <summary>LaTeX for Overleaf</summary>
                <pre className="latex">{c.latex}</pre>
                <CopyButton text={c.latex} />
              </details>
            )}
          </Card>
        ))}
      </Section>

      <Section title="Website">
        {wb.website ? (
          <Card>
            <div className="muted">{wb.website_reason}</div>
            <h3>{wb.website.title}</h3>
            <div>{wb.website.hook}</div>
            <div className="muted">
              {wb.website.metric}
              {wb.website.scope ? ` · ${wb.website.scope}` : ""}
            </div>
            <p><strong>Problem.</strong> {wb.website.problem}</p>
            <p><strong>Approach.</strong> {wb.website.approach}</p>
            <p><strong>Result.</strong> {wb.website.result}</p>
            <div>
              {wb.website.tags.map((t) => (<span key={t}><Pill>{t}</Pill>{" "}</span>))}
            </div>
            <EvidenceLines evidence={wb.website.evidence} />
            <p className="muted">
              Approving opens a pull request on your site. Merging it is what puts this live.
            </p>
          </Card>
        ) : (
          <Card>No website entry. {wb.website_reason}</Card>
        )}
      </Section>

      <Section title="README and case study">
        <details>
          <summary>README for the project repository</summary>
          <Rendered html={wb.readme_html} />
        </details>
        <details>
          <summary>Case study (docs/case_study.md)</summary>
          <Rendered html={wb.case_study_html} />
        </details>
      </Section>
    </>
  );
}
