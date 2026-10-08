# Phase 6.1 design: automatic answers, who answered, readable WhatsApp messages

Date: 2026-10-08. Follow-up to Phase 6, asked by the owner after reading the Phase 6 runs on the phone.

There were three problems:

* During the Phase 6 tests, decisions were answered on the dashboard by Claude (the assistant in the coding session). The phone only said "answered on the dashboard", and the owner read that as orq approving things by itself.
* Messages were dense blocks of text.
* The owner wants a way to let decisions be answered when he does not answer, within limits of time and complexity.

Decisions taken with the owner (2026-10-08):

* **D1** orq can answer a decision automatically, by a configurable policy, off by default. It answers only when every condition holds:
  * the owner has not answered for `auto_answer_minutes` (default 30) since the decision was sent;
  * the asking agent rated it `stakes: low`, and the rating is at or under `auto_answer_max_stakes`;
  * it is not destructive, not a `business` decision, not from the guard, and not a plan approval;
  * there is a recommendation with a reason, and the recommended option is not `abort`.

  The answer is always the recommended option.
* **D2** Every answer records who gave it: `owner`, `claude` or `auto`, besides the channel. WhatsApp, `DECISIONS.md`, the summary and the replay say it.
* **D3** WhatsApp messages get blank lines between sections, bold labels and emojis. Line breaks inside the context are kept.
* **Language:** WhatsApp texts, orq's own decision texts and the agents' owner-facing fields are in Brazilian Portuguese. Code, identifiers, comments, docs and commits stay English. This exception is recorded in `CLAUDE.md`. Machine keywords stay as they are: `APPROVE`, `DENY`, `STATUS`, decision IDs, and the `approve`/`deny`/`abort` option labels the loop parses.

## 1. Data

* `Decision` gains `answered_by` (`owner` | `claude` | `auto`) and `stakes` (`low` | `medium` | `high` | empty). Both are new columns, migrated in place.
* `record_answer(..., via, by="owner")`:
  * `POST /api/decisions/{id}/answer` accepts `by` (`owner` | `claude`, default `owner`; the page sends `owner`);
  * `orq answer --by claude`;
  * the hub's automatic answers use `via="auto", by="auto"`.
  * `DECISIONS.md` writes `**Answer (by <who>, via <channel>):**`.
* The `human` object of the contracts and the implementer marker gain `stakes`. The prompt defines it:
  * `low`: a reversible technical detail with no effect for the software's users;
  * `medium`: visible behaviour or more work;
  * `high`: business rules, data, money, security, or hard to undo.
* orq's own decisions set `stakes` themselves:
  * agent error retry: `low`;
  * CI timeout, keep waiting: `low`;
  * no progress: `medium`;
  * everything else: `high`.

## 2. Policy and hub task

* `src/orq/core/auto_answer.py: check(decision, settings, sent_at, now) -> (eligible, reason)` is pure.
* Settings in layers (global and project): `notify.auto_answer` (bool, default false), `notify.auto_answer_minutes` (default 30), `notify.auto_answer_max_stakes` (default `low`). They are editable on the Settings pages.
* `hub/auto_answer.py: AutoAnswerer.tick()` runs in the hub every `[queue].poll_seconds`, whether or not WhatsApp is on:
  * for each pending decision of a live (not finished) run, the clock starts at the WhatsApp send time, else at the decision's creation;
  * an eligible decision is answered through `record_answer(via="auto", by="auto")`, and the event `auto_answered` is logged with the reason;
  * a waiting run continues; a run without a process is resumed by the dispatcher (Phase 6).
* WhatsApp then announces it: `🤖 D… respondida automaticamente (30 min sem resposta): <option>`.

## 3. Messages (pt-BR, formatted)

Decision:

```
🟡 *Decisão de negócio* · D7K2
📦 repo · iteração 2 · milestone 1/3 · pergunta do planejador

*Contexto*
<context, line breaks kept>

❓ *Pergunta*
<question>

*Opções*
1️⃣ *First people* ⭐
     ↳ <consequence>
2️⃣ *Last people*
     ↳ <consequence>

💡 *Recomendo a 1:* <reason>
⚙️ Complexidade: baixa · resposta automática em 30 min   (only when the policy is on and the decision is eligible)

↩️ Responda `D7K2 1` ou `D7K2 <texto>`
```

* Emojis by type: business 🟡, ambiguity 🔵, risk 🔴, blocked ⛔. Destructive decisions reply with `APPROVE D… / DENY D…`.
* Cap: 2000 characters, context cut first.
* Answered elsewhere:
  * `✅ D… respondida por você no dashboard: <answer>`
  * `🧑‍💻 … pelo Claude no dashboard: …`
  * `🤖 … automaticamente (<n> min sem resposta): …`
* Run states: `✅ *Concluída*` with the PR, `❌ *Falhou*` with the reason, `🛑 *Abortada*` with the reason.
* Digest:
  * header `📊 *Progresso* · RUN · repo`, with title and work time;
  * `✅ *Feito*` milestone lines;
  * `🔨 *Agora*`;
  * `📝 *Última atualização*`;
  * `🧪 checks · 📁 files`.
* `STATUS` list and hints are in Portuguese.

## 4. Testing

* `test_auto_answer.py`:
  * every exclusion (destructive, business, guard, plan approval, no recommendation, abort recommended, stakes above the limit, too early, policy off);
  * the eligible case;
  * the hub tick answers once and logs `auto_answered`.
* `test_answers.py` / `test_hub.py` / `test_cli.py`: `answered_by` through every channel; the API and CLI `by`.
* `test_messages.py` and `test_digest.py`: the new layouts, the line breaks kept, the cap, the attribution variants.
* Prompt and schema: `stakes` present; Portuguese instruction present.
* A real sandbox run with a decision left unanswered, policy on with a short timeout, so the owner sees the messages on the phone.
