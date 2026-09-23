"""Batch driver: run extract_meeting.py --execute over ~50 real meeting guids,
sequentially, logging per-meeting results and timing. Must be run with CWD set
to C:\\Work\\projectsheets so cortado_manager.py's relative credentials/ path
resolves (extract_meeting.py's fetch_meeting() chdirs into --cortado-skill-dir
before the live API call).

Resolves each meeting's real client via resolve_client.py instead of bucketing
everything under one shared test client -- confirmed necessary after the
shared-bucket run showed ~25-30 "new" entities per meeting: not a bug, just
11+ different real companies' entire casts piling into one fake entity pool,
inflating both cost (bigger entity inventory sent on every call) and
correctness (entities from unrelated companies mixed together).
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from resolve_client import build_domain_keyword_index, resolve_client_for_meeting_with_fallback
from seed_from_cortado import Cortado

# The child `python extract_meeting.py` process writes its own stdout/stderr
# using Windows' default console codepage (cp1252) unless explicitly told
# otherwise -- extract_meeting.py's own print statements contain an em-dash
# ("[Pass A -- Resolve]"), which isn't valid UTF-8 as cp1252 bytes, and this
# driver was decoding strictly as UTF-8 -> UnicodeDecodeError, which then
# aborted communicate() before stdout/stderr got set at all (the follow-on
# TypeError). PYTHONIOENCODING forces the child itself to write UTF-8.
CHILD_ENV = dict(os.environ, PYTHONIOENCODING="utf-8")

GUIDS = Path("C:/Work/agent_memory/batch50_guids.txt").read_text().strip().splitlines()
TESTDB = sys.argv[1] if len(sys.argv) > 1 else r"C:\Users\DAVID~1.RUS\AppData\Local\Temp\agent_memory_batch50_test.db"
SKILL_DIR = r"C:\Work\projectsheets\.claude\commands\cortado-api"
EXTRACT_PY = r"C:\Work\agent_memory\extract_meeting.py"
LOG_PATH = Path("C:/Work/agent_memory/batch50_results.jsonl")
LOCAL_TRANSCRIPT_GLOB = "C:/Work/marketing_commercial_intelligence/data/transcripts/*.json"

# Maps resolve_client.py's result status to the attribution fields
# extract_meeting.py now records honestly (see its --attribution-source/
# --attribution-confidence, added alongside this driver).
ATTRIBUTION = {
    "resolved": ("cortado_account", "certain"),
    "resolved_via_domain": ("attendee_domain", "likely"),
    "resolved_via_title": ("content_inference", "guessed"),
    "cross_portfolio": ("manual", "guessed"),
    "unassigned": ("manual", "guessed"),
}


def load_local_meeting(guid: str) -> dict | None:
    import glob
    matches = glob.glob(f"C:/Work/marketing_commercial_intelligence/data/transcripts/*{guid}*.json")
    if not matches:
        return None
    return json.loads(Path(matches[0]).read_text(encoding="utf-8"))


# Resume support: skip guids that already have a logged result (success or
# failure) from a prior run of this driver, rather than re-running (and
# re-paying for) meetings that already finished.
already_done = set()
if LOG_PATH.exists():
    n_skipped_failures = 0
    for line in LOG_PATH.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        # Only successes count as "done" -- a logged failure (including the
        # empty-stdout/stderr kind caused by the out-of-band memory-pressure
        # kills this environment has hit repeatedly) should be retried on
        # resume, not silently skipped forever just because it has a log line.
        if row["ok"]:
            already_done.add(row["guid"])
        else:
            n_skipped_failures += 1
    print(f"Resuming: {len(already_done)} succeeded guid(s) skipped, "
          f"{n_skipped_failures} failed guid(s) will be retried.", flush=True)

cortado = Cortado(SKILL_DIR)

import sqlite3
conn = sqlite3.connect(TESTDB)
conn.execute("PRAGMA foreign_keys = ON")
index = build_domain_keyword_index(conn, LOCAL_TRANSCRIPT_GLOB)
print(
    f"Client-resolution index: {len(index.domain_to_account)} unambiguous domains, "
    f"{len(index.domain_to_multi_accounts)} cross-portfolio domains, "
    f"{len(index.account_to_client)} known clients so far.",
    flush=True,
)

results = []
for i, guid in enumerate(GUIDS, start=1):
    if guid in already_done:
        continue

    meeting = load_local_meeting(guid)
    if meeting is None:
        entry = {"i": i, "guid": guid, "ok": False, "elapsed_s": 0,
                  "stdout_tail": "", "stderr_tail": "no local transcript cache for this guid"}
        results.append(entry)
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
        print(f"[{i}/{len(GUIDS)}] {guid} SKIPPED: no local transcript", flush=True)
        continue

    resolution = resolve_client_for_meeting_with_fallback(cortado, conn, meeting, index)
    # a newly-created client changes the index (account_to_client/client_names) --
    # rebuild it so the NEXT meeting in this batch sees it too, not just future runs.
    index = build_domain_keyword_index(conn, LOCAL_TRANSCRIPT_GLOB)

    if resolution.client_slug is None:  # account_fetch_failed
        entry = {"i": i, "guid": guid, "ok": False, "elapsed_s": 0,
                  "stdout_tail": "", "stderr_tail": f"client resolution failed: {resolution.reason}"}
        results.append(entry)
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
        print(f"[{i}/{len(GUIDS)}] {guid} SKIPPED: {resolution.reason}", flush=True)
        continue

    attribution_source, attribution_confidence = ATTRIBUTION[resolution.status]

    t0 = time.time()
    proc = subprocess.run(
        [sys.executable, EXTRACT_PY,
         "--client-slug", resolution.client_slug,
         "--meeting-guid", guid,
         "--cortado-skill-dir", SKILL_DIR,
         "--db", TESTDB,
         "--attribution-source", attribution_source,
         "--attribution-confidence", attribution_confidence,
         "--execute"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=CHILD_ENV, timeout=1200,
    )
    elapsed = time.time() - t0
    ok = proc.returncode == 0
    entry = {
        "i": i, "guid": guid, "ok": ok, "elapsed_s": round(elapsed, 1),
        "client_slug": resolution.client_slug, "resolution_status": resolution.status,
        "resolution_reason": resolution.reason,
        "stdout_tail": proc.stdout[-1500:], "stderr_tail": proc.stderr[-800:] if not ok else "",
    }
    results.append(entry)
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")
    print(f"[{i}/{len(GUIDS)}] {guid} -> client={resolution.client_slug} ({resolution.status}) "
          f"ok={ok} elapsed={elapsed:.1f}s", flush=True)

ok_count = sum(1 for r in results if r["ok"])
print(f"\nDone. {ok_count}/{len(results)} succeeded.")
