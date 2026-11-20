-- Run all statements in ONE transaction. Existing monitoring rows are preserved.
SET LOCAL lock_timeout = '5s';
-- next
SET LOCAL statement_timeout = '120s';
-- next
SELECT pg_advisory_xact_lock(739103002);
-- next
ALTER TABLE monitoring_runs
    ADD COLUMN IF NOT EXISTS request_count INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS request_count_over_600ms INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS request_latency_average_ms DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS request_latency_p50_ms DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS request_latency_p95_ms DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS request_latency_p99_ms DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS request_latency_maximum_ms DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS rating_drift_tvd DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS event_type_drift_tvd DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS rating_drift_sample_count INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS event_type_baseline_counts JSONB NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS event_type_recent_counts JSONB NOT NULL DEFAULT '{}'::jsonb;
-- next
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid = 'monitoring_runs'::regclass AND conname = 'ck_monitoring_request_count') THEN
        ALTER TABLE monitoring_runs ADD CONSTRAINT ck_monitoring_request_count CHECK (request_count >= 0);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid = 'monitoring_runs'::regclass AND conname = 'ck_monitoring_slow_request_count') THEN
        ALTER TABLE monitoring_runs ADD CONSTRAINT ck_monitoring_slow_request_count CHECK (request_count_over_600ms >= 0 AND request_count_over_600ms <= request_count);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid = 'monitoring_runs'::regclass AND conname = 'ck_monitoring_latency_shape') THEN
        ALTER TABLE monitoring_runs ADD CONSTRAINT ck_monitoring_latency_shape CHECK (
            (request_count = 0 AND request_latency_average_ms IS NULL AND request_latency_p50_ms IS NULL AND request_latency_p95_ms IS NULL AND request_latency_p99_ms IS NULL AND request_latency_maximum_ms IS NULL)
            OR
            (request_count > 0 AND request_latency_average_ms >= 0 AND request_latency_p50_ms >= 0 AND request_latency_p50_ms <= request_latency_p95_ms AND request_latency_p95_ms <= request_latency_p99_ms AND request_latency_p99_ms <= request_latency_maximum_ms AND request_latency_average_ms <= request_latency_maximum_ms)
        );
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid = 'monitoring_runs'::regclass AND conname = 'ck_monitoring_rating_drift_tvd') THEN
        ALTER TABLE monitoring_runs ADD CONSTRAINT ck_monitoring_rating_drift_tvd CHECK (rating_drift_tvd IS NULL OR (rating_drift_tvd >= 0 AND rating_drift_tvd <= 1));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid = 'monitoring_runs'::regclass AND conname = 'ck_monitoring_event_type_drift_tvd') THEN
        ALTER TABLE monitoring_runs ADD CONSTRAINT ck_monitoring_event_type_drift_tvd CHECK (event_type_drift_tvd IS NULL OR (event_type_drift_tvd >= 0 AND event_type_drift_tvd <= 1));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid = 'monitoring_runs'::regclass AND conname = 'ck_monitoring_rating_drift_sample_count') THEN
        ALTER TABLE monitoring_runs ADD CONSTRAINT ck_monitoring_rating_drift_sample_count CHECK (rating_drift_sample_count >= 0);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid = 'monitoring_runs'::regclass AND conname = 'ck_monitoring_event_type_baseline_counts') THEN
        ALTER TABLE monitoring_runs ADD CONSTRAINT ck_monitoring_event_type_baseline_counts CHECK (jsonb_typeof(event_type_baseline_counts) = 'object');
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid = 'monitoring_runs'::regclass AND conname = 'ck_monitoring_event_type_recent_counts') THEN
        ALTER TABLE monitoring_runs ADD CONSTRAINT ck_monitoring_event_type_recent_counts CHECK (jsonb_typeof(event_type_recent_counts) = 'object');
    END IF;
END $$;
-- next
CREATE TABLE IF NOT EXISTS monitoring_route_latency (
    run_id UUID NOT NULL REFERENCES monitoring_runs(run_id),
    model_route VARCHAR(100) NOT NULL,
    request_count INTEGER NOT NULL,
    request_count_over_600ms INTEGER NOT NULL,
    request_latency_average_ms DOUBLE PRECISION NOT NULL,
    request_latency_p50_ms DOUBLE PRECISION NOT NULL,
    request_latency_p95_ms DOUBLE PRECISION NOT NULL,
    request_latency_p99_ms DOUBLE PRECISION NOT NULL,
    request_latency_maximum_ms DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (run_id, model_route),
    CONSTRAINT ck_route_latency_request_count CHECK (request_count > 0),
    CONSTRAINT ck_route_latency_slow_request_count CHECK (request_count_over_600ms >= 0 AND request_count_over_600ms <= request_count),
    CONSTRAINT ck_route_latency_shape CHECK (
        request_latency_average_ms >= 0 AND request_latency_p50_ms >= 0
        AND request_latency_p50_ms <= request_latency_p95_ms
        AND request_latency_p95_ms <= request_latency_p99_ms
        AND request_latency_p99_ms <= request_latency_maximum_ms
        AND request_latency_average_ms <= request_latency_maximum_ms
    )
);
