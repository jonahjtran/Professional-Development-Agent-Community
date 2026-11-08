"""The Architect's job when you ask for a task to be prioritized: decide the order.

One structured model call on the Architect's tier (the same machinery as the fix planner). It never
touches the task list itself; it returns a `TaskOrder`, which `task_control.apply_order` checks and
applies. The Architect owns priorities, so it may also keep the current order and say why.
"""

from pathlib import Path

from orchestrator.api_runner import ApiAgentSpec, ApiRunner, MessagesClient, ValidatingRunner
from orchestrator.config import Config
from orchestrator.pipeline import AgentError, AgentRequest
from orchestrator.priority_types import Prioritizer, PriorityRequest, PriorityResult
from orchestrator.prompts import DEFAULT_PROMPTS_DIR, Prompt, load_prompt
from orchestrator.state import Stage
from schemas import TaskOrder


class ApiPrioritizer:
    """A Prioritizer backed by the Messages API, on the Architect's model tier."""

    def __init__(
        self,
        config: Config,
        *,
        prompt: Prompt | None = None,
        prompts_dir: Path = DEFAULT_PROMPTS_DIR,
        client: MessagesClient | None = None,
    ) -> None:
        spec = ApiAgentSpec(
            stage=Stage.ARCHITECT,
            prompt=prompt or load_prompt("architect_prioritize", prompts_dir),
            tier="strong",
            schema=TaskOrder,
            max_tokens=4000,
        )
        self._runner = ValidatingRunner(
            ApiRunner(config, [spec], client=client), {Stage.ARCHITECT: TaskOrder}
        )

    def prioritize(self, request: PriorityRequest) -> PriorityResult:
        asked = "\n".join(
            f"- {item['task_id']}: {item.get('reason') or '(no reason given)'}"
            for item in request.asked
        )
        result = self._runner.run(
            AgentRequest(
                stage=Stage.ARCHITECT,
                project=request.project,
                idea=request.idea,
                inputs={
                    "tasks.json": request.tasks_json,
                    "architecture.md": request.architecture,
                },
                comments=f"The person asked for more priority on:\n{asked}",
            )
        )
        try:
            order = TaskOrder.model_validate_json(result.output)
        except ValueError as error:
            raise AgentError(
                f"the Architect's order was not valid: {error}", cost_usd=result.cost_usd
            ) from error
        return PriorityResult(order=order, cost_usd=result.cost_usd, model=result.model)


__all__ = ["ApiPrioritizer", "Prioritizer", "PriorityRequest", "PriorityResult"]
