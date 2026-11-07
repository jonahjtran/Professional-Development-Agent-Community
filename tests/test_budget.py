from pathlib import Path

import pytest

from orchestrator.budget import BudgetLevel, assess_budget, pause_for_budget, record_cost
from orchestrator.state import (
    Allowances,
    InterventionKind,
    ProjectState,
    Stage,
    Status,
    load_state,
    save_state,
)
from tests.factories import make_config, new_state


def state_with_spend(**spend_by_stage: float) -> ProjectState:
    return new_state().evolve(cost_usd={Stage(name): usd for name, usd in spend_by_stage.items()})


def test_a_new_project_has_spent_nothing() -> None:
    assert new_state().total_cost_usd == 0.0


def test_record_cost_adds_to_the_stage_and_the_total() -> None:
    state = record_cost(new_state(), Stage.PRODUCT_OWNER, 0.40)

    assert state.cost_usd[Stage.PRODUCT_OWNER] == pytest.approx(0.40)
    assert state.total_cost_usd == pytest.approx(0.40)


def test_record_cost_accumulates_across_calls_and_stages() -> None:
    state = record_cost(new_state(), Stage.PRODUCT_OWNER, 0.40)
    state = record_cost(state, Stage.PRODUCT_OWNER, 0.35)
    state = record_cost(state, Stage.ARCHITECT, 1.00)

    assert state.cost_usd[Stage.PRODUCT_OWNER] == pytest.approx(0.75)
    assert state.cost_usd[Stage.ARCHITECT] == pytest.approx(1.00)
    assert state.total_cost_usd == pytest.approx(1.75)


def test_record_cost_leaves_the_original_state_unchanged() -> None:
    original = new_state()

    record_cost(original, Stage.QA, 0.50)

    assert original.total_cost_usd == 0.0


def test_record_cost_rejects_a_negative_amount() -> None:
    with pytest.raises(ValueError, match="negative"):
        record_cost(new_state(), Stage.QA, -0.01)


def test_recorded_cost_survives_save_and_load(tmp_path: Path) -> None:
    state = record_cost(new_state(), Stage.ARCHITECT, 1.25)

    save_state(tmp_path, state)

    assert load_state(tmp_path, "demo").cost_usd == {Stage.ARCHITECT: 1.25}


def test_spend_well_under_the_caps_is_ok() -> None:
    state = state_with_spend(product_owner=0.5)

    assessment = assess_budget(state, make_config(), Stage.PRODUCT_OWNER)

    assert assessment.level == BudgetLevel.OK


def test_project_spend_just_under_the_warning_threshold_is_ok() -> None:
    state = state_with_spend(developer=15.99)

    assessment = assess_budget(state, make_config(), Stage.ARCHITECT)

    assert assessment.level == BudgetLevel.OK


def test_reaching_eighty_percent_of_the_project_cap_warns() -> None:
    state = state_with_spend(developer=16.0)

    assessment = assess_budget(state, make_config(), Stage.ARCHITECT)

    assert assessment.level == BudgetLevel.WARNING
    assert "$16.00" in assessment.detail
    assert "$20.00" in assessment.detail


def test_reaching_the_project_cap_is_exceeded() -> None:
    state = state_with_spend(
        developer=10.0, architect=2.5, product_owner=1.5, qa=2.5, career_coach=2.0
    )
    state = record_cost(state, Stage.DEVELOPER, 1.5)

    assessment = assess_budget(state, make_config(), Stage.CAREER_COACH)

    assert assessment.level == BudgetLevel.EXCEEDED
    assert "project" in assessment.detail.lower()
    assert "$20.00" in assessment.detail


def test_reaching_a_stage_cap_is_exceeded_even_when_the_project_is_under_budget() -> None:
    state = state_with_spend(product_owner=1.5)

    assessment = assess_budget(state, make_config(), Stage.PRODUCT_OWNER)

    assert assessment.level == BudgetLevel.EXCEEDED
    assert "product_owner" in assessment.detail
    assert "$1.50" in assessment.detail


def test_one_stage_being_at_its_cap_does_not_block_another_stage() -> None:
    state = state_with_spend(product_owner=1.5)

    assessment = assess_budget(state, make_config(), Stage.ARCHITECT)

    assert assessment.level == BudgetLevel.OK


def test_the_project_cap_is_reported_before_a_stage_cap() -> None:
    state = state_with_spend(developer=20.0)

    assessment = assess_budget(state, make_config(), Stage.DEVELOPER)

    assert assessment.level == BudgetLevel.EXCEEDED
    assert "project" in assessment.detail.lower()


