"""The Architect's job after a failed QA report: decide what to change and plan the fix tasks.

This is one structured model call (the same machinery as the Architect itself). It never touches
code or the pipeline; it returns a `FixPlan`, and the orchestrator decides what to do with it.
"""

from pathlib import Path

from orchestrator.api_runner import ApiAgentSpec, ApiRunner, MessagesClient, ValidatingRunner
from orchestrator.config import Config
from orchestrator.fix_types import FixPlanner, FixRequest, FixResult, render_plan
from orchestrator.pipeline import AgentError, AgentRequest
from orchestrator.prompts import DEFAULT_PROMPTS_DIR, Prompt, load_prompt
from orchestrator.state import Stage
from schemas import FixPlan


class ApiFixPlanner:
    """A FixPlanner backed by the Messages API, on the Architect's model tier."""

    def __init__(
        self,
        config: Config,
        *,
        prompt: Prompt | None = None,
        prompts_dir: Path = DEFAULT_PROMPTS_DIR,
        client: MessagesClient | None = None,
    ) -> None:
        def runner_for(name: str, given: Prompt | None) -> ValidatingRunner:
            spec = ApiAgentSpec(
                stage=Stage.ARCHITECT,
                prompt=given or load_prompt(name, prompts_dir),
                tier="strong",
                schema=FixPlan,
                max_tokens=8000,
            )
            return ValidatingRunner(
                ApiRunner(config, [spec], client=client), {Stage.ARCHITECT: FixPlan}
            )

        self._runners = {
            "qa_failure": runner_for("qa_fix_planner", prompt),
            "design_problem": runner_for("architect_replan", None),
        }

    def plan(self, request: FixRequest) -> FixResult:
        if request.kind == "design_problem":
            inputs = {
                "developer_problem.md": request.problem,
                "architecture.md": request.architecture,
                "tasks.json": request.tasks_json,
            }
            comments = (
                f"This is replan round {request.round}. Number your tasks "
                f"R{request.round}-1, R{request.round}-2 and so on."
            )
        else:
            inputs = {
                "qa_report.json": request.report_json,
                "architecture.md": request.architecture,
                "tasks.json": request.tasks_json,
            }
            comments = (
                f"This is fix round {request.round}. Number your tasks "
                f"F{request.round}-1, F{request.round}-2 and so on."
            )
        result = self._runners.get(request.kind, self._runners["qa_failure"]).run(
            AgentRequest(
                stage=Stage.ARCHITECT,
                project=request.project,
                idea=request.idea,
                inputs=inputs,
                comments=comments,
            )
        )
        try:
            plan = FixPlan.model_validate_json(result.output)
        except ValueError as error:
            raise AgentError(
                f"the fix plan was not valid: {error}", cost_usd=result.cost_usd
            ) from error
        return FixResult(plan=plan, cost_usd=result.cost_usd, model=result.model)


__all__ = ["ApiFixPlanner", "FixPlanner", "FixRequest", "FixResult", "render_plan"]
