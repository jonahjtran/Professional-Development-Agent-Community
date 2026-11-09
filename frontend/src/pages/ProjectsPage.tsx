import { useCallback, useState } from "react";
import { Card, Group, Item, Loading, Pill, StatusPill, money } from "../components/ui";
import { NewProjectDialog } from "../components/NewProjectDialog";
import { useLoad } from "../hooks";
import type { Summary } from "../types";

export function ProjectsPage() {
  const { data, error } = useLoad<Summary[]>("/api/projects", 4000);
  const [creating, setCreating] = useState(false);
  const close = useCallback(() => setCreating(false), []);
  return (
    <>
      <div className="title-row">
        <h1>Projects</h1>
        <button
          type="button"
          className="plus"
          aria-label="New project"
          aria-expanded={creating}
          onClick={() => setCreating(!creating)}
        >
          +
        </button>
      </div>
      {creating && <NewProjectDialog onClose={close} />}
      <p className="lede">Projects that need you are listed first.</p>
      {error && <Card><p>{error.message}</p></Card>}
      {data === null && !error && <Loading what="projects" />}
      {data !== null && data.length === 0 && (
        <Card>
          <p>No projects yet. <button type="button" className="link" onClick={() => setCreating(true)}>Start one</button>, or run <code>studio run</code>.</p>
        </Card>
      )}
      {data !== null && data.length > 0 && (
        <Group>
          {data.map((p) => (
            <Item key={p.slug} to={`/p/${p.slug}`}>
              <div className="head">
                <span>{p.slug}</span>
                <StatusPill status={p.status} />
                {p.pending > 0 && <Pill tone="waiting">{p.pending} waiting for you</Pill>}
                {p.status === "running" && !p.alive && <Pill tone="halted">not running</Pill>}
              </div>
              <div className="meta">
                {p.stage.replace("_", " ")} · {money(p.cost_usd)} of {money(p.cap_usd, 0)}
              </div>
              <div className="meta">{p.idea.length > 160 ? `${p.idea.slice(0, 160)}…` : p.idea}</div>
              {p.detail && <div>{p.detail}</div>}
            </Item>
          ))}
        </Group>
      )}
    </>
  );
}
