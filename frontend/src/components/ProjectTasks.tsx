import { useState } from "react";
import { ApiError, post } from "../api";
import { useLoad } from "../hooks";
import type { BoardProject, BoardTask, TaskState } from "../types";
import { Banner, Button, Pill } from "./ui";

const WORDS: Record<TaskState, string> = {
  working: "working now",
  in_review: "waiting for your review",
  ready: "next up",
  waiting: "waiting",
  done: "done",
};

function stateText(task: BoardTask): string {
  if (task.state === "waiting" && task.waiting_on.length > 0) return `waiting for ${task.waiting_on.join(", ")}`;
  return WORDS[task.state];
}

/**
 * One project's agent and Developer tasks, with Pause and Stop. A project runs one agent at a time,
 * so Pause pauses the project and Resume continues the same task. A stopped task is removed from
 * the list. The Architect owns the plan, so tasks cannot be added, edited or reordered here.
 */
export function ProjectTasks({ slug }: { slug: string }) {
  const { data, error, reload } = useLoad<BoardProject>(`/api/projects/${slug}/tasks`, 3000);
  const [problem, setProblem] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  async function run(url: string) {
    setProblem(null);
    try {
      await post(url);
      reload();
    } catch (e) {
      setProblem(e instanceof ApiError ? e.message : String(e));
    }
  }

  function pause(task: BoardTask | null) {
    const what = task ? `${task.id}: ${task.title}` : `${slug}`;
    if (!window.confirm(`Pause ${what}? It stops before its next step and a running agent stops. Nothing is lost, and you can resume at any time.`)) return;
    void run(`/api/projects/${slug}/pause`);
  }

  async function stop(task: BoardTask) {
    const running = task.state === "working" ? " The running agent stops and its unfinished work is discarded." : "";
    if (!window.confirm(`Stop ${task.id}: ${task.title}? It is removed from your task list.${running}`)) return;
    setProblem(null);
    try {
      const reply = await post<{ state: string }>(`/api/projects/${slug}/tasks/${task.id}/kill`);
      setNotice(reply.state === "stopping" ? `${task.id} will be removed when the run stops.` : `${task.id} was stopped and removed.`);
      reload();
    } catch (e) {
      setProblem(e instanceof ApiError ? e.message : String(e));
    }
  }

  async function stopGoal(title: string) {
    if (!window.confirm(`Stop “${title}”? It is removed, and the project goes back to where it was before. Tasks already planned for it stay in your list, each one stoppable.`)) return;
    setProblem(null);
    try {
      const reply = await post<{ state: string }>(`/api/projects/${slug}/goal/stop`);
      setNotice(reply.state === "stopping" ? `“${title}” will be removed when the run stops.` : `“${title}” was stopped and removed.`);
      reload();
    } catch (e) {
      setProblem(e instanceof ApiError ? e.message : String(e));
    }
  }

  if (error || data === null || (data.status === "done" && !data.goal)) return null;

  const open = data.tasks.filter((t) => t.state !== "done");
  const done = data.tasks.length - open.length;
  const running = open.find((t) => t.state === "working") ?? null;
  const control = (task: BoardTask | null) =>
    data.can_pause ? (
      <Button onClick={() => pause(task)}>Pause</Button>
    ) : data.can_resume ? (
      <Button onClick={() => void run(`/api/projects/${slug}/resume`)}>Resume</Button>
    ) : null;

  return (
    <>
      {problem && <Banner>{problem}</Banner>}
      {notice && <Banner>{notice}</Banner>}
      <div className="board-agent">
        <span>
          {data.agent.label} <Pill tone={data.agent.state === "running" ? "running" : "waiting"}>{data.agent.state}</Pill>
          {data.goal && <span className="muted"> · Working on: {data.goal.title}</span>}
        </span>
        <div className="actions">
          {running === null && control(null)}
          {data.goal?.can_stop && (
            <Button tone="destructive" onClick={() => void stopGoal(data.goal?.title ?? "")}>Stop</Button>
          )}
        </div>
      </div>
      {open.length > 0 && (
        <ul className="board-tasks">
          {open.map((task) => (
            <li key={task.id} className={`board-task ${task.state}`}>
              <div className="board-task-main">
                <div className="head">{task.title}</div>
                <div className="meta">
                  {task.id} · {stateText(task)}
                  {task.pr_url && (
                    <>
                      {" · "}
                      <a href={task.pr_url} rel="noopener noreferrer" target="_blank">pull request</a>
                    </>
                  )}
                </div>
                {!task.can_kill && task.kill_blocked && <div className="meta">Cannot stop: {task.kill_blocked}</div>}
              </div>
              <div className="actions">
                {task === running && control(task)}
                {task.can_kill && <Button tone="destructive" onClick={() => void stop(task)}>Stop</Button>}
              </div>
            </li>
          ))}
        </ul>
      )}
      {done > 0 && <p className="hint">{done} of {data.tasks.length} tasks done.</p>}
    </>
  );
}
