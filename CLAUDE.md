# CLAUDE.md

## Project

`orq` is a local orchestrator that runs a Claude Code (implementer) ↔ Codex (reviewer) loop on GitHub repos until the task is merged, pausing only for owner decisions. The full specification is in `docs/SPEC.md`. Read it before any work and treat it as the source of truth.

## How to work

* Work **one phase at a time**, in the order defined in `docs/SPEC.md` section 15. Do not start a phase before the previous one meets its exit criteria.
* **Phase 0 comes first and produces no product code.** Only probes and `docs/phase0-findings.md`.
* Before each phase, propose a short plan and wait for approval.
* When a finding contradicts the spec, update `docs/SPEC.md` in the same change and call it out.

## Hard rules

* Never call the Anthropic API or the OpenAI API. Only the local `claude` and `codex` CLIs.
* Windows native. No bash only scripts, no tmux, no WSL assumptions. Use Python for scripts and hooks. Use `pathlib` for paths.
* Everything in English: code, identifiers, comments, docs, commits.
  * Exception (owner's decision, 2026-10-08): texts the owner reads on WhatsApp are in Brazilian Portuguese. That covers the message templates, orq's own decision texts, and the agents' owner-facing fields (`context`, `question`, `option_details`, `recommendation_reason`, `owner_update`). Machine keywords stay as they are (`APPROVE`, `DENY`, `STATUS`, IDs, the `approve`/`deny`/`abort` options the loop parses).
* Never commit secrets. Config secrets come from environment variables.
* Runtime data (logs, worktrees, DB) lives under `%USERPROFILE%\.orq\`, never in this repo.

## Stack

Python 3.12+, `uv`, `typer`, `sqlite3`, `asyncio` subprocesses, `httpx`, `pytest`. `FastAPI` + SSE only from Phase 4.

## Conventions

* Package layout: `src/orq/` with modules `core`, `adapters`, `guard`, `git`, `verify`, `notify`, `store`, `cli`.
* Every CLI adapter behind one interface so the reviewer can be swapped at runtime.
* Tests in `tests/`, run with `uv run pytest -q`. Mock the CLIs in unit tests; real CLI calls only in tests marked `integration`.
* Small commits with clear messages.
