"""
backfill_memory_sensitivity.py - Set sensitivity on existing z_memory rows by re-reading
the saved LLM response JSONs in /tmp/violet_*.json.

Each saved response is a JSON object with a "notes" array; each note has memory_type,
content, sensitivity, justification. We match by exact content + memory_type and update
the sensitivity if not currently set.

Usage:
    backfill_memory_sensitivity.py --client-slug hig_growth_partners --responses /tmp/violet_*.json
"""

import argparse
import glob
import json
import os
import sqlite3
import sys


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--client-slug", required=True)
    p.add_argument("--db", default=os.path.join(os.path.dirname(__file__), "agent_memory.db"))
    p.add_argument("--responses", nargs="+", help="Paths or globs to JSON response files")
    args = p.parse_args()

    paths = []
    for r in (args.responses or []):
        if any(ch in r for ch in "*?["):
            paths.extend(glob.glob(r))
        else:
            paths.append(r)
    if not paths:
        print("No response files matched.", file=sys.stderr)
        sys.exit(1)

    conn = sqlite3.connect(os.path.expanduser(args.db))
    conn.execute("PRAGMA foreign_keys = ON")
    client_id = conn.execute("SELECT client_id FROM client WHERE slug=?", (args.client_slug,)).fetchone()[0]

    total_updated = 0
    for path in paths:
        try:
            with open(path) as f:
                txt = f.read()
        except OSError:
            continue
        # Strip code fences if present
        txt = txt.strip()
        if txt.startswith("```"):
            lines = txt.splitlines()
            if lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].startswith("```"):
                lines = lines[:-1]
            txt = "\n".join(lines).strip()
        try:
            data = json.loads(txt)
        except json.JSONDecodeError:
            print(f"  skip {path}: not valid JSON")
            continue

        file_updates = 0
        for note in (data.get("notes") or []):
            content = (note.get("content") or "").strip()
            mtype = note.get("memory_type")
            sens = note.get("sensitivity")
            if not content or not mtype or sens not in ("routine", "sensitive", "hr_grade"):
                continue
            n = conn.execute(
                """UPDATE z_memory SET sensitivity=?
                    WHERE client_id=? AND content=? AND memory_type=?
                      AND (sensitivity IS NULL OR sensitivity='routine')""",
                (sens, client_id, content, mtype),
            ).rowcount
            file_updates += n
        conn.commit()
        print(f"  {path}: updated {file_updates} notes")
        total_updated += file_updates

    print(f"\nTotal: {total_updated} notes had sensitivity backfilled")


if __name__ == "__main__":
    main()
