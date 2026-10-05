"""CLI surface (SPEC section 14)."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import typer

from orq import __version__
from orq.adapters.claude import ClaudeImplementer, ClaudeReviewer
from orq.adapters.codex import CodexReviewer
from orq.config import Config, load_config
from orq.core.loop import Runner, SandboxError, implementer_system_prompt
from orq.core.models import Decision, RunState
from orq.core.task import TaskError, parse_task
from orq.git.manager import GitManager
from orq.paths import OrqPaths
from orq.store.db import Store
from orq.verify.secrets import SecretScanner

app = typer.Typer(no_args_is_help=True, add_completion=False)


def _version(value: bool) -> None:
    if value:
        typer.echo(f"orq {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(False, "--version", callback=_version, is_eager=True, help="Show the version."),
) -> None:
    """Local Claude Code <-> Codex orchestrator."""


def _ask_in_terminal(decision: Decision) -> str:
    typer.echo("")
    typer.secho(f"[orq] {decision.decision_id} needs you ({decision.source}, {decision.decision_type})", fg=typer.colors.YELLOW, bold=True)
    typer.echo(decision.question)
    for index, option in enumerate(decision.options):
        marker = " (recommended)" if decision.recommendation == index else ""
        typer.echo(f"  {index}. {option}{marker}")
    return typer.prompt("Reply with an option number or free text")


def _build_reviewer(config: Config):
    if config.reviewer.primary == "claude":
        return ClaudeReviewer(model=config.implementer.default_model)
    return CodexReviewer(model=config.reviewer.codex_model, effort=config.reviewer.routine_effort,
                         ignore_user_config=config.reviewer.codex_ignore_user_config)


@app.command()
def run(
    task_file: Path = typer.Argument(..., exists=True, readable=True, help="Path to TASK.md"),
    clone_url: str | None = typer.Option(None, "--clone-url", hidden=True, help="Override the clone URL (tests)."),
) -> None:
    """Run a task from TASK.md until a PR is open or the owner is needed."""
    paths = OrqPaths.from_env()
    config = load_config(paths.config)
    task_text = task_file.read_text(encoding="utf-8")
    try:
        task = parse_task(task_text)
    except TaskError as exc:
        typer.secho(f"TASK.md invalid: {exc}", fg=typer.colors.RED)
        raise typer.Exit(1)
    try:
        runner = Runner(
            config=config, paths=paths, store=Store(paths.db), task=task, task_text=task_text, git=GitManager(),
            implementer=ClaudeImplementer(model=config.implementer.default_model, system_prompt=implementer_system_prompt()),
            reviewer=_build_reviewer(config), scanner=SecretScanner(), human=_ask_in_terminal,
            clone_url=clone_url, printer=typer.echo,
        )
    except SandboxError as exc:
        typer.secho(str(exc), fg=typer.colors.RED)
        raise typer.Exit(1)
    typer.echo(f"run {runner.run_id}: {task.title} on {task.repo} (branch {runner.branch})")
    typer.echo(f"run dir: {runner.rundir.path}")
    final = asyncio.run(runner.execute())
    if final is not RunState.DONE:
        raise typer.Exit(1)


@app.command()
def status(run_id: str | None = typer.Argument(None)) -> None:
    """Show all runs, or one run with its pending decisions."""
    paths = OrqPaths.from_env()
    store = Store(paths.db)
    if run_id is None:
        runs = store.list_runs()
        if not runs:
            typer.echo("no runs")
            return
        for item in runs:
            typer.echo(f"{item.run_id}  {item.state.value:<22} iter {item.iteration:<3} {item.repo}  {item.task_title}")
        return
    item = store.get_run(run_id)
    if item is None:
        typer.secho(f"no run {run_id}", fg=typer.colors.RED)
        raise typer.Exit(1)
    typer.echo(f"{item.run_id}  {item.state.value}  iteration {item.iteration}")
    typer.echo(f"repo {item.repo}  branch {item.branch}\nworktree {item.worktree}\ntask {item.task_title}")
    for decision in store.pending_decisions(run_id):
        typer.echo(f"\npending {decision.decision_id} ({decision.source}, {decision.decision_type}): {decision.question}")
        for index, option in enumerate(decision.options):
            typer.echo(f"  {index}. {option}")


@app.command()
def logs(run_id: str, follow: bool = typer.Option(False, "--follow", "-f")) -> None:
    """Print a run's events.jsonl, optionally following it."""
    path = OrqPaths.from_env().run_dir(run_id) / "events.jsonl"
    if not path.exists():
        typer.secho(f"no events for run {run_id}", fg=typer.colors.RED)
        raise typer.Exit(1)
    with path.open(encoding="utf-8") as handle:
        while True:
            line = handle.readline()
            if line:
                typer.echo(_format_event(line))
            elif follow:
                time.sleep(1)
            else:
                break


def _format_event(line: str) -> str:
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return line.rstrip()
    rest = {k: v for k, v in event.items() if k not in ("ts", "type")}
    return f"{event.get('ts', '')}  {event.get('type', ''):<10} {json.dumps(rest, ensure_ascii=False)}"
