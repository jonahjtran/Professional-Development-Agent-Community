import type { ButtonHTMLAttributes, ReactNode } from "react";
import { Link } from "react-router-dom";
import type { StageRow, Status } from "../types";

export const money = (usd: number, digits = 2): string => `$${usd.toFixed(digits)}`;

export function Pill({ tone, children }: { tone?: string; children: ReactNode }) {
  return <span className={`pill ${tone ?? ""}`}>{children}</span>;
}

export function statusTone(status: Status): string {
  return status === "awaiting_human" || status === "halted" ? "waiting" : status;
}

/** A stopped project is shown as paused: it can always be resumed. */
export function statusLabel(status: Status): string {
  return status === "halted" ? "paused" : status.replace("_", " ");
}

export function StatusPill({ status }: { status: Status }) {
  return <Pill tone={statusTone(status)}>{statusLabel(status)}</Pill>;
}

export function Group({ children }: { children: ReactNode }) {
  return <div className="group">{children}</div>;
}

/** One row of a grouped list. With `to` it is a link with a chevron; with `href` an external link. */
export function Item({
  to,
  href,
  children,
}: {
  to?: string;
  href?: string;
  children: ReactNode;
}) {
  if (to) return <Link className="item go" to={to}>{children}</Link>;
  if (href) {
    return (
      <a className="item go" href={href} rel="noopener noreferrer" target="_blank">
        {children}
      </a>
    );
  }
  return <div className="item">{children}</div>;
}

export function Banner({ children }: { children: ReactNode }) {
  return <div className="banner" role="status">{children}</div>;
}

export function Card({ children }: { children: ReactNode }) {
  return <div className="card">{children}</div>;
}

export function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <>
      <h2>{title}</h2>
      {children}
    </>
  );
}

export function Button({
  tone,
  ...rest
}: ButtonHTMLAttributes<HTMLButtonElement> & { tone?: "secondary" | "destructive" }) {
  return <button {...rest} className={`${tone ?? ""} ${rest.className ?? ""}`.trim()} />;
}

export function Meter({
  label,
  spent,
  cap,
  warn,
  strong,
}: {
  label: string;
  spent: number;
  cap: number;
  warn: number;
  strong?: boolean;
}) {
  const fraction = cap ? Math.min(spent / cap, 1) : 0;
  const level = fraction >= 1 ? "over" : fraction >= warn ? "warn" : "";
  return (
    <div className="meter">
      <div className="row">
        {strong ? <strong>{label}</strong> : <span>{label}</span>}
        <span className={strong ? "" : "muted"}>
          {money(spent)} of {money(cap)}
        </span>
      </div>
      <div className="bar-track" role="meter" aria-label={label} aria-valuenow={Math.round(fraction * 100)} aria-valuemin={0} aria-valuemax={100}>
        <span className={level} style={{ width: `${fraction * 100}%` }} />
        <i style={{ left: `${warn * 100}%` }} />
      </div>
    </div>
  );
}

export function Steps({ stages }: { stages: StageRow[] }) {
  return (
    <div className="steps" role="list">
      {stages.map((s) => (
        <div key={s.stage} className={`step ${s.state}`} role="listitem">
          {s.label}
          <small>{s.state}</small>
        </div>
      ))}
    </div>
  );
}

/** Choose one of a few, like Apple's segmented control. */
export function Segmented<T extends string>({
  name,
  value,
  options,
  onChange,
}: {
  name: string;
  value: T;
  options: { value: T; label: string }[];
  onChange: (value: T) => void;
}) {
  return (
    <div className="segmented" role="radiogroup">
      {options.map((o) => (
        <label key={o.value}>
          <input
            type="radio"
            name={name}
            value={o.value}
            checked={value === o.value}
            onChange={() => onChange(o.value)}
          />
          <span>{o.label}</span>
        </label>
      ))}
    </div>
  );
}

/** An on/off switch inside a grouped list row. */
export function SwitchRow({
  label,
  checked,
  onChange,
}: {
  label: string;
  checked: boolean;
  onChange: (checked: boolean) => void;
}) {
  return (
    <label className="switch-row">
      <span>{label}</span>
      <input type="checkbox" role="switch" checked={checked} onChange={(e) => onChange(e.target.checked)} />
    </label>
  );
}

/** HTML the server already escaped (Markdown with raw HTML turned into text). */
export function Rendered({ html }: { html: string }) {
  // eslint-disable-next-line react/no-danger -- the server escapes raw HTML in agent text
  return <div className="card md" dangerouslySetInnerHTML={{ __html: html }} />;
}

export function Loading({ what }: { what: string }) {
  return <p className="muted">Loading {what}…</p>;
}
