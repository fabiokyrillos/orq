# Phase 7 findings: projects from the owner's local folders

Date: 2026-10-08. Design: `docs/superpowers/specs/2026-10-08-phase7-local-folders-design.md`. Plan: `docs/superpowers/plans/2026-10-08-phase7-local-folders.md`.

## 1. Why

The owner's repos are already cloned under `D:\Projetos\GitHub`. orq downloaded every repo again into `~/.orq/repos`, and adding a project meant typing `owner/repo` or a path. The owner asked for two things:

* orq uses the local folders;
* adding a project offers the folders on this PC first, and the owner's GitHub repos only when a repo is not on the PC.

Constraint: orq must not disturb the owner's work in those folders (uncommitted changes, branches, repo config).

## 2. What the owner's folders look like (read-only inspection)

* 9 checkouts, several one level below the root (`Volei do Binho\volei-do-binho`, `FaCa\casa-amor-fluxo`). A scan needs depth 2 to 3.
* The volleyball repo already has three linked worktrees from other tools:
  * `~/.codex/worktrees/...` (Codex desktop);
  * `.claude/worktrees/...` (Claude Code);
  * `.worktrees/...`.

  Its main folder is on a feature branch, and `extensions.worktreeConfig` is on. `my-brain-navigation-performance` is a linked worktree of `my-brain`.
* The volleyball repo is **private**. SPEC section 3 said target repos are public. Nothing in the code required that; the only real difference is that GitHub Actions on a private repo spends the plan's minutes. SPEC updated (owner's decision D3).
* `Wardrobe\wardrobe` has `origin` = `tandpfun/wardrobe` (the upstream), while the owner's fork `fabiokyrillos/wardrobe` is a separate GitHub repo. The picker shows what `origin` says, so that folder is listed under the upstream's name and the fork under "Only on GitHub". Adding the folder would target a repo the owner cannot push to; `gh repo view` accepts it because it is public. Left as is; noted for the owner.

## 3. Why orq does not work inside the owner's folder

Worktrees directly in the owner's repo would change it, which the owner ruled out:

* `create_worktree` runs `git config core.autocrlf false` and `core.longpaths true`. In a worktree that writes the shared repo config.
* Branches `orq/*` would mix with the owner's branches.
* `branch -D`, `fetch --prune` and `worktree prune` would run on the owner's repo.
* The repo's own hooks would run on orq's commits.
* `<clone>.orq.lock` would be created next to the owner's folder.
* git locks would be contended with the owner's editor and with the Codex and Claude Code worktrees.

So orq keeps its own clone and seeds it from the folder: `git clone --reference <git dir> --dissociate <GitHub URL>` (decision D1). See SPEC 10.6.

## 4. What changed

* **Seeded clone.** `GitManager.ensure_repo(..., seed, on_clone)`:
  * resolves the seed's git dir; a linked worktree seeds from its main repo;
  * a plain folder inside some other repo does not count (its top level must be the folder itself);
  * a seed that fails (shallow, broken, gone) is dropped, the half-made clone is removed, and a plain clone runs;
  * the run logs `repo_cloned` with `local:<git dir>` or `github`.
