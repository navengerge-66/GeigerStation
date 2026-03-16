/**
 * seed-test-data.js
 * Inserts 90 rows (last 90 minutes) into geiger_logs for dashboard testing.
 * Run once: node scripts/seed-test-data.js
 * DELETE after testing: see bottom of this file.
 */

const SERVICE_KEY = 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6IndiYXdldnRiamZnc2FwYmh4ZmVhIiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImlhdCI6MTc3MzY3MTYwMSwiZXhwIjoyMDg5MjQ3NjAxfQ.PB23nJL50vlqrRENiZo_zWnNzIqunuQuT8-wFlroz9s';
const SUPABASE_URL = 'https://wbawevtbjfgsapbhxfea.supabase.co';

const now = Date.now();
const rows = [];

for (let i = 89; i >= 0; i--) {
    const ts    = new Date(now - i * 60_000).toISOString();
    // Normal background 10–18 µRh/h; spike at minutes 15–17
    const spike = (i >= 13 && i <= 17);
    const val   = spike
        ? +(44 + Math.random() * 6).toFixed(2)      // anomaly: 44–50 µRh/h
        : +(10 + Math.random() * 8).toFixed(2);     // normal:  10–18 µRh/h
    rows.push({ created_at: ts, mrh_value: val, is_anomaly: spike });
}

const url     = `${SUPABASE_URL}/rest/v1/geiger_logs`;
const headers = {
    'apikey':        SERVICE_KEY,
    'Authorization': `Bearer ${SERVICE_KEY}`,
    'Content-Type':  'application/json',
    'Prefer':        'return=minimal',
};

(async () => {
    const res = await fetch(url, {
        method:  'POST',
        headers,
        body:    JSON.stringify(rows),
    });

    if (!res.ok) {
        const txt = await res.text();
        console.error('Insert failed:', res.status, txt);
        process.exit(1);
    }

    console.log(`Inserted ${rows.length} rows (${rows.filter(r => r.is_anomaly).length} anomalies).`);
    console.log('Refresh https://navenger-geiger.netlify.app to verify.');
    console.log('\nTo clean up test data run:');
    console.log('  node scripts/seed-test-data.js --delete');
})();

// ── Optional cleanup ──────────────────────────────────────────────────────────
if (process.argv.includes('--delete')) {
    (async () => {
        const oldest = new Date(now - 90 * 60_000).toISOString();
        const delUrl = `${SUPABASE_URL}/rest/v1/geiger_logs?created_at=gte.${oldest}&is_anomaly=eq.false`;
        const res = await fetch(delUrl, { method: 'DELETE', headers });
        console.log('Delete status:', res.status);
    })();
}
