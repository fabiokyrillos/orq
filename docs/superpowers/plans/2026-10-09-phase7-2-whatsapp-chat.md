# Phase 7.2 Implementation Plan: project conversations on WhatsApp, transient errors retried

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** ask about a project from a WhatsApp group; transient agent failures retried before the owner is asked.

**Architecture:** `channel` on the n8n contract; a `ChatBridge` hub loop on top of `core/chat.ask`; a WhatsApp formatter; a transient-error retry in `Runner._call`.

**Tech Stack:** Python 3.12, httpx, asyncio, n8n. Design: `docs/superpowers/specs/2026-10-09-phase7-2-whatsapp-chat-design.md`.

---

### Task 1: transient retry

- [ ] `transient_error()`; `[limits].transient_retries`, `transient_wait_seconds`; `_call` retry with `transient_retry` events; loop tests (retried then ok; exhausted then decision; non-transient asks at once).
- [ ] Commit `feat(loop): retry transient agent failures before asking the owner`.

### Task 2: WhatsApp conversations

- [ ] `channel` in `WhatsAppClient`; tests.
- [ ] `core/whatsapp_format.py` with tests.
- [ ] `ClaudeChat` per-call system prompt; `chat.ask(style, via)`; tests.
- [ ] `hub/whatsapp_chat.py: ChatBridge`; routing in `inbound_once`; hub lifespan; tests.
- [ ] Commit `feat(whatsapp): project conversations in a WhatsApp group`.

### Task 3: n8n and docs

- [ ] `docs/n8n/orq-workflow.json`: `channel` column, group branch, notify routing; `docs/n8n-setup.md` section.
- [ ] Commit `docs(n8n): group branch for project conversations`.

### Task 4: real use

- [ ] The owner applies the n8n changes and creates the group; exit criteria; `docs/phase7-findings.md` section 10; SPEC; push.