* **Discovery** (`core/discovery.py`):
  * scans `projects.scan_roots` breadth first up to `projects.scan_depth`;
  * a `.git` directory is a checkout and the walk stops there; a `.git` file (linked worktree, submodule), hidden folders, `node_modules`, virtualenvs, `dist`, `build` are skipped;
  * reads `origin`, the branch and `git --no-optional-locks status --porcelain` (which does not refresh the owner's index);
  * lists the owner's and organizations' GitHub repos with `gh api --paginate user/repos?affiliation=owner,organization_member`, archived ones left out;
  * marks repos already added and private ones.
* **Settings.** `projects.scan_roots` and `projects.scan_depth` are global-only keys: the project layer refuses them and the project form hides them. The code default is empty; the owner's install has `D:\Projetos\GitHub`.
* **Surfaces.** `GET /api/projects/candidates`, `orq project candidates`, and a new Add project page:
  * "On this PC": repo, folder, branch and uncommitted count, Add (or "Use this folder" for a repo already added);
  * "Only on GitHub": a filter and Add;
  * "Other": the free input as before.

## 5. Finding: concurrent git children stall on Windows

The first version read the 8 checkouts with a thread pool. The scan took 5 to 10 s, in steps of about 5 s, while each git call alone took 40 ms. Tracing showed random calls (`remote get-url`, `branch --show-current`, `status`) taking exactly 5.05 s whenever several git children ran at once in one process.

The checkouts are now read one at a time. The `gh` listing still runs alongside the scan. The whole candidates call takes about 0.9 s.

The same stall may explain why creating a worktree took 12 s in both runs below, which happened at the same time. This was not investigated further; worktree creation already takes the clone's lock.

## 6. Real runs

Setup:
* orq's own clones of both sandboxes were renamed to `*.pre-phase7`, so both runs had to clone again;
* an owner-style checkout of sandbox B was made at `D:\Projetos\GitHub\orq-sandboxes\orq-phase5-sandbox-b`:
  * on branch `owner/local-work`;
  * with an untracked `owner-notes.txt`;
  * with an uncommitted edit to `README.md`.

A snapshot of the folder was taken before the run:
* `.git/config`, `HEAD`, branch, `for-each-ref`, `status`, worktree list, hooks;
* the index hash;
* the sibling folders;
* a hash of every file.

**Picker.** It listed 9 local checkouts, the volleyball repo and the new sandbox folder included, and 15 GitHub-only repos. No linked worktree showed up as a separate entry. Sandbox B was already a project, so its row offered "Use this folder"; clicking it set the project's folder.

**`R59GV9`, sandbox B, "Add a clamp helper":**

```
17:39:02 slot acquired
17:39:06 repo_cloned source=local:D:/Projetos/GitHub/orq-sandboxes/orq-phase5-sandbox-b/.git
17:39:18 worktree created
17:42:15 PR #5 merged (squash)
17:42:16 DONE
```

* orq's new clone has no `objects/info/alternates`, so it is dissociated from the folder.
* The folder snapshot after the run is **identical** to the one before.

**`RJ5DDN`, Phase 0 sandbox, "Add a slugify helper":**

```
17:39:08 slot acquired
17:39:10 repo_cloned source=github
17:39:22 worktree created
17:40:24 planner asks DJCCR
```

* `repo_cloned` says `github`: the sandbox is not on the PC and the project has no folder.
* The planner found that `slug.py` already existed in the sandbox from an earlier test and that it converts accented letters. That conflicted with the task's ASCII rule, so it asked DJCCR.
* The task had been written against orq's renamed clone, whose local `main` was stale: `fetch` only moves `origin/*`. Lesson: write sandbox tasks against `origin/main`.
* The owner answered on WhatsApp ("Aplicar a regra ASCII"), recorded `by owner, via whatsapp`.
* The implementer applied the answer and renamed the two accent tests to expect the ASCII result. The diff guard reported them as removed tests (`removed_test`), asked DTMEX, and recommended `deny`. The owner approved on WhatsApp.
* 17:48:20 PR #11 merged, DONE.

**Guard and the owner's earlier answer.** The diff guard does not know that a removal follows from a decision the owner already answered, so it recommended the opposite of what the owner had chosen. Here a rename counted as a removal. Not changed in this phase; candidate follow-up: pass the run's answered decisions to the guard decision's recommendation, or treat a test whose body moved under a new name as renamed.

## 7. Finding: console windows flashing while the hub works

The owner saw command prompt windows open and close many times on the Add project page.

**Cause.** The hub started at logon runs under `pythonw`, which has no console. Every console program it starts (git, gh, taskkill, a model probe) opens a window of its own. The scan starts about 25 of them. Runs were spawned with `DETACHED_PROCESS`, so they had no console either, and their git, claude, codex and check children could flash the same way.

**Fix.**
* Runs are spawned with `CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW`: a hidden console of their own, which their children inherit.
* The hub's own children get `CREATE_NO_WINDOW` only when the process has no console (`GetConsoleCP() == 0`).

Passing the flag always was tried first. It gives every child a new hidden console, which nearly doubles a git call (32 to 59 ms) and made the test suite take 9.5 minutes instead of 6.7.

## 8. Cleanup

* The owner-style checkout `D:\Projetos\GitHub\orq-sandboxes` was created only for this test. It is kept until the owner says it can go.
* The renamed clones `~/.orq/repos/fabiokyrillos/*.pre-phase7` are no longer used by any run. They are kept until the owner says they can go.
* While the runs went on, the owner added `fabiokyrillos/casa-amor-fluxo` from the picker.
