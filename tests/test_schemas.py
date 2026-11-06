from typing import Any

import pytest
from pydantic import ValidationError

from schemas import ADR, PRD, Architecture, TaskList, Track


def prd_data(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "problem": "Student orgs track events in scattered spreadsheets.",
        "users": ["Org officers"],
        "track": "engineering",
        "goals": ["One place to plan events"],
        "success_metrics": [
            {"name": "Weekly active officers", "target": ">= 10", "how_measured": "login logs"}
        ],
        "scope": ["Create and list events"],
        "non_goals": ["Payments"],
        "risks": ["Low adoption"],
        "assumptions": ["Officers have laptops"],
    }
    return {**data, **overrides}


def architecture_data(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "components": [{"name": "API", "responsibility": "Serve events", "tech": "FastAPI"}],
        "data_flow": "Browser calls the API, which reads the database.",
        "interfaces": ["GET /events"],
        "tech_choices": [
            {
                "choice": "SQLite",
                "alternatives": ["Postgres"],
                "rationale": "Single user, no server to run",
            }
        ],
        "risks": ["SQLite will not scale past one writer"],
    }
    return {**data, **overrides}


def task_data(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": "T1",
        "title": "Create the events table",
        "description": "Add the table and a migration.",
        "acceptance_criteria": ["Table exists after migrate"],
        "depends_on": [],
        "size": "S",
    }
    return {**data, **overrides}


# --- PRD -------------------------------------------------------------------------------------


def test_a_complete_engineering_prd_is_valid() -> None:
    prd = PRD.model_validate(prd_data())

    assert prd.track == Track.ENGINEERING
    assert prd.success_metrics[0].name == "Weekly active officers"
    assert prd.stakeholders is None


@pytest.mark.parametrize(
    "field",
    ["problem", "users", "track", "goals", "success_metrics", "scope", "non_goals", "risks"],
)
def test_a_prd_missing_a_required_field_is_rejected(field: str) -> None:
    data = prd_data()
    del data[field]

    with pytest.raises(ValidationError, match=field):
        PRD.model_validate(data)


@pytest.mark.parametrize("field", ["users", "goals", "success_metrics", "scope", "non_goals"])
def test_a_prd_list_that_must_not_be_empty_is_rejected_when_empty(field: str) -> None:
    with pytest.raises(ValidationError, match=field):
        PRD.model_validate(prd_data(**{field: []}))


def test_a_blank_problem_is_rejected() -> None:
    with pytest.raises(ValidationError, match="problem"):
        PRD.model_validate(prd_data(problem="   "))


def test_a_success_metric_needs_a_target_and_a_way_to_measure_it() -> None:
    bad = {"name": "Adoption", "target": "", "how_measured": ""}

    with pytest.raises(ValidationError):
        PRD.model_validate(prd_data(success_metrics=[bad]))


def test_an_unknown_prd_field_is_rejected() -> None:
    with pytest.raises(ValidationError, match="surprise"):
        PRD.model_validate(prd_data(surprise="x"))


def test_an_unknown_track_is_rejected() -> None:
    with pytest.raises(ValidationError, match="track"):
        PRD.model_validate(prd_data(track="hobby"))


@pytest.mark.parametrize("track", ["consulting", "both"])
def test_consulting_tracks_need_stakeholders_and_adoption_risks(track: str) -> None:
    with pytest.raises(ValidationError, match="stakeholders"):
        PRD.model_validate(prd_data(track=track))


def test_a_consulting_prd_with_its_extra_fields_is_valid() -> None:
    prd = PRD.model_validate(
        prd_data(
            track="consulting",
            stakeholders=["Faculty advisor"],
            adoption_risks=["Officers keep using spreadsheets"],
        )
    )

    assert prd.stakeholders == ["Faculty advisor"]


def test_a_consulting_prd_with_empty_extra_fields_is_rejected() -> None:
    with pytest.raises(ValidationError, match="adoption_risks"):
        PRD.model_validate(prd_data(track="both", stakeholders=["Advisor"], adoption_risks=[]))


def test_an_engineering_prd_may_still_include_the_consulting_fields() -> None:
    prd = PRD.model_validate(prd_data(stakeholders=["Advisor"], adoption_risks=["Habit"]))

    assert prd.adoption_risks == ["Habit"]


def test_a_prd_survives_a_json_round_trip() -> None:
    prd = PRD.model_validate(prd_data())

    assert PRD.model_validate_json(prd.model_dump_json()) == prd


def test_a_prd_is_immutable() -> None:
    prd = PRD.model_validate(prd_data())

    with pytest.raises(ValidationError):
        prd.problem = "changed"


