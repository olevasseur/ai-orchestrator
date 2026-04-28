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
    executor_mode: str = "cli"          # "cli" | "demo"
    executor_provider: str = "claude"   # "claude" | "codex" — which adapter to build for "cli" mode
    claude_cli_path: str = "claude"     # path to the `claude` binary
    codex_cli_path: str = "codex"       # path to the `codex` binary (experimental)

    # Executor workspace isolation (generic across providers).
    # executor_workspace_strategy: "inplace" runs the executor directly in the
    #   target repo (legacy behaviour). "worktree" creates a disposable git
    #   worktree under executor_worktree_base_dir and runs the executor there,
    #   isolating its filesystem writes from the user's working tree.
    executor_workspace_strategy: str = "inplace"        # "inplace" | "worktree"
    executor_worktree_base_dir: str = "/tmp/ai-orchestrator-executor-worktrees"
    # executor_apply_policy controls what happens to the worktree after the
    # executor runs:
    #   "manual" — leave the diff for the human to inspect/apply
    #   "auto"   — apply the diff back to the source repo automatically
    #   "discard"— throw the worktree away once the run is captured
    executor_apply_policy: str = "manual"               # "manual" | "auto" | "discard"
    # Post-run worktree cleanup audit. The audit writes a recommendation
    # artifact for successful worktree-strategy tiny_loop runs. Auto-remove is
    # intentionally disabled by default and is not performed by the audit.
    audit_worktrees_after_run: bool = True
    auto_remove_clean_merged_worktrees: bool = False

    # Legacy Codex-specific aliases. Kept as separate fields so existing
    # config.yaml files and call sites that read `cfg.codex_*` keep working.
    # Config.load() mirrors values between the generic and legacy forms when
    # only one is provided.
    codex_workspace_strategy: str = "inplace"
    codex_worktree_base_dir: str = "/tmp/ai-orchestrator-executor-worktrees"
    codex_apply_policy: str = "manual"

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

        # Support a nested `executor:` block as an alternative to flat keys:
        #   executor:
        #     provider: claude
        #     claude: { command: claude }
        #     codex:  { command: codex }
        # Flat keys (executor_mode, executor_provider, claude_cli_path,
        # codex_cli_path) still work and take precedence if both are set.
        executor_block = raw.get("executor")
        if isinstance(executor_block, dict):
            if "provider" in executor_block and "executor_provider" not in raw:
                raw["executor_provider"] = executor_block["provider"]
            if "mode" in executor_block and "executor_mode" not in raw:
                raw["executor_mode"] = executor_block["mode"]
            claude_block = executor_block.get("claude")
            if isinstance(claude_block, dict) and "command" in claude_block \
                    and "claude_cli_path" not in raw:
                raw["claude_cli_path"] = claude_block["command"]
            codex_block = executor_block.get("codex")
            if isinstance(codex_block, dict) and "command" in codex_block \
                    and "codex_cli_path" not in raw:
                raw["codex_cli_path"] = codex_block["command"]

        # Mirror generic executor workspace fields with their legacy codex_*
        # aliases. The generic form wins when both are present; otherwise
        # whichever side is set populates the other so call sites reading
        # either name see a consistent value.
        for generic, legacy in (
            ("executor_workspace_strategy", "codex_workspace_strategy"),
            ("executor_worktree_base_dir", "codex_worktree_base_dir"),
            ("executor_apply_policy", "codex_apply_policy"),
        ):
            if generic in raw:
                raw.setdefault(legacy, raw[generic])
            elif legacy in raw:
                raw.setdefault(generic, raw[legacy])

        # 2. Override with environment variables for secrets
        cfg = cls(**{k: v for k, v in raw.items() if k in cls.__dataclass_fields__})
        if key := os.environ.get("OPENAI_API_KEY"):
            cfg.openai_api_key = key
        if model := os.environ.get("OPENAI_MODEL"):
            cfg.openai_model = model
        if mode := os.environ.get("EXECUTOR_MODE"):
            cfg.executor_mode = mode
        if provider := os.environ.get("EXECUTOR_PROVIDER"):
            cfg.executor_provider = provider

        return cfg
