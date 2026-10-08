# Phase 7 design: projects from the owner's local folders

Date: 2026-10-08. Asked by the owner before the first real project (the volleyball repo).

The owner's GitHub repos are already cloned under `D:\Projetos\GitHub`, often one level down (`Volei do Binho\volei-do-binho`). orq should use those folders instead of downloading every repo again, and adding a project should offer the folders on this PC first and the owner's GitHub repos only when a repo is not on the PC. orq must not disturb the owner's work in those folders: uncommitted changes, the checked-out branch, the repo config.

What the owner's folders look like (read-only inspection, 2026-10-08):

* 9 repos, several one level below the root.
* The volleyball repo already has three linked worktrees from other tools (`~/.codex/worktrees/...`, `.claude/worktrees/...`, `.worktrees/...`), its main folder is on a feature branch, and `extensions.worktreeConfig` is on. `my-brain-navigation-performance` is a linked worktree of `my-brain`.
* The volleyball repo is private. SPEC section 3 said target repos are public.
* Working in the owner's repo directly would change it: `create_worktree` writes `core.autocrlf` and `core.longpaths` with `git config`, which lands in the shared repo config. Branches `orq/*`, the `.orq.lock` file, `branch -D` and the repo's own hooks would all reach the owner's repo too.

Decisions taken with the owner (2026-10-08):

* **D1** orq keeps its own clone under `~/.orq/repos/<owner>/<repo>`. When the project has a local folder, the clone is seeded from it: `git clone --reference <folder's git dir> --dissociate <GitHub URL>`. Objects come from the folder, so nothing is downloaded twice. `--dissociate` copies them, so the clone does not depend on the folder afterwards. `origin` stays GitHub. The owner's folder is only read. A folder that cannot seed (shallow, broken, gone) falls back to a plain clone from GitHub.
* **D2** A repo that is only on GitHub is cloned into `~/.orq/repos` only. orq never writes into the owner's folders.
* **D3** Private repos are supported. The only real difference is that GitHub Actions on a private repo spends the plan's minutes (2,000 a month on Free). The picker marks private repos.
* **D4** The folders to scan are a global setting `projects.scan_roots` (list), with `projects.scan_depth` (default 3). Both are editable on the dashboard's Settings page. The GitHub list covers the owner's repos and the repos of the organizations the owner belongs to, without archived repos.

Out of scope: updating the owner's folder after a merge (the owner keeps using `git pull`), cloning when a project is added (the clone still happens at the first run).

## 1. Seeded clone

* `GitManager.ensure_repo(repo, repos_root, clone_url=None, seed=None, on_clone=None)`:
  * an existing clone is fetched as before; `seed` is ignored;
  * otherwise, when `seed` is a folder, its git dir is resolved with `git rev-parse --path-format=absolute --git-common-dir` (a linked worktree resolves to its main repo);
  * the clone runs with `--reference <git dir> --dissociate`; if the seed does not resolve or the seeded clone fails, the half-made clone is removed and a plain clone runs;
  * `on_clone(source)` reports `local:<git dir>` or `github`.
* Nothing is written in the seed: the command runs with cwd at the clone's parent, and every read of the folder is a plain query.
* `_setup` passes `seed=project.local_path` and logs the event `repo_cloned` with the source.
* Test: the seed's `.git/config` bytes, `for-each-ref`, `worktree list`, `status --porcelain`, `HEAD`, hooks and siblings are identical before and after a seeded clone and a worktree on the clone, and the clone has no `objects/info/alternates`.

## 2. Discovery

`src/orq/core/discovery.py`, pure apart from the git and gh calls it is given:

* `scan_local(roots, depth, git) -> list[LocalRepo(path, repo, branch, dirty)]`
  * walks each root breadth first up to `depth` levels;
  * a folder with a `.git` directory is a repo, and the walk does not go inside it;
  * a folder whose `.git` is a file (linked worktree or submodule) is skipped;
  * hidden folders, `node_modules`, virtualenvs, `dist`, `build` are skipped;
  * only folders whose `origin` is on GitHub are kept;
  * `branch` comes from `git branch --show-current`; `dirty` counts `git --no-optional-locks status --porcelain` lines (`--no-optional-locks` keeps git from refreshing the owner's index).
* `list_github(gh) -> list[RemoteRepo(repo, private, description, pushed_at)]` from `gh api --paginate "user/repos?per_page=100&affiliation=owner,organization_member"`, without archived repos, newest push first.
* `candidates(store, git, roots, depth) -> {"roots", "local", "github", "github_error"}`:
  * `local` entries carry `added` (a project with that repo exists);
  * `github` leaves out the repos found locally and carries `added` too;
  * a gh failure leaves `github` empty and fills `github_error`; the local list still shows.

## 3. Settings

* New config section `[projects]`: `scan_roots = []`, `scan_depth = 3`. The code default is empty; the owner's install sets `D:\Projetos\GitHub` in the dashboard.
* `projects.scan_roots` (list) and `projects.scan_depth` (positive int) join the settings keys as **global only**: a project override of them is refused, and the project settings form does not show them.

## 4. Surfaces

* `GET /api/projects/candidates` (runs in a thread).
* `orq project candidates` prints the two lists.
* Add project page:
  * **On this PC**: one row per folder with repo, path, branch, uncommitted count, private mark, "added" badge, and an Add button; a note says orq only reads the folder and seeds its own clone from it;
  * **Only on GitHub**: a filter box and the remaining repos, private marked, each with Add;
  * **Other**: the existing free input (`owner/repo` or a folder);
  * no scan roots set: a hint pointing to Settings.
* The project settings tab says "orq's clone was seeded from <folder>; orq only reads that folder".

## 5. Exit criteria (real runs)

* The picker lists the repos under `D:\Projetos\GitHub`, the volleyball repo included, and no linked worktree as a separate entry.
* Sandbox B, with orq's existing clone renamed aside: an owner-style checkout of it in a scan root, on another branch and with an uncommitted file, added from the picker. The run's `repo_cloned` event says `local:`, the task merges, and the folder is identical before and after (HEAD, branch, status, refs, config, worktree list, hooks).
* Phase 0 sandbox, with orq's clone renamed aside: picked from "Only on GitHub", cloned from GitHub, the task merges.
