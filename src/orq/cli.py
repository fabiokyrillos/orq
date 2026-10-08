"""CLI surface (SPEC section 14)."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import threading
import time
from pathlib import Path

import typer

from orq import __version__
from orq.adapters.claude import ClaudeImplementer, ClaudeReviewer
from orq.adapters.codex import CodexReviewer
from orq.adapters.router import ReviewerRouter
from orq.config import Config, load_config
from orq.core.answers import AnswerError, record_answer
from orq.core.checkpoint import Checkpoint
from orq.core.control import ControlError, abort_run, request_pause
from orq.core.loop import PAUSE_FLAG, ResumeError, Runner, SandboxError, implementer_system_prompt
from orq.core.models import Decision, RunState
from orq.core.procs import pid_alive
from orq.core.task import TaskError, parse_task
from orq.git.manager import GitError, GitManager
from orq.notify.toast import show_toast
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


def _terminal_answers(paths: OrqPaths, run_id: str) -> None:
    """Daemon thread: whatever the owner types answers the run's pending decision, like `orq answer` would."""
    store = Store(paths.db)
    while True:
        line = sys.stdin.readline()
        if not line:
            return
        text = line.strip()
        if not text:
            continue
        pending = store.pending_decisions(run_id)
        if not pending:
            typer.echo("(no pending decision right now)")
            continue
        try:
            answered = record_answer(store, paths, pending[0].decision_id, text, via="terminal")
            typer.echo(f"{answered.decision_id} answered: {answered.answer}")
        except AnswerError as exc:
            typer.secho(str(exc), fg=typer.colors.RED)


def _start_terminal_thread(paths: OrqPaths, run_id: str) -> None:
    if sys.stdin is None or not sys.stdin.isatty():
        return
    threading.Thread(target=_terminal_answers, args=(paths, run_id), daemon=True).start()


def _build_reviewer(config: Config):
    # Models and efforts given here are only defaults: the runner resolves them before every call (Phase 6 settings).
    claude_model = config.reviewer.claude_model or config.implementer.default_model
    if config.reviewer.primary == "claude":
        return ClaudeReviewer(model=claude_model)
    codex = CodexReviewer(model=config.reviewer.codex_model, effort=config.reviewer.routine_effort,
                          ignore_user_config=config.reviewer.codex_ignore_user_config,
                          windows_sandbox=config.reviewer.codex_windows_sandbox)
    if config.reviewer.fallback != "claude":
        return codex
    # Codex usage limit -> Claude reviewer until the limit resets (SPEC 12).
    return ReviewerRouter(codex, ClaudeReviewer(model=claude_model), switch_at_used_percent=config.reviewer.switch_at_used_percent)


def _runner_parts(config: Config) -> dict:
    return dict(
        git=GitManager(),
        implementer=ClaudeImplementer(model=config.implementer.default_model, system_prompt=implementer_system_prompt()),
        reviewer=_build_reviewer(config), scanner=SecretScanner(),
        notifier=show_toast if config.notify.toast else None,
    )


def _resume_runner(run_id: str, *, wait_for_answers: bool) -> Runner:
    paths = OrqPaths.from_env()
    config = load_config(paths.config)
    try:
        return Runner.resume(run_id=run_id, config=config, paths=paths, store=Store(paths.db), human=None, printer=typer.echo,
                             wait_for_answers=wait_for_answers, **_runner_parts(config))
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
    no_prompt: bool = typer.Option(False, "--no-prompt", help="Exit at the first decision instead of waiting for an answer."),
) -> None:
    """Run a task from TASK.md to a merged PR. Decisions are answered here, in the dashboard, on WhatsApp or with `orq answer`."""
    paths = OrqPaths.from_env()
    config = load_config(paths.config)
    task_text = task_file.read_text(encoding="utf-8")
    try:
        task = parse_task(task_text)
    except TaskError as exc:
        typer.secho(f"TASK.md invalid: {exc}", fg=typer.colors.RED)
        raise typer.Exit(1)
    try:
        runner = Runner(config=config, paths=paths, store=Store(paths.db), task=task, task_text=task_text, human=None,
                        wait_for_answers=not no_prompt, clone_url=clone_url, printer=typer.echo, **_runner_parts(config))
    except SandboxError as exc:
        typer.secho(str(exc), fg=typer.colors.RED)
        raise typer.Exit(1)
    typer.echo(f"run {runner.run_id}: {task.title} on {task.repo} (branch {runner.branch})")
    typer.echo(f"run dir: {runner.rundir.path}")
    if not no_prompt:
        _start_terminal_thread(paths, runner.run_id)
    final = asyncio.run(runner.execute())
    if final is not RunState.DONE:
        raise typer.Exit(1)


