import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, get } from "./api";
import type { InboxItem, TodoItem } from "./types";

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

function announce(title: string, body: string, url: string): void {
  if (!("Notification" in window) || Notification.permission !== "granted") return;
  const note = new Notification(title, { body });
  note.onclick = () => {
    window.focus();
    window.location.href = url;
  };
}

/**
 * Everything waiting for you, refreshed often: it feeds the nav badge, the tab title and browser
 * notifications. Questions are an ordinary alert; action items (only you can do them) are a
 * separate, louder one. What was already open when the page loaded is not announced again.
 */
export function useAttention(every = 5000): { questions: InboxItem[]; todos: TodoItem[] } {
  const { data: asked } = useLoad<InboxItem[]>("/api/inbox", every);
  const { data: todoData } = useLoad<{ todos: TodoItem[] }>("/api/todos", every);
  const seenQuestions = useRef<Set<string> | null>(null);
  const seenTodos = useRef<Set<string> | null>(null);
  const questions = asked ?? [];
  const todos = todoData?.todos ?? [];

  useEffect(() => {
    if (asked === null) return;
    if (seenQuestions.current !== null) {
      for (const item of asked) {
        if (!seenQuestions.current.has(item.id)) {
          announce("Agent Studio needs you", `${item.project}: ${item.title}`, `/p/${item.project}/q/${item.id}`);
        }
      }
    }
    seenQuestions.current = new Set(asked.map((i) => i.id));
  }, [asked]);

  useEffect(() => {
    if (todoData === null) return;
    if (seenTodos.current !== null) {
      for (const item of todoData.todos) {
        if (!seenTodos.current.has(item.id)) {
          announce("You need to do something", `${item.project ?? "Setup"}: ${item.title}`, "/inbox");
        }
      }
    }
    seenTodos.current = new Set(todoData.todos.map((i) => i.id));
  }, [todoData]);

  useEffect(() => {
    if (asked === null && todoData === null) return;
    const base = document.title.replace(/^\(\d+\) (Action needed · )?/, "");
    const total = questions.length + todos.length;
    if (total === 0) document.title = base;
    else document.title = todos.length > 0 ? `(${total}) Action needed · ${base}` : `(${total}) ${base}`;
  }, [asked, todoData, questions.length, todos.length]);

  return { questions, todos };
}
