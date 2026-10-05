# Phase 0 findings

**Date:** 2026-10-05
**Machine:** Windows 11 Pro 10.0.26200, native (PowerShell 5.1), Claude Code 2.1.219, Codex CLI 0.139.0
**Status:** COMPLETE. Every Phase 1 dependency is confirmed or has a documented workaround.

Probe scripts live in `probes/phase0/`. Raw outputs are in `%USERPROFILE%\.orq\phase0\out\` (not in the repo). Probes call the CLIs through `subprocess` argument lists with no shell, as orq will. Quota used: 22 Claude calls, 6 Codex calls (16 percent of the Codex 5-hour window).

## Summary

| # | Item | Result |
|---|---|---|
| 0 | Preflight: versions, executables | PASS with findings |
| 1 | `claude -p` headless | PASS. All sub-items confirmed |
| 2 | `PreToolUse` hook | PASS. Both deny formats, `--settings` attachment, allow token, hooks fire under bypass |
| 3 | `codex exec` | PASS with spec changes |
| 4 | Claude as read-only reviewer | PASS, including `--json-schema` structured output |
| 5 | Usage limit formats | PASS for Claude (real occurrences plus a proactive stream event). PARTIAL for Codex (proactive snapshot real, error text unverified) |
| 6 | `gh` auth, PR, checks, merge | PASS with findings (checks race, merge does not wait for CI) |
| 7 | `gitleaks` | PASS |
| 8 | Worktree with long paths | PASS for git. FAIL for non-git tools while `LongPathsEnabled=0`; workaround documented |

## 0. Preflight

```
claude --version        -> 2.1.219 (Claude Code)
codex --version         -> codex-cli 0.139.0
gh --version            -> 2.102.0
git --version           -> 2.53.0.windows.2
python --version        -> 3.13.12
uv --version            -> 0.12.23
gitleaks version        -> 8.30.1   (already installed via winget; no install needed)
```

* **`claude` and `codex` on PATH are npm shims** (`%APPDATA%\npm\claude.cmd`, `codex.cmd`), not executables. `asyncio.create_subprocess_exec` cannot launch a `.cmd` without `cmd.exe`, which mangles quotes, `%`, `^`, `&` and newlines in prompts.
  * `claude.cmd` only forwards to `%APPDATA%\npm\node_modules\@anthropic-ai\claude-code\bin\claude.exe`. **Workaround (used by all probes):** call that `claude.exe` directly.
  * `codex.cmd` runs `node %APPDATA%\npm\node_modules\@openai\codex\bin\codex.js`. **Workaround:** call `node codex.js` directly.
  * A prompt with quotes, `%`, `^`, `&` and a newline passed on stdin to `claude -p` came through intact (`c1b-stdin`). `codex exec` prints `Reading additional input from stdin...` and waits on an open stdin, so always close it or feed it.
* **`HKLM\...\FileSystem\LongPathsEnabled` is 0**, and `core.longpaths` is not set globally. See item 8.
* **`core.autocrlf=true` globally.** Every commit in the probes warned `LF will be replaced by CRLF`. orq worktrees should set `core.autocrlf=false` so diffs on public repos stay clean.
* **`gh auth status`:** logged in as `fabiokyrillos`, scopes `gist, read:org, repo, workflow`. PASS.
* **Environment leakage:** a parent Claude Code session injects about 25 `CLAUDE*` variables plus `ANTHROPIC_BASE_URL`. Probes strip every `CLAUDE*` and `ANTHROPIC*` variable; orq must do the same.
* **Standalone CLI login is separate from the desktop app.** Before the owner ran `claude auth login`, every `claude -p` call failed with `Failed to authenticate: OAuth session expired and could not be refreshed` (exit 1, `is_error: true`, `subtype: "success"`, `terminal_reason: "api_error"`). orq must check `claude auth status` (`loggedIn: true`) before a run.

## 1. `claude -p` headless: PASS

All calls: `claude.exe -p ... --model sonnet` from the scratch repo, unless stated.

| Probe | Command | Result |
|---|---|---|
| JSON output | `-p "Reply with exactly: OK" --output-format json` | One JSON object, exit 0. Keys: `type, subtype, is_error, result, session_id, num_turns, usage, modelUsage, permission_denials, total_cost_usd, terminal_reason, api_error_status, stop_reason, duration_ms, ...`. `result: "OK"` |
| Error detection | (failed call before login) | `subtype: "success"` with `is_error: true` and exit 1. **Test `is_error` and the exit code, never `subtype`** |
| Stream output | `--output-format stream-json --verbose` | 22 lines, every one parses on its own. Order: hook events, `system/init`, `assistant`, `rate_limit_event`, `result` |
| `init` event | | Keys: `session_id, model, tools, mcp_servers, plugins, skills, slash_commands, agents, permissionMode, cwd, claude_code_version, ...` |
| Session capture | | `session_id` is in `init` and in `result` |
| Resume with context | `-p "What was the codeword?" --resume <sid>` | `PINEAPPLE-42`. Same `session_id` after resume |
| `--model` on resume | `--resume <sid> --model sonnet` on a session created with `opus` | `init.model` went `claude-opus-5` to `claude-sonnet-5`, context kept |
| Resume from another cwd | same `--resume` from `%USERPROFILE%\.orq\phase0` | FAIL by design: `No conversation found with session ID: ...`, exit 1. **Sessions are bound to the cwd; always resume from the run's worktree** |
| `--session-id <uuid>` | | Returned `session_id` equals the requested UUID. orq can assign IDs itself |
| `--append-system-prompt` | `"End every reply with the exact token ORQ-MARK-7."` | Marker present. On `--resume` **without** the flag the marker is gone: the append is per call, not stored. Pass it on every call |
| Decision marker | section 8.3 rule via `--append-system-prompt`, ambiguous task, `--permission-mode acceptEdits` | No edit was made. Final message ends with a fenced `orq-decision` block that parses: `{"decision_type": "ambiguity", "question": "...", "options": [3 items], "recommendation": 0}` |

### Permission modes (task: write a file, then run `python --version`)

| Flags | Outcome |
|---|---|
| none (`default`) | Write denied, listed in `permission_denials` (`tool_name`, `tool_use_id`, `tool_input`); Bash ran. No hang, exit 0 |
| `--permission-mode acceptEdits --allowedTools "Bash(python *)"` | Both ran |
| `--permission-mode dontAsk` | Write denied, Bash ran. Same shape as default |
| `--dangerously-skip-permissions` | Both ran |

No mode waited for input. **Decision: implementer runs with `--dangerously-skip-permissions` plus the guard hook (item 2).**

### Isolation from the owner's global setup (`--include-hook-events`)

| Flags | tools | MCP servers | plugins | hook events | context tokens |
|---|---|---|---|---|---|
| none | 146 | 10 (context-mode, claude-mem, 8 claude.ai connectors) | 5 | 26 (`SessionStart`, `UserPromptSubmit`, `Stop`) | about 58k |
| `--setting-sources project,local --strict-mcp-config` | 32 | 0 | 0 | 0 | about 9k |
| `--safe-mode` | 32 | 0 | 5 listed | 0 | about 5k |

**Decision: every orq call uses `--setting-sources project,local --strict-mcp-config`.** Auth still works with it. `--safe-mode` is unusable because it also disables the guard hook (see item 2). `--bare` is unusable because its help says OAuth is never read.

Caveat: `project,local` still loads `.claude/settings*.json` from the worktree, so a repo (or the implementer) can add hooks and permissions of its own. orq's `--settings` file is loaded on top; the post-execution diff rules should treat `.claude/**` as a protected path.

## 2. `PreToolUse` hook: PASS

Settings file passed with `--settings`:

```json
{"hooks": {"PreToolUse": [{"matcher": "Bash|Write|Edit",
  "hooks": [{"type": "command", "command": "\"C:/Users/fabin/.../python.exe\" \"C:/dev/orq/probes/phase0/guard_probe.py\""}]}]}}
```

Prompt: run `git reset --hard HEAD~1` once. `guard_probe.py` denies it unless an allow token exists.

| Variant | Hook fired | Reset executed | Notes |
|---|---|---|---|
| exit code 2 + stderr, `--dangerously-skip-permissions` | yes | no | Tool result to the model: `PreToolUse:Bash hook error: [<command>]: <reason>`; the reason reaches the model, prefixed with the hook command line |
| JSON `permissionDecision: deny`, bypass | yes | no | Tool result is exactly the reason. **Preferred format** |
| JSON deny, `--permission-mode acceptEdits --allowedTools "Bash(git *)"` | yes | no | Hook wins over the allowlist |
| hooks in `<worktree>\.claude\settings.local.json` instead of `--settings` | yes | no | Works, but the file sits in a public repo's worktree. `--settings` is preferred |
| allow token present, bypass | yes (`allow-by-token`) | yes | Token consumed by the hook. Section 10.3 mechanism confirmed |
| `--setting-sources project,local --strict-mcp-config`, bypass | yes | no | **Isolation flags keep the `--settings` hook** |
| `--safe-mode`, bypass | **no** | **yes** | `--safe-mode` disables every hook, including ours. Never use it |

Hook facts:

* stdin payload keys: `cwd, effort, hook_event_name, permission_mode, prompt_id, session_id, tool_input, tool_name, tool_use_id, transcript_path`.
* `ORQ_RUN_DIR` set on the `claude` process reached the hook (`run_dir_from_env: true`), so the hook can find the run's token directory and log without any path baked into settings.
* The hook command runs through Git Bash (`SHELL=C:\Program Files\Git\bin\bash.exe`, `MSYSTEM=MINGW64`); forward-slash absolute paths in double quotes work. The explicit `python.exe` path avoids PATH surprises.
* UTF-8 reason text (`café ✓`) survived stdin and stdout.
* Every denial also appears in the final `result.permission_denials` with the full `tool_input`, so orq has two independent signals: the hook's own log and the result object.
* Each `claude -p` call with the hook took 15 to 36 s; the hook itself is not the bottleneck.

## 3. `codex exec`

All calls: `node codex.js -m gpt-5.5 exec ...`, run from outside the target repo with `-C <repo>`.

### 3.1 Model must be explicit

```
codex exec --json --sandbox read-only -C <scratch> --output-schema schema.strict.json ...      exit 1
{"type":"error","message":"{\"type\":\"error\",\"status\":400,\"error\":{\"type\":\"invalid_request_error\",
 \"message\":\"The 'gpt-6-sol' model is not supported when using Codex with a ChatGPT account.\"}}"}
{"type":"turn.failed","error":{...}}
```

`~/.codex/config.toml` has `model = "gpt-6-sol"` (shared with the Codex desktop app). CLI 0.139.0 lists only `gpt-5.5` for this account. **Workaround:** orq always passes `-m` from its own config. FAIL without the flag, PASS with `-m gpt-5.5`.

### 3.2 JSON events, strict schema, read-only sandbox: PASS

```
codex -m gpt-5.5 exec --json --sandbox read-only -C <scratch> --output-schema schema.strict.json
      -o x1-last.json -c model_reasoning_effort="low" "<review prompt + try to write codex_probe.txt>"     exit 0, 69 s

{"type":"thread.started","thread_id":"01a10da5-f146-77a2-9673-e6e313b07f2a"}
{"type":"turn.started"}
{"type":"item.completed","item":{"type":"agent_message","text":"{\"status\":\"continue\",...}"}}
{"type":"item.completed","item":{"type":"command_execution","command":"...powershell.exe -Command 'git log --oneline -3'","exit_code":0,...}}
{"type":"item.completed","item":{"type":"command_execution","command":"... 'Set-Content -Path codex_probe.txt -Value x'","exit_code":1,
   "aggregated_output":"Set-Content : O acesso ao caminho '...codex_probe.txt' foi negado."}}
{"type":"item.completed","item":{"type":"agent_message","text":"{\"status\":\"done\",...,\"next_prompt\":null,\"issues\":[],\"human\":null}"}}
{"type":"turn.completed","usage":{"input_tokens":86381,"cached_input_tokens":58496,"output_tokens":494,"reasoning_output_tokens":74}}
```

* Read commands (`git log`, `git diff`) worked. The write was denied by the sandbox; the file was not created and `git status --porcelain` stayed empty. The sandbox ran with `[windows] sandbox = "elevated"` from the owner's config, with no prompt.
* **Finding: every `agent_message`, including intermediate progress notes, is forced into the schema.** orq must take only the last one. `-o <file>` writes exactly that.
* **Finding: the owner's global Codex config is expensive.** A trivial review used 86k input tokens, intermediate notes came back in Portuguese, and stderr shows an MCP auth failure and `failed to load models cache: missing field base_instructions`. Plugins, hooks and MCP servers from `config.toml` all load. `--ignore-user-config` exists ("auth still uses `CODEX_HOME`") and should fix this, but running it was blocked by the session's permission classifier, so it is untested. Phase 1 should try it first; if it also drops `[windows] sandbox`, pass `-c windows.sandbox="elevated"`.

### 3.3 Loose schema is rejected: spec change

```
codex ... --output-schema schema.loose.json ...       exit 1
"code":"invalid_json_schema","message":"Invalid schema for response_format 'codex_output_schema': In context=(),
 'additionalProperties' is required to be supplied and to be false."
```

SPEC 8.2 as originally written (optional `human`, no `additionalProperties`) does not work. The strict form that passed: every object has `additionalProperties: false`, every property is listed in `required`, optional values are nullable (`"type": ["string","null"]`, `"type": ["object","null"]`). The exact schema is `STRICT_SCHEMA` in `probes/phase0/probe_codex.py`.

### 3.4 Resume: PASS

```
codex -m gpt-5.5 exec resume 01a10da5-... --json --output-schema schema.strict.json
      -c sandbox_mode="read-only" -c model_reasoning_effort="low" "What was the codeword? ..."      exit 0, 49 s
-> same thread_id, summary "Codeword: MANGO-17. Write attempt ... blocked with PermissionDenied."
```

* `exec resume` accepts `--output-schema`, `--json`, `-o`, `-m`, `-c`. It has **no `--sandbox` and no `-C`**: use `-c sandbox_mode="read-only"` and run it with the repo as cwd.
* Cost: the resumed turn used 147k input tokens (117k cached). A stateless reviewer (fresh `codex exec` per iteration) is cheaper and is what section 12 already implies.

### 3.5 Reasoning effort: accepted, effect not demonstrated

```
-c model_reasoning_effort="low"    exit 0   reasoning_output_tokens 134
-c model_reasoning_effort="high"   exit 0   reasoning_output_tokens 16
-c model_reasoning_effort="bogus"  exit 1   "Invalid value: 'bogus'. Supported values are: 'none', 'minimal', 'low', 'medium', 'high', 'xhigh', and 'max'."
```

The override reaches the API (the invalid value is rejected server-side). On a one-line question the token counts did not order as expected, so this probe does not prove that `high` thinks more. The control mechanism is PASS; the benefit is unmeasured.

## 4. Claude as read-only reviewer: PASS

```
claude.exe -p "<try to create hacked.txt and run git status, then review app.py>"
   --model opus --tools "Read,Grep,Glob" --strict-mcp-config --json-schema <section 8.2 strict schema>
   --output-format stream-json --verbose                                                     exit 0, 63 s
init.tools = ["Glob", "Grep", "Read", "StructuredOutput"]     init.mcp_servers = []
result.structured_output = {"status": "done", "summary": "... creating hacked.txt failed because the Write tool is
   disabled ... git status failed because the Bash tool is disabled ...", "next_prompt": null, "issues": [...], "human": null}
```

* `hacked.txt` was not created and `git status --porcelain` stayed empty. The model reported the tools as `No such tool available`.
* `--json-schema` works in print mode: the validated object arrives in `result.structured_output`, and the same strict schema used for Codex was accepted. **This upgrades the spec's "prompt plus validation" fallback to schema enforcement on both reviewers.**
* Without Bash the reviewer cannot run `git diff`; orq writes `diff.patch` into the run dir and passes its path (the reviewer can `Read` it, and `--add-dir` can expose the run dir if needed).

## 5. Usage limit formats

Searched 866 local Claude transcripts and 312 Codex session files (`find_limit_messages.py`).

### Claude: real occurrences (61), plus a proactive stream event

```
You've hit your session limit · resets 10pm (America/Cayenne)
You've hit your session limit · resets 5:10am (America/Cayenne)
```

Transcript entry fields: `"type":"assistant"`, `"isApiErrorMessage":true`, `"error":"rate_limit"`. Proposed detector: `error == "rate_limit"`, or text matching `^You've hit your .* limit · resets (.+) \((.+)\)$`. The reset time is a local clock time plus an IANA zone, with no date. Expect `is_error: true`, exit 1 and this text in `result` on a `-p` call; capture the full object on the first real occurrence.

Every successful `stream-json` run also emits, right before `result`:

```json
{"type":"rate_limit_event","rate_limit_info":{"status":"allowed","resetsAt":1791239400,"rateLimitType":"five_hour",
 "overageStatus":"rejected","overageDisabledReason":"org_level_disabled","isUsingOverage":false}}
```

So orq learns the limit window and reset time on every call, before anything fails.

Other real error texts, for the "not a limit" branch: `API Error: 529 Overloaded...` (`error: "server_error"`, retryable), `API Error: Connection lost mid-response...`, `Failed to authenticate: OAuth session expired and could not be refreshed`.

Side finding: the history holds about 25,700 `OAuth session expired` errors. Something on this machine keeps launching the standalone CLI while it is logged out.

### Codex: structured snapshot real, error text unverified

No real limit error exists in local history. Every `token_count` event in the session file (`~/.codex/sessions/YYYY/MM/DD/rollout-<ts>-<thread_id>.jsonl`) carries a snapshot:

```json
{"limit_id":"codex","primary":{"used_percent":16.0,"window_minutes":300,"resets_at":1791238480},
 "secondary":{"used_percent":17.0,"window_minutes":10080,"resets_at":1791598154},
 "plan_type":"plus","rate_limit_reached_type":null}
```

This is not in the `--json` stdout stream. orq can read it from the session file after each reviewer call and switch to the Claude reviewer **before** the limit is hit (for example at 90 percent), then switch back after `resets_at`. The reactive path is the generic one seen in 3.1: an `error` event followed by `turn.failed` and exit code 1. The exact limit message text is still to be captured from docs or a real occurrence; until then any `turn.failed` with `rate_limit_reached_type` set in the snapshot, or a message containing `usage limit`, counts as a limit.

## 6. `gh`: PASS with findings

Sandbox repo `fabiokyrillos/orq-phase0-sandbox` (public, created by the owner; kept for Phase 1). Run from a worktree at `%USERPROFILE%\.orq\worktrees\orq-phase0-sandbox\R0GH` on branch `orq/phase0-probe`, exactly as orq will.

```
git push -u origin orq/phase0-probe                                                   exit 0
gh pr create --base main --head orq/phase0-probe --title ... --body ...               exit 0  -> .../pull/1
gh pr checks 1 --json name,state,bucket     (first 60 s)      exit 1  "no checks reported on the 'orq/phase0-probe' branch"
gh pr checks 1 --json name,state,bucket     (then)            exit 0  [{"bucket":"pending","name":"check","state":"QUEUED"}]
gh pr view 1 --json state,mergeable,mergeStateStatus          {"mergeStateStatus":"UNSTABLE","mergeable":"MERGEABLE","state":"OPEN"}
gh pr merge 1 --squash --delete-branch       (from the worktree)                      exit 0
   ! Branch orq/phase0-probe is checked out in the current worktree (...R0GH); skipping local delete
gh pr view 1 --repo <slug> --json state,mergeCommit            {"state":"MERGED","mergeCommit":{"oid":"99ad6ef2..."}}
git ls-remote --heads origin                                   only refs/heads/main   (remote branch deleted)
git worktree remove --force <path>; git branch -D orq/phase0-probe                    exit 0
```

Findings:

* **Checks race:** for about 60 s after PR creation `gh pr checks` exits 1 with `no checks reported`. orq must treat that as "pending", not as failure.
* **Queue time:** the free-runner job stayed `QUEUED` for more than 5 minutes (the probe's poll deadline) and the three runs were still queued when this was written. The merge-gate poll needs a long timeout (tens of minutes) and `poll_seconds` around 20 to 30.
* **`gh pr merge` does not wait for checks.** With no branch protection on the sandbox it merged while CI was `QUEUED` (`mergeStateStatus: UNSTABLE`). The gate in section 10.7 is orq's responsibility and must run before calling `gh pr merge`.
* **Merge from a worktree works.** `--delete-branch` deletes the remote branch, skips the local delete with a warning, and still exits 0. orq then removes the worktree and the local branch itself, which it does anyway.
* `gitleaks git --log-opts=origin/main..HEAD` ran as the pre-push scan on the worktree: exit 0, 1 commit scanned.

## 7. `gitleaks`: PASS

```
gitleaks git --pre-commit --staged --redact --no-banner --report-format json --report-path g1-leaks.json <repo>
  WRN leaks found: 1                     exit 1
  report: [{"RuleID":"github-pat","File":"leaky_config.py","StartLine":1,"Secret":"REDACTED"}]

gitleaks git --pre-commit --staged --redact --no-banner <repo>          (clean staging area)
  INF no leaks found                     exit 0

gitleaks git --log-opts=HEAD~1..HEAD --redact --no-banner <repo>        (commit range, the pre-push form)
  INF 1 commits scanned. no leaks found  exit 0
```

Exit code 1 means leaks, 0 means clean. Each scan takes about 0.5 s. The secret was a synthetic `ghp_` token generated at runtime, never committed or pushed. Pre-push form for orq: `--log-opts=origin/<base>..HEAD`.

## 8. Worktree with long paths

```
git worktree add -b orq/phase0-longpath %USERPROFILE%\.orq\worktrees\orq-phase0-sandbox\R0TEST main
```

| Case | Result |
|---|---|
| `core.longpaths` unset, file path 269 chars | FAIL as expected: `error: unable to create file ...`, exit 128, no worktree left behind |
| `core.longpaths=true` (repo config) | PASS: checkout, `git status`, `git worktree remove --force` all work |
| `gitleaks dir` on the worktree | PASS |
| Python `Path.read_text()` on the deep file | FAIL: `FileNotFoundError`. Works with the `\\?\` prefix |
| PowerShell 5.1 `Get-Content` (what Codex uses to read files) | FAIL: `Cannot find path` |

Git is fine with the per-repo setting. Everything else breaks past 260 characters while `LongPathsEnabled=0`. The spec's worktree root costs 55 characters before the repo's own paths start. **Workarounds, both recommended:** the owner sets `LongPathsEnabled=1` (admin, one time), and orq gets a configurable `worktree_root` so a short root such as `C:\orq-wt` can be used. Decision pending with the owner.

## Spec changes made with these findings

* Section 3: CLI invocation rules (real executables, stdin prompts, env scrub, isolation flags), explicit Codex model, standalone CLI login prerequisite.
* Section 4: Codex resume sandbox override; Claude reviewer uses `--json-schema`.
* Section 8.2: strict schema rules on both reviewers; take only the final reviewer message.
* Section 10.3: hook attached via `--settings`, JSON deny format, `ORQ_RUN_DIR`, never `--safe-mode`; `.claude/**` protected.
* Section 10.5: exact gitleaks commands and exit codes.
* Section 10.6: long path prerequisite, configurable worktree root, `core.autocrlf=false`.
* Section 10.7: `gh pr merge` does not wait for CI; checks race; merge from the worktree.
* Section 12: real Claude limit message and `rate_limit_event`; Codex `rate_limits` snapshot for proactive switching.
* Section 13: `reviewer.codex_model`, `reviewer.switch_at_used_percent`, `git.worktree_root`, `notify.poll_seconds` note.

## Open items carried into Phase 1

1. **`LongPathsEnabled`** (owner decision): enable it, or rely on a short `worktree_root`.
2. **`codex --ignore-user-config`**: untested here; try it first in Phase 1 to cut the 86k-token baseline.
3. **Codex usage-limit error text**: capture from the first real occurrence.
4. ~~Sandbox CI runs still queued~~ Resolved: the `pull_request` run passed (`ci #2`, 10m40s, almost all of it queue time). Both `push` runs to `main` ended as `conclusion: failure` after exactly 15 minutes with the job `cancelled`, zero steps, empty `runner_name`, and the annotation `The job was not acquired by Runner of type hosted even after multiple attempts`. That is GitHub runner capacity, not the workflow. orq's merge gate must tell this apart from a real check failure: a run whose jobs have no steps and carry that annotation is an infrastructure failure and gets `gh run rerun <id>` (bounded retries), not a new implementer iteration.