@app.command()
def queue(
    task_file: Path = typer.Argument(..., exists=True, readable=True, help="Path to TASK.md"),
    clone_url: str | None = typer.Option(None, "--clone-url", hidden=True, help="Override the clone URL (tests)."),
) -> None:
    """Queue a task. The hub (`orq dashboard`) starts it when a slot is free; `orq resume <run_id>` starts it by hand."""
    from orq.core.queue import QueueError, enqueue

    paths = OrqPaths.from_env()
    try:
        run_id = enqueue(paths, Store(paths.db), load_config(paths.config), task_file.read_text(encoding="utf-8"), clone_url=clone_url)
    except (TaskError, QueueError) as exc:
        typer.secho(f"not queued: {exc}", fg=typer.colors.RED)
        raise typer.Exit(1)
    typer.echo(f"queued {run_id}")
    typer.echo(f"`orq dashboard` starts it when a slot is free, or run `orq resume {run_id}` now.")


@app.command()
def resume(
    run_id: str,
    no_prompt: bool = typer.Option(False, "--no-prompt", help="Exit at the next decision instead of waiting for an answer."),
) -> None:
    """Continue a paused, crashed or answered run from its last checkpoint."""
    runner = _resume_runner(run_id, wait_for_answers=not no_prompt)
    typer.echo(f"resuming {run_id} at phase {runner.cp.phase}, iteration {runner.cp.iteration}")
    if not no_prompt:
        _start_terminal_thread(OrqPaths.from_env(), run_id)
    final = asyncio.run(runner.execute())
    if final is not RunState.DONE:
        raise typer.Exit(1)


@app.command()
def answer(
    decision_id: str,
    text: str | None = typer.Argument(None, help="Free-text answer, or an option as written in `orq status`."),
    approve: bool = typer.Option(False, "--approve", help="Answer 'approve'."),
    deny: bool = typer.Option(False, "--deny", help="Answer 'deny'."),
    by: str = typer.Option("owner", "--by", help="Who answers: owner, or claude when the coding assistant answers."),
) -> None:
    """Answer a pending decision. The run continues with `orq resume <run_id>`."""
    if sum([bool(text), approve, deny]) != 1:
        typer.secho("give exactly one of: a text answer, --approve, --deny", fg=typer.colors.RED)
        raise typer.Exit(1)
    paths = OrqPaths.from_env()
    value = "approve" if approve else "deny" if deny else str(text)
    try:
        if by not in ("owner", "claude"):
            raise AnswerError("--by must be owner or claude")
        decision = record_answer(Store(paths.db), paths, decision_id, value, via="cli", by=by)
    except AnswerError as exc:
        typer.secho(str(exc), fg=typer.colors.RED)
        raise typer.Exit(1)
    cp = Checkpoint.try_load(paths.run_dir(decision.run_id) / "state.json")
    hint = "" if cp and pid_alive(cp.pid) else f" The hub resumes it; without the hub, run `orq resume {decision.run_id}`."
    typer.echo(f"{decision_id} answered: {decision.answer}.{hint}")


@app.command()
def pause(run_id: str) -> None:
    """Ask a running run to stop between steps; `orq resume` continues it."""
    try:
        live = request_pause(OrqPaths.from_env(), run_id)
    except ControlError as exc:
        typer.secho(str(exc), fg=typer.colors.RED)
        raise typer.Exit(1)
    note = "" if live else " (no live process; the run stays paused until `orq resume`)"
    typer.echo(f"pause requested for {run_id}{note}")


