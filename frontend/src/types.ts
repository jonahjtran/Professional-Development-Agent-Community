// The shapes the server sends. They mirror orchestrator/ui/serialize.py.

export type Status = "running" | "awaiting_human" | "halted" | "done";

export interface Summary {
  slug: string;
  idea: string;
  stage: string;
  status: Status;
  detail: string;
  cost_usd: number;
  cap_usd: number;
  pending: number;
  alive: boolean;
}

export interface StageRow {
  stage: string;
  label: string;
  state: "done" | "current" | "waiting" | "upcoming";
  spent: number;
  cap: number;
  fraction: number;
}

export interface InboxItem {
  id: string;
  project: string;
  kind: "gate" | "cap" | "merge" | "tool";
  title: string;
  detail: string;
  created: string;
}

export interface Project {
  starting: false;
  summary: Summary;
  stages: StageRow[];
  warn_fraction: number;
  gates: { ts: string; stage: string; outcome: string; comments: string }[];
  links: { label: string; url: string }[];
  artifacts: string[];
  pending: InboxItem[];
}

export interface StartingProject {
  starting: true;
  slug: string;
  log: string;
}

export interface Artifact {
  name: string;
  revision: number;
  approved_at: string | null;
  kind: "markdown" | "code";
  html: string | null;
  text: string | null;
}

export interface TraceEvent {
  ts: string;
  event: string;
  stage: string | null;
  agent: string | null;
  model: string | null;
  prompt_version: string | null;
  tokens_in: number | null;
  tokens_out: number | null;
  cost_usd: number | null;
  latency_ms: number | null;
  details: Record<string, unknown>;
}

export interface Trace {
  types: string[];
  selected: string | null;
  events: TraceEvent[];
}

export interface Check {
  name: string;
  status: "ok" | "warn" | "fail";
  detail: string;
  fix: string;
}

export interface Health {
  checks: Check[];
  failing: number;
}

export interface Evidence {
  id: string;
  type: string;
  description: string;
}

export interface Workbench {
  fit: { track: string; decision: string; reason: string }[];
  changes: {
    action: string;
    section: string;
    entry: string;
    reason: string;
    replaces: string | null;
    bullets: { text: string; evidence: Evidence[] }[];
    latex: string;
  }[];
  website: {
    title: string;
    slug: string;
    hook: string;
    metric: string;
    scope: string | null;
    problem: string;
    approach: string;
    result: string;
    tags: string[];
    evidence: Evidence[];
  } | null;
  website_reason: string;
  readme_html: string;
  case_study_html: string;
}

export interface GateRequest {
  stage: string;
  title: string;
  summary: string;
  cost_usd: number;
  items: { id: string; label: string }[];
  questions: string[];
}

export interface CapRequest {
  kind: string;
  stage: string;
  detail: string;
  can_approve_anyway: boolean;
  unit: "usd" | "revisions";
}

export interface OpenQuestion {
  status: "open";
  id: string;
  project: string;
  kind: "gate" | "cap" | "merge" | "tool";
  title: string;
  detail: string;
  request: Record<string, unknown>;
  summary_html: string;
  refused: string | null;
  workbench: Workbench | null;
}

export type Question = OpenQuestion | { status: "answered"; id: string; project: string };

export interface NewProjectFields {
  name: string;
  idea: string;
  track: string;
  agent: string;
  visibility: string;
  public_ok: boolean;
}
