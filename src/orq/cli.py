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
from orq.adapters.router import ReviewerRouter
from orq.config import Config, load_config
from orq.core.checkpoint import Checkpoint
from orq.core.loop import PAUSE_FLAG, ResumeError, Runner, SandboxError, implementer_system_prompt
from orq.core.models import Decision, RunState
from orq.core.procs import pid_alive
from orq.core.task import TaskError, parse_task
from orq.git.manager import GitError, GitManager
from orq.paths import OrqPaths
from orq.store.db import Store
from orq.store.rundir import RunDir
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
    codex = CodexReviewer(model=config.reviewer.codex_model, effort=config.reviewer.routine_effort,
                          ignore_user_config=config.reviewer.codex_ignore_user_config)
    if config.reviewer.fallback != "claude":
        return codex
    # Codex usage limit -> Claude reviewer until the limit resets (SPEC 12).
    return ReviewerRouter(codex, ClaudeReviewer(model=config.implementer.default_model),
                          switch_at_used_percent=config.reviewer.switch_at_used_percent)


def _runner_parts(config: Config) -> dict:
    return dict(
        git=GitManager(),
        implementer=ClaudeImplementer(model=config.implementer.default_model, system_prompt=implementer_system_prompt()),
        reviewer=_build_reviewer(config), scanner=SecretScanner(),
    )


def _resume_runner(run_id: str, *, human) -> Runner:
    paths = OrqPaths.from_env()
    config = load_config(paths.config)
    try:
        return Runner.resume(run_id=run_id, config=config, paths=paths, store=Store(paths.db), human=human, printer=typer.echo,
                             **_runner_parts(config))
    except (ResumeError, SandboxError, TaskError) as exc:
        typer.secho(str(exc), fg=typer.colors.RED)
        raise typer.Exit(1)


def _checkpoint_or_exit(run_id: str) -> tuple[OrqPaths, Checkpoint]:
    paths = OrqPaths.from_env()
    cp = Checkpoint.load(paths.run_dir(run_id) / "state.json")
    if cp is None:
        typer.secho(f"no run {run_id} (no state.json)", fg=typer.colors.RED)
        raise typer.Exit(1)
    return paths, cp


