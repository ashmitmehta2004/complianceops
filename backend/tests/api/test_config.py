"""backend/.env loading: location, precedence over nothing but the process environment, safety."""

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.agent.gemini import GeminiLLM
from app.agent.providers import llm_from_env
from app.config import BACKEND_DIR, ENV_FILE_VARIABLE, default_env_path, load_env_file
from app.main import create_app

KEYS = ("LLM_PROVIDER", "GEMINI_API_KEY", "GEMINI_MODEL", "ANTHROPIC_API_KEY", "ANTHROPIC_MODEL")
FAKE_KEY = "fake-key-for-tests-only"


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """Start with none of the LLM variables set; monkeypatch restores them (even ones that
    load_dotenv sets directly in os.environ) at teardown."""
    for key in KEYS:
        monkeypatch.setenv(key, "placeholder")
        monkeypatch.delenv(key)
    return monkeypatch


def write_env(path: Path, **values: str) -> Path:
    path.write_text("".join(f"{k}={v}\n" for k, v in values.items()), encoding="utf-8")
    return path


def test_default_path_is_backend_dir_regardless_of_cwd(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv(ENV_FILE_VARIABLE, raising=False)
    monkeypatch.chdir(tmp_path)
    assert default_env_path() == BACKEND_DIR / ".env"
    assert (BACKEND_DIR / "app" / "config.py").is_file()


def test_loading_can_be_disabled_or_redirected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_FILE_VARIABLE, "")
    assert default_env_path() is None and load_env_file() is False
    monkeypatch.setenv(ENV_FILE_VARIABLE, "somewhere/else.env")
    assert default_env_path() == Path("somewhere/else.env")


def test_missing_file_is_not_an_error(tmp_path: Path, clean_env: pytest.MonkeyPatch) -> None:
    assert load_env_file(tmp_path / "absent.env") is False


def test_env_file_configures_the_gemini_provider_from_any_cwd(
    tmp_path: Path, clean_env: pytest.MonkeyPatch
) -> None:
    env = write_env(
        tmp_path / ".env",
        LLM_PROVIDER="gemini",
        GEMINI_API_KEY=FAKE_KEY,
        GEMINI_MODEL="gemini-3-flash-preview",
    )
    clean_env.chdir(tmp_path.parent)  # an unrelated working directory
    assert load_env_file(env) is True
    assert os.environ["LLM_PROVIDER"] == "gemini"
    assert isinstance(llm_from_env(), GeminiLLM)  # constructs a client; makes no network call


def test_process_environment_wins_over_the_file(
    tmp_path: Path, clean_env: pytest.MonkeyPatch
) -> None:
    env = write_env(tmp_path / ".env", LLM_PROVIDER="gemini", GEMINI_API_KEY=FAKE_KEY)
    clean_env.setenv("LLM_PROVIDER", "anthropic")  # explicitly supplied
    load_env_file(env)
    assert os.environ["LLM_PROVIDER"] == "anthropic"
    assert os.environ["GEMINI_API_KEY"] == FAKE_KEY  # unset variables are still filled in


def test_status_endpoints_reflect_the_loaded_file_without_leaking_values(
    tmp_path: Path,
    clean_env: pytest.MonkeyPatch,
    session_factory,  # type: ignore[no-untyped-def]
) -> None:
    load_env_file(write_env(tmp_path / ".env", LLM_PROVIDER="gemini", GEMINI_API_KEY=FAKE_KEY))
    client = TestClient(create_app(session_factory))  # default llm_from_env factory
    assert client.get("/meta").json()["llm_provider"] == "gemini"
    ready = client.get("/ready")
    assert ready.json()["llm_configured"] is True
    assert FAKE_KEY not in ready.text and FAKE_KEY not in client.get("/meta").text


def test_without_any_configuration_the_app_reports_unconfigured(
    clean_env: pytest.MonkeyPatch,
    session_factory,  # type: ignore[no-untyped-def]
) -> None:
    client = TestClient(create_app(session_factory))
    assert client.get("/ready").json()["llm_configured"] is False
