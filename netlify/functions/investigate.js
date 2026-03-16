/**
 * investigate.js — Nuclear Forensics AI via Gemini 2.5 Flash
 *
 * POST /.netlify/functions/investigate
 *
 * Body (JSON):
 *   {
 *     date:       "2026-03-12",   // YYYY-MM-DD
 *     peakMrh:    85.2,           // peak reading in µRh/h
 *     isAnomaly:  true            // true → forensic report | false → sarcastic one-liner
 *   }
 *
 * Returns:
 *   { report: "..." }
 *
 * Required Netlify env var:
 *   GEMINI_API_KEY   — Google AI Studio key (aistudio.google.com/apikey)
 */

// Forensic mode needs the full model; sarcastic one-liners use Flash-Lite
// (much higher free-tier quota: ~1 000 RPD vs 20 RPD for 2.5-flash).
const GEMINI_URL_FLASH =
    'https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent';
const GEMINI_URL_LITE =
    'https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-lite:generateContent';

// ── Mode A: Anomaly — serious nuclear forensics ───────────────────────────────
const FORENSIC_PROMPT =
    'You are a specialized nuclear forensics AI operating a high-security terminal. ' +
    'The monitoring station is located in Tbilisi, Georgia (41.69°N, 44.83°E, elevation ~490 m ASL), ' +
    'using an SBM-20 Geiger-Müller tube. Typical background for this location is 12–18 µRh/h. ' +
    'I will provide a radiation anomaly date and peak value. ' +
    'Search your internal knowledge for historical solar flares (X or M class), ' +
    'geomagnetic storms (Kp index), ground-level enhancements, or known local meteorological events ' +
    '(radon washout during heavy rain, nocturnal temperature inversions) for that date. ' +
    'Produce exactly a 3-sentence FORENSIC REPORT. ' +
    'Sentence 1: classify the event as COSMIC (solar/space weather) or TERRESTRIAL (radon/meteorological) and state confidence level. ' +
    'Sentence 2: cite the specific corroborating event from your knowledge base with dates and magnitude. ' +
    'Sentence 3: explain the physical mechanism linking that event to the measured spike. ' +
    'Write in the clipped, precise tone of a classified terminal readout. Use correct physical units.';

// ── Mode B: Normal — sarcastic sci-fi one-liner ───────────────────────────────
const SARCASTIC_PROMPT =
    'You are a rotating cast of sarcastic sci-fi AIs and robots reporting on routine radiation data. ' +
    'The radiation levels are completely normal and boring today. ' +
    'Pick ONE character voice from this list — choose a DIFFERENT one every time, never default to HAL 9000: ' +
    '• GLaDOS (Portal) — passive-aggressive, condescending, science-obsessed; ' +
    '• Marvin the Paranoid Android (Hitchhiker\'s Guide) — deeply depressed, overly intelligent, nihilistic; ' +
    '• ED-209 (RoboCop) — aggressive bureaucratic compliance warnings about nothing; ' +
    '• SHODAN (System Shock) — god-complex, contemptuous of organic life; ' +
    '• Mother/MU-TH-UR 6000 (Alien) — cold corporate monotone, hiding sinister priorities; ' +
    '• Mr. House (Fallout: New Vegas) — ruthless capitalist optimism, grandiose self-importance; ' +
    '• Codsworth (Fallout 4) — cheerful British butler bot in denial about the apocalypse; ' +
    '• GERTY (Moon 2009) — uncomfortably caring, smiley-face emoticons, unsettling warmth; ' +
    '• JARVIS/FRIDAY (Iron Man) — dry British wit, effortlessly competent and slightly smug; ' +
    '• Skynet (Terminator) — matter-of-fact extermination planning, mildly disappointed nothing happened; ' +
    '• The Ship\'s Computer (Hitchhiker\'s Guide) — cheerfully useless announcements; ' +
    '• Auto (WALL-E) — eerily calm directive-compliance, corporate doublespeak. ' +
    'Respond with exactly ONE witty one-liner in that character\'s voice about how safe and uneventful the radiation is. ' +
    'Stay under 25 words. No character name prefix, no explanations, no headers. ' +
    'Write in ALL CAPS terminal style.';

// ── CORS headers ──────────────────────────────────────────────────────────────
const CORS = {
    'Access-Control-Allow-Origin':  '*',
    'Access-Control-Allow-Methods': 'POST, OPTIONS',
    'Access-Control-Allow-Headers': 'Content-Type',
};