def test_pausing_for_budget_waits_for_a_human_with_a_budget_cap_intervention() -> None:
    state = state_with_spend(product_owner=1.5)
    assessment = assess_budget(state, make_config(), Stage.PRODUCT_OWNER)

    paused = pause_for_budget(state, assessment)

    assert paused.status == Status.AWAITING_HUMAN
    assert paused.intervention is not None
    assert paused.intervention.kind == InterventionKind.BUDGET_CAP
    assert paused.intervention.detail == assessment.detail


def test_pausing_for_budget_keeps_the_rest_of_the_state() -> None:
    state = state_with_spend(product_owner=1.5).evolve(stage=Stage.PRODUCT_OWNER)
    assessment = assess_budget(state, make_config(), Stage.PRODUCT_OWNER)

    paused = pause_for_budget(state, assessment)

    assert paused.stage == Stage.PRODUCT_OWNER
    assert paused.cost_usd == state.cost_usd


def test_pausing_for_budget_when_nothing_is_wrong_is_an_error() -> None:
    state = new_state()
    assessment = assess_budget(state, make_config(), Stage.PRODUCT_OWNER)

    with pytest.raises(ValueError, match="within budget"):
        pause_for_budget(state, assessment)


def test_a_stage_with_no_dollar_cap_is_limited_only_by_the_project_cap() -> None:
    state = state_with_spend(product_owner=1.0)

    assessment = assess_budget(state, make_config(), Stage.INTAKE)

    assert assessment.level == BudgetLevel.OK


def test_extra_project_dollars_raise_the_project_cap() -> None:
    state = state_with_spend(developer=20.0)  # exactly the $20.00 project cap
    state = state.evolve(allowances=Allowances(extra_project_usd=20.0))  # cap is now $40.00

    assessment = assess_budget(state, make_config(), Stage.ARCHITECT)

    assert assessment.level == BudgetLevel.OK


def test_the_raised_project_cap_is_the_one_reported_when_it_is_reached() -> None:
    state = state_with_spend(developer=26.0)
    state = state.evolve(allowances=Allowances(extra_project_usd=5.0))

    assessment = assess_budget(state, make_config(), Stage.DEVELOPER)

    assert assessment.level == BudgetLevel.EXCEEDED
    assert "$25.00 project cap" in assessment.detail


def test_extra_dollars_for_a_stage_raise_only_that_stage_cap() -> None:
    state = state_with_spend(product_owner=1.5, architect=2.5)
    state = state.evolve(allowances=Allowances(extra_stage_usd={Stage.PRODUCT_OWNER: 1.0}))
    config = make_config()

    assert assess_budget(state, config, Stage.PRODUCT_OWNER).level == BudgetLevel.OK
    assert assess_budget(state, config, Stage.ARCHITECT).level == BudgetLevel.EXCEEDED


def test_the_raised_stage_cap_is_the_one_reported_when_it_is_reached() -> None:
    state = state_with_spend(product_owner=3.0)
    state = state.evolve(allowances=Allowances(extra_stage_usd={Stage.PRODUCT_OWNER: 1.0}))

    assessment = assess_budget(state, make_config(), Stage.PRODUCT_OWNER)

    assert assessment.level == BudgetLevel.EXCEEDED
    assert "$2.50 stage cap" in assessment.detail


def test_reaching_eighty_percent_of_a_stage_cap_warns() -> None:
    state = state_with_spend(product_owner=1.2)  # 80% of the $1.50 stage cap

    assessment = assess_budget(state, make_config(), Stage.PRODUCT_OWNER)

    assert assessment.level == BudgetLevel.WARNING
    assert "product_owner" in assessment.detail
    assert "$1.50" in assessment.detail


def test_stage_spend_just_under_eighty_percent_is_ok() -> None:
    state = state_with_spend(product_owner=1.19)

    assessment = assess_budget(state, make_config(), Stage.PRODUCT_OWNER)

    assert assessment.level == BudgetLevel.OK


def test_a_stage_warning_applies_only_to_that_stage() -> None:
    state = state_with_spend(product_owner=1.2)

    assessment = assess_budget(state, make_config(), Stage.ARCHITECT)

    assert assessment.level == BudgetLevel.OK


def test_the_stage_warning_uses_the_raised_stage_cap() -> None:
    allowances = Allowances(extra_stage_usd={Stage.PRODUCT_OWNER: 1.0})  # cap is now $2.50
    config = make_config()

    below = state_with_spend(product_owner=1.9).evolve(allowances=allowances)
    at_eighty = state_with_spend(product_owner=2.0).evolve(allowances=allowances)

    assert assess_budget(below, config, Stage.PRODUCT_OWNER).level == BudgetLevel.OK
    assert assess_budget(at_eighty, config, Stage.PRODUCT_OWNER).level == BudgetLevel.WARNING


