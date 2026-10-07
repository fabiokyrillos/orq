# WhatsApp bridge: n8n + Evolution API setup

orq talks to three authenticated n8n webhooks (SPEC 9.3) and never to Evolution or Postgres directly. The owner's phone number lives only in n8n. The files under `docs/n8n/` were written by hand against n8n 1.x node schemas, not exported from a live instance: import, then open each node once to confirm credentials and expressions.

## 1. Postgres

Run `docs/n8n/orq_messages.sql` on the VPS database n8n can reach. Create an n8n Postgres credential named `orq postgres` for it.

## 2. n8n variables and credentials

Environment variables on the n8n host (restart n8n after setting them):

| Variable | Meaning |
|---|---|
| `EVOLUTION_URL` | Evolution API base URL, e.g. `https://evo.example.com` |
| `EVOLUTION_INSTANCE` | the instance of the dedicated sender number |
| `EVOLUTION_APIKEY` | that instance's API key |
| `ORQ_OWNER_NUMBER` | your number in E.164 without `+`, e.g. `5511999998888` |

Credential `orq bearer` (type *Header Auth*): name `Authorization`, value `Bearer <long random token>`. The same token goes into the `ORQ_N8N_TOKEN` environment variable on the PC that runs orq.

## 3. Import the workflow

Import `docs/n8n/orq-workflow.json`, attach `orq bearer` to the three authenticated webhooks and `orq postgres` to the three Postgres nodes, then activate it. Production URLs look like `https://<n8n>/webhook/orq/notify`; `[notify].n8n_base_url` in `config.toml` is the part before `/orq/...`, so `https://<n8n>/webhook`.

Point Evolution's webhook for the sender instance at `https://<n8n>/webhook/orq/evolution` with the `MESSAGES_UPSERT` event enabled. The `Only the owner` node drops everything that is not from `ORQ_OWNER_NUMBER` or that the sender itself wrote.

## 4. Test from the PC

```bash
curl -sS -X POST "$N8N/orq/notify" -H "Authorization: Bearer $ORQ_N8N_TOKEN" -H "Content-Type: application/json" -d '{"text":"*[orq] test*"}'
```

Expect `{"ok":true}` and the message on your phone. Reply `hello` from your phone, then:

```bash
curl -sS "$N8N/orq/replies?since=0" -H "Authorization: Bearer $ORQ_N8N_TOKEN"
```

Expect `{"messages":[{"id":1,"text":"hello","received_at":"..."}]}`. Ack it:

```bash
curl -sS -X POST "$N8N/orq/replies/ack" -H "Authorization: Bearer $ORQ_N8N_TOKEN" -H "Content-Type: application/json" -d '{"ids":[1]}'
```

A second `replies?since=0` must now return an empty list. A request without the header must get HTTP 401/403 from n8n.

## 5. orq side

```toml
[notify]
n8n_base_url = "https://<n8n>/webhook"
n8n_token_env = "ORQ_N8N_TOKEN"
```

Set `ORQ_N8N_TOKEN` in the environment, then `orq dashboard`. It prints `WhatsApp: on` and from then on sends every new decision and run completion, reminds after `[notify].reminder_hours`, and polls replies every `[notify].poll_seconds`.

Reply formats (also sent back as a hint when a message is not understood):

* `DQMKR 2` picks option 2 of decision `DQMKR`; `DQMKR <text>` is a free-text answer.
* `APPROVE DQMKR` or `DENY DQMKR` for destructive approvals; a number is refused for those.
* `STATUS`, `STATUS <run>`, `PAUSE <run>`, `RESUME <run>`, `ABORT <run>`.
