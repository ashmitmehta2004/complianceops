"""Local configuration: load `backend/.env` into the process environment.

The file is located relative to this package, not the working directory, so the server starts
with the same configuration wherever it is launched from. Variables already present in the
process environment always win (`override=False`), so a real deployment or an explicit
`$env:LLM_PROVIDER=...` is never silently replaced by a local file. Values are never logged.

`COMPLIANCEOPS_ENV_FILE` points at a different file; set it to an empty string to disable
loading (the test suite does this so tests never read a developer's real keys).
"""

import os
from pathlib import Path

from dotenv import load_dotenv

BACKEND_DIR = Path(__file__).resolve().parents[1]
ENV_FILE_VARIABLE = "COMPLIANCEOPS_ENV_FILE"


def default_env_path() -> Path | None:
    override = os.environ.get(ENV_FILE_VARIABLE)
    if override is None:
        return BACKEND_DIR / ".env"
    return Path(override) if override else None


def load_env_file(path: Path | None = None) -> bool:
    """Load the env file if it exists. Returns True when a file was read."""
    target = path or default_env_path()
    if target is None or not target.is_file():
        return False
    return load_dotenv(target, override=False)
