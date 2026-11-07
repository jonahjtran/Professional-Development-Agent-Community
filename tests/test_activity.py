import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from orchestrator.activity import humanize, project_activity
from orchestrator.artifacts import ArtifactStore, approve_artifacts, record_artifacts
from orchestrator.state import ProjectState, Stage, Status
from orchestrator.trace import EventType, TraceEvent, TraceWriter
from tests.factories import new_state
from tests.test_schemas import task_data

TASKS = {
    "tasks": [
        task_data(id="T1", title="Create the table"),
        task_data(id="T2", title="Add the API", depends_on=["T1"]),
        task_data(id="T3", title="Build the page", depends_on=["T2"]),
        task_data(id="T4", title="Write the README"),
    ]
}


def project(tmp_path: Path, **fields: Any) -> ProjectState:
    store = ArtifactStore(tmp_path, "demo")
    state = record_artifacts(
        new_state(), {"tasks.json": store.write("tasks.json", json.dumps(TASKS), revision=1)}
    )
    state = approve_artifacts(state, ["tasks.json"], now=datetime(2026, 11, 4, tzinfo=UTC))
    return state.evolve(**fields)


def by_stage(tmp_path: Path, state: ProjectState, alive: bool = True) -> dict[str, Any]:
    return {a.stage: a for a in project_activity(tmp_path, state, alive=alive)}


def trace(tmp_path: Path, stage: Stage, tool: str, summary: str, event: str = "tool_call") -> None:
    TraceWriter(tmp_path, "demo").write(
        TraceEvent(
            project="demo",
            stage=stage,
            event=EventType(event),
            agent=stage.value,
            details={"tool": tool, "summary": summary, "task_id": "T2"},
        )
    )


# --- who is doing what -----------------------------------------------------------------------


def test_finished_agents_are_done_and_later_ones_wait_for_the_one_before(tmp_path: Path) -> None:
    state = project(tmp_path, stage=Stage.DEVELOPER, current_task_id="T2", completed_tasks=["T1"])

    people = by_stage(tmp_path, state)

    assert people["product_owner"].status == "done"
    assert people["architect"].status == "done"
    assert people["developer"].status == "working"
    assert people["qa"].status == "upcoming" and people["qa"].waiting_on == "Developer"
    assert "Waiting for the Developer" in people["qa"].doing
    assert people["career_coach"].waiting_on == "QA"


def test_the_developer_is_described_by_its_task_and_latest_step(tmp_path: Path) -> None:
    trace(tmp_path, Stage.DEVELOPER, "Edit", "/home/x/worktrees/demo/T2/src/app/page.tsx")
    state = project(tmp_path, stage=Stage.DEVELOPER, current_task_id="T2", completed_tasks=["T1"])

    dev = by_stage(tmp_path, state)["developer"]

    assert dev.task == {"id": "T2", "title": "Add the API"}
    assert "Add the API" in dev.doing and "page.tsx" in dev.doing


def test_a_project_waiting_on_a_human_decision_shows_the_agent_as_waiting_for_you(
    tmp_path: Path,
) -> None:
    state = project(
        tmp_path,
        stage=Stage.DEVELOPER,
        status=Status.AWAITING_HUMAN,
        intervention={"kind": "gate", "detail": "Review the developer output"},
        completed_tasks=["T1"],
    )

    dev = by_stage(tmp_path, state)["developer"]

    assert dev.status == "waiting_for_you" and "Review the developer output" in dev.doing


def test_a_paused_project_shows_its_agent_as_paused(tmp_path: Path) -> None:
    state = project(tmp_path, stage=Stage.DEVELOPER, status=Status.HALTED)

    dev = by_stage(tmp_path, state)["developer"]

    assert dev.status == "paused" and "Paused" in dev.doing


def test_a_running_project_with_no_live_run_is_stopped(tmp_path: Path) -> None:
    state = project(tmp_path, stage=Stage.DEVELOPER)

    dev = by_stage(tmp_path, state, alive=False)["developer"]

    assert dev.status == "stopped" and "Resume" in dev.doing


def test_a_finished_project_has_everyone_done(tmp_path: Path) -> None:
    state = project(tmp_path, stage=Stage.CAREER_COACH, status=Status.DONE)

    assert {a.status for a in project_activity(tmp_path, state, alive=False)} == {"done"}


def test_the_agents_that_only_write_documents_say_what_they_are_writing(tmp_path: Path) -> None:
    state = project(tmp_path, stage=Stage.ARCHITECT)

    people = by_stage(tmp_path, state)

    assert people["architect"].status == "working" and "architecture" in people["architect"].doing
    assert people["product_owner"].doing.startswith("Finished")


