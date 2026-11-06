from pydantic import Field

from schemas._base import Schema, Text


class DevReport(Schema):
    """What the Developer agent reports at the end of a task, as JSON in its final message."""

    task_id: Text
    summary: Text
    deviations_from_plan: list[Text]
    known_gaps: list[Text]
    # Things only the human can do (a key to add, an account to set up); each becomes an item.
    needs_from_human: list[Text] = Field(default_factory=list)


class DevResult(Schema):
    """A finished task: the agent's report plus what the orchestrator knows about the branch."""

    task_id: Text
    branch: Text
    pr_url: str | None = None  # filled in when the orchestrator opens the pull request
    summary: Text
    deviations_from_plan: list[Text]
    known_gaps: list[Text]
    needs_from_human: list[Text] = Field(default_factory=list)
