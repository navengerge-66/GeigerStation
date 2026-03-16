-- =============================================================================
-- GeigerStation — System Status / Heartbeat Table
-- Migration: 20260316190220_system_status
-- =============================================================================

-- ── 1. system_status table ────────────────────────────────────────────────────
-- Single-row table: the Pi upserts this every 5 minutes.
-- The frontend checks last_seen to decide UPLINK_STABLE vs LINK_LOST.

CREATE TABLE IF NOT EXISTS system_status (
    station_id  text        PRIMARY KEY,
    last_seen   timestamptz NOT NULL DEFAULT now(),
    version     text,           -- optional: Python script version string
    queue_depth int  DEFAULT 0  -- offline queue depth for observability
);

-- Seed the single row so the dashboard can always SELECT it
INSERT INTO system_status (station_id, last_seen)
VALUES ('tbilisi-01', now() - interval '1 hour')
ON CONFLICT (station_id) DO NOTHING;

-- ── 2. Row-Level Security ─────────────────────────────────────────────────────
ALTER TABLE system_status ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "public_read" ON system_status;
CREATE POLICY "public_read"
    ON system_status
    FOR SELECT
    TO anon
    USING (true);
-- service_role (Pi) bypasses RLS for upserts automatically.
