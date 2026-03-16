-- =============================================================================
-- GeigerStation — Initial Schema
-- Migration: 20260316164246_initial_schema
-- =============================================================================

-- ── 1. Core readings table ────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS geiger_logs (
    id          uuid        DEFAULT gen_random_uuid() PRIMARY KEY,
    created_at  timestamptz DEFAULT now()             NOT NULL,
    mrh_value   float4                                NOT NULL,
    is_anomaly  bool        DEFAULT false             NOT NULL
);

-- Covering index for the two most common query patterns:
--   SELECT ... ORDER BY created_at DESC LIMIT n        (live value + trend chart)
--   SELECT ... WHERE created_at BETWEEN x AND y        (day drill-down)
CREATE INDEX IF NOT EXISTS idx_geiger_created_at
    ON geiger_logs (created_at DESC);

CREATE INDEX IF NOT EXISTS idx_geiger_anomaly
    ON geiger_logs (created_at DESC)
    WHERE is_anomaly = true;


-- ── 2. Row-Level Security ─────────────────────────────────────────────────────
-- The anon (public) key used by the dashboard may read but never write.
-- The service_role key used by the Pi bypasses RLS for inserts.

ALTER TABLE geiger_logs ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "public_read" ON geiger_logs;
CREATE POLICY "public_read"
    ON geiger_logs
    FOR SELECT
    TO anon
    USING (true);


-- ── 3. daily_summary view ─────────────────────────────────────────────────────
-- Aggregates one row per calendar day in the station's local timezone.
-- has_anomaly = true if ANY single reading in that day was flagged.
-- The frontend queries this for the 30-day calendar heatmap.

CREATE OR REPLACE VIEW daily_summary AS
SELECT
    -- Truncate to calendar day in Tbilisi local time (UTC+4)
    (date_trunc('day', created_at AT TIME ZONE 'Asia/Tbilisi'))::date   AS day,

    round(avg(mrh_value)::numeric, 3)::float4   AS avg_mrh,
    round(max(mrh_value)::numeric, 3)::float4   AS peak_mrh,
    round(min(mrh_value)::numeric, 3)::float4   AS min_mrh,
    count(*)::int                               AS reading_count,

    -- A single spike flags the whole day orange on the calendar
    bool_or(is_anomaly)                         AS has_anomaly
FROM
    geiger_logs
GROUP BY
    1
ORDER BY
    1 DESC;

-- Allow the anon role to SELECT the view
GRANT SELECT ON daily_summary TO anon;


-- ── 4. Optional: retention policy helper ──────────────────────────────────────
-- Run this manually (or as a pg_cron job) to prune data older than 365 days
-- while keeping any row that was ever flagged as an anomaly.
--
-- DELETE FROM geiger_logs
-- WHERE created_at < now() - interval '365 days'
--   AND is_anomaly = false;
