# WhatsApp bridge: n8n + Evolution API setup

orq talks to three authenticated n8n webhooks (SPEC 9.3) and never to Evolution directly. Inbound messages are kept in an n8n **Data table** (no external database). The owner's phone number lives only in n8n. `docs/n8n/orq-workflow.json` was written by hand against n8n 1.x node schemas (Data table node 1.1), not exported from a live instance: import, then open each node once to confirm credentials and the three placeholders.

## 1. Data table

In n8n, **Overview → Data tables → Create data table** named exactly `orq_messages`, with these columns (the `id`, `createdAt` and `updatedAt` columns are automatic):

| Column | Type |
|---|---|
| `text` | String |
| `from_number` | String |
| `consumed` | Boolean |

## 2. Credentials

Two *Header Auth* credentials, created under **Credentials**:

| Credential | Name header | Value |
|---|---|---|
| `orq bearer` | `Authorization` | `Bearer <long random token>` (the same token goes into `ORQ_N8N_TOKEN` on the PC) |
| `evolution apikey` | `apikey` | the instance's API key shown in Evolution Manager (eye icon on the instance card) |

Generate the token on the PC: `python -c "import secrets; print(secrets.token_urlsafe(32))"`.

## 3. Import the workflow

**Create workflow → Import from file → `docs/n8n/orq-workflow.json`**, then:

1. `Webhook notify`, `Webhook replies`, `Webhook ack`: select the credential `orq bearer`.
2. `Evolution sendText`: select `evolution apikey`; set the URL to `https://<evolution host>/message/sendText/<instance name>`; in the JSON body replace `OWNER_NUMBER` with your personal number, international format without `+` (`5581XXXXXXXXX`). This is the number that receives and answers, not the instance's own number.
3. `Only the owner`: replace `OWNER_NUMBER` with the same number.
4. `Unconsumed rows`, `Mark consumed`, `Insert inbound`: open each once so the node loads the `orq_messages` columns; the table is referenced by name.
5. Save and **activate**. Production URLs are `https://<n8n>/webhook/orq/...`; `[notify].n8n_base_url` in `config.toml` is the part before `/orq/...`, so `https://<n8n>/webhook`.

## 4. Evolution webhook

In Evolution Manager, instance settings → **Webhook**: enabled, URL `https://<n8n>/webhook/orq/evolution`, event `MESSAGES_UPSERT` only. The `Only the owner` node drops messages from any other number and the sender's own messages.

## 5. Test from the PC (PowerShell)

```powershell
$N8N = "https://<n8n>/webhook"
Invoke-RestMethod -Method Post -Uri "$N8N/orq/notify" -Headers @{Authorization="Bearer $env:ORQ_N8N_TOKEN"} -ContentType "application/json" -Body '{"text":"*[orq] test*"}'
```

Expect `ok True` and the message on your phone. Reply `hello` from your phone, then:

```powershell
Invoke-RestMethod -Uri "$N8N/orq/replies?since=0" -Headers @{Authorization="Bearer $env:ORQ_N8N_TOKEN"}
```

Expect `messages` with one entry (`id`, `text`, `received_at`). Ack it with that `id`:

```powershell
Invoke-RestMethod -Method Post -Uri "$N8N/orq/replies/ack" -Headers @{Authorization="Bearer $env:ORQ_N8N_TOKEN"} -ContentType "application/json" -Body '{"ids":[1]}'
```

A second `replies?since=0` must return an empty list. A request without the header must be rejected by n8n (401/403).

## 6. orq side

```toml
[notify]
n8n_base_url = "https://<n8n>/webhook"
n8n_token_env = "ORQ_N8N_TOKEN"
```

```powershell
[Environment]::SetEnvironmentVariable("ORQ_N8N_TOKEN", "<token>", "User")
```

Open a new terminal, then `orq dashboard`. It prints `WhatsApp: on` and from then on sends every new decision and run completion, reminds after `[notify].reminder_hours`, and polls replies every `[notify].poll_seconds`.

Reply formats (also sent back as a hint when a message is not understood):

* `DQMKR 2` picks option 2 of decision `DQMKR`; `DQMKR <text>` is a free-text answer.
* `APPROVE DQMKR` or `DENY DQMKR` for destructive approvals; a number is refused for those.
* `STATUS`, `STATUS <run>`, `PAUSE <run>`, `RESUME <run>`, `ABORT <run>`.
