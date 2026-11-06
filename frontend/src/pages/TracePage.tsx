import { Link, useParams, useSearchParams } from "react-router-dom";
import { Banner, Loading, money } from "../components/ui";
import { useLoad } from "../hooks";
import type { Trace } from "../types";

export function TracePage() {
  const { slug = "" } = useParams();
  const [params] = useSearchParams();
  const type = params.get("type");
  const query = type ? `?type=${encodeURIComponent(type)}` : "";
  const { data, error } = useLoad<Trace>(`/api/projects/${slug}/trace${query}`, 4000);
  return (
    <>
      <Link className="back" to={`/p/${slug}`}>‹ {slug}</Link>
      <h1>Trace</h1>
      {error && <Banner>{error.message}</Banner>}
      {data === null && !error && <Loading what="the trace" />}
      {data !== null && (
        <>
          <div className="filters">
            <Link to={`/p/${slug}/trace`} className={type ? "" : "on"}>all</Link>
            {data.types.map((t) => (
              <Link key={t} to={`/p/${slug}/trace?type=${t}`} className={type === t ? "on" : ""}>{t}</Link>
            ))}
          </div>
          <div className="table">
            <table>
              <thead>
                <tr>
                  <th>Time</th><th>Event</th><th>Stage</th><th>Agent</th><th>Model</th>
                  <th>Prompt</th><th>Tokens in/out</th><th>Cost</th><th>Time</th><th>Details</th>
                </tr>
              </thead>
              <tbody>
                {data.events.length === 0 && (
                  <tr><td colSpan={10} className="muted">No events yet.</td></tr>
                )}
                {data.events.map((e, i) => (
                  <tr key={`${e.ts}-${i}`}>
                    <td>{new Date(e.ts).toLocaleTimeString()}</td>
                    <td>{e.event}</td>
                    <td>{e.stage ?? ""}</td>
                    <td>{e.agent ?? ""}</td>
                    <td>{e.model ?? ""}</td>
                    <td>{e.prompt_version ?? ""}</td>
                    <td>{e.tokens_in !== null ? `${e.tokens_in} / ${e.tokens_out ?? 0}` : ""}</td>
                    <td>{e.cost_usd !== null ? money(e.cost_usd, 4) : ""}</td>
                    <td>{e.latency_ms !== null ? `${(e.latency_ms / 1000).toFixed(1)}s` : ""}</td>
                    <td><code>{Object.keys(e.details).length ? JSON.stringify(e.details) : ""}</code></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </>
  );
}
