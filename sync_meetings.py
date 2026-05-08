#!/usr/bin/env python3
"""
sync_meetings.py — Pull new meetings from Cortado for a client and extract them.

Fetches all meetings for a given Cortado account GUID, upserts any not yet
in source_meeting, then runs extract_meeting.py --execute on any that have
not yet been extracted (content_extracted_at IS NULL).

Usage:
    python3 sync_meetings.py --client-slug hig_growth_partners \\
        --account-guid c3955a7b-afee-4643-9ae9-38b95a6a7423 \\
        [--db ~/work/agent_memory/agent_memory.db] \\
        [--cortado-skill-dir ~/.clawdbot/skills/cortado-api] \\
        [--extract-script ~/work/agent_memory/extract_meeting.py] \\
        [--max-parallel 2] \\
        [--dry-run]
"""

import argparse
import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path


# ---------------------------------------------------------------------------
# Cortado client (minimal — just enough to list and describe meetings)
# ---------------------------------------------------------------------------

def load_cortado(skill_dir):
    skill_dir = Path(os.path.expanduser(skill_dir)).resolve()
    script = skill_dir / "scripts" / "cortado_manager.py"
    if not script.exists():
        raise SystemExit(f"cortado_manager.py not found at {script}")
    old_cwd = os.getcwd()
    os.chdir(skill_dir)
    try:
        spec = importlib.util.spec_from_file_location("cortado_manager", str(script))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        headers, auth = mod.get_auth()
        return mod, headers, auth
    finally:
        os.chdir(old_cwd)


def fetch_meetings_for_account(mod, headers, auth, account_guid):
    """Return list of meeting dicts belonging to the given account_guid."""
    all_meetings = mod.fetch_all(f"{mod.BASE_URL}/meetings/", headers, auth)
    return [m for m in all_meetings if m.get("account") == account_guid]


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def open_db(db_path):
    conn = sqlite3.connect(os.path.expanduser(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def get_client_id(conn, slug):
    row = conn.execute("SELECT client_id FROM client WHERE slug=?", (slug,)).fetchone()
    if not row:
        raise SystemExit(f"Client '{slug}' not found in database.")
    return row["client_id"]


def upsert_source_meeting(conn, client_id, guid, occurred_at, name):
    url = f"https://cg.cortadogroup.ai/meetings/console/{guid}/"
    row = conn.execute(
        "SELECT source_meeting_id, content_extracted_at FROM source_meeting WHERE external_id=?",
        (guid,),
    ).fetchone()
    if row:
        conn.execute(
            "UPDATE source_meeting SET client_id=?, occurred_at=?, url=?, title=? WHERE source_meeting_id=?",
            (client_id, occurred_at, url, name or None, row["source_meeting_id"]),
        )
        conn.commit()
        return row["source_meeting_id"], row["content_extracted_at"] is not None
    else:
        cur = conn.execute(
            """INSERT INTO source_meeting (client_id, external_id, occurred_at, url, title,
               attribution_source, attribution_confidence)
               VALUES (?, ?, ?, ?, ?, 'cortado', 'confirmed')""",
            (client_id, guid, occurred_at, url, name or None),
        )
        conn.commit()
        return cur.lastrowid, False


def pending_extractions(conn, client_id):
    """Return list of external_ids for meetings not yet extracted."""
    rows = conn.execute(
        """SELECT external_id FROM source_meeting
           WHERE client_id=? AND content_extracted_at IS NULL
           ORDER BY occurred_at ASC""",
        (client_id,),
    ).fetchall()
    return [r["external_id"] for r in rows]


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------

def run_extract(extract_script, client_slug, meeting_guid, cortado_skill_dir, db_path, dry_run):
    cmd = [
        sys.executable, os.path.expanduser(extract_script),
        "--client-slug", client_slug,
        "--meeting-guid", meeting_guid,
        "--cortado-skill-dir", os.path.expanduser(cortado_skill_dir),
        "--db", os.path.expanduser(db_path),
    ]
    if dry_run:
        cmd.append("--dry-run")
    else:
        cmd.append("--execute")

    label = f"[{meeting_guid[:8]}]"
    print(f"{label} starting extract_meeting.py", flush=True)
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"{label} FAILED (rc={result.returncode})\n{result.stderr[-1000:]}", flush=True)
        return False
    print(f"{label} OK", flush=True)
    return True


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--client-slug", required=True)
    p.add_argument("--account-guid", required=True)
    p.add_argument("--db", default="~/work/agent_memory/agent_memory.db")
    p.add_argument("--cortado-skill-dir", default="~/.clawdbot/skills/cortado-api")
    p.add_argument("--extract-script", default="~/work/agent_memory/extract_meeting.py")
    p.add_argument("--max-parallel", type=int, default=1,
                   help="Parallel extraction workers (default 1 — sequential)")
    p.add_argument("--dry-run", action="store_true",
                   help="Upsert meetings but don't run extraction")
    args = p.parse_args()

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"[sync_meetings] {ts} client={args.client_slug} account={args.account_guid}", flush=True)

    # 1. Load Cortado, fetch meetings for this account
    mod, headers, auth = load_cortado(args.cortado_skill_dir)
    print("[sync_meetings] fetching meetings from Cortado...", flush=True)
    meetings = fetch_meetings_for_account(mod, headers, auth, args.account_guid)
    print(f"[sync_meetings] {len(meetings)} meetings found for account", flush=True)

    # 2. Upsert all into source_meeting
    conn = open_db(args.db)
    client_id = get_client_id(conn, args.client_slug)

    new_count = 0
    for m in meetings:
        guid = m.get("guid") or m.get("external_id")
        occurred_at = m.get("occurred_at")
        name = m.get("name", "")
        if not guid:
            continue
        _, already_extracted = upsert_source_meeting(conn, client_id, guid, occurred_at, name)
        if not already_extracted:
            new_count += 1

    print(f"[sync_meetings] {new_count} meeting(s) pending extraction", flush=True)

    # 3. Extract anything not yet done
    to_extract = pending_extractions(conn, client_id)
    conn.close()

    if not to_extract:
        print("[sync_meetings] nothing to extract — done.", flush=True)
        return

    print(f"[sync_meetings] extracting {len(to_extract)} meeting(s) "
          f"(parallel={args.max_parallel})...", flush=True)

    success = fail = 0
    if args.max_parallel <= 1:
        for guid in to_extract:
            ok = run_extract(args.extract_script, args.client_slug, guid,
                             args.cortado_skill_dir, args.db, args.dry_run)
            if ok:
                success += 1
            else:
                fail += 1
    else:
        with ThreadPoolExecutor(max_workers=args.max_parallel) as pool:
            futs = {
                pool.submit(run_extract, args.extract_script, args.client_slug, guid,
                            args.cortado_skill_dir, args.db, args.dry_run): guid
                for guid in to_extract
            }
            for fut in as_completed(futs):
                if fut.result():
                    success += 1
                else:
                    fail += 1

    print(f"[sync_meetings] done. success={success} fail={fail}", flush=True)


if __name__ == "__main__":
    main()
