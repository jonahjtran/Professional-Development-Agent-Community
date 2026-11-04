import json
from pathlib import Path

from orchestrator.budget import cost_report_path, write_cost_report
from orchestrator.state import Stage
from tests.factories import new_state


def test_cost_report_lists_each_stage_and_the_total(tmp_path: Path) -> None:
    state = new_state().evolve(cost_usd={Stage.PRODUCT_OWNER: 0.5, Stage.ARCHITECT: 1.25})

    write_cost_report(tmp_path, state)

    report = json.loads(cost_report_path(tmp_path, "demo").read_text())
    assert report == {"stages": {"product_owner": 0.5, "architect": 1.25}, "total_usd": 1.75}


def test_cost_report_for_a_new_project_is_empty(tmp_path: Path) -> None:
    write_cost_report(tmp_path, new_state())

    report = json.loads(cost_report_path(tmp_path, "demo").read_text())
    assert report == {"stages": {}, "total_usd": 0.0}


def test_rewriting_the_cost_report_replaces_it(tmp_path: Path) -> None:
    write_cost_report(tmp_path, new_state().evolve(cost_usd={Stage.QA: 1.0}))
    write_cost_report(tmp_path, new_state().evolve(cost_usd={Stage.QA: 2.0}))

    report = json.loads(cost_report_path(tmp_path, "demo").read_text())
    assert report["total_usd"] == 2.0
