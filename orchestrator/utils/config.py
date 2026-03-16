"""
Config loading: merges .env secrets with config.yaml settings.

Lookup order:
  1. Environment variables (including those loaded from .env)
  2. config.yaml in the current working directory
  3. Hardcoded defaults
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

# Load .env from cwd or project root on import
load_dotenv(dotenv_path=Path.cwd() / ".env", override=False)


@dataclass
class Config:
    # Planner
    openai_api_key: str = ""
    openai_model: str = "gpt-4o"

    # Executor
    executor_mode: str = "cli"          # "cli" | "sdk"
    claude_cli_path: str = "claude"     # path to the `claude` binary

    # Storage
    log_dir: str = "~/.orchestrator/runs"

    # Timeouts (seconds)
    executor_timeout: int = 600         # 10 minutes
    validation_timeout: int = 120       # 2 minutes

    # Memory
    memory_refresh_interval: int = 5   # auto-refresh every N completed iterations

    # Safety
    command_allowlist: list[str] = field(default_factory=lambda: [
        "pytest", "python", "npm", "npx", "cargo", "go", "make",
        "git status", "git diff", "git log",
    ])
    command_denylist: list[str] = field(default_factory=lambda: [
        "rm -rf /", "sudo", "> /dev/sda",
    ])

    # Repo defaults
    default_repo_path: str = "."

    @classmethod
    def load(cls, config_path: Path | None = None) -> "Config":
        """Load config from YAML file + environment variables."""
        raw: dict[str, Any] = {}

        # 1. Read YAML if present
        path = config_path or Path.cwd() / "config.yaml"
        if path.exists():
            with path.open() as f:
                raw = yaml.safe_load(f) or {}

        # 2. Override with environment variables for secrets
        cfg = cls(**{k: v for k, v in raw.items() if k in cls.__dataclass_fields__})
        if key := os.environ.get("OPENAI_API_KEY"):
            cfg.openai_api_key = key
        if model := os.environ.get("OPENAI_MODEL"):
            cfg.openai_model = model
        if mode := os.environ.get("EXECUTOR_MODE"):
            cfg.executor_mode = mode

        return cfg
