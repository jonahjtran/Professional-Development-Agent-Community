from enum import StrEnum
from typing import Annotated, Self

from pydantic import Field, model_validator

from schemas._base import Schema, Text


class Verdict(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    UNTESTED = "untested"


class CheckStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"  # the tool is not installed; never counts as a pass


class CriterionVerdict(Schema):
    """The QA agent's judgement of one acceptance criterion."""

    task_id: Text
    criterion: Text
    verdict: Verdict
    evidence: Text  # for a pass: the test that proves it, by name


class QaAssessment(Schema):
    """What the QA agent returns. It judges criteria only; the orchestrator owns the checks."""

    criteria: Annotated[list[CriterionVerdict], Field(min_length=1)]
    summary: Text


class CheckResult(Schema):
    """One real tool run by the orchestrator (pytest, ruff, ...)."""

    name: Text
    command: Text
    status: CheckStatus
    output_tail: str  # the last lines of output, with secrets removed


class QaReport(Schema):
    """The stored QA result. `passed` is computed by rule, never taken from the model."""

    checks: list[CheckResult]
    criteria: list[CriterionVerdict]
    summary: Text
    passed: bool

    @model_validator(mode="after")
    def _passed_follows_the_rule(self) -> Self:
        if self.passed != passes(self.checks, self.criteria):
            raise ValueError("passed must follow the rule: no failed or skipped check, no fail")
        return self


def passes(checks: list[CheckResult], criteria: list[CriterionVerdict]) -> bool:
    """Pass only if every check passed and no criterion failed. `untested` is shown, not failed."""
    return all(c.status == CheckStatus.PASSED for c in checks) and not any(
        c.verdict == Verdict.FAIL for c in criteria
    )
