"""Global configuration (SPEC section 13). Secrets never live here; they come from env vars."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any


@dataclass
class LimitsConfig:
    max_iterations: int = 15
    max_wall_hours: float = 6
    max_concurrent_runs: int = 2
    # Times a Claude usage limit is waited out within one iteration before the owner is asked.
    rate_limit_retries: int = 3


@dataclass
class ImplementerConfig:
    default_model: str = "opus"
    mechanical_model: str = "sonnet"


@dataclass
class ReviewerConfig:
    primary: str = "codex"
    codex_model: str = "gpt-6.1-sol"
    claude_model: str = ""                 # Claude reviewer (primary or fallback); empty follows implementer.default_model
    fallback: str = "claude"
    routine_effort: str = "low"
    final_effort: str = "high"
    switch_at_used_percent: int = 90
    # Keeps the owner's Codex plugins, hooks and MCP servers out of reviewer calls (about 30% fewer tokens).
    codex_ignore_user_config: bool = True
    # Restated because the user config is ignored. On CLI 0.161.0 only "unelevated" starts processes read-only (Phase 6).
    codex_windows_sandbox: str = "unelevated"


@dataclass
class GitConfig:
    merge_strategy: str = "squash"
    # Short root: Windows LongPathsEnabled is often off and non-git tools fail past 260 chars.
    worktree_root: Path = Path("C:/orq-wt")
    protected_paths: list[str] = field(default_factory=lambda: [".github/**", "migrations/**", "**/.env*"])
    # Optional allowlist; an empty list allows any repo (the guard is the safety layer since Phase 2).
    sandbox_repos: list[str] = field(default_factory=list)
    # Debugging aid: leave the worktree and local branch in place after the merge.
    keep_worktree: bool = False

    def __post_init__(self) -> None:
        self.worktree_root = Path(str(self.worktree_root)).expanduser()


@dataclass
class GuardConfig:
    """Post-execution diff rules (SPEC 10.4)."""

    # `negative_balance` fires when source files lose this many more lines than they gain in one iteration.
    max_net_deleted_lines: int = 300
    source_globs: list[str] = field(default_factory=lambda: [
        "**/*.py", "**/*.js", "**/*.ts", "**/*.tsx", "**/*.jsx", "**/*.rs", "**/*.go", "**/*.java", "**/*.cs",
    ])


@dataclass
class MergeConfig:
    """Merge gate (SPEC 10.7)."""

    poll_seconds: float = 20
    ci_timeout_minutes: float = 60
    # No checks at all after this long means the repo has no CI; the owner decides (Phase 3).
    ci_grace_minutes: float = 5
    max_ci_reruns: int = 3
    # Gate restarts (CI failure or final review not done) before the owner is asked.
    max_gate_rounds: int = 3
    # Implementer turns to resolve a rebase conflict (one per conflicting commit) before the owner is asked (Phase 6).
    max_conflict_rounds: int = 3


@dataclass
class NotifyConfig:
    """Toast and WhatsApp via n8n (SPEC 9.3, 9.4). The token lives in the environment, never here."""

    toast: bool = True
    n8n_base_url: str = ""                 # empty disables WhatsApp
    n8n_token_env: str = "ORQ_N8N_TOKEN"
    poll_seconds: float = 20               # inbound replies poll (hub)
    outbox_poll_seconds: float = 5         # new decisions and run states to send (hub)
    answer_poll_seconds: float = 3         # a waiting run re-reads SQLite this often
    reminder_hours: float = 3
    progress_minutes: float = 30           # digest of a working run this often, and when a milestone ends; 0 disables
    # Phase 6.1: answer low-stakes decisions with the recommendation when the owner has not answered (off by default).
    auto_answer: bool = False
    auto_answer_minutes: int = 30
    auto_answer_max_stakes: str = "low"    # low | medium | high


@dataclass
class DashboardConfig:
    port: int = 8765                       # loopback only


@dataclass
class QueueConfig:
    """Queue and slots (Phase 5). The global cap is [limits].max_concurrent_runs."""

    poll_seconds: float = 5                # hub dispatcher tick and a run's wait for a free slot
    project_concurrency: int = 1           # active runs per project unless the project sets its own


@dataclass
class Config:
    limits: LimitsConfig = field(default_factory=LimitsConfig)
    implementer: ImplementerConfig = field(default_factory=ImplementerConfig)
    reviewer: ReviewerConfig = field(default_factory=ReviewerConfig)
    git: GitConfig = field(default_factory=GitConfig)
    guard: GuardConfig = field(default_factory=GuardConfig)
    merge: MergeConfig = field(default_factory=MergeConfig)
    notify: NotifyConfig = field(default_factory=NotifyConfig)
    dashboard: DashboardConfig = field(default_factory=DashboardConfig)
    queue: QueueConfig = field(default_factory=QueueConfig)


def load_config(path: Path) -> Config:
    """Load config.toml; missing file means all defaults. Unknown keys are an error."""
    if not path.exists():
        return Config()
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    return _build(Config, raw, prefix="")


def _build(cls: type, raw: dict[str, Any], prefix: str) -> Any:
    known = {f.name: f for f in fields(cls)}
    for key in raw:
        if key not in known:
            raise ValueError(f"unknown config key: {prefix}{key}")
    kwargs: dict[str, Any] = {}
    for name, value in raw.items():
        target = known[name].type
        if isinstance(value, dict) and isinstance(target, str) and target.endswith("Config"):
            kwargs[name] = _build(globals()[target], value, prefix=f"{prefix}{name}.")
        elif is_dataclass(target) and isinstance(value, dict):
            kwargs[name] = _build(target, value, prefix=f"{prefix}{name}.")
        else:
            kwargs[name] = value
    return cls(**kwargs)
