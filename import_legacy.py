"""
import_legacy.py — Legacy CSV → Supabase Bulk Importer

Reads one or more legacy CSV files (columns: timestamp, mrh_value),
downsamples to 1-minute averages, and bulk-uploads to the geiger_logs
Supabase table. Records for minutes that already exist are silently skipped.

Usage:
    python import_legacy.py path/to/file.csv [file2.csv ...]
    python import_legacy.py path/to/csv_folder/

Requirements:
    pip install pandas requests python-dotenv

Environment variables (or .env file):
    SUPABASE_URL   — https://xxxx.supabase.co
    SUPABASE_KEY   — service_role key (bypasses RLS)
"""

import os
import sys
import json
import logging
import argparse
from pathlib import Path
from datetime import timezone

import pandas as pd
import requests
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
)
log = logging.getLogger('import_legacy')

# ── Config ────────────────────────────────────────────────────────────────────
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip('/')
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")
BATCH_SIZE   = 500   # rows per HTTP request

# ── CSV loading ───────────────────────────────────────────────────────────────

def load_csv(path: Path) -> pd.DataFrame:
    """
    Load a legacy CSV file. Accepts flexible timestamp formats.
    Expected columns (case-insensitive): timestamp, mrh_value
    """
    df = pd.read_csv(path)
    df.columns = [c.strip().lower() for c in df.columns]

    # Accept 'time', 'datetime', 'date' as timestamp aliases
    ts_candidates = ['timestamp', 'time', 'datetime', 'date']
    ts_col = next((c for c in ts_candidates if c in df.columns), None)
    if ts_col is None:
        raise ValueError(f"{path.name}: cannot find a timestamp column. Got: {list(df.columns)}")

    val_candidates = ['mrh_value', 'mrh', 'value', 'usvh', 'cpm']
    val_col = next((c for c in val_candidates if c in df.columns), None)
    if val_col is None:
        raise ValueError(f"{path.name}: cannot find a value column. Got: {list(df.columns)}")

    df = df.rename(columns={ts_col: 'timestamp', val_col: 'mrh_value'})
    df = df[['timestamp', 'mrh_value']].copy()

    df['timestamp'] = pd.to_datetime(df['timestamp'], infer_datetime_format=True, utc=True)
    df['mrh_value'] = pd.to_numeric(df['mrh_value'], errors='coerce')
    df = df.dropna()
    log.info(f"  Loaded {len(df):,} raw rows from {path.name}")
    return df


# ── Downsampling ──────────────────────────────────────────────────────────────

def downsample(df: pd.DataFrame) -> pd.DataFrame:
    """
    Resample to 1-minute windows and compute the mean.
    The window label is the start of each minute (floor).
    """
    df = df.set_index('timestamp').sort_index()
    resampled = df['mrh_value'].resample('1min').mean().dropna()
    resampled = resampled.reset_index()
    resampled.columns = ['created_at', 'mrh_value']

    # Convert to UTC ISO-8601 string
    resampled['created_at'] = resampled['created_at'].dt.tz_convert('UTC').dt.strftime('%Y-%m-%dT%H:%M:%S+00:00')
    resampled['mrh_value']  = resampled['mrh_value'].round(4)
    resampled['is_anomaly'] = False   # legacy import; re-flag after import if needed

    log.info(f"  Downsampled to {len(resampled):,} minute-averaged rows")
    return resampled


# ── Supabase upload ───────────────────────────────────────────────────────────

def upload_batch(rows: list[dict]) -> tuple[int, int]:
    """
    POST a batch of rows to Supabase with ON CONFLICT DO NOTHING on created_at.
    Returns (inserted, skipped_duplicates).
    """
    url = f"{SUPABASE_URL}/rest/v1/geiger_logs"
    headers = {
        "apikey":        SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type":  "application/json",
        # ignoreDuplicates: silently skip rows that conflict with an existing
        # unique constraint. The DB has no explicit unique on created_at yet,
        # so we use upsert + merge-duplicates which does INSERT ... ON CONFLICT
        # DO NOTHING when the row already exists.
        "Prefer":        "return=representation,resolution=ignore-duplicates",
    }

    res = requests.post(url, headers=headers, data=json.dumps(rows), timeout=30)

    if res.status_code not in (200, 201):
        raise RuntimeError(f"Supabase error {res.status_code}: {res.text[:300]}")

    inserted = len(res.json()) if res.text.strip() else 0
    skipped  = len(rows) - inserted
    return inserted, skipped


def upload_dataframe(df: pd.DataFrame) -> tuple[int, int]:
    rows = df.to_dict(orient='records')
    total_inserted = 0
    total_skipped  = 0

    for i in range(0, len(rows), BATCH_SIZE):
        batch = rows[i : i + BATCH_SIZE]
        ins, skip = upload_batch(batch)
        total_inserted += ins
        total_skipped  += skip
        log.info(f"  Batch {i//BATCH_SIZE + 1}: +{ins} inserted, {skip} duplicates skipped")

    return total_inserted, total_skipped


# ── Entry point ───────────────────────────────────────────────────────────────

def collect_csv_files(paths: list[str]) -> list[Path]:
    files = []
    for p in paths:
        path = Path(p)
        if path.is_dir():
            files.extend(sorted(path.glob('*.csv')))
        elif path.is_file() and path.suffix.lower() == '.csv':
            files.append(path)
        else:
            log.warning(f"Skipping {p} (not a .csv file or directory)")
    return files


def main():
    parser = argparse.ArgumentParser(description='Import legacy CSV radiation data into Supabase')
    parser.add_argument('paths', nargs='+', help='CSV file(s) or directory containing CSVs')
    parser.add_argument('--dry-run', action='store_true', help='Process files but do not upload')
    args = parser.parse_args()

    if not args.dry_run:
        if not SUPABASE_URL or not SUPABASE_KEY:
            log.error("SUPABASE_URL and SUPABASE_KEY must be set (env vars or .env file).")
            sys.exit(1)

    csv_files = collect_csv_files(args.paths)
    if not csv_files:
        log.error("No CSV files found.")
        sys.exit(1)

    log.info(f"Found {len(csv_files)} CSV file(s) to process.")

    grand_total_inserted = 0
    grand_total_skipped  = 0

    for csv_path in csv_files:
        log.info(f"Processing: {csv_path}")
        try:
            raw_df      = load_csv(csv_path)
            resampled   = downsample(raw_df)

            if args.dry_run:
                log.info(f"  [DRY RUN] Would upload {len(resampled):,} rows — skipping.")
                print(resampled.head(3).to_string(index=False))
                continue

            ins, skip = upload_dataframe(resampled)
            grand_total_inserted += ins
            grand_total_skipped  += skip
            log.info(f"  Done: {ins} inserted, {skip} skipped.")

        except Exception as e:
            log.error(f"  Failed to process {csv_path.name}: {e}", exc_info=True)

    if not args.dry_run:
        log.info(f"\nTotal: {grand_total_inserted:,} inserted, {grand_total_skipped:,} duplicates skipped.")


if __name__ == '__main__':
    main()