# --- which model each agent is using ----------------------------------------------------------


def end(tmp_path: Path, stage: Stage, model: str | None) -> None:
    TraceWriter(tmp_path, "demo").write(
        TraceEvent(
            project="demo", stage=stage, event=EventType.AGENT_END, agent=stage.value, model=model
        )
    )


def test_a_finished_agent_shows_the_model_that_actually_answered(tmp_path: Path) -> None:
    end(tmp_path, Stage.PRODUCT_OWNER, "claude-opus-5-5-20261001")
    state = project(tmp_path, stage=Stage.DEVELOPER, current_task_id="T2")

    people = by_stage(tmp_path, state)

    assert people["product_owner"].model == "claude-opus-5-5-20261001"
    assert people["product_owner"].model_source == "used"


def test_an_agent_that_has_not_run_shows_the_configured_model(tmp_path: Path) -> None:
    state = project(tmp_path, stage=Stage.DEVELOPER, current_task_id="T2")

    people = {
        a.stage: a
        for a in project_activity(
            tmp_path,
            state,
            alive=True,
            configured={
                "qa": ("claude-haiku-5-5", "claude"),
                "developer": ("claude-sonnet-5-5", "claude"),
            },
        )
    }

    assert (people["qa"].model, people["qa"].model_source) == ("claude-haiku-5-5", "configured")
    assert people["qa"].backend == "claude"


def test_the_developer_shows_the_model_its_current_task_started_on(tmp_path: Path) -> None:
    from orchestrator.developer_backend import record_running_model

    end(tmp_path, Stage.DEVELOPER, "claude-sonnet-5-5")  # the previous task
    record_running_model(tmp_path, "demo", "T2", "local", "qwen3-coder:27b")
    state = project(tmp_path, stage=Stage.DEVELOPER, current_task_id="T2")

    dev = by_stage(tmp_path, state)["developer"]

    assert dev.model == "qwen3-coder:27b" and dev.backend == "local"
    assert dev.model_source == "running"


def test_a_recorded_model_for_another_task_is_not_shown_as_running(tmp_path: Path) -> None:
    from orchestrator.developer_backend import record_running_model

    record_running_model(tmp_path, "demo", "T1", "local", "qwen3-coder:27b")
    end(tmp_path, Stage.DEVELOPER, "claude-sonnet-5-5")
    state = project(tmp_path, stage=Stage.DEVELOPER, current_task_id="T2")

    dev = by_stage(tmp_path, state)["developer"]

    assert dev.model == "claude-sonnet-5-5" and dev.model_source == "used"


def test_a_model_name_that_is_not_the_local_model_counts_as_claude(tmp_path: Path) -> None:
    end(tmp_path, Stage.PRODUCT_OWNER, "claude-opus-5-5")
    state = project(tmp_path, stage=Stage.ARCHITECT)

    people = {
        a.stage: a
        for a in project_activity(tmp_path, state, alive=True, local_model="qwen3-coder:27b")
    }

    assert people["product_owner"].backend == "claude"


def test_a_model_name_that_is_the_local_model_counts_as_local(tmp_path: Path) -> None:
    end(tmp_path, Stage.DEVELOPER, "qwen3-coder:27b")
    state = project(tmp_path, stage=Stage.DEVELOPER, current_task_id="T2")

    people = {
        a.stage: a
        for a in project_activity(tmp_path, state, alive=True, local_model="qwen3-coder:27b")
    }

    assert people["developer"].backend == "local"


# --- the Developer's agenda -------------------------------------------------------------------


def test_the_agenda_shows_each_task_as_done_working_waiting_or_ready(tmp_path: Path) -> None:
    state = project(tmp_path, stage=Stage.DEVELOPER, current_task_id="T2", completed_tasks=["T1"])

    agenda = {t["id"]: t for t in by_stage(tmp_path, state)["developer"].agenda}

    assert agenda["T1"]["state"] == "done"
    assert agenda["T2"]["state"] == "working"
    assert agenda["T3"]["state"] == "waiting" and agenda["T3"]["waiting_on"] == ["T2"]
    assert agenda["T4"]["state"] == "ready"


def test_a_finished_task_on_the_agenda_links_to_its_pull_request(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path, "demo")
    report = {
        "task_id": "T1",
        "branch": "agent/T1",
        "pr_url": "https://x/pull/1",
        "summary": "Built it",
        "deviations_from_plan": [],
        "known_gaps": [],
    }
    state = record_artifacts(
        project(tmp_path, stage=Stage.DEVELOPER, current_task_id="T2", completed_tasks=["T1"]),
        {"dev/T1.json": store.write("dev/T1.json", json.dumps(report), revision=1)},
    )

    agenda = {t["id"]: t for t in by_stage(tmp_path, state)["developer"].agenda}

    assert agenda["T1"]["pr_url"] == "https://x/pull/1"
    assert agenda["T2"]["pr_url"] is None