@app.command()
def run(
    task_file: Path = typer.Argument(..., exists=True, readable=True, help="Path to TASK.md"),
    clone_url: str | None = typer.Option(None, "--clone-url", hidden=True, help="Override the clone URL (tests)."),
    no_prompt: bool = typer.Option(False, "--no-prompt", help="Headless: stop at the first decision instead of asking in the terminal."),
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
        runner = Runner(config=config, paths=paths, store=Store(paths.db), task=task, task_text=task_text,
                        human=None if no_prompt else _ask_in_terminal, clone_url=clone_url, printer=typer.echo, **_runner_parts(config))
    except SandboxError as exc:
        typer.secho(str(exc), fg=typer.colors.RED)
        raise typer.Exit(1)
    typer.echo(f"run {runner.run_id}: {task.title} on {task.repo} (branch {runner.branch})")
    typer.echo(f"run dir: {runner.rundir.path}")
    final = asyncio.run(runner.execute())
    if final is not RunState.DONE:
        raise typer.Exit(1)


@app.command()
def resume(
    run_id: str,
    no_prompt: bool = typer.Option(False, "--no-prompt", help="Headless: stop at the next decision instead of asking in the terminal."),
) -> None:
    """Continue a paused, crashed or answered run from its last checkpoint."""
    runner = _resume_runner(run_id, human=None if no_prompt else _ask_in_terminal)
    typer.echo(f"resuming {run_id} at phase {runner.cp.phase}, iteration {runner.cp.iteration}")
    final = asyncio.run(runner.execute())
    if final is not RunState.DONE:
        raise typer.Exit(1)


@app.command()
def answer(
    decision_id: str,
    text: str | None = typer.Argument(None, help="Free-text answer, or an option as written in `orq status`."),
    approve: bool = typer.Option(False, "--approve", help="Answer 'approve'."),
    deny: bool = typer.Option(False, "--deny", help="Answer 'deny'."),
) -> None:
    """Answer a pending decision. The run continues with `orq resume <run_id>`."""
    if sum([bool(text), approve, deny]) != 1:
        typer.secho("give exactly one of: a text answer, --approve, --deny", fg=typer.colors.RED)
        raise typer.Exit(1)
    paths = OrqPaths.from_env()
    store = Store(paths.db)
    decision = store.get_decision(decision_id)
    if decision is None:
        typer.secho(f"no decision {decision_id}", fg=typer.colors.RED)
        raise typer.Exit(1)
    if decision.status != "pending":
        typer.secho(f"{decision_id} already answered: {decision.answer}", fg=typer.colors.RED)
        raise typer.Exit(1)
    value = "approve" if approve else "deny" if deny else str(text).strip()
    if value.isdigit() and decision.options and 0 <= int(value) < len(decision.options):
        value = decision.options[int(value)]  # same shorthand as the terminal prompt
    store.answer_decision(decision_id, answer=value, answered_via="cli")
    rundir = RunDir(paths.run_dir(decision.run_id))
    rundir.append_decision(decision_id, decision.question, value)
    rundir.event("answer", decision_id=decision_id, answer=value, via="cli")
    typer.echo(f"{decision_id} answered: {value}. Run `orq resume {decision.run_id}` to continue.")


@app.command()
def pause(run_id: str) -> None:
    """Ask a running run to stop between steps; `orq resume` continues it."""
    paths, cp = _checkpoint_or_exit(run_id)
    (paths.run_dir(run_id) / PAUSE_FLAG).write_text("", encoding="utf-8")
    note = "" if pid_alive(cp.pid) else " (no live process; the run stays paused until `orq resume`)"
    typer.echo(f"pause requested for {run_id}{note}")


@app.command()
def abort(run_id: str) -> None:
    """Mark a stopped run ABORTED and remove its worktree."""
    paths, cp = _checkpoint_or_exit(run_id)
    if pid_alive(cp.pid):
        typer.secho(f"{run_id} is still running (pid {cp.pid}); `orq pause {run_id}` first", fg=typer.colors.RED)
        raise typer.Exit(1)
    rundir = RunDir(paths.run_dir(run_id))
    if cp.repo_path and Path(cp.worktree).exists():
        try:
            GitManager().remove_worktree(Path(cp.repo_path), Path(cp.worktree), branch=cp.branch)
        except GitError as exc:
            typer.secho(f"worktree not removed: {exc}", fg=typer.colors.YELLOW)
    cp.state, cp.phase = RunState.ABORTED.value, "done"
    cp.save(rundir.path / "state.json")
    Store(paths.db).set_state(run_id, RunState.ABORTED)
    rundir.event("state", state=RunState.ABORTED.value, reason="aborted by owner")
    typer.echo(f"{run_id} aborted")


@app.command()
def rollback(run_id: str, to: int = typer.Option(..., "--to", help="Iteration to roll back to (0 = base commit).")) -> None:
    """Reset a stopped run's worktree to an earlier iteration; `orq resume` then continues from there."""
    runner = _resume_runner(run_id, human=None)
    try:
        runner.rollback_cli(to)
    except ValueError as exc:
        typer.secho(str(exc), fg=typer.colors.RED)
        raise typer.Exit(1)
    typer.echo(f"{run_id} rolled back to iteration {to}; run `orq resume {run_id}` to continue")


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
    cp = Checkpoint.load(paths.run_dir(run_id) / "state.json")
    if cp and cp.plan and cp.plan.get("milestones"):
        milestones = cp.plan["milestones"]
        index = min(cp.milestone_index, len(milestones) - 1)
        typer.echo(f"milestone {index + 1}/{len(milestones)}: {milestones[index]['title']} [{milestones[index]['difficulty']}]")
    if cp and cp.pr_url:
        typer.echo(f"pr {cp.pr_url}")
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
