"""Which environment variables look like secrets, shared by everything that runs untrusted code."""

import os
import re

SECRET_ENV_NAME = re.compile(r"TOKEN|SECRET|PASSWORD|PASSWD|API_?KEY|CREDENTIAL|PRIVATE_?KEY", re.I)


def hidden_env() -> dict[str, str]:
    """Environment overrides for the agent's subprocess: scrub credentials it must not see.

    The SDK flag strips Anthropic and cloud credentials from the agent's shell. Other secrets in
    our own environment (a GitHub token, say) are blanked here. Anthropic and Claude variables are
    left alone because blanking them would break the agent's own login.
    """
    hidden = {
        name: ""
        for name in os.environ
        if SECRET_ENV_NAME.search(name) and not name.startswith(("ANTHROPIC_", "CLAUDE"))
    }
    return {**hidden, "CLAUDE_CODE_SUBPROCESS_ENV_SCRUB": "1"}
