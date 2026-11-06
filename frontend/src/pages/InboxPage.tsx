import { Card, Group, Item, Loading, Pill } from "../components/ui";
import { useLoad } from "../hooks";
import type { InboxItem } from "../types";

export function InboxPage() {
  const { data, error } = useLoad<InboxItem[]>("/api/inbox", 3000);
  return (
    <>
      <h1>Needs you</h1>
      <p className="lede">Every decision the pipeline is waiting for, oldest first.</p>
      {error && <Card><p>{error.message}</p></Card>}
      {data === null && !error && <Loading what="questions" />}
      {data !== null && data.length === 0 && <Card><p>Nothing is waiting for you.</p></Card>}
      {data !== null && data.length > 0 && (
        <Group>
          {data.map((q) => (
            <Item key={q.id} to={`/p/${q.project}/q/${q.id}`}>
              <div className="head">
                <span>{q.title}</span>
                <Pill>{q.project}</Pill>
              </div>
              <div className="meta">
                {q.detail} · {new Date(q.created).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}
              </div>
            </Item>
          ))}
        </Group>
      )}
    </>
  );
}
