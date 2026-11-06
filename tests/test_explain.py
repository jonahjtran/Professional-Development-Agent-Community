import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import anthropic
import pytest

from orchestrator.assistant_cost import assistant_totals, record_assistant_cost
from orchestrator.explain import Explainer, ExplainInput
from tests.factories import make_config

GOOD = {
    "what": "Installs the project's npm dependencies.",
    "why": "The task needs its libraries before it can run tests.",
    "effect": "Writes node_modules inside the worktree.",
    "risk": "low",
    "unsure": False,
}


class FakeClient:
    def __init__(self, *outcomes: Any) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[dict[str, Any]] = []
        self.messages = self

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def reply(
    body: dict[str, Any] | str = GOOD, input_tokens: int = 1000, output_tokens: int = 200
) -> Any:
    text = body if isinstance(body, str) else json.dumps(body)
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        stop_reason="end_turn",
        model="fast-model-id",
        usage=SimpleNamespace(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
            server_tool_use=None,
        ),
    )


def item(**changes: str) -> ExplainInput:
    fields = {
        "project": "demo",
        "question_id": "abc123",
        "tool": "Bash",
        "summary": "npm install --no-audit",
        "reason": "npm install is not on the allowed list",
        "idea": "A coffee chat tracker",
        "stage": "developer",
    }
    return ExplainInput(**{**fields, **changes})


def explainer(tmp_path: Path, client: FakeClient) -> Explainer:
    return Explainer(make_config(), tmp_path, client=client)


def test_the_request_is_explained_by_the_fast_model(tmp_path: Path) -> None:
    client = FakeClient(reply())

    result = explainer(tmp_path, client).explain(item())

    assert result.error is None and result.explanation is not None
    assert result.explanation.risk == "low"
    assert client.calls[0]["model"] == "fast-model-id"
    sent = json.dumps(client.calls[0]["messages"])
    assert "npm install --no-audit" in sent and "coffee chat tracker" in sent


def test_the_cost_is_priced_and_written_to_the_assistant_ledger(tmp_path: Path) -> None:
    result = explainer(
        tmp_path, FakeClient(reply(input_tokens=1_000_000, output_tokens=0))
    ).explain(item())

    assert result.cost_usd == pytest.approx(0.1)  # fast tier: $0.10 per million input tokens
    assert assistant_totals(tmp_path, "demo")["explain_usd"] == pytest.approx(0.1)


def test_secrets_in_the_command_are_not_sent_to_the_model(tmp_path: Path) -> None:
    secret = "sk-ant-api03-" + "a" * 40
    token = "github_pat_" + "B" * 30
    client = FakeClient(reply())

    explainer(tmp_path, client).explain(
        item(summary=f"API_KEY=hunter2 curl -H 'x-api-key: {secret}' -u me:{token} https://x.test")
    )

    sent = json.dumps(client.calls[0]["messages"])
    assert secret not in sent and token not in sent and "hunter2" not in sent


def test_a_very_long_command_is_cut(tmp_path: Path) -> None:
    client = FakeClient(reply())

    explainer(tmp_path, client).explain(item(summary="echo " + "x" * 10_000))

    assert len(json.dumps(client.calls[0]["messages"])) < 4_000


def test_an_explanation_is_cached_so_the_same_question_costs_once(tmp_path: Path) -> None:
    client = FakeClient(reply())
    helper = explainer(tmp_path, client)

    first = helper.explain(item())
    second = helper.explain(item())

    assert len(client.calls) == 1
    assert second.explanation == first.explanation and second.cost_usd == 0.0


def test_a_failed_call_is_reported_and_not_cached(tmp_path: Path) -> None:
    client = FakeClient(anthropic.APIConnectionError(request=None), reply())  # type: ignore[arg-type]
    helper = explainer(tmp_path, client)

    failed = helper.explain(item())
    retried = helper.explain(item())

    assert failed.explanation is None and failed.error
    assert retried.explanation is not None


def test_an_answer_that_is_not_the_schema_is_an_error_not_a_crash(tmp_path: Path) -> None:
    result = explainer(tmp_path, FakeClient(reply('{"what": "x"}'))).explain(item())

    assert result.explanation is None and "unusable" in (result.error or "")


def test_ledger_totals_add_up_by_kind(tmp_path: Path) -> None:
    record_assistant_cost(tmp_path, "demo", "explain_usd", 0.01)
    record_assistant_cost(tmp_path, "demo", "explain_usd", 0.02)
    record_assistant_cost(tmp_path, "demo", "chat_usd", 0.5)

    totals = assistant_totals(tmp_path, "demo")

    assert totals["explain_usd"] == pytest.approx(0.03)
    assert totals["chat_usd"] == pytest.approx(0.5)
    assert assistant_totals(tmp_path, "nobody") == {"explain_usd": 0.0, "chat_usd": 0.0}
