-- Inbound WhatsApp messages for orq (SPEC 9.3). Postgres on the VPS, written only by n8n.
CREATE TABLE IF NOT EXISTS orq_messages (
    id           BIGSERIAL PRIMARY KEY,
    decision_id  TEXT,
    direction    TEXT NOT NULL CHECK (direction IN ('in', 'out')),
    from_number  TEXT,
    text         TEXT NOT NULL,
    received_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    consumed_at  TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS orq_messages_unconsumed ON orq_messages (id) WHERE direction = 'in' AND consumed_at IS NULL;
