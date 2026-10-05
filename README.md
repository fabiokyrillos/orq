# orq

Local orchestrator that runs a Claude Code (implementer) and Codex (reviewer) loop on a GitHub repo until the task is merged, pausing only for owner decisions.

See `docs/SPEC.md` for the specification and `docs/phase0-findings.md` for the environment validation.

```
uv sync
uv run pytest -q
uv run orq --version
```
