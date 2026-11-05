"""Which environment variables look like secrets, shared by everything that runs untrusted code."""

import re

SECRET_ENV_NAME = re.compile(r"TOKEN|SECRET|PASSWORD|PASSWD|API_?KEY|CREDENTIAL|PRIVATE_?KEY", re.I)
