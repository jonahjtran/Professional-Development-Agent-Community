from typing import Any

import pytest
from pydantic import ValidationError

from orchestrator.career_rules import numbers_in, plan_problems
from schemas import (
    CareerPlan,
    CareerTrack,
    Evidence,
    EvidenceSet,
    EvidenceType,
    ResumeAction,
    ResumeBullet,
    ResumeChange,
    ResumeData,
    WebsiteEntry,
)


def evidence() -> EvidenceSet:
    return EvidenceSet(
        items=[
            Evidence(id="qa", type=EvidenceType.QA, ref="r", description="9 of 9 checks passed"),
            Evidence(id="cost", type=EvidenceType.METRIC, ref="r", description="Spend was $4.20"),
            Evidence(id="pr-T1", type=EvidenceType.PR, ref="https://x/1", description="Task T1"),
            Evidence(
                id="target-1", type=EvidenceType.DOC, ref="d", description="Planned target 99%"
            ),
            Evidence(
                id="me", type=EvidenceType.HUMAN, ref="you", description="40 students used it"
            ),
        ]
    )


def resume(max_bullets: int = 2, bullets: int = 2) -> ResumeData:
    return ResumeData.model_validate(
        {
            "sections": [
                {
                    "name": "Projects",
                    "max_bullets": max_bullets,
                    "entries": [
                        {"title": "Old project", "bullets": [f"b{i}" for i in range(bullets)]}
                    ],
                }
            ]
        }
    )


def bullet(text: str = "Built a pipeline", ids: list[str] | None = None) -> ResumeBullet:
    return ResumeBullet(text=text, track=CareerTrack.AI_ENGINEERING, evidence_ids=ids or ["pr-T1"])


def change(
    action: ResumeAction = ResumeAction.ADD, *bullets: ResumeBullet, **fields: Any
) -> ResumeChange:
    values: dict[str, Any] = {
        "action": action,
        "section": "Projects",
        "entry": "Agent Studio",
        "bullets": list(bullets or [bullet()]),
        "reason": "It shows agent reliability.",
    }
    return ResumeChange(**{**values, **fields})


def plan(*changes: ResumeChange, website: WebsiteEntry | None = None) -> CareerPlan:
    return CareerPlan.model_validate(
        {
            "fit": [{"track": "ai_engineering", "decision": "include", "reason": "fits"}],
            "readme_md": "# R",
            "case_study_md": "# C",
            "resume_changes": [c.model_dump() for c in changes],
            "website_entry": website.model_dump() if website else None,
            "website_reason": "reason",
        }
    )


def entry(**fields: Any) -> WebsiteEntry:
    values: dict[str, Any] = {
        "slug": "agent-studio",
        "title": "Agent Studio",
        "hook": "A gated agent pipeline",
        "metric": "9 checks",
        "problem": "p",
        "approach": "a",
        "result": "r",
        "tags": ["Python"],
        "evidence_ids": ["qa"],
    }
    return WebsiteEntry(**{**values, **fields})


# --- reading numbers -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("raised fraud prevented from $45M to $85M", {"45", "85"}),
        ("served 20,000+ users, up 45%", {"20000", "45"}),
        ("a 3.5 second load", {"3.5"}),
        ("Built a layered API.", set()),
        ("ended a sentence with 12.", {"12"}),
    ],
)
def test_numbers_are_read_as_plain_digits(text: str, expected: set[str]) -> None:
    assert numbers_in(text) == expected


# --- the schemas -----------------------------------------------------------------------------


def test_a_bullet_without_evidence_is_rejected() -> None:
    with pytest.raises(ValidationError):
        ResumeBullet(text="Did a thing", track=CareerTrack.CONSULTING, evidence_ids=[])


def test_a_skipped_change_carries_no_bullets_and_other_actions_need_them() -> None:
    with pytest.raises(ValidationError, match="cannot carry bullets"):
        change(ResumeAction.SKIP, bullet())
    with pytest.raises(ValidationError, match="at least one bullet"):
        ResumeChange(action=ResumeAction.ADD, section="Projects", entry="X", bullets=[], reason="r")
    assert change(ResumeAction.SKIP, bullets=[]).action == ResumeAction.SKIP


