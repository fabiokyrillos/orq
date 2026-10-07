# WhatsApp bridge: n8n + Evolution API setup

orq talks to three authenticated n8n webhooks (SPEC 9.3) and never to Evolution or Postgres directly. The owner's phone number lives only in n8n. The files under `docs/n8n/` were written by hand against n8n 1.x node schemas, not exported from a live instance: import, then open each node once to confirm credentials and expressions.

## 1. Postgres

Run `docs/n8n/orq_messages.sql` on the VPS database n8n can reach. Create an n8n Postgres credential named `orq postgres` for it.

## 2. n8n credentials

Two *Header Auth* credentials and one Postgres credential, created under Credentials:

| Credential | Type | Name header | Value |
|---|---|---|---|
| `orq bearer` | Header Auth | `Authorization` | `Bearer <long random token>` (the same token goes into `ORQ_N8N_TOKEN` on the PC) |
| `evolution apikey` | Header Auth | `apikey` | the instance's API key shown in Evolution Manager (eye icon on the instance card) |
| `orq postgres` | Postgres | | host, database, user, password of a Postgres n8n can reach |

## 3. Import the workflow

Import `docs/n8n/orq-workflow.json`, attach `orq bearer` to the three authenticated webhooks, `evolution apikey` to the `Evolution sendText` node and `orq postgres` to the three Postgres nodes. Then edit three placeholders: in `Evolution sendText`, the URL `https://EVOLUTION_HOST/message/sendText/INSTANCE_NAME` (your Evolution host and the instance name) and `OWNER_NUMBER` in the JSON body; in `Only the owner`, `OWNER_NUMBER` again. The owner number is your personal number in international format without `+`, not the instance's number. Activate the workflow. Production URLs look like `https://<n8n>/webhook/orq/notify`; `[notify].n8n_base_url` in `config.toml` is the part before `/orq/...`, so `https://<n8n>/webhook`.

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
