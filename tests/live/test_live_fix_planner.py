"""Live fix-planner test: the real Architect plans fixes for a seeded failing QA report.

Costs a few cents (one call on the strong tier) and needs ANTHROPIC_API_KEY. It checks what the
fakes cannot: that the prompt makes the model return a plan the pipeline accepts (valid ids,
dependencies and acceptance criteria that name the failure). Run it with:

    ANTHROPIC_API_KEY=... uv run pytest tests/live --live -s -k fix_planner
"""

from pathlib import Path

import pytest

from orchestrator.config import load_config
from orchestrator.fix_planner import ApiFixPlanner
from orchestrator.fix_types import FixRequest
from schemas import (
    CheckResult,
    CheckStatus,
    CriterionVerdict,
    QaReport,
    TaskList,
    Verdict,
)

pytestmark = pytest.mark.live

CONFIG = Path(__file__).parent.parent.parent / "config.yaml"

TASKS = {
    "tasks": [
        {
            "id": "T1",
            "title": "Greeting and doubling",
            "description": "Two small functions in app.py.",
            "acceptance_criteria": [
                "greet(name) returns 'Hello, <name>!'",
                "double(n) returns n multiplied by 2",
            ],
            "depends_on": [],
            "size": "S",
        }
    ]
}

PYTEST_OUTPUT = """\
FAILED tests/test_app.py::test_double - assert 9 == 6
    def double(n):
>       return n * 3
E   assert 9 == 6
1 failed, 1 passed in 0.03s
"""


def test_the_architect_plans_a_small_valid_fix_for_a_failing_test() -> None:
    report = QaReport(
        checks=[
            CheckResult(
                name="tests",
                command="uv run pytest",
                status=CheckStatus.FAILED,
                output_tail=PYTEST_OUTPUT,
            ),
            CheckResult(
                name="lint",
                command="uv run ruff check .",
                status=CheckStatus.PASSED,
                output_tail="",
            ),
        ],
        criteria=[
            CriterionVerdict(
                task_id="T1",
                criterion="greet(name) returns 'Hello, <name>!'",
                verdict=Verdict.PASS,
                evidence="tests/test_app.py::test_greet",
            ),
            CriterionVerdict(
                task_id="T1",
                criterion="double(n) returns n multiplied by 2",
                verdict=Verdict.FAIL,
                evidence="test_double: assert 9 == 6",
            ),
        ],
        summary="double() is wrong.",
        passed=False,
    )
    request = FixRequest(
        project="demo",
        idea="A tiny library with greet and double.",
        architecture="# Architecture\nA single module, app.py, tested with pytest.",
        tasks_json=TaskList.model_validate(TASKS).model_dump_json(indent=2),
        report_json=report.model_dump_json(indent=2),
        round=1,
    )

    result = ApiFixPlanner(load_config(CONFIG)).plan(request)

    plan = result.plan
    print(f"\nanalysis: {plan.analysis}\ntasks: {[(t.id, t.title) for t in plan.tasks]}")
    assert 1 <= len(plan.tasks) <= 3  # a one-line bug is not a project
    assert all(t.id.startswith("F1-") for t in plan.tasks)
    assert "double" in (plan.analysis + " ".join(t.description for t in plan.tasks)).lower()
    TaskList.model_validate(  # the pipeline would accept it next to the existing tasks
        {"tasks": [*TASKS["tasks"], *[t.model_dump() for t in plan.tasks]]}
    )
    assert plan.needs_human == []  # nothing here needs the person
    assert 0 < result.cost_usd < 0.5
