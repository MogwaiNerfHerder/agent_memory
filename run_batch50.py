"""Batch driver: run extract_meeting.py --execute over ~50 real meeting guids,
sequentially, logging per-meeting results and timing. Must be run with CWD set
to C:\\Work\\projectsheets so cortado_manager.py's relative credentials/ path
resolves (extract_meeting.py's fetch_meeting() chdirs into --cortado-skill-dir
before the live API call).
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

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

# Resume support: skip guids that already have a logged result (success or
# failure) from a prior run of this driver, rather than re-running (and
# re-paying for) meetings that already finished. This matters here because
# the batch is long enough (~5min/meeting * 50 = hours) that it has already
# been interrupted once by an out-of-band process kill unrelated to this code.
already_done = set()
if LOG_PATH.exists():
    for line in LOG_PATH.read_text(encoding="utf-8").splitlines():
        if line.strip():
            already_done.add(json.loads(line)["guid"])
    print(f"Resuming: {len(already_done)} guid(s) already logged, skipping those.", flush=True)

results = []
for i, guid in enumerate(GUIDS, start=1):
    if guid in already_done:
        continue
    t0 = time.time()
    proc = subprocess.run(
        [sys.executable, EXTRACT_PY,
         "--client-slug", "batch-test",
         "--meeting-guid", guid,
         "--cortado-skill-dir", SKILL_DIR,
         "--db", TESTDB,
         "--execute"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=CHILD_ENV, timeout=1200,
    )
    elapsed = time.time() - t0
    ok = proc.returncode == 0
    entry = {
        "i": i, "guid": guid, "ok": ok, "elapsed_s": round(elapsed, 1),
        "stdout_tail": proc.stdout[-1500:], "stderr_tail": proc.stderr[-800:] if not ok else "",
    }
    results.append(entry)
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")
    print(f"[{i}/{len(GUIDS)}] {guid} ok={ok} elapsed={elapsed:.1f}s", flush=True)

ok_count = sum(1 for r in results if r["ok"])
print(f"\nDone. {ok_count}/{len(results)} succeeded.")
