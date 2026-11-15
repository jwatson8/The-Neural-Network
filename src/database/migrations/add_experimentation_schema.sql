-- Run all statements in ONE transaction. No backfill or experiment activation.
SET LOCAL lock_timeout = '5s';
-- next
SET LOCAL statement_timeout = '120s';
-- next
SELECT pg_advisory_xact_lock(739103001);
-- next
CREATE TABLE IF NOT EXISTS experiments (
    experiment_id UUID PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    status VARCHAR(20) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    starts_at TIMESTAMPTZ,
    ends_at TIMESTAMPTZ,
    control_policy VARCHAR(255) NOT NULL,
    control_version VARCHAR(255) NOT NULL,
    treatment_policy VARCHAR(255) NOT NULL,
    treatment_version VARCHAR(255) NOT NULL,
    traffic_allocation NUMERIC(7,6) NOT NULL,
    treatment_allocation NUMERIC(7,6) NOT NULL,
    config JSONB NOT NULL,
    CONSTRAINT uq_experiments_name UNIQUE (name),
    CONSTRAINT ck_experiment_name CHECK (length(trim(name)) > 0),
    CONSTRAINT ck_experiment_status CHECK (status IN ('draft','running','paused','completed','aborted')),
    CONSTRAINT ck_experiment_allocation CHECK (traffic_allocation >= 0 AND traffic_allocation <= 1 AND treatment_allocation >= 0 AND treatment_allocation <= 1),
    CONSTRAINT ck_experiment_window CHECK (ends_at IS NULL OR (starts_at IS NOT NULL AND ends_at > starts_at)),
    CONSTRAINT ck_experiment_timestamps CHECK (updated_at >= created_at),
    CONSTRAINT ck_experiment_policies CHECK (length(trim(control_policy)) > 0 AND length(trim(control_version)) > 0 AND length(trim(treatment_policy)) > 0 AND length(trim(treatment_version)) > 0)
);
-- next
CREATE INDEX IF NOT EXISTS ix_experiments_status_starts ON experiments (status, starts_at);
-- next
CREATE TABLE IF NOT EXISTS experiment_assignments (
    assignment_id UUID PRIMARY KEY,
    experiment_id UUID NOT NULL REFERENCES experiments(experiment_id),
    user_id VARCHAR(255) NOT NULL REFERENCES users(user_id),
    variant VARCHAR(20) NOT NULL,
    assigned_at TIMESTAMPTZ NOT NULL,
    CONSTRAINT uq_experiment_assignments_experiment_user UNIQUE (experiment_id, user_id),
    CONSTRAINT ck_experiment_assignment_variant CHECK (variant IN ('control','treatment'))
);
-- next
CREATE INDEX IF NOT EXISTS ix_experiment_assignments_user_id ON experiment_assignments (user_id);
-- next
CREATE INDEX IF NOT EXISTS ix_experiment_assignments_experiment_variant_assigned ON experiment_assignments (experiment_id, variant, assigned_at);
-- next
CREATE TABLE IF NOT EXISTS experiment_reports (
    report_id UUID PRIMARY KEY,
    experiment_id UUID NOT NULL REFERENCES experiments(experiment_id),
    generated_at TIMESTAMPTZ NOT NULL,
    window_start TIMESTAMPTZ NOT NULL,
    window_end TIMESTAMPTZ NOT NULL,
    status VARCHAR(30) NOT NULL,
    results JSONB NOT NULL,
    analysis_version VARCHAR(255) NOT NULL,
    CONSTRAINT ck_experiment_report_window CHECK (window_end > window_start),
    CONSTRAINT ck_experiment_report_status CHECK (status IN ('provisional','final','insufficient_data','failed')),
    CONSTRAINT ck_experiment_report_analysis_version CHECK (length(trim(analysis_version)) > 0)
);
-- next
CREATE INDEX IF NOT EXISTS ix_experiment_reports_experiment_window ON experiment_reports (experiment_id, window_start, window_end);
-- next
ALTER TABLE recommendation_serves ADD COLUMN IF NOT EXISTS experiment_assignment_id UUID;
-- next
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'recommendation_serves'::regclass
          AND conname = 'recommendation_serves_experiment_assignment_id_fkey'
    ) THEN
        ALTER TABLE recommendation_serves
            ADD CONSTRAINT recommendation_serves_experiment_assignment_id_fkey
            FOREIGN KEY (experiment_assignment_id)
            REFERENCES experiment_assignments(assignment_id) NOT VALID;
    END IF;
END $$;
-- next
ALTER TABLE recommendation_serves VALIDATE CONSTRAINT recommendation_serves_experiment_assignment_id_fkey;
-- next
CREATE INDEX IF NOT EXISTS ix_recommendation_serves_experiment_assignment_id ON recommendation_serves (experiment_assignment_id);
