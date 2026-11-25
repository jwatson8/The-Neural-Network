-- Run all statements in ONE transaction. Additive only; no existing rows change.
SET LOCAL lock_timeout = '5s';
-- next
SET LOCAL statement_timeout = '120s';
-- next
SELECT pg_advisory_xact_lock(739103003);
-- next
CREATE TABLE IF NOT EXISTS rejected_requests (
    event_id UUID PRIMARY KEY,
    topic VARCHAR(255) NOT NULL,
    partition_id INTEGER NOT NULL,
    offset_id BIGINT NOT NULL,
    received_at TIMESTAMP WITH TIME ZONE NOT NULL,
    event_time TIMESTAMP WITH TIME ZONE,
    event_kind VARCHAR(20) NOT NULL,
    reason VARCHAR(50) NOT NULL,
    user_id VARCHAR(255),
    error_details JSONB NOT NULL,
    raw_line TEXT NOT NULL,
    raw_line_truncated BOOLEAN NOT NULL,
    CONSTRAINT uq_rejected_requests_offset UNIQUE (topic, partition_id, offset_id),
    CONSTRAINT ck_rejected_request_partition CHECK (partition_id >= 0),
    CONSTRAINT ck_rejected_request_offset CHECK (offset_id >= 0)
);
-- next
CREATE INDEX IF NOT EXISTS ix_rejected_requests_received_at ON rejected_requests (received_at);
-- next
CREATE INDEX IF NOT EXISTS ix_rejected_requests_event_kind ON rejected_requests (event_kind);
-- next
CREATE INDEX IF NOT EXISTS ix_rejected_requests_reason ON rejected_requests (reason);
-- next
CREATE INDEX IF NOT EXISTS ix_rejected_requests_user_id ON rejected_requests (user_id);
