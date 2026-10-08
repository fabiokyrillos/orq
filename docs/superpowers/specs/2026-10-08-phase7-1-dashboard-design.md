# Phase 7.1 design: dashboard for daily use

Date: 2026-10-08. Asked by the owner after using the Phase 7 dashboard.

Decisions taken with the owner (2026-10-08):

* **D1** Every dashboard text is in Brazilian Portuguese (labels, buttons, hints, empty states, state names). Code, docs, commits, the CLI and the API's error texts stay English. Machine keywords stay as they are (`APPROVE`, `DENY`, IDs, `approve`/`deny`/`abort`). `CLAUDE.md` records the extended exception.
* **D2** A project can be **pinned** (a star; pinned projects come first), **archived** (hidden in a collapsed "Arquivados" group, kept with its clone and settings, restorable) or **removed**. Removing deletes orq's clone and the worktrees of the project's runs, and hides the project. It never touches the owner's folder. Run history stays under "Todos os runs". Adding the project again brings it back. Archive and remove are refused while a run of the project is active, waiting or queued; an archived project takes no new tasks.
* **D3** Each project gets a **Conversa** tab: the owner chats with Claude (Opus by default) about the project.
  * Read only: `--tools Read,Grep,Glob`.
  * It reads a dedicated checkout of the project at the latest `origin/<base>`.
  * One Claude session per conversation, resumed on each message. Answers in Portuguese.
  * Never edits, never starts runs.
* **D4** A **Uso** view with three levels: overall, per project and per run (the run summary has it already). It shows:
  * tokens by day, project, role and model, chat included;
  * the latest plan limits: Codex 5 h and weekly percentages, and Claude's rate limit status;
  * how often Claude compacted a context.
* **D5** The global slot count (`limits.max_concurrent_runs`, 1 to 6) and the default per-project count (`queue.project_concurrency`) are settings editable in the dashboard, applied at the next dispatcher tick. A hint warns that ChatGPT Plus limits are tight.
* Also: the side rail stays fixed while the page scrolls; Add project can add several repos at once; the settings pages are split into sections.

## 1. Projects: pin, archive, remove

* `projects` gains `status` (`active` | `archived` | `removed`, default `active`) and `pinned` (0/1), migrated in place.
* `Store.list_projects()` keeps returning every row. The API adds `status` and `pinned`, and leaves out removed projects unless `?all=1`.
* `core/projects.py`:
  * `set_pinned(store, repo, pinned)`;
  * `archive_project` and `restore_project`;
  * `remove_project(store, paths, config, repo)`, which refuses while a run of the project is not finished (`DONE`, `FAILED`, `ABORTED`), deletes the worktrees of the project's runs, then `~/.orq/repos/<owner>/<repo>` and its lock, and sets `status = removed`, `local_path = NULL`.
  * `add_project` on an archived or removed project sets it `active` again.
* Endpoints: `POST /api/projects/{o}/{r}/pin`, `/archive`, `/restore`, and `DELETE /api/projects/{o}/{r}`.
* `POST /api/tasks` refuses a project that is not active.
* Rail order: pinned first (★), then by name; archived projects sit in a collapsed group at the bottom.

## 2. Slots

* `limits.max_concurrent_runs` (1 to 6) and `queue.project_concurrency` (1 to 6) join the settings keys as global only.
* `settings.slot_limits(config, store) -> (global, per_project)` is read by the dispatcher on every tick, by a run when it takes a slot, by `/api/queue`, by the project data and by `orq status`.

## 3. Usage

`core/usage.py: collect(paths, store, project=None) -> dict`, pure apart from reading run dirs and chat logs:

* calls: every role event (`planner`, `implementer`, `reviewer`) in `events.jsonl` and every chat message log, with day, project, role, model, input and output tokens (input counts Claude's cache tokens, as the summary does);
* totals by day, by project, by role and by model;
* `compactions`: `system`/`compact_boundary` lines in the implementer streams and chat logs;
* `limits.codex`: the latest `rate_limit` snapshot with `primary`/`secondary` `used_percent` and `resets_at`;
* `limits.claude`: the latest `rate_limit_info` (`status`, `resetsAt`, `rateLimitType`).

`GET /api/usage?project=`. A "Uso" link in the rail (overall) and a "Uso" tab in each project.

## 4. Chat

* `core/chat.py`. Chats live in `~/.orq/chats/<owner>__<repo>/<chat_id>/`:
  * `meta.json`: title, session id, model, created and updated;
  * `messages.jsonl`: role, text, ts, usage;
  * `claude.stream.jsonl`.
* Checkout: `<worktree_root>/<repo name>/_chat`, a detached worktree of orq's clone. Before each message, orq takes the clone's lock, fetches, and checks out `origin/<base>` again. A fixed path keeps the Claude session resumable.
* The agent is `ClaudeChat`: `claude -p --output-format stream-json --verbose` with the isolation flags, `--tools Read,Grep,Glob`, `--model`, `--append-system-prompt` (read-only, Portuguese, about this repo), and `--session-id` or `--resume`.
* Endpoints:
  * `GET /api/projects/{o}/{r}/chats` (list);
  * `GET .../chats/{id}` (messages);
  * `POST .../chats` (`{message, chat_id?, model?}`), which answers when Claude is done; one message at a time per chat;
  * `DELETE .../chats/{id}`.
* The usage view counts chat tokens under role `chat`.

## 5. Dashboard

* Rail: fixed (`position: fixed` with its own scroll); pinned and archived groups; links "Todos os runs", "Uso", "Adicionar projeto", "Configurações".
* Settings: sections as tabs (Modelos, Guard, Respostas automáticas, Execução, Pastas); a project's settings show only the sections that apply.
* Add project: checkboxes and "Adicionar selecionados", which adds them one by one and reports each.
* All texts in Portuguese; `<html lang="pt-BR">`.

## 6. Exit criteria (real use, hub on the owner's PC)

* Pin, archive and restore a sandbox project. Remove one and add it back (its clone is recreated at the next run).
* Slots changed to 3 in the dashboard; the queue shows 3; then back to 2.
* A chat about a sandbox answers in Portuguese from the code, and a follow-up message keeps the context.
* A sandbox run merges, and the usage view shows it overall and per project.
* The rail stays fixed while a long page scrolls.
