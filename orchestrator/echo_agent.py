from orchestrator.pipeline import AgentRequest, AgentResult
from orchestrator.state import Stage


class EchoAgent:
    """A stand-in agent that makes no LLM calls: it echoes its input back as the "draft".

    It lets the whole pipeline (gates, budgets, loops, resume) be exercised for free.
    `qa_failures` is how many QA calls fail before QA starts passing.
    """

    def __init__(self, cost_usd: float = 0.10, qa_failures: int = 0) -> None:
        self._cost_usd = cost_usd
        self._qa_failures_left = qa_failures

    def run(self, request: AgentRequest) -> AgentResult:
        lines = [f"[echo] {request.stage.value} (revision {request.revision})", request.idea]
        if request.comments:
            lines.append(f"Addressing: {request.comments}")
        passed: bool | None = None
        if request.stage == Stage.QA:
            passed = self._qa_failures_left <= 0
            self._qa_failures_left -= 1
        return AgentResult(
            output="\n".join(lines), cost_usd=self._cost_usd, model="echo", passed=passed
        )
