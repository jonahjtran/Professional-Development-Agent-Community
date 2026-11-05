from orchestrator.config import Config
from orchestrator.state import ProjectState


def make_config() -> Config:
    return Config.model_validate(
        {
            "models": {"strong": "strong-model-id", "fast": "fast-model-id"},
            "budget": {
                "project_cap_usd": 20.0,
                "warn_at_fraction": 0.8,
                "stage_caps_usd": {
                    "product_owner": 1.5,
                    "architect": 2.5,
                    "developer": 10.0,
                    "qa": 2.5,
                    "career_coach": 2.0,
                },
            },
            "limits": {
                "max_revisions": 3,
                "max_qa_loops": 2,
                "max_web_searches": 5,
                "max_turns": {
                    "product_owner": 20,
                    "architect": 20,
                    "developer": 40,
                    "qa": 20,
                    "career_coach": 20,
                },
            },
            "pricing": {
                "strong": {"input": 4.0, "output": 20.0, "cache_read": 0.2, "cache_write": 5.0},
                "fast": {"input": 0.1, "output": 0.5, "cache_read": 0.01, "cache_write": 0.125},
                "web_search_usd_per_request": 0.01,
            },
        }
    )


def new_state() -> ProjectState:
    return ProjectState(project_slug="demo", idea="A tool that tracks student org events.")
