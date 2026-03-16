/**
 * analyze-anomaly.js — Netlify Serverless Function
 *
 * POST /.netlify/functions/analyze-anomaly
 *
 * Body (JSON):
 *   {
 *     date:          "2026-03-12",          // YYYY-MM-DD
 *     peakMrh:       85.2,                  // µRh/h
 *     avgMrh:        22.4,                  // daily average
 *     backgroundMrh: 19.1,                  // 100-reading rolling baseline
 *     readingCount:  1440,                  // rows for that day
 *     hourlyProfile: [18.2, 19.1, ..., 85.2, ...]   // 24 hourly averages
 *   }
 *
 * Returns:
 *   { analysis: "..." }   — 3-sentence scientific interpretation
 *
 * AI provider selection (set exactly ONE in Netlify env vars):
 *   OPENAI_API_KEY    → uses GPT-4o
 *   ANTHROPIC_API_KEY → uses Claude 3.5 Sonnet (preferred if both are set)
 */

const SYSTEM_PROMPT = `You are a nuclear physicist and environmental radiation analyst \
with expertise in atmospheric physics and space weather. \
The monitoring station is located in Tbilisi, Georgia (41.69°N, 44.83°E, elevation ~490 m ASL), \
using a SBM-20 Geiger-Müller tube. \
Given a specific date, peak reading, and a 24-hour hourly radiation profile, \
correlate the timing and shape of the spike with known global or regional phenomena: \
solar energetic particle events, geomagnetic storms (Kp index), ground-level enhancements, \
radon washout during precipitation, nocturnal temperature inversions concentrating radon, \
or any relevant nuclear or industrial events. \
Provide a grounded, 3-sentence scientific interpretation. \
Do not speculate beyond the data. Be precise and use correct physical units.`;

// ── Provider implementations ──────────────────────────────────────────────────

async function callOpenAI(userMessage) {
    const res = await fetch('https://api.openai.com/v1/chat/completions', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
            'Authorization': `Bearer ${process.env.OPENAI_API_KEY}`,
        },
        body: JSON.stringify({
            model: 'gpt-4o',
            messages: [
                { role: 'system', content: SYSTEM_PROMPT },
                { role: 'user',   content: userMessage },
            ],
            max_tokens: 320,
            temperature: 0.5,
        }),
    });

    if (!res.ok) {
        const err = await res.text();
        throw new Error(`OpenAI API error ${res.status}: ${err}`);
    }

    const data = await res.json();
    return data.choices[0].message.content.trim();
}

async function callAnthropic(userMessage) {
    const res = await fetch('https://api.anthropic.com/v1/messages', {
        method: 'POST',
        headers: {
            'Content-Type':         'application/json',
            'x-api-key':            process.env.ANTHROPIC_API_KEY,
            'anthropic-version':    '2023-06-01',
        },
        body: JSON.stringify({
            model: 'claude-3-5-sonnet-20241022',
            max_tokens: 320,
            system: SYSTEM_PROMPT,
            messages: [
                { role: 'user', content: userMessage },
            ],
        }),
    });

    if (!res.ok) {
        const err = await res.text();
        throw new Error(`Anthropic API error ${res.status}: ${err}`);
    }

    const data = await res.json();
    return data.content[0].text.trim();
}

// ── Handler ───────────────────────────────────────────────────────────────────

exports.handler = async (event) => {
    // CORS preflight
    if (event.httpMethod === 'OPTIONS') {
        return {
            statusCode: 204,
            headers: {
                'Access-Control-Allow-Origin':  '*',
                'Access-Control-Allow-Methods': 'POST, OPTIONS',
                'Access-Control-Allow-Headers': 'Content-Type',
            },
            body: '',
        };
    }

    if (event.httpMethod !== 'POST') {
        return { statusCode: 405, body: JSON.stringify({ error: 'Method Not Allowed' }) };
    }

    // ── Parse + validate body ─────────────────────────────────────────────────
    let body;
    try {
        body = JSON.parse(event.body || '{}');
    } catch {
        return { statusCode: 400, body: JSON.stringify({ error: 'Invalid JSON body' }) };
    }

    const { date, peakMrh, avgMrh, backgroundMrh, readingCount, hourlyProfile } = body;

    if (!date || peakMrh == null) {
        return {
            statusCode: 400,
            body: JSON.stringify({ error: 'Required fields: date, peakMrh' }),
        };
    }

    // ── Build user message ────────────────────────────────────────────────────
    const anomalyRatio = avgMrh > 0 ? (peakMrh / avgMrh).toFixed(1) : 'N/A';
    const bgRatio      = backgroundMrh > 0 ? (peakMrh / backgroundMrh).toFixed(1) : 'N/A';

    const hourlyStr = Array.isArray(hourlyProfile)
        ? hourlyProfile.map((v, i) => `${String(i).padStart(2, '0')}:00 → ${v != null ? v.toFixed(2) : 'no data'} µRh/h`).join('\n')
        : 'Not available';

    const userMessage = [
        `EVENT DATE:        ${date}`,
        `LOCATION:          Tbilisi, Georgia (UTC+4)`,
        `PEAK READING:      ${peakMrh} µRh/h`,
        `DAILY AVERAGE:     ${avgMrh != null ? Number(avgMrh).toFixed(2) : 'N/A'} µRh/h`,
        `BACKGROUND BASELINE (100-min rolling avg): ${backgroundMrh != null ? Number(backgroundMrh).toFixed(2) : 'N/A'} µRh/h`,
        `PEAK / DAILY AVG:  ${anomalyRatio}×`,
        `PEAK / BACKGROUND: ${bgRatio}×`,
        `TOTAL READINGS:    ${readingCount ?? 'N/A'}`,
        ``,
        `24-HOUR HOURLY PROFILE:`,
        hourlyStr,
    ].join('\n');

    // ── Call AI provider ──────────────────────────────────────────────────────
    let analysis;
    try {
        if (process.env.ANTHROPIC_API_KEY) {
            analysis = await callAnthropic(userMessage);
        } else if (process.env.OPENAI_API_KEY) {
            analysis = await callOpenAI(userMessage);
        } else {
            return {
                statusCode: 500,
                body: JSON.stringify({ error: 'No AI API key configured (set OPENAI_API_KEY or ANTHROPIC_API_KEY).' }),
            };
        }
    } catch (err) {
        console.error('AI call failed:', err.message);
        return {
            statusCode: 502,
            body: JSON.stringify({ error: `AI provider error: ${err.message}` }),
        };
    }

    return {
        statusCode: 200,
        headers: {
            'Content-Type':                'application/json',
            'Access-Control-Allow-Origin': '*',
        },
        body: JSON.stringify({ analysis }),
    };
};
