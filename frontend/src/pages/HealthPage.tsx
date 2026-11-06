import { Banner, Card, Group, Item, Loading, Pill } from "../components/ui";
import { useLoad } from "../hooks";
import type { Health } from "../types";

const tone = { ok: "done", warn: "waiting", fail: "halted" } as const;

export function HealthPage() {
  const { data, error } = useLoad<Health>("/api/health");
  return (
    <>
      <h1>Health</h1>
      {error && <Card><p>{error.message}</p></Card>}
      {data === null && !error && <Loading what="the checks" />}
      {data !== null && (
        <>
          {data.failing > 0 ? (
            <Banner>{data.failing} thing(s) will stop a full run. Fix them first.</Banner>
          ) : (
            <p className="lede">Nothing will stop a full run.</p>
          )}
          <Group>
            {data.checks.map((c) => (
              <Item key={c.name}>
                <div className="head">
                  <Pill tone={tone[c.status]}>{c.status}</Pill>
                  <span>{c.name}</span>
                </div>
                <div className="meta">
                  {c.detail}
                  {c.fix && <> · <em>{c.fix}</em></>}
                </div>
              </Item>
            ))}
          </Group>
        </>
      )}
    </>
  );
}