def test_the_project_warning_uses_the_raised_project_cap() -> None:
    state = state_with_spend(developer=20.0).evolve(allowances=Allowances(extra_project_usd=5.0))

    assessment = assess_budget(state, make_config(), Stage.ARCHITECT)

    assert assessment.level == BudgetLevel.WARNING
    assert "$25.00" in assessment.detail


def test_project_and_stage_warnings_are_both_reported() -> None:
    state = state_with_spend(developer=14.0, qa=2.0)  # project $16.00 of $20.00; qa $2.00 of $2.50

    assessment = assess_budget(state, make_config(), Stage.QA)

    assert assessment.level == BudgetLevel.WARNING
    assert "Project spend" in assessment.detail
    assert "qa spend" in assessment.detail


def test_exceeding_a_cap_takes_priority_over_a_warning() -> None:
    state = state_with_spend(product_owner=1.5, developer=16.0)

    assessment = assess_budget(state, make_config(), Stage.PRODUCT_OWNER)

    assert assessment.level == BudgetLevel.EXCEEDED


def test_the_warning_is_raised_on_every_check_not_only_the_first() -> None:
    state = state_with_spend(developer=16.0)
    config = make_config()

    first = assess_budget(state, config, Stage.ARCHITECT)
    second = assess_budget(state, config, Stage.ARCHITECT)

    assert first.level == second.level == BudgetLevel.WARNING


def test_going_back_to_an_earlier_stage_warns_again() -> None:
    config = make_config()
    state = state_with_spend(developer=8.5)  # 85% of the developer stage cap
    assert assess_budget(state, config, Stage.DEVELOPER).level == BudgetLevel.WARNING

    state = record_cost(state, Stage.QA, 0.5)  # the work moves on to QA...
    assert assess_budget(state, config, Stage.QA).level == BudgetLevel.OK

    # ...and then loops back to the Developer, which must warn again.
    assert assess_budget(state, config, Stage.DEVELOPER).level == BudgetLevel.WARNING


# --- an approved warning stays approved ------------------------------------------------------


def _approved(state: ProjectState, stage: Stage) -> ProjectState:
    """The state after the human chose "continue" at the warning assess_budget gave."""
    from orchestrator.caps import CapAction, CapResolution, resolve_cap

    config = make_config()
    paused = pause_for_budget(state, assess_budget(state, config, stage))
    return resolve_cap(paused, config, CapResolution(action=CapAction.APPROVE_ANYWAY), stage=stage)


def test_a_warning_that_was_approved_is_not_raised_again_while_spend_grows() -> None:
    config = make_config()
    state = _approved(state_with_spend(developer=8.5), Stage.DEVELOPER)

    state = record_cost(state, Stage.DEVELOPER, 0.5)  # more spend, cap not reached

    assert assess_budget(state, config, Stage.DEVELOPER).level == BudgetLevel.OK


def test_an_approved_warning_stays_approved_when_the_work_returns_to_the_stage() -> None:
    config = make_config()
    state = _approved(state_with_spend(developer=8.5), Stage.DEVELOPER)

    assert assess_budget(state, config, Stage.QA).level == BudgetLevel.OK
    assert assess_budget(state, config, Stage.DEVELOPER).level == BudgetLevel.OK


def test_an_approved_warning_still_stops_at_the_cap() -> None:
    config = make_config()
    state = _approved(state_with_spend(developer=8.5), Stage.DEVELOPER)

    state = record_cost(state, Stage.DEVELOPER, 5.0)

    assert assess_budget(state, config, Stage.DEVELOPER).level == BudgetLevel.EXCEEDED


def test_approving_a_stage_warning_does_not_silence_the_project_warning() -> None:
    config = make_config()
    state = _approved(state_with_spend(developer=8.5), Stage.DEVELOPER)

    state = record_cost(state, Stage.ARCHITECT, 40.0)  # the project total reaches its warning

    assert assess_budget(state, config, Stage.ARCHITECT).level != BudgetLevel.OK


def test_raising_a_cap_warns_again_at_the_new_threshold() -> None:
    config = make_config()
    state = _approved(state_with_spend(developer=8.5), Stage.DEVELOPER)
    raised = state.evolve(
        allowances=Allowances(
            **{**state.allowances.model_dump(), "extra_stage_usd": {Stage.DEVELOPER: 10.0}}
        )
    )

    # 8.5 is far under 80% of the new $20 cap, then spend climbs to it.
    assert assess_budget(raised, config, Stage.DEVELOPER).level == BudgetLevel.OK
    later = record_cost(raised, Stage.DEVELOPER, 7.5)  # 16.0 = 80% of 20
    assert assess_budget(later, config, Stage.DEVELOPER).level == BudgetLevel.WARNING
