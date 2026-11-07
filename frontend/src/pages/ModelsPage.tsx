import { useState } from "react";
import { ApiError, post } from "../api";
import { Banner, Button, Card, Loading, Pill, Section, Segmented } from "../components/ui";
import { useLoad } from "../hooks";
import type { ModelsReply } from "../types";

type Backend = "claude" | "local";

export function ModelsPage() {
  const { data, error, reload } = useLoad<ModelsReply>("/api/models");
  const [problem, setProblem] = useState<string | null>(null);
  const [changed, setChanged] = useState(false);

  async function choose(backend: Backend) {
    setProblem(null);
    try {
      await post("/api/models/developer", { backend });
      setChanged(true);
      reload();
    } catch (e) {
      setProblem(e instanceof ApiError ? e.message : String(e));
    }
  }

  if (error) return <Banner>{error.message}</Banner>;
  if (data === null) return <Loading what="the models" />;
  const { developer, local } = data;

  return (
    <>
      <h1>Models</h1>
      <p className="lede">Which model each agent uses, by its exact id, and where the Developer runs.</p>
      {problem && <Banner>{problem}</Banner>}

      <Section title="Developer model, by default">
        <Card>
          <Segmented<Backend>
            name="default-backend"
            value={developer.default}
            onChange={(b) => void choose(b)}
            options={[
              { value: "claude", label: `Claude (${developer.claude_model})` },
              { value: "local", label: `Local (${developer.local_model})` },
            ]}
          />
          <p className="hint">
            A project can override this on its own page. A switch applies from the next task; a task already
            running finishes on the model it started with.
          </p>
          {changed && <p className="hint">Saved. It applies from the next task.</p>}
        </Card>
      </Section>

      <Section title="Local server (Ollama)">
        <Card>
          <p>
            <Pill tone={local.ok ? "done" : "halted"}>{local.ok ? "Ready" : "Not ready"}</Pill>{" "}
            <span className="muted">{developer.endpoint}</span>
          </p>
          <p>{local.detail}</p>
          {local.fix && <p className="hint">{local.fix}</p>}
          <Button tone="secondary" onClick={reload}>Check again</Button>
        </Card>
      </Section>

      <Section title="Which model each agent uses">
        <Card>
          <table className="models">
            <tbody>
              {data.agents.map((a) => (
                <tr key={a.id}>
                  <th scope="row">{a.name}</th>
                  <td><code>{a.model}</code></td>
                  <td><Pill tone={a.backend === "local" ? "waiting" : undefined}>{a.backend === "local" ? "Local" : "Claude"}</Pill></td>
                  <td className="muted">{a.note}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </Card>
      </Section>
    </>
  );
}
