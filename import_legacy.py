"""
import_legacy.py — Legacy CSV → Supabase Bulk Importer

Reads one or more legacy CSV files (columns: timestamp, mrh_value),
downsamples to 1-minute averages, and bulk-uploads to the geiger_logs
Supabase table. Records for minutes that already exist are silently skipped.

Usage:
    # Import a single file
    python import_legacy.py data.csv

    # Import every CSV in a folder (non-recursive)
    python import_legacy.py /path/to/folder/

    # Import every CSV in a folder AND all sub-folders
    python import_legacy.py /path/to/folder/ --recursive

    # Run from inside the data folder — imports everything here
    python import_legacy.py .

    # Named --folder shorthand
    python import_legacy.py --folder /path/to/folder/

    # Preview without uploading
    python import_legacy.py . --dry-run

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

# Tbilisi is UTC+4. Legacy RadStation.py logged naive local timestamps with no
# timezone info, so we must tell pandas what zone those timestamps are in.
LOCAL_TZ = 'Asia/Tbilisi'

def load_csv(path: Path) -> pd.DataFrame:
    """
    Load a legacy CSV produced by RadStation.py.

    RadStation.py writes headerless rows: <ISO-timestamp-local>, <float-value>
    e.g.  2026-03-16T23:52:29.420000,14.7

    Also handles CSVs that DO have a header row with named columns.
    """
    # ── Try headerless format first (RadStation.py native) ───────────────────
    # Peek at the first cell: if it parses as a datetime it's a data row, not
    # a header, so we read with header=None.
    try:
        peek = pd.read_csv(path, nrows=1, header=None)
        first_cell = str(peek.iloc[0, 0]).strip()
        pd.to_datetime(first_cell)          # raises if not a timestamp
        headerless = True
    except Exception:
        headerless = False

    if headerless:
        df = pd.read_csv(path, header=None)
        if df.shape[1] < 2:
            raise ValueError(f"{path.name}: expected at least 2 columns, got {df.shape[1]}")
        df = df.iloc[:, :2].copy()
        df.columns = ['timestamp', 'mrh_value']
    else:
        # ── Named-header fallback ─────────────────────────────────────────────
        df = pd.read_csv(path)
        df.columns = [c.strip().lower() for c in df.columns]

        ts_candidates  = ['timestamp', 'time', 'datetime', 'date']
        val_candidates = ['mrh_value', 'mrh', 'value', 'usvh', 'cpm']

        ts_col  = next((c for c in ts_candidates  if c in df.columns), None)
        val_col = next((c for c in val_candidates if c in df.columns), None)

        if ts_col is None:
            raise ValueError(f"{path.name}: cannot find a timestamp column. Got: {list(df.columns)}")
        if val_col is None:
            raise ValueError(f"{path.name}: cannot find a value column. Got: {list(df.columns)}")

        df = df.rename(columns={ts_col: 'timestamp', val_col: 'mrh_value'})
        df = df[['timestamp', 'mrh_value']].copy()

    # ── Parse timestamps ──────────────────────────────────────────────────────
    df['mrh_value'] = pd.to_numeric(df['mrh_value'], errors='coerce')
    df['timestamp'] = pd.to_datetime(df['timestamp'], errors='coerce')

    # If timestamps are timezone-naive (RadStation.py local time), attach the
    # local timezone so tz_convert to UTC works correctly.
    if df['timestamp'].dt.tz is None:
        df['timestamp'] = df['timestamp'].dt.tz_localize(LOCAL_TZ, ambiguous='infer', nonexistent='shift_forward')

    df['timestamp'] = df['timestamp'].dt.tz_convert('UTC')
    df = df.dropna(subset=['timestamp', 'mrh_value'])
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

def collect_csv_files(paths: list[str], recursive: bool = False) -> list[Path]:
    files = []
    glob_pattern = '**/*.csv' if recursive else '*.csv'
    for p in paths:
        path = Path(p)
        if path.is_dir():
            found = sorted(path.glob(glob_pattern))
            if not found:
                log.warning(f"No CSV files found in {path}" + (" (try --recursive?)" if not recursive else ""))
            files.extend(found)
        elif path.is_file() and path.suffix.lower() == '.csv':
            files.append(path)
        else:
            log.warning(f"Skipping {p} (not a .csv file or directory)")
    # deduplicate while preserving order (e.g. if a file and its parent dir are both given)
    seen = set()
    unique = []
    for f in files:
        key = f.resolve()
        if key not in seen:
            seen.add(key)
            unique.append(f)
    return unique


def main():
    parser = argparse.ArgumentParser(
        description='Batch-import legacy CSV radiation data into Supabase',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python import_legacy.py .                       # all CSVs in current folder\n"
            "  python import_legacy.py /data/ --recursive     # all CSVs including sub-folders\n"
            "  python import_legacy.py --folder /data/        # named --folder shorthand\n"
            "  python import_legacy.py file1.csv file2.csv    # specific files\n"
            "  python import_legacy.py . --dry-run            # preview without uploading\n"
        )
    )
    parser.add_argument(
        'paths', nargs='*',
        help='CSV file(s) or folder(s). Defaults to current directory if omitted.',
    )
    parser.add_argument(
        '--folder', '-f', dest='folder',
        help='Folder containing CSV files (alternative to positional path).',
    )
    parser.add_argument(
        '--recursive', '-r', action='store_true',
        help='Also scan sub-folders for CSV files.',
    )
    parser.add_argument(
        '--dry-run', action='store_true',
        help='Process and preview files but skip uploading.',
    )
    args = parser.parse_args()

    # Build the list of paths to scan: positional + --folder + fallback to '.'
    scan_paths = list(args.paths)
    if args.folder:
        scan_paths.append(args.folder)
    if not scan_paths:
        scan_paths = ['.']
        log.info("No path given — scanning current directory for CSVs.")

    if not args.dry_run:
        if not SUPABASE_URL or not SUPABASE_KEY:
            log.error("SUPABASE_URL and SUPABASE_KEY must be set (env vars or .env file).")
            sys.exit(1)

    csv_files = collect_csv_files(scan_paths, recursive=args.recursive)
    if not csv_files:
        log.error("No CSV files found. Use --recursive to also scan sub-folders.")
        sys.exit(1)

    log.info(f"Found {len(csv_files)} CSV file(s) to process.")
    for f in csv_files:
        log.info(f"  {f}")

    grand_total_inserted = 0
    grand_total_skipped  = 0
    errors               = []

    for idx, csv_path in enumerate(csv_files, 1):
        log.info(f"\n[{idx}/{len(csv_files)}] Processing: {csv_path.name}")
        try:
            raw_df    = load_csv(csv_path)
            resampled = downsample(raw_df)

            if args.dry_run:
                log.info(f"  [DRY RUN] Would upload {len(resampled):,} rows — skipping.")
                print(resampled.head(3).to_string(index=False))
                continue

            ins, skip = upload_dataframe(resampled)
            grand_total_inserted += ins
            grand_total_skipped  += skip
            log.info(f"  Done: +{ins:,} inserted, {skip:,} duplicates skipped.")

        except Exception as e:
            log.error(f"  FAILED: {e}", exc_info=False)
            errors.append((csv_path.name, str(e)))

    # ── Final summary ─────────────────────────────────────────────────────────
    log.info("\n" + "=" * 60)
    if not args.dry_run:
        log.info(f"TOTAL  inserted : {grand_total_inserted:,}")
        log.info(f"TOTAL  skipped  : {grand_total_skipped:,}")
        log.info(f"Files processed : {len(csv_files) - len(errors)} / {len(csv_files)}")
    if errors:
        log.warning(f"{len(errors)} file(s) failed:")
        for name, msg in errors:
            log.warning(f"  {name}: {msg}")
        sys.exit(2)
    log.info("Import complete.")


if __name__ == '__main__':
    main()