# --- Architecture and ADR ---------------------------------------------------------------------


def test_a_complete_architecture_is_valid() -> None:
    architecture = Architecture.model_validate(architecture_data())

    assert architecture.components[0].tech == "FastAPI"
    assert architecture.tech_choices[0].alternatives == ["Postgres"]


@pytest.mark.parametrize(
    "field", ["components", "data_flow", "interfaces", "tech_choices", "risks"]
)
def test_an_architecture_missing_a_required_field_is_rejected(field: str) -> None:
    data = architecture_data()
    del data[field]

    with pytest.raises(ValidationError, match=field):
        Architecture.model_validate(data)


def test_a_tech_choice_must_name_at_least_one_alternative() -> None:
    choice = {"choice": "SQLite", "alternatives": [], "rationale": "simple"}

    with pytest.raises(ValidationError, match="alternatives"):
        Architecture.model_validate(architecture_data(tech_choices=[choice]))


def test_an_architecture_needs_at_least_one_component() -> None:
    with pytest.raises(ValidationError, match="components"):
        Architecture.model_validate(architecture_data(components=[]))


def test_an_adr_has_a_numbered_file_name() -> None:
    adr = ADR(
        number=3,
        title="Use SQLite for storage",
        context="One user",
        decision="SQLite",
        consequences="No concurrent writers",
    )

    assert adr.filename == "003-use-sqlite-for-storage.md"


def test_an_adr_number_must_be_positive() -> None:
    with pytest.raises(ValidationError, match="number"):
        ADR(number=0, title="x", context="x", decision="x", consequences="x")


# --- TaskList --------------------------------------------------------------------------------


def test_a_task_list_with_one_task_is_valid() -> None:
    tasks = TaskList.model_validate({"tasks": [task_data()]})

    assert tasks.tasks[0].size == "S"


def test_a_task_list_must_not_be_empty() -> None:
    with pytest.raises(ValidationError, match="tasks"):
        TaskList.model_validate({"tasks": []})


def test_a_task_needs_acceptance_criteria() -> None:
    with pytest.raises(ValidationError, match="acceptance_criteria"):
        TaskList.model_validate({"tasks": [task_data(acceptance_criteria=[])]})


def test_a_task_size_must_be_small_medium_or_large() -> None:
    with pytest.raises(ValidationError, match="size"):
        TaskList.model_validate({"tasks": [task_data(size="XXL")]})


def test_a_task_needs_an_id() -> None:
    with pytest.raises(ValidationError, match="id"):
        TaskList.model_validate({"tasks": [task_data(id="")]})


def test_an_unknown_task_field_is_rejected() -> None:
    with pytest.raises(ValidationError, match="surprise"):
        TaskList.model_validate({"tasks": [task_data(surprise=1)]})


# --- shared ----------------------------------------------------------------------------------


@pytest.mark.parametrize("model", [PRD, Architecture, TaskList])
def test_every_schema_can_describe_itself_as_json_schema(model: Any) -> None:
    # The API call asks the model for output matching this schema.
    schema = model.model_json_schema()

    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False


def test_a_long_adr_title_gives_a_short_file_name() -> None:
    title = "Use RSVP updates allowed by re-entering the same email with a confirmation sent"
    adr = ADR(number=7, title=title, context="c", decision="d", consequences="e")

    assert adr.filename.startswith("007-use-rsvp-updates-allowed")
    assert adr.filename.endswith(".md")
    assert len(adr.filename) <= 4 + 50 + 3
    assert "--" not in adr.filename and "-.md" not in adr.filename


def test_a_short_adr_title_is_not_shortened() -> None:
    adr = ADR(number=1, title="Use SQLite", context="c", decision="d", consequences="e")

    assert adr.filename == "001-use-sqlite.md"


def test_a_cut_that_lands_on_a_hyphen_leaves_no_trailing_hyphen() -> None:
    title = "a" * 49 + " bbb"  # the 50th slug character is the hyphen between the words
    adr = ADR(number=1, title=title, context="c", decision="d", consequences="e")

    assert adr.filename == "001-" + "a" * 49 + ".md"


def test_a_developer_report_may_say_what_it_needs_from_the_human() -> None:
    from schemas import DevReport

    plain = DevReport(task_id="T1", summary="Did it", deviations_from_plan=[], known_gaps=[])
    asking = DevReport(
        task_id="T1",
        summary="Did it",
        deviations_from_plan=[],
        known_gaps=[],
        needs_from_human=["Add APOLLO_API_KEY to the Supabase project settings"],
    )

    assert plain.needs_from_human == []  # old reports without the field still load
    assert asking.needs_from_human == ["Add APOLLO_API_KEY to the Supabase project settings"]
