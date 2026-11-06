import { useState } from "react";
import { ApiError, post } from "../api";
import { Banner, Button, Group, Item, Loading, Section } from "../components/ui";
import { useLoad } from "../hooks";
import type { RuleView, RulesReply } from "../types";

function when(rule: RuleView): string {
  if (rule.uses === 0) return "never used";
  return `used ${rule.uses} time${rule.uses === 1 ? "" : "s"}, last ${new Date(rule.last_used ?? "").toLocaleString()}`;
}

export function RulesPage() {
  const { data, error, reload } = useLoad<RulesReply>("/api/rules");
  const [problem, setProblem] = useState<string | null>(null);

  async function revoke(body: { scope: "global" | "project"; project?: string; id: string }) {
    if (!window.confirm("Stop always allowing this? The Developer will ask again.")) return;
    setProblem(null);
    try {
      await post("/api/rules/revoke", body);
      reload();
    } catch (e) {
      setProblem(e instanceof ApiError ? e.message : String(e));
    }
  }

  if (error) return <Banner>{error.message}</Banner>;
  if (data === null) return <Loading what="the rules" />;
  const projects = Object.entries(data.projects).filter(([, rules]) => rules.length > 0);
  const none = data.global.length === 0 && projects.length === 0;

  return (
    <>
      <h1>Always allowed</h1>
      <p className="lede">Commands you chose to remember when approving. A rule never overrides a refusal.</p>
      {problem && <Banner>{problem}</Banner>}
      {none && <p className="muted">Nothing is always allowed yet. Choose “Always…” when approving a command.</p>}
      {data.global.length > 0 && (
        <Section title="Everywhere">
          <Group>
            {data.global.map((r) => (
              <Item key={r.id}>
                <div className="head"><code>{r.description}</code></div>
                <div className="meta">{when(r)}</div>
                <Button tone="destructive" onClick={() => void revoke({ scope: "global", id: r.id })}>Revoke</Button>
              </Item>
            ))}
          </Group>
        </Section>
      )}
      {projects.map(([slug, rules]) => (
        <Section key={slug} title={slug}>
          <Group>
            {rules.map((r) => (
              <Item key={r.id}>
                <div className="head"><code>{r.description}</code></div>
                <div className="meta">{when(r)}</div>
                <Button tone="destructive" onClick={() => void revoke({ scope: "project", project: slug, id: r.id })}>Revoke</Button>
              </Item>
            ))}
          </Group>
        </Section>
      ))}
    </>
  );
}
