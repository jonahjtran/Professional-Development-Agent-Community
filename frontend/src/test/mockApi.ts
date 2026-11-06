import { vi } from "vitest";
import { resetSessionForTests } from "../api";

type Reply = unknown | ((body: unknown) => unknown);
export interface Call { method: string; url: string; body: unknown; headers: Record<string, string> }

/** Stand in for the server: routes are "GET /api/x" or "POST /api/x", replies are JSON. */
export function mockApi(routes: Record<string, Reply | { status: number; body: unknown }>) {
  resetSessionForTests();
  const calls: Call[] = [];
  const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
    const method = init?.method ?? "GET";
    const body = init?.body ? JSON.parse(String(init.body)) : undefined;
    calls.push({ method, url, body, headers: (init?.headers ?? {}) as Record<string, string> });
    const key = `${method} ${url}`;
    const raw = key === "GET /api/session" ? { csrf: "tok" } : routes[key];
    if (raw === undefined) return new Response(JSON.stringify({ error: `no route ${key}` }), { status: 500 });
    const reply = typeof raw === "function" ? (raw as (b: unknown) => unknown)(body) : raw;
    if (reply && typeof reply === "object" && "stream" in reply) {
      const events = (reply as { stream: unknown[] }).stream;
      const text = events.map((e) => `data: ${JSON.stringify(e)}\n\n`).join("");
      return new Response(text, { status: 200, headers: { "content-type": "text/event-stream" } });
    }
    if (reply && typeof reply === "object" && "status" in reply && "body" in reply) {
      const r = reply as { status: number; body: unknown };
      return new Response(JSON.stringify(r.body), { status: r.status });
    }
    return new Response(JSON.stringify(reply), { status: 200 });
  });
  vi.stubGlobal("fetch", fetchMock);
  return { calls, posts: () => calls.filter((c) => c.method === "POST") };
}
