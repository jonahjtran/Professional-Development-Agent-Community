import { useState } from "react";
import { ApiError, post } from "../api";
import { Button, Item, Pill } from "./ui";
import type { TodoItem } from "../types";

const WHO: Record<TodoItem["source"], string> = {
  developer: "Developer needs you",
  error: "Stopped",
  qa: "QA needs a tool",
  system: "Setup",
};

/** One thing only you can do. System items clear themselves once the problem is fixed. */
export function TodoRow({ todo, onChange }: { todo: TodoItem; onChange: () => void }) {
  const [problem, setProblem] = useState<string | null>(null);

  async function done() {
    setProblem(null);
    try {
      await post(`/api/projects/${todo.project}/todos/${todo.id}/done`);
      onChange();
    } catch (e) {
      setProblem(e instanceof ApiError ? e.message : String(e));
    }
  }

  return (
    <Item>
      <div className="head">
        <span>{todo.title}</span>
        <Pill tone="waiting">{WHO[todo.source]}</Pill>
        {todo.project && <Pill>{todo.project}</Pill>}
      </div>
      <div className="meta">{todo.detail}</div>
      {problem && <div className="meta">{problem}</div>}
      {todo.can_mark_done && todo.project && <Button tone="secondary" onClick={() => void done()}>Mark done</Button>}
    </Item>
  );
}
