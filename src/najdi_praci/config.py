"""Configuration loading and validation for najdi_praci."""

from __future__ import annotations

import os
from pathlib import Path

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field, ValidationError

PROJECT_ROOT: Path = Path(__file__).resolve().parents[2]

_PLACEHOLDER_API_KEY = "your_gemini_api_key_here"


class ConfigError(Exception):
    """Raised when configuration or required environment variables are invalid."""


class ScrapingConfig(BaseModel):
    """Settings controlling jobs.cz scraping and pagination."""

    max_pages: int = Field(default=20, ge=1)
    max_age_days: int = Field(default=3, ge=0)
    seen_streak_stop: int = Field(default=15, ge=1)
    request_delay_sec: float = Field(default=1.0, ge=0.0)
    use_playwright_fallback: bool = True


class GeminiConfig(BaseModel):
    """Settings controlling Gemini models, batching, and retries."""

    stage1_model: str = "gemini-3.5-flash-lite"
    stage2_model: str = "gemini-3.5-flash"
    stage1_batch_size: int = Field(default=75, ge=1)
    max_retries: int = Field(default=3, ge=0)


class OutputConfig(BaseModel):
    """Paths for generated reports, logs, and SQLite database."""

    reports_dir: Path = Path("reports")
    logs_dir: Path = Path("logs")
    db_path: Path = Path("data/jobs.db")


class AppConfig(BaseModel):
    """Top-level validated application configuration."""

    search_url: str
    cv_path: Path
    scraping: ScrapingConfig = Field(default_factory=ScrapingConfig)
    gemini: GeminiConfig = Field(default_factory=GeminiConfig)
    output: OutputConfig = Field(default_factory=OutputConfig)
    gemini_api_key: str | None = None
    project_root: Path = PROJECT_ROOT


def _resolve_against_root(path: Path, project_root: Path) -> Path:
    """Resolve relative paths against project_root rather than CWD."""
    if path.is_absolute():
        return path
    return (project_root / path).resolve()


def _clean_env_value(value: str | None) -> str | None:
    """Normalize optional environment variable strings."""
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def load_config(
    config_path: Path | None = None,
    env_path: Path | None = None,
    require_api_key: bool = True,
) -> AppConfig:
    """Load configuration from YAML and .env, resolving paths against PROJECT_ROOT."""
    project_root = PROJECT_ROOT

    resolved_config_path = _resolve_against_root(
        config_path if config_path is not None else Path("config.yaml"),
        project_root,
    )
    if not resolved_config_path.is_file():
        raise ConfigError(
            f"Configuration file not found: {resolved_config_path}"
        )

    try:
        raw_text = resolved_config_path.read_text(encoding="utf-8")
        raw_data = yaml.safe_load(raw_text)
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(
            f"Failed to read configuration file {resolved_config_path}: {exc}"
        ) from exc

    if raw_data is None:
        raw_data = {}
    if not isinstance(raw_data, dict):
        raise ConfigError(
            f"Configuration file {resolved_config_path} must contain a YAML mapping."
        )

    resolved_env_path = _resolve_against_root(
        env_path if env_path is not None else Path(".env"),
        project_root,
    )
    load_dotenv(dotenv_path=resolved_env_path, override=False)

    try:
        config = AppConfig(**raw_data, project_root=project_root)
    except ValidationError as exc:
        raise ConfigError(f"Invalid configuration in {resolved_config_path}: {exc}") from exc

    api_key = _clean_env_value(os.environ.get("GEMINI_API_KEY"))
    if api_key == _PLACEHOLDER_API_KEY:
        api_key = None

    if require_api_key and not api_key:
        raise ConfigError(
            "GEMINI_API_KEY is missing or unset. Copy .env.example to .env and set "
            "GEMINI_API_KEY to a valid Google Gemini API key."
        )

    stage1_override = _clean_env_value(os.environ.get("STAGE1_MODEL"))
    stage2_override = _clean_env_value(os.environ.get("STAGE2_MODEL"))

    gemini_update: dict[str, str] = {}
    if stage1_override:
        gemini_update["stage1_model"] = stage1_override
    if stage2_override:
        gemini_update["stage2_model"] = stage2_override

    updated_gemini = (
        config.gemini.model_copy(update=gemini_update)
        if gemini_update
        else config.gemini
    )
    updated_output = config.output.model_copy(
        update={
            "reports_dir": _resolve_against_root(config.output.reports_dir, project_root),
            "logs_dir": _resolve_against_root(config.output.logs_dir, project_root),
            "db_path": _resolve_against_root(config.output.db_path, project_root),
        }
    )

    return config.model_copy(
        update={
            "cv_path": _resolve_against_root(config.cv_path, project_root),
            "gemini": updated_gemini,
            "output": updated_output,
            "gemini_api_key": api_key,
            "project_root": project_root,
        }
    )
