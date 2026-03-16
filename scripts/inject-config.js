/**
 * inject-config.js
 *
 * Runs at Netlify build time. Replaces placeholder tokens in Site/Index.html
 * with the actual environment variable values so the frontend can connect to
 * Supabase without exposing credentials in the repository.
 *
 * Required env vars (set in Netlify UI → Site → Environment variables):
 *   SUPABASE_URL       Full project URL, e.g. https://xxxx.supabase.co
 *   SUPABASE_ANON_KEY  Public anon/read-only key (safe to ship in browser JS)
 */

const fs   = require('fs');
const path = require('path');

const HTML_FILE = path.join(__dirname, '..', 'Site', 'Index.html');

const REPLACEMENTS = [
    ['YOUR_SUPABASE_URL',      process.env.SUPABASE_URL],
    ['YOUR_SUPABASE_ANON_KEY', process.env.SUPABASE_ANON_KEY],
];

let html = fs.readFileSync(HTML_FILE, 'utf8');
let changed = 0;

for (const [token, value] of REPLACEMENTS) {
    if (!value) {
        console.error(`[inject-config] ERROR: env var for "${token}" is not set.`);
        process.exit(1);
    }
    if (!html.includes(token)) {
        console.warn(`[inject-config] WARN: token "${token}" not found — already injected?`);
        continue;
    }
    html = html.replaceAll(token, value);
    changed++;
    console.log(`[inject-config] Injected ${token.replace('YOUR_', '')}`);
}

fs.writeFileSync(HTML_FILE, html, 'utf8');
console.log(`[inject-config] Done — ${changed} substitution(s) applied.`);
