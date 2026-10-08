# Phase 6.1 Implementation Plan: automatic answers, attribution, readable messages

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:**
* Decisions say who answered them.
* A low-stakes decision can be answered automatically after a timeout, when the owner turns that on.
* WhatsApp messages are formatted, in Portuguese, and easy to read on a phone.

**Architecture:**
* New decision fields `answered_by` and `stakes`.
* A pure policy (`core/auto_answer.py`) and a hub task that applies it through `record_answer`.
* Message formatters rewritten, still pure.

**Tech Stack:** Python 3.12, FastAPI, sqlite3. Design: `docs/superpowers/specs/2026-10-08-phase6-1-auto-answer-and-messages-design.md`.

---

### Task 1: attribution, stakes, automatic answers

- [ ] `Decision.answered_by` and `stakes` (columns migrated); `record_answer(by=...)`; API `by`; `orq answer --by`; `DECISIONS.md` line.
- [ ] `stakes` in the `human` schema and the marker; prompt definition; orq decisions set their own.
- [ ] Settings keys `notify.auto_answer*` (bool, int, stakes); dashboard inputs.
- [ ] `core/auto_answer.check`; `hub/auto_answer.AutoAnswerer` in the hub lifespan.
- [ ] Commit `feat(decisions): record who answered; optional automatic answers for low-stakes decisions`.

### Task 2: Portuguese, formatted WhatsApp messages

- [ ] `CLAUDE.md` exception for owner-facing texts.
- [ ] Prompts ask for the owner-facing fields in Brazilian Portuguese; orq's decision texts and questions in Portuguese.
- [ ] `format_decision`, `format_answered_elsewhere`, `format_run_state`, `format_status`, hints, `build_digest`, in the new layouts.
- [ ] Commit `feat(whatsapp): formatted Portuguese messages with attribution`.

### Task 3: real run, findings, spec

- [ ] A sandbox run with an unanswered low-stakes question, the policy on with a short timeout; the owner reads the messages on the phone.
- [ ] `docs/phase6-findings.md` section 7; SPEC; push.
