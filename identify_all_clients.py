"""Client-identification-only pass across every locally cached transcript --
no extraction, no Codex, no LLM calls, no DB writes. Just resolves which
client each meeting belongs to (reusing the same resolve_client.py logic
that extraction uses) and, when confidently resolved, patches the local
transcript file's `account` field so extraction later takes the fast,
certain, direct-account-match path instead of re-deriving it.

Read-only against the DB (only reads client rows, never creates one) --
deliberately does NOT call resolve_client_for_meeting_with_fallback()
directly, since its final "unassigned" branch creates a new isolated
holding client row per call. Creating ~3,000 throwaway client rows for
meetings that may never even get extracted is the wrong tradeoff; genuinely
unresolvable meetings are just reported, not written anywhere, and get
their isolated holding client the normal way if/when they're actually
extracted.

Must be run with CWD set to C:\\Work\\projectsheets, same reason as
run_batch50.py (cortado_manager.py's relative credentials/ path).
"""
import glob
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from resolve_client import (
    INTERNAL_DOMAIN,
    _cross_portfolio_candidates,
    _resolve_via_domain,
    _resolve_via_title_keyword,
    build_domain_keyword_index,
)

import sqlite3

TESTDB = sys.argv[1] if len(sys.argv) > 1 else r"C:\Users\DAVID~1.RUS\AppData\Local\Temp\agent_memory_batch50_test.db"
TRANSCRIPT_GLOB = "C:/Work/marketing_commercial_intelligence/data/transcripts/*.json"

conn = sqlite3.connect(TESTDB)
conn.execute("PRAGMA foreign_keys = ON")
index = build_domain_keyword_index(conn, TRANSCRIPT_GLOB)
print(f"Index: {len(index.domain_to_account)} unambiguous domains, "
      f"{len(index.domain_to_multi_accounts)} cross-portfolio domains, "
      f"{len(index.account_to_client)} known clients.", flush=True)


def account_guid_for_client(client_id: int) -> str | None:
    row = conn.execute("SELECT cortado_client_id FROM client WHERE client_id=?", (client_id,)).fetchone()
    return row[0] if row else None


counts = {"already_linked": 0, "resolved_via_domain": 0, "resolved_via_title": 0,
          "cross_portfolio": 0, "unresolved": 0, "read_error": 0}
patched = 0

for path in glob.glob(TRANSCRIPT_GLOB):
    try:
        meeting = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        counts["read_error"] += 1
        continue

    if meeting.get("account"):
        counts["already_linked"] += 1
        continue

    via_domain = _resolve_via_domain(index, meeting)
    if via_domain:
        client_id, slug = via_domain
        account_guid = account_guid_for_client(client_id)
        counts["resolved_via_domain"] += 1
        if account_guid:
            meeting["account"] = account_guid
            Path(path).write_text(json.dumps(meeting, indent=1), encoding="utf-8")
            patched += 1
        continue

    via_title = _resolve_via_title_keyword(index, meeting)
    if via_title:
        client_id, slug = via_title
        account_guid = account_guid_for_client(client_id)
        counts["resolved_via_title"] += 1
        if account_guid:
            meeting["account"] = account_guid
            Path(path).write_text(json.dumps(meeting, indent=1), encoding="utf-8")
            patched += 1
        continue

    cross = _cross_portfolio_candidates(index, meeting)
    if cross:
        counts["cross_portfolio"] += 1
        continue

    counts["unresolved"] += 1

print()
for k, v in counts.items():
    print(f"  {k}: {v}")
print(f"\nPatched {patched} local transcript files with a resolved account guid.")
