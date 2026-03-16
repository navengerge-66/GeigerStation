/**
 * investigate.js — Nuclear Forensics AI via Gemini 1.5 Flash
 *
 * POST /.netlify/functions/investigate
 *
 * Body (JSON):
 *   {
 *     date:     "2026-03-12",   // YYYY-MM-DD
 *     peakMrh:  85.2            // peak reading in µRh/h
 *   }
 *
 * Returns:
 *   { report: "..." }   — 3-sentence forensic interpretation
 *
 * Required Netlify env var:
 *   GEMINI_API_KEY   — Google AI Studio key (aistudio.google.com/apikey)
 */

const GEMINI_URL =
    'https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent';

const SYSTEM_PROMPT =
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

// ── CORS headers ──────────────────────────────────────────────────────────────
const CORS = {
    'Access-Control-Allow-Origin':  '*',
    'Access-Control-Allow-Methods': 'POST, OPTIONS',
    'Access-Control-Allow-Headers': 'Content-Type',
};

// ── Handler ───────────────────────────────────────────────────────────────────
exports.handler = async (event) => {
    if (event.httpMethod === 'OPTIONS') {
        return { statusCode: 204, headers: CORS, body: '' };
    }

    if (event.httpMethod !== 'POST') {
        return { statusCode: 405, headers: CORS, body: JSON.stringify({ error: 'Method Not Allowed' }) };
    }

    // ── Validate API key ──────────────────────────────────────────────────────
    const apiKey = process.env.GEMINI_API_KEY;
    if (!apiKey) {
        return {
            statusCode: 500,
            headers: CORS,
            body: JSON.stringify({ error: 'GEMINI_API_KEY is not configured.' }),
        };
    }

    // ── Parse body ────────────────────────────────────────────────────────────
    let body;
    try {
        body = JSON.parse(event.body || '{}');
    } catch {
        return { statusCode: 400, headers: CORS, body: JSON.stringify({ error: 'Invalid JSON body' }) };
    }

    const { date, peakMrh } = body;
    if (!date || peakMrh == null) {
        return {
            statusCode: 400,
            headers: CORS,
            body: JSON.stringify({ error: 'Required fields: date, peakMrh' }),
        };
    }

    // ── Build user message ────────────────────────────────────────────────────
    const userMessage =
        `ANOMALY REPORT\n` +
        `DATE:      ${date}\n` +
        `LOCATION:  Tbilisi, Georgia (UTC+4)\n` +
        `PEAK:      ${Number(peakMrh).toFixed(2)} µRh/h\n` +
        `\nInitiate forensic analysis.`;

    // ── Call Gemini 1.5 Flash ─────────────────────────────────────────────────
    let report;
    try {
        const res = await fetch(`${GEMINI_URL}?key=${apiKey}`, {
            method:  'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                system_instruction: {
                    parts: [{ text: SYSTEM_PROMPT }],
                },
                contents: [{
                    parts: [{ text: userMessage }],
                }],
                generationConfig: {
                    maxOutputTokens: 350,
                    temperature:     0.65,
                    topP:            0.9,
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

        const data = await res.json();

        // Extract text from Gemini response structure
        report = data?.candidates?.[0]?.content?.parts?.[0]?.text?.trim();

        if (!report) {
            const finishReason = data?.candidates?.[0]?.finishReason;
            throw new Error(`Empty response from Gemini (finishReason: ${finishReason ?? 'unknown'})`);
        }

    } catch (err) {
        console.error('Gemini call failed:', err.message);
        return {
            statusCode: 502,
            headers: CORS,
            body: JSON.stringify({ error: `AI provider error: ${err.message}` }),
        };
    }

    return {
        statusCode: 200,
        headers: { ...CORS, 'Content-Type': 'application/json' },
        body: JSON.stringify({ report }),
    };
};
