"""Settings loaded from environment variables and `.env`."""

from __future__ import annotations

import re
from pathlib import Path

from pydantic import SecretStr, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_PATTERN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]*)/[A-Za-z0-9._-]+$")

_ENV_VARS = {"github_token": "GITHUB_TOKEN", "sandbox_repo": "SANDBOX_REPO"}


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or invalid."""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # SecretStr keeps the token out of repr() and logs.
    github_token: SecretStr
    sandbox_repo: str

    @field_validator("github_token")
    @classmethod
    def _token_not_placeholder(cls, v: SecretStr) -> SecretStr:
        raw = v.get_secret_value().strip()
        if not raw or raw.endswith("_xxx"):
            raise ValueError("looks like an empty or placeholder token")
        return SecretStr(raw)

    @field_validator("sandbox_repo")
    @classmethod
    def _repo_format(cls, v: str) -> str:
        v = v.strip()
        if not REPO_PATTERN.match(v):
            raise ValueError("must have the form owner/name")
        return v


def load_settings(env_file: str | Path | None = ".env") -> Settings:
    """Load settings, turning validation errors into one readable ConfigError.

    Pass `env_file=None` to read only from the process environment (used in tests).
    """
    try:
        return Settings(_env_file=env_file)  # type: ignore[call-arg]
    except ValidationError as exc:
        problems = []
        for err in exc.errors():
            field = str(err["loc"][0]) if err["loc"] else "?"
            name = _ENV_VARS.get(field, field.upper())
            reason = "is not set" if err["type"] == "missing" else err["msg"]
            problems.append(f"  - {name}: {reason}")
        raise ConfigError(
            "Invalid configuration:\n"
            + "\n".join(problems)
            + "\nCopy .env.example to .env and fill in the values (see README 'Sandbox setup')."
        ) from None
