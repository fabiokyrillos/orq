# Phase 7 Implementation Plan: projects from the owner's local folders

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** adding a project offers the owner's local folders first and GitHub repos second; orq's clone is seeded from the local folder, which orq only reads.

**Architecture:** `ensure_repo` gains a seed (`--reference --dissociate`, fallback to a plain clone); a discovery module scans folders and lists GitHub repos; global-only settings for the scan roots; one API endpoint, one CLI command, a new Add project page.

**Tech Stack:** Python 3.12, FastAPI, sqlite3, git, gh. Design: `docs/superpowers/specs/2026-10-08-phase7-local-folders-design.md`.

---

### Task 1: seeded clone

- [ ] Tests in `tests/test_git.py`: seeded clone reports `local:`, has no alternates, leaves the seed identical; a linked worktree seeds from its main repo; a shallow or missing seed falls back to `github`; an existing clone ignores the seed.
- [ ] `GitManager.ensure_repo(..., seed, on_clone)`.
- [ ] `_setup` passes `project.local_path` and logs `repo_cloned`; loop test.
- [ ] Commit `feat(git): seed orq's clone from the owner's local folder`.

### Task 2: discovery and settings

- [ ] `ProjectsConfig` (`scan_roots`, `scan_depth`); settings keys global only; tests in `tests/test_settings.py`, `tests/test_config.py`.
- [ ] `core/discovery.py` with tests in `tests/test_discovery.py`: nested repos found, worktrees and skipped folders left out, non-GitHub origins left out, dirty count, depth limit; GitHub list parsing, archived out, local repos out, `added` flags, gh failure.
- [ ] `orq project candidates`; CLI test.
- [ ] Commit `feat(projects): discover local folders and GitHub repos`.

### Task 3: dashboard

- [ ] `GET /api/projects/candidates`; project settings refuse global-only keys; hub tests.
- [ ] Add project page (On this PC, Only on GitHub, Other); settings form hides global-only keys on projects; project tab text.
- [ ] Commit `feat(dashboard): pick a project from local folders or GitHub`.

### Task 4: real runs, findings, spec

- [ ] Scan roots set to `D:\Projetos\GitHub` on the owner's install; picker checked.
- [ ] Sandbox B from a local owner-style checkout (snapshot before and after); Phase 0 sandbox from GitHub.
- [ ] `docs/phase7-findings.md`; SPEC sections 3, 10.5, 10.6, 11, 13, 14, 15; push.
