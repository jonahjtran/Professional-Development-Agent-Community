from typing import Any

import pytest
from pydantic import ValidationError

from schemas import PRD, TaskList, unmapped_metrics
from tests.test_schemas import prd_data, task_data


def tasks(*items: dict[str, Any]) -> TaskList:
    return TaskList.model_validate({"tasks": list(items)})


def task(id_: str, *deps: str, criteria: str = "It works") -> dict[str, Any]:
    return task_data(id=id_, depends_on=list(deps), acceptance_criteria=[criteria])


# --- ids and references ----------------------------------------------------------------------


def test_task_ids_must_be_unique() -> None:
    with pytest.raises(ValidationError, match="duplicate"):
        tasks(task("T1"), task("T1"))


def test_a_dependency_must_name_an_existing_task() -> None:
    with pytest.raises(ValidationError, match="T9"):
        tasks(task("T1", "T9"))


def test_a_task_cannot_depend_on_itself() -> None:
    with pytest.raises(ValidationError, match="itself"):
        tasks(task("T1", "T1"))


# --- cycles and order ------------------------------------------------------------------------


def test_a_dependency_cycle_is_rejected_and_named() -> None:
    with pytest.raises(ValidationError, match="cycle") as error:
        tasks(task("A", "C"), task("B", "A"), task("C", "B"))

    assert "A" in str(error.value) and "B" in str(error.value) and "C" in str(error.value)


def test_a_two_task_cycle_is_a_cycle_not_an_ordering_problem() -> None:
    with pytest.raises(ValidationError, match="cycle"):
        tasks(task("A", "B"), task("B", "A"))


def test_dependencies_must_come_before_the_tasks_that_need_them() -> None:
    with pytest.raises(ValidationError, match="before"):
        tasks(task("T2", "T1"), task("T1"))


def test_a_correctly_ordered_chain_is_valid() -> None:
    result = tasks(task("T1"), task("T2", "T1"), task("T3", "T1", "T2"))

    assert [t.id for t in result.tasks] == ["T1", "T2", "T3"]


def test_a_diamond_of_dependencies_is_not_a_cycle() -> None:
    result = tasks(task("A"), task("B", "A"), task("C", "A"), task("D", "B", "C"))

    assert len(result.tasks) == 4


def test_independent_tasks_are_valid_in_any_order() -> None:
    assert len(tasks(task("B"), task("A")).tasks) == 2


# --- metric coverage -------------------------------------------------------------------------


def prd_with_metrics(*names: str) -> PRD:
    metrics = [{"name": n, "target": "x", "how_measured": "y"} for n in names]
    return PRD.model_validate(prd_data(success_metrics=metrics))


def test_a_metric_named_in_an_acceptance_criterion_is_covered() -> None:
    prd = prd_with_metrics("Weekly active officers")
    plan = tasks(task("T1", criteria="Reports weekly active officers on the dashboard"))

    assert unmapped_metrics(prd, plan) == []


def test_a_metric_no_task_mentions_is_reported_as_unmapped() -> None:
    prd = prd_with_metrics("Weekly active officers", "Event creation time")
    plan = tasks(task("T1", criteria="Reports weekly active officers"))

    assert unmapped_metrics(prd, plan) == ["Event creation time"]


def test_metric_matching_ignores_case_and_extra_spaces() -> None:
    prd = prd_with_metrics("Event  Creation Time")
    plan = tasks(task("T1", criteria="measure EVENT CREATION TIME in the UI"))

    assert unmapped_metrics(prd, plan) == []


def test_a_metric_can_be_covered_by_any_task() -> None:
    prd = prd_with_metrics("Latency")
    plan = tasks(task("T1"), task("T2", criteria="p95 latency under 200 ms"))

    assert unmapped_metrics(prd, plan) == []


def test_unmapped_metrics_are_reported_in_prd_order() -> None:
    prd = prd_with_metrics("Zeta", "Alpha")

    assert unmapped_metrics(prd, tasks(task("T1"))) == ["Zeta", "Alpha"]
