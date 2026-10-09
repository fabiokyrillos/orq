# Phase 7.2 design: project conversations on WhatsApp, transient errors retried

Date: 2026-10-09. Asked by the owner after Phase 7.1.

The owner wants to ask questions about a project from WhatsApp, the way the dashboard's Conversa tab works. The owner proposed a WhatsApp group whose messages n8n routes to orq.

Decisions taken with the owner (2026-10-09):

* **D1** A dedicated WhatsApp group (the owner and orq's Evolution number) carries the conversations. Decisions and commands stay in the private chat. orq never knows the group id or any number: n8n maps them to a `channel`.
* **D2** One group for every project. `PROJETO <name>` picks the project and stays until changed; `PROJETO` alone lists the projects.
* **D3** Each project has one ongoing WhatsApp conversation. `NOVA` starts a new one. It is a normal Conversa (marked `via whatsapp`), so the dashboard shows it and can continue it.
* **D4** On WhatsApp, Claude is asked for phone formatting: no tables, WhatsApp bold, short lists. orq converts what is left of Markdown and splits long answers into parts of up to 3,000 characters. A question gets an immediate "🔎 lendo o código…". Conversations never block the decision traffic.
* **Also:** a transient agent failure (HTTP 5xx, "overloaded", "Service Unavailable", "Reconnecting") is retried by orq before the owner is asked (Phase 7.1 runs cost the owner two decisions for a Codex 503).

## 1. n8n contract (backward compatible)

* `POST /orq/notify` accepts an optional `channel`. With `"chat"` the message goes to the group; without it, to the owner's number as before.
* Inbound: a second branch after `Webhook Evolution inbound` accepts messages whose `remoteJid` is the group and whose sender (`key.participant`) is the owner, not sent by orq itself. They go into `orq_messages` with a new column `channel = "chat"`. The owner's branch writes `channel = "owner"`.
* `GET /orq/replies` returns `channel` with each message. A missing channel means `owner`.
* Newer WhatsApp versions may identify group senders by a LID (`…@lid`) instead of the number; the setup guide has the owner read one real group event before fixing the condition.

## 2. orq

* `WhatsAppClient.send(text, channel=None)`; `InboundMessage.channel` (default `owner`).
* `WhatsAppTasks.inbound_once` hands `chat` messages to a `ChatBridge` and handles the rest as before. Every message is acked in the same pass.
* `hub/whatsapp_chat.py: ChatBridge`:
  * a queue processed by one async loop in the hub, one message at a time;
  * commands: `PROJETO [name]`, `NOVA`, `AJUDA`; anything else is a question;
  * the current project and each project's WhatsApp conversation are kept in the store's key-value table (`wa_chat.project`, `wa_chat.chat.<repo>`);
  * a question sends "🔎 lendo o código de <projeto>…", then `chat.ask(..., style="whatsapp", via="whatsapp")`, then the answer formatted and split, all on `channel="chat"`.
* `chat.ask` gains `style` and `via`. `ClaudeChat.run` takes the system prompt per call; `--append-system-prompt` is not stored in the session, so a conversation can move between the dashboard and WhatsApp.
* `core/whatsapp_format.py`: Markdown to WhatsApp (`**x**` and headings to `*x*`, tables to lines) and `split_message(text, limit=3000)` on paragraph, then line, boundaries, with `(1/3)` markers.

## 3. Transient failures

* `ratelimit.transient_error(text)`: 5xx statuses, `overloaded`, `service unavailable`, `temporarily unavailable`, `reconnecting`, `connection reset`, `timed out`.
* `_call`: a failure of kind `error` that is transient is retried after `[limits].transient_wait_seconds` (default 60), at most `[limits].transient_retries` times (default 2) per call, with a `transient_retry` event. After that, the owner decision as today.

## 4. Exit criteria

* The owner creates the group, applies the n8n changes, and asks about the Phase 0 sandbox: `PROJETO`, a question, a follow-up that needs the context, `NOVA`.
* A message from someone else in the group is ignored; decisions still arrive in the private chat.
* The WhatsApp conversation shows in the dashboard's Conversa tab.
* The transient retry is covered by tests. A real 503 is not reproducible on demand.
