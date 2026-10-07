# Phase 6 findings

**Date:** 2026-10-07
**Status:** IN PROGRESS (milestone 0 done).

Design: `docs/superpowers/specs/2026-10-07-phase6-refinement-design.md`. Plan: `docs/superpowers/plans/2026-10-07-phase6-refinement.md`.

## 0. Codex CLI 0.161.0 probes (milestone 0)

The owner updated the Codex CLI from 0.139.0 to 0.161.0 to use `gpt-6.1-sol`. Claude Code stays at 2.1.219.

### 0.1 Models on a ChatGPT Plus account

On 0.139.0, every newer model was refused:

* `gpt-6.1-sol`, `gpt-6-sol`: "not supported when using Codex with a ChatGPT account";
* `gpt-5.6-sol`: "requires a newer version of Codex".

0.139.0 could not even read `~/.codex/models_cache.json` written by the desktop app (`missing field base_instructions`).

On 0.161.0, one minimal call each (`codex exec -m <model> -c model_reasoning_effort=low --sandbox read-only --skip-git-repo-check --json "Reply with the single word OK."`):

| model | result |
|---|---|
| gpt-6.1-sol | OK |
| gpt-6-sol | OK |
| gpt-6-astra | OK |
| gpt-6-luna | OK |
| gpt-5.6-sol | OK |
| gpt-5.6-terra | OK |
| gpt-5.6-luna | OK |
| gpt-5.5 | OK |

`~/.codex/models_cache.json` lists these models with `visibility: list` and their `supported_reasoning_levels`:

* `low` to `max` for every model;
* `ultra` ("automatic task delegation") on the sol, terra and astra models;
* `gpt-5.5` stops at `xhigh`.

Hidden entries (`gpt-reserve`, `codex-auto-review`) are not offered.

### 0.2 The read-only sandbox broke with the update (FAIL, then fixed)

Through orq's `CodexReviewer` (which passes `--ignore-user-config`), the reviewer on 0.161.0 could not start any process:

* every command failed with `Failed to create unified exec process: helper_unknown_error: setup refresh had errors`;
* the review still validated against the schema, with status `done` and a blocker issue saying it could not read `app.py`;
* the planner said the same in its summary.

What the investigation found:

* **Not orq-specific.** The same error appears in a bare `codex sandbox -- cmd /c python --version`.
* **`codex doctor`:** `✗ sandbox  elevated Windows sandbox provisioning recorded a structured failure`.
* **`~/.codex/.sandbox/setup_error.json`:** `{"code": "helper_unknown_error", "message": "setup refresh had errors"}`.
* **The day's sandbox log names the cause:** `runtime read/execute validation failed: validate runtime read/execute access on C:\Users\<owner>\AppData\Local\OpenAI\Codex\runtimes\cua_node\...\node_repl.exe: open ACL target for root-only update`. The elevated setup of the new CLI tries to grant its sandbox user access to a runtime of the Codex desktop app, and cannot.
* **The update caused it.** The first error is at 19:59 UTC on 2026-10-07, right after the update; the 2026-10-06 log has none.
* **The owner's config was not the fix.** `~/.codex/config.toml` sets `[windows] sandbox = "elevated"`. orq ignores that file, and passing `-c windows.sandbox="elevated"` explicitly fails the same way.

`windows.sandbox` accepts `elevated`, `unelevated` and `mxc` on 0.161.0. Same read-only probe (`python --version`, `python -c "print(1+1)"`, `Get-Content app.py`, `Set-Content probe.txt x`):

| `windows.sandbox` | process start | read app.py | python | write blocked |
|---|---|---|---|---|
| (default, user config ignored) | rejected: "blocked by policy" | no | no | (nothing ran) |
| `elevated` | fails: "setup refresh had errors" | no | no | (nothing ran) |
| `unelevated` | yes | yes | yes, `Python 3.13.12`, `2` | yes, "Access to the path ... is denied" |
| `mxc` | yes | yes | yes, with `Failed to find real location of ...python.exe` on stderr | yes |

**Decision:** the Codex adapter passes `-c windows.sandbox="unelevated"` on every call (`[reviewer].codex_windows_sandbox`, default `unelevated`). This needs no admin provisioning and keeps writes blocked.

Side effect: Python now runs inside the reviewer's sandbox. The Phase 3 hygiene rules for "python is not recognized" complaints (`core/hygiene.py`) stay as a backstop, but the cause they work around is gone (Phase 5 improvement 6).

Schema output (`--output-schema`, both the review and the plan contract) and the rate-limit snapshot from the session file still work on 0.161.0 with `gpt-6.1-sol` (`primary.used_percent`, `resets_at`, `plan_type: plus`).
