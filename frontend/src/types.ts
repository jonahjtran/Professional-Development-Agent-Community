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
  // "finished" is for now: the project may still go back to that agent. "done" is only for a
  // project that is complete.
  state: "finished" | "done" | "current" | "waiting" | "upcoming";
  spent: number;
  cap: number;
  fraction: number;
  visits: number;
  returned: boolean;
}

export interface JourneyStep {
  stage: string;
  label: string;
  direction: "forward" | "back";
  reason: string;
  ts: string;
}

export interface Journey {
  path: JourneyStep[];
  going_back: { stage: string; label: string; reason: string; came_from: string | null } | null;
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
  helper: { explain_usd: number; chat_usd: number };
  autonomy: "cautious" | "standard" | "autonomous";
  todos: TodoItem[];
  developer: {
    backend: "claude" | "local";
    source: "project" | "global" | "default";
    model: string;
    billing: Billing;
    billing_source: "project" | "global" | "default";
  };
  journey: Journey;
  can_send_back: boolean;
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
}

export interface ToolExplanation {
  what: string;
  why: string;
  effect: string;
  risk: "low" | "medium" | "high";
  unsure: boolean;
}

export interface ExplanationReply {
  explanation: ToolExplanation | null;
  error: string | null;
  cost_usd: number;
}

export interface RuleOptionsReply {
  available: boolean;
  reason: string | null;
  parts: { label: string; exact: string[]; any_label: string | null }[];
}

export interface RuleView {
  id: string;
  description: string;
  uses: number;
  last_used: string | null;
}

export interface RulesReply {
  global: RuleView[];
  projects: Record<string, RuleView[]>;
}

export interface TodoItem {
  id: string;
  project: string | null;
  title: string;
  detail: string;
  source: "developer" | "error" | "qa" | "system";
  created: string | null;
  can_mark_done: boolean;
}

export interface AgendaItem {
  id: string;
  title: string;
  state: "done" | "working" | "waiting" | "ready";
  waiting_on: string[];
  pr_url: string | null;
}

export interface AgentActivity {
  stage: string;
  name: string;
  status: "finished" | "done" | "working" | "waiting_for_you" | "paused" | "stopped" | "upcoming";
  visits: number;
  returned_because: string | null;
  doing: string;
  waiting_on: string | null;
  task: { id: string; title: string } | null;
  agenda: AgendaItem[];
  notes: string[];
  recent: string[];
  model: string | null;
  backend: "claude" | "local" | null;
  model_source: "running" | "used" | "configured" | null;
}

export interface ProposalView {
  id: string;
  project: string;
  action: string;
  summary: string;
  status: "pending" | "confirmed" | "dismissed" | "failed";
  result?: string | null;
}

export type ChatEvent =
  | { type: "session"; id: string }
  | { type: "step"; text: string }
  | { type: "text"; delta: string }
  | { type: "error"; message: string }
  | { type: "done"; html: string; cost_usd: number; session_cost_usd: number; proposals: ProposalView[] };

export interface ChatRepos {
  repos: { id: string; label: string }[];
  cap_usd: number;
}

export interface AgentModelRow {
  id: string;
  name: string;
  backend: "claude" | "local";
  model: string;
  note: string;
}

export interface LocalState {
  ok: boolean;
  reachable: boolean;
  installed: boolean;
  context: number | null;
  detail: string;
  fix: string;
}

export interface ModelsReply {
  developer: { default: "claude" | "local"; claude_model: string; local_model: string; endpoint: string };
  billing: {
    default: Billing;
    sign_in: { ok: boolean; detail: string; fix: string; skipped: boolean } | null;
  };
  agents: AgentModelRow[];
  local: LocalState;
}

export type TaskState = "working" | "in_review" | "ready" | "waiting" | "done";

export interface BoardTask {
  id: string;
  title: string;
  state: TaskState;
  waiting_on: string[];
  pr_url: string | null;
  can_kill: boolean;
  kill_blocked: string | null;
}

export interface BoardProject {
  slug: string;
  status: string;
  alive: boolean;
  can_pause: boolean;
  agent: { stage: string; label: string; state: "running" | "waiting" | "paused" };
  tasks: BoardTask[];
  cancelled: { id: string; title: string; ts: string }[];
  pending: number;
}

export interface TaskBoardReply {
  projects: BoardProject[];
}

/** Who pays for the Developer: Anthropic API credits, or your Claude subscription. */
export type Billing = "api" | "subscription";

export interface ChatSummary {
  id: string;
  title: string;
  repo: string;
  project: string | null;
  updated: string;
  turns: number;
  cost_usd: number;
}

export interface StoredChat {
  id: string;
  title: string;
  repo: string;
  tier: "strong" | "fast";
  project: string | null;
  cost_usd: number;
  turns: {
    role: "you" | "assistant";
    text: string;
    html?: string | null;
    steps?: string[];
    proposals?: ProposalView[];
    error?: string;
  }[];
}
