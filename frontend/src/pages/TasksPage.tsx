import { useState } from "react";
import { Link } from "react-router-dom";
import { ApiError, post } from "../api";
import { Banner, Button, Loading, Pill } from "../components/ui";
import { useLoad } from "../hooks";
import type { BoardProject, BoardTask, TaskBoardReply, TaskState } from "../types";

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

function priorityText(priority: NonNullable<BoardTask["priority"]>): string {
  switch (priority.status) {
    case "asked":
      return "Waiting for the Architect to decide the priority.";
    case "moved":
      return `The Architect moved this up: ${priority.answer}`;
    case "kept":
      return `The Architect kept the order: ${priority.answer}`;
    default:
      return `Not applied: ${priority.answer}`;
  }
}

export function TasksPage() {
  const { data, error, reload } = useLoad<TaskBoardReply>("/api/tasks", 3000);
  const [problem, setProblem] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  async function run(url: string, then?: () => void) {
    setProblem(null);
    try {
      await post(url);
      then?.();
      reload();
    } catch (e) {
      setProblem(e instanceof ApiError ? e.message : String(e));
    }
  }

  function pause(project: BoardProject) {
    if (!window.confirm(`Pause ${project.slug}? It stops before its next step and a running agent stops. Nothing is lost, and you can resume at any time.`)) return;
    void run(`/api/projects/${project.slug}/pause`);
  }

  async function kill(project: BoardProject, task: BoardTask) {
    const stops = task.state === "working" ? " The running agent stops and its unfinished work is discarded." : "";
    if (!window.confirm(`Kill ${task.id}: ${task.title}? It is cancelled and removed from the task list.${stops}`)) return;
    setProblem(null);
    try {
      const reply = await post<{ state: string }>(`/api/projects/${project.slug}/tasks/${task.id}/kill`);
      setNotice(
        reply.state === "stopping"
          ? `${task.id} will be cancelled when the run stops.`
          : `${task.id} was cancelled.`,
      );
      reload();
    } catch (e) {
      setProblem(e instanceof ApiError ? e.message : String(e));
    }
  }

  async function prioritize(project: BoardProject, task: BoardTask) {
    const reason = window.prompt(
      `Ask the Architect to give ${task.id} more priority. Why does it matter now? (optional)\nThe Architect decides the order; it may keep it as it is.`,
      "",
    );
    if (reason === null) return;
    setProblem(null);
    try {
      await post(`/api/projects/${project.slug}/tasks/${task.id}/prioritize`, { reason: reason.trim() });
      setNotice(`Asked the Architect about ${task.id}. The Architect decides the order between tasks and tells you what it chose.`);
      reload();
    } catch (e) {
      setProblem(e instanceof ApiError ? e.message : String(e));
    }
  }

  if (error) return <Banner>{error.message}</Banner>;
  if (data === null) return <Loading what="the tasks" />;

  return (
    <>
      <h1>Tasks</h1>
      <p className="lede">
        What each active project is doing. Pause stops a project without losing anything. Kill cancels a task.
        Prioritize asks the Architect to consider moving a task up; the Architect decides the order, so you cannot reorder tasks yourself.
      </p>
      {problem && <Banner>{problem}</Banner>}
      {notice && <Banner>{notice}</Banner>}
      {data.projects.length === 0 && <p className="muted">No active projects. Finished projects are not listed.</p>}
      {data.projects.map((project) => (
        <section key={project.slug} className="board-project">
          <h2>
            <Link to={`/p/${project.slug}`}>{project.slug}</Link>
          </h2>
          <div className="board-agent">
            <span>
              {project.agent.label} <Pill tone={project.agent.state === "running" ? "running" : "waiting"}>{project.agent.state}</Pill>
            </span>
            {project.can_pause && <Button onClick={() => pause(project)}>Pause</Button>}
          </div>
          {project.tasks.length > 0 && (
            <ul className="board-tasks">
              {project.tasks
                .filter((t) => t.state !== "done")
                .map((task) => (
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
                      {!task.can_kill && task.kill_blocked && <div className="meta">Cannot kill: {task.kill_blocked}</div>}
                      {task.priority && <div className="meta">{priorityText(task.priority)}</div>}
                    </div>
                    {task.can_prioritize && (
                      <Button tone="secondary" onClick={() => void prioritize(project, task)}>Prioritize</Button>
                    )}
                    {task.can_kill && (
                      <Button tone="destructive" onClick={() => void kill(project, task)}>Kill</Button>
                    )}
                  </li>
                ))}
              {project.tasks.every((t) => t.state === "done") && <li className="muted">Every task is done.</li>}
            </ul>
          )}
          {project.tasks.filter((t) => t.state === "done").length > 0 && (
            <p className="hint">
              {project.tasks.filter((t) => t.state === "done").length} of {project.tasks.length} tasks done.
            </p>
          )}
          {project.cancelled.length > 0 && (
            <>
              <h3>Cancelled</h3>
              <ul className="board-tasks">
                {project.cancelled.map((c) => (
                  <li key={`${c.id}-${c.ts}`} className="board-task cancelled">
                    <div className="board-task-main">
                      <div className="head">{c.title}</div>
                      <div className="meta">{c.id} · cancelled {new Date(c.ts).toLocaleString([], { dateStyle: "short", timeStyle: "short" })}</div>
                    </div>
                  </li>
                ))}
              </ul>
            </>
          )}
        </section>
      ))}
    </>
  );
}