@app.command()
def abort(run_id: str) -> None:
    """Mark a stopped run ABORTED and remove its worktree."""
    try:
        warning = abort_run(OrqPaths.from_env(), run_id)
    except ControlError as exc:
        typer.secho(str(exc), fg=typer.colors.RED)
        raise typer.Exit(1)
    if warning:
        typer.secho(warning, fg=typer.colors.YELLOW)
    typer.echo(f"{run_id} aborted")


@app.command()
def dashboard(
    port: int | None = typer.Option(None, "--port", help="Override [dashboard].port."),
    log_file: Path | None = typer.Option(None, "--log-file", help="Write all output here (used when started at logon with pythonw)."),
) -> None:
    """Serve the local dashboard on 127.0.0.1 and own the WhatsApp channel while running."""
    import logging

    import uvicorn

    if log_file is not None:
        # pythonw has no console (sys.stdout is None); everything, uvicorn's logs included, goes to the file.
        log_file.parent.mkdir(parents=True, exist_ok=True)
        stream = log_file.open("a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stderr = stream
        logging.basicConfig(stream=stream, level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

    from orq.hub.app import create_app
    from orq.notify.whatsapp import WhatsAppClient

    paths = OrqPaths.from_env()
    config = load_config(paths.config)
    whatsapp = WhatsAppClient.from_config(config)
    typer.echo("WhatsApp: " + ("on" if whatsapp else f"off (set [notify].n8n_base_url and the {config.notify.n8n_token_env} variable)"))
    chosen = port or config.dashboard.port
    config.dashboard.port = chosen  # the hub only answers requests addressed to this port (Host check)
    typer.echo(f"dashboard: http://127.0.0.1:{chosen}/")
    uvicorn.run(create_app(paths, config, whatsapp=whatsapp), host="127.0.0.1", port=chosen, log_level="warning")


AUTOSTART_TASK = "orq-hub"
hub_app = typer.Typer(no_args_is_help=True, help="The hub (orq dashboard) as a background service.")
app.add_typer(hub_app, name="hub")


def autostart_argv(action: str, *, python: Path, log_file: Path) -> list[str]:
    """schtasks command lines for the logon task that starts the hub without a console (Phase 6)."""
    if action == "on":
        command = f'"{python}" -m orq dashboard --log-file "{log_file}"'
        return ["schtasks", "/Create", "/F", "/SC", "ONLOGON", "/RL", "LIMITED", "/TN", AUTOSTART_TASK, "/TR", command]
    if action == "off":
        return ["schtasks", "/Delete", "/F", "/TN", AUTOSTART_TASK]
    if action == "status":
        return ["schtasks", "/Query", "/TN", AUTOSTART_TASK, "/FO", "LIST"]
    raise ValueError(f"unknown action {action!r}: on, off or status")


@hub_app.command("autostart")
def hub_autostart(action: str = typer.Argument(..., help="on | off | status")) -> None:
    """Start the hub at logon (a Windows scheduled task running pythonw), stop doing so, or show the task."""
    paths = OrqPaths.from_env()
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    try:
        argv = autostart_argv(action, python=pythonw if pythonw.exists() else Path(sys.executable), log_file=paths.root / "hub.log")
    except ValueError as exc:
        typer.secho(str(exc), fg=typer.colors.RED)
        raise typer.Exit(1)
    proc = subprocess.run(argv, capture_output=True, text=True)
    output = (proc.stdout + proc.stderr).strip()
    if proc.returncode != 0:
        hint = " Run it again from a terminal opened as administrator." if "denied" in output.lower() else ""
        typer.secho(f"schtasks failed: {output}{hint}", fg=typer.colors.RED)
        raise typer.Exit(1)
    typer.echo(output)
    if action == "on":
        typer.echo(f"{AUTOSTART_TASK}: the hub starts at your next logon; logs in {paths.root / 'hub.log'}")


project_app = typer.Typer(no_args_is_help=True, help="Projects: the GitHub repos orq works on.")
app.add_typer(project_app, name="project")


@project_app.command("add")
def project_add(
    source: str = typer.Argument(..., help="owner/repo, or a local folder whose origin is on GitHub."),
    name: str | None = typer.Option(None, "--name", help="Display name (default: the repo name)."),
    base: str | None = typer.Option(None, "--base", help="Base branch (default: the repo's default branch)."),
    check: str | None = typer.Option(None, "--check", help="Default check command for new tasks."),
    max_concurrent: int | None = typer.Option(None, "--max-concurrent", help="Active runs at once for this project."),
) -> None:
    """Register a project (or update one). orq always works in its own clone, never in the given folder."""
    from orq.core.projects import ProjectError, add_project

    paths = OrqPaths.from_env()
    try:
        project = add_project(Store(paths.db), GitManager(), source, name=name, base_branch=base, check_command=check,
                              max_concurrent=max_concurrent)
    except ProjectError as exc:
        typer.secho(str(exc), fg=typer.colors.RED)
        raise typer.Exit(1)
    typer.echo(f"project {project.repo} ({project.name}), base {project.base_branch}")


@project_app.command("candidates")
def project_candidates() -> None:
    """Repos to add: your local checkouts under projects.scan_roots first, then GitHub repos not on this PC."""
    from orq.core.discovery import candidates
    from orq.core.settings import resolve

    paths = OrqPaths.from_env()
    store = Store(paths.db)
    eff = resolve(load_config(paths.config), store.get_settings(), {}, {})
    found = candidates(store, GitManager(), eff["projects.scan_roots"], eff["projects.scan_depth"])
    typer.echo(f"On this PC ({', '.join(found['roots']) or 'no projects.scan_roots set'}):")
    for r in found["local"]:
        marks = " ".join(m for m in ("private" if r["private"] else "", "added" if r["added"] else "") if m)
        typer.echo(f"  {r['repo']:<40} {r['path']}  [{r['branch'] or 'detached'}, {r['dirty']} uncommitted] {marks}".rstrip())
    typer.echo("Only on GitHub:")
    if found["github_error"]:
        typer.secho(f"  gh failed: {found['github_error']}", fg=typer.colors.RED)
    for r in found["github"]:
        marks = " ".join(m for m in ("private" if r["private"] else "", "added" if r["added"] else "") if m)
        typer.echo(f"  {r['repo']:<40} {marks:<14} {r['description']}".rstrip())


@project_app.command("list")
def project_list() -> None:
    """List the projects with their defaults."""
    paths = OrqPaths.from_env()
    config = load_config(paths.config)
    projects = Store(paths.db).list_projects()
    if not projects:
        typer.echo("no projects")
        return
    for p in projects:
        cap = p.max_concurrent if p.max_concurrent is not None else config.queue.project_concurrency
        typer.echo(f"{p.repo:<40} {p.name:<24} base {p.base_branch:<10} max {cap}  check: {p.check_command or '-'}")


@app.command()
def rollback(run_id: str, to: int = typer.Option(..., "--to", help="Iteration to roll back to (0 = base commit).")) -> None:
    """Reset a stopped run's worktree to an earlier iteration; `orq resume` then continues from there."""
    runner = _resume_runner(run_id, wait_for_answers=False)
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
        limit = load_config(paths.config).limits.max_concurrent_runs
        queued = sum(1 for r in runs if r.state is RunState.QUEUED)
        typer.echo(f"slots {store.held_slots()}/{limit} in use, {queued} queued")
        for item in runs:
            typer.echo(f"{item.run_id}  {item.state.value:<22} iter {item.iteration:<3} {item.repo}  {item.task_title}")
        return
    item = store.get_run(run_id)
    if item is None:
        typer.secho(f"no run {run_id}", fg=typer.colors.RED)
        raise typer.Exit(1)
    typer.echo(f"{item.run_id}  {item.state.value}  iteration {item.iteration}")
    typer.echo(f"repo {item.repo}  branch {item.branch}\nworktree {item.worktree}\ntask {item.task_title}")
    cp = Checkpoint.try_load(paths.run_dir(run_id) / "state.json")
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