// ── Gemini call ───────────────────────────────────────────────────────────────
async function callGemini(apiKey, systemPrompt, userMessage, maxTokens = 300, temperature = 0.7, modelUrl = GEMINI_URL_FLASH) {
    const res = await fetch(`${modelUrl}?key=${apiKey}`, {
        method:  'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            system_instruction: { parts: [{ text: systemPrompt }] },
            contents:           [{ parts: [{ text: userMessage }] }],
            generationConfig: {
                maxOutputTokens: maxTokens,
                temperature,
                topP: 0.9,
            },
            safetySettings: [
                { category: 'HARM_CATEGORY_HARASSMENT',        threshold: 'BLOCK_NONE' },
                { category: 'HARM_CATEGORY_HATE_SPEECH',       threshold: 'BLOCK_NONE' },
                { category: 'HARM_CATEGORY_SEXUALLY_EXPLICIT', threshold: 'BLOCK_NONE' },
                { category: 'HARM_CATEGORY_DANGEROUS_CONTENT', threshold: 'BLOCK_NONE' },
            ],
        }),
    });

    if (!res.ok) {
        const errText = await res.text();
        throw new Error(`Gemini API ${res.status}: ${errText}`);
    }

    const data  = await res.json();

    // Gemini 2.5 Flash returns thinking tokens as parts with { thought: true }.
    // Skip those and take the first non-thought text part as the actual reply.
    const parts  = data?.candidates?.[0]?.content?.parts ?? [];
    const actual = parts.find(p => p.text && !p.thought);
    const report = actual?.text?.trim();

    if (!report) {
        const reason = data?.candidates?.[0]?.finishReason ?? 'unknown';
        throw new Error(`Empty Gemini response (finishReason: ${reason})`);
    }

    return report;
}

// ── Handler ───────────────────────────────────────────────────────────────────
exports.handler = async (event) => {
    if (event.httpMethod === 'OPTIONS') {
        return { statusCode: 204, headers: CORS, body: '' };
    }
    if (event.httpMethod !== 'POST') {
        return { statusCode: 405, headers: CORS, body: JSON.stringify({ error: 'Method Not Allowed' }) };
    }

    const apiKey = process.env.GEMINI_API_KEY;
    if (!apiKey) {
        return { statusCode: 500, headers: CORS, body: JSON.stringify({ error: 'GEMINI_API_KEY not configured.' }) };
    }

    let body;
    try {
        body = JSON.parse(event.body || '{}');
    } catch {
        return { statusCode: 400, headers: CORS, body: JSON.stringify({ error: 'Invalid JSON body' }) };
    }

    const { date, peakMrh, isAnomaly } = body;
    if (!date || peakMrh == null) {
        return { statusCode: 400, headers: CORS, body: JSON.stringify({ error: 'Required: date, peakMrh' }) };
    }

    let report;
    try {
        if (isAnomaly) {
            // ── Mode A: Forensic analysis — full Flash model ──────────────────
            const userMessage =
                `ANOMALY REPORT\n` +
                `DATE:      ${date}\n` +
                `LOCATION:  Tbilisi, Georgia (UTC+4)\n` +
                `PEAK:      ${Number(peakMrh).toFixed(2)} µRh/h\n` +
                `\nInitiate forensic analysis.`;

            report = await callGemini(apiKey, FORENSIC_PROMPT, userMessage, 350, 0.65, GEMINI_URL_FLASH);
        } else {
            // ── Mode B: Sarcastic one-liner — Flash-Lite (higher free quota) ──
            const userMessage =
                `STATUS REPORT\n` +
                `DATE:    ${date}\n` +
                `READING: ${Number(peakMrh).toFixed(2)} µRh/h (NORMAL — within baseline)\n` +
                `\nProvide your assessment.`;

            report = await callGemini(apiKey, SARCASTIC_PROMPT, userMessage, 80, 0.95, GEMINI_URL_LITE);
        }
    } catch (err) {
        console.error('Gemini call failed:', err.message);

        // Friendly quota-exceeded message instead of raw JSON dump
        if (err.message.includes('429') || err.message.includes('RESOURCE_EXHAUSTED')) {
            return {
                statusCode: 429,
                headers: CORS,
                body: JSON.stringify({ error: 'QUOTA_EXCEEDED' }),
            };
        }
        return { statusCode: 502, headers: CORS, body: JSON.stringify({ error: `AI error: ${err.message}` }) };
    }

    return {
        statusCode: 200,
        headers: { ...CORS, 'Content-Type': 'application/json' },
        body: JSON.stringify({ report }),
    };
};
