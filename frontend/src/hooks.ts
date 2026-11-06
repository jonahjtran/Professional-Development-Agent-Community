import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, get } from "./api";
import type { InboxItem } from "./types";

export interface Loaded<T> {
  data: T | null;
  error: ApiError | null;
  reload: () => void;
}

/** Load a URL, and keep it fresh by asking again every `every` milliseconds (0 = once). */
export function useLoad<T>(url: string, every = 0): Loaded<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const current = useRef(url);
  current.current = url;

  const load = useCallback(() => {
    get<T>(url)
      .then((value) => {
        if (current.current === url) {
          setData(value);
          setError(null);
        }
      })
      .catch((e: unknown) => {
        if (current.current === url) setError(e instanceof ApiError ? e : new ApiError(0, String(e)));
      });
  }, [url]);

  useEffect(() => {
    setData(null);
    setError(null);
    load();
    if (every <= 0) return undefined;
    const timer = setInterval(load, every);
    return () => clearInterval(timer);
  }, [load, every]);

  return { data, error, reload: load };
}

/** The open questions, refreshed often: it feeds the badge, the tab title and notifications. */
export function useInbox(every = 5000): InboxItem[] {
  const { data } = useLoad<InboxItem[]>("/api/inbox", every);
  const seen = useRef<Set<string> | null>(null);
  const items = data ?? [];

  useEffect(() => {
    if (data === null) return;
    const base = document.title.replace(/^\(\d+\) /, "");
    document.title = data.length ? `(${data.length}) ${base}` : base;
    const ids = new Set(data.map((i) => i.id));
    if (seen.current !== null && "Notification" in window && Notification.permission === "granted") {
      for (const item of data) {
        if (!seen.current.has(item.id)) {
          const note = new Notification("Agent Studio needs you", {
            body: `${item.project}: ${item.title}`,
          });
          note.onclick = () => {
            window.focus();
            window.location.href = `/p/${item.project}/q/${item.id}`;
          };
        }
      }
    }
    seen.current = ids;
  }, [data]);

  return items;
}