def test_a_replace_change_must_say_what_it_replaces() -> None:
    with pytest.raises(ValidationError, match="what it replaces"):
        change(ResumeAction.REPLACE)
    assert change(ResumeAction.REPLACE, replaces="Old project").replaces == "Old project"


def test_duplicate_evidence_ids_are_rejected() -> None:
    item = Evidence(id="a", type=EvidenceType.PR, ref="r", description="d")
    with pytest.raises(ValidationError, match="duplicate evidence ids"):
        EvidenceSet(items=[item, item])


def test_a_website_slug_must_be_url_safe() -> None:
    with pytest.raises(ValidationError):
        entry(slug="Agent Studio!")


# --- the honesty rules -----------------------------------------------------------------------


def test_a_plan_whose_claims_are_backed_has_no_problems() -> None:
    backed = bullet("Passed 9 checks and spent $4.20", ["qa", "cost"])

    assert (
        plan_problems(plan(change(ResumeAction.ADD, backed, replaces="x")), evidence(), resume())
        == []
    )


def test_a_number_that_no_cited_evidence_contains_is_a_problem() -> None:
    claim = bullet("Cut costs by 60%", ["cost"])

    problems = plan_problems(
        plan(change(ResumeAction.ADD, claim, replaces="x")), evidence(), resume()
    )

    assert any("states 60" in p for p in problems)


def test_a_number_backed_only_by_a_document_target_does_not_count() -> None:
    claim = bullet("Reached 99% accuracy", ["target-1"])

    problems = plan_problems(
        plan(change(ResumeAction.ADD, claim, replaces="x")), evidence(), resume()
    )

    assert any("states 99" in p for p in problems)


def test_a_number_the_person_supplied_counts_as_evidence() -> None:
    claim = bullet("Used by 40 students", ["me"])

    assert (
        plan_problems(plan(change(ResumeAction.ADD, claim, replaces="x")), evidence(), resume())
        == []
    )


def test_citing_evidence_that_does_not_exist_is_a_problem() -> None:
    claim = bullet("Built a thing", ["pr-T9"])

    problems = plan_problems(
        plan(change(ResumeAction.ADD, claim, replaces="x")), evidence(), resume()
    )

    assert any("pr-T9" in p and "not in the evidence" in p for p in problems)


def test_a_number_cited_from_the_wrong_evidence_item_is_a_problem() -> None:
    claim = bullet("Spent $4.20", ["qa"])  # the 4.20 lives in the cost item, which is not cited

    assert plan_problems(plan(change(ResumeAction.ADD, claim, replaces="x")), evidence(), resume())


def test_adding_past_the_page_budget_without_a_replacement_is_a_problem() -> None:
    problems = plan_problems(plan(change(ResumeAction.ADD)), evidence(), resume(2, 2))

    assert any("page budget" in p for p in problems)


def test_adding_within_the_budget_or_with_a_replacement_is_fine() -> None:
    assert plan_problems(plan(change(ResumeAction.ADD)), evidence(), resume(3, 2)) == []
    assert (
        plan_problems(
            plan(change(ResumeAction.ADD, replaces="Old project")), evidence(), resume(2, 2)
        )
        == []
    )


def test_two_additions_together_can_pass_the_budget() -> None:
    two = plan(change(ResumeAction.ADD), change(ResumeAction.ADD, entry="Other"))

    assert any("page budget" in p for p in plan_problems(two, evidence(), resume(3, 2)))


def test_a_change_to_an_unknown_section_is_a_problem() -> None:
    problems = plan_problems(plan(change(section="Hobbies")), evidence(), resume(5, 0))

    assert any("unknown section" in p for p in problems)


def test_a_skip_is_always_acceptable() -> None:
    skip = change(ResumeAction.SKIP, bullets=[])

    assert plan_problems(plan(skip), evidence(), resume(1, 1)) == []


def test_the_website_entry_is_held_to_the_same_number_rule() -> None:
    ok = plan(website=entry(metric="9 checks"))
    bad = plan(website=entry(metric="300 users"))

    assert plan_problems(ok, evidence(), resume()) == []
    assert any("states 300" in p for p in plan_problems(bad, evidence(), resume()))