# --- recent steps -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("tool", "summary", "expected"),
    [
        ("Edit", "/w/demo/T2/src/app/page.tsx", "Editing page.tsx"),
        ("Write", "/w/demo/T2/tests/a.test.ts", "Writing a.test.ts"),
        ("Read", "/w/demo/T2/package.json", "Reading package.json"),
        ("Bash", "npx vitest run tests/a.test.ts 2>&1 | tail -30", "Running tests"),
        ("Bash", "npm test", "Running tests"),
        ("Bash", "uv run pytest -q", "Running tests"),
        ("Bash", "npm run build", "Building the project"),
        ("Bash", "npx tsc --noEmit", "Checking types"),
        ("Bash", "npm run lint", "Checking style"),
        ("Bash", "git commit -q -m x", "Committing work"),
        ("Bash", "git status --short", "Checking what changed"),
        ("Bash", "npm install --no-audit", "Installing dependencies"),
        ("Bash", "ls -la src", "Looking around the project"),
        ("Bash", "somethingodd --flag", "Running a command"),
        ("Grep", "useState", "Searching the code"),
        ("Glob", "**/*.tsx", "Searching the code"),
    ],
)
def test_a_tool_call_is_put_in_plain_words(tool: str, summary: str, expected: str) -> None:
    assert humanize(tool, summary) == expected


def test_recent_steps_are_newest_first_and_capped(tmp_path: Path) -> None:
    for i in range(12):
        trace(tmp_path, Stage.DEVELOPER, "Edit", f"/w/demo/T2/src/file{i}.ts")
    state = project(tmp_path, stage=Stage.DEVELOPER, current_task_id="T2")

    recent = by_stage(tmp_path, state)["developer"].recent

    assert len(recent) == 8 and recent[0] == "Editing file11.ts"


def test_steps_of_other_agents_do_not_show_up_for_this_one(tmp_path: Path) -> None:
    trace(tmp_path, Stage.QA, "Bash", "pytest -q")
    state = project(tmp_path, stage=Stage.DEVELOPER, current_task_id="T2")

    assert by_stage(tmp_path, state)["developer"].recent == []


def test_a_refused_request_is_not_described_as_work_done(tmp_path: Path) -> None:
    trace(tmp_path, Stage.DEVELOPER, "Bash", "git push origin main", event="tool_denied")
    state = project(tmp_path, stage=Stage.DEVELOPER, current_task_id="T2")

    recent = by_stage(tmp_path, state)["developer"].recent

    assert recent == ["Was refused: git push origin main"]


# --- notes ------------------------------------------------------------------------------------


def test_notes_carry_what_the_developer_reported(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path, "demo")
    report = {
        "task_id": "T1",
        "branch": "agent/T1",
        "pr_url": "https://x/pull/1",
        "summary": "Built the table",
        "deviations_from_plan": ["Used SQLite"],
        "known_gaps": ["No index"],
        "needs_from_human": ["Add the Apollo key"],
    }
    state = record_artifacts(
        project(tmp_path, stage=Stage.DEVELOPER, current_task_id="T2", completed_tasks=["T1"]),
        {"dev/T1.json": store.write("dev/T1.json", json.dumps(report), revision=1)},
    )

    notes = by_stage(tmp_path, state)["developer"].notes

    assert "T1: Built the table" in notes
    assert "Deviation: Used SQLite" in notes
    assert "Gap: No index" in notes
    assert "Needs you: Add the Apollo key" in notes


def test_the_architects_notes_carry_its_qa_fix_plans(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path, "demo")
    plan = {
        "analysis": "A column is missing.",
        "tasks": [task_data(id="F1-1")],
        "needs_human": ["Add the key"],
    }
    state = record_artifacts(
        project(tmp_path, stage=Stage.QA),
        {"qa_fix_plan_1.json": store.write("qa_fix_plan_1.json", json.dumps(plan), revision=1)},
    )

    notes = by_stage(tmp_path, state)["architect"].notes

    assert "QA fix round 1: A column is missing. Tasks: F1-1." in notes
    assert "Needs you: Add the key" in notes


def test_a_damaged_artifact_is_skipped_not_fatal(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path, "demo")
    state = record_artifacts(
        project(tmp_path, stage=Stage.DEVELOPER, current_task_id="T2", completed_tasks=["T1"]),
        {"dev/T1.json": store.write("dev/T1.json", "{nope", revision=1)},
    )

    assert by_stage(tmp_path, state)["developer"].notes == []
