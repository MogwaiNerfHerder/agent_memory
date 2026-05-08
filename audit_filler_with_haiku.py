"""
audit_filler_with_haiku.py - Send post-stripper transcripts to Haiku for residual-filler audit.

For each meeting (specified by --meeting-guid or auto-detected from a client tenant's
extracted meetings), fetch the cleaned transcript and ask Haiku to identify additional
patterns of noise/filler that the deterministic stripper missed. Aggregates patterns
across meetings to inform extension of the cleaner.

Usage:
    audit_filler_with_haiku.py --client-slug hig_growth_partners
    audit_filler_with_haiku.py --meeting-guid <guid> [--meeting-guid <guid>] ...
    audit_filler_with_haiku.py --all-extracted   # all source_meeting rows in db
"""

import argparse
import importlib.util
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from collections import defaultdict
from pathlib import Path


SKILL_DIR = Path(os.path.expanduser("~/.clawdbot/skills/cortado-api"))
CLEAN_SCRIPT = Path(os.path.expanduser("~/.clawdbot/skills/clean-transcript/scripts/clean_transcript.py"))


def cortado_module():
    old_cwd = os.getcwd()
    os.chdir(SKILL_DIR)
    try:
        spec = importlib.util.spec_from_file_location(
            "cortado_manager", str(SKILL_DIR / "scripts" / "cortado_manager.py")
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod, *mod.get_auth()
    finally:
        os.chdir(old_cwd)


def load_clean():
    spec = importlib.util.spec_from_file_location("ct", str(CLEAN_SCRIPT))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.clean


def fetch_meeting(guid):
    m, h, a = cortado_module()
    r = m.api_request("GET", f"{m.BASE_URL}/meetings/{guid}/", h, a)
    if r.status_code != 200:
        raise SystemExit(f"cortado meeting {guid}: {r.status_code} {r.text[:200]}")
    return r.json()


def render_transcript(meeting):
    clean_arr = meeting.get("transcript_clean")
    if isinstance(clean_arr, list):
        lines = []
        for turn in clean_arr:
            if not isinstance(turn, dict):
                continue
            t = turn.get("t") or ""
            sp = turn.get("speaker") or ""
            tx = turn.get("text") or ""
            lines.append(f"[{t}] {sp}: {tx}")
        return "\n".join(lines)
    return meeting.get("transcript_text") or meeting.get("transcript") or ""


AUDIT_PROMPT = """You are auditing a meeting transcript for RESIDUAL FILLER / NOISE.

The transcript has already been processed by a deterministic stripper that removed:
  - Standalone fillers (um, uh, ahh, mhm, hmm, uh-huh)
  - Filler phrases (you know, I mean, kind of, sort of, basically, essentially, literally, actually)
  - Doubled words (the the, and and)
  - False-start repeats (I think — I think we should)

Your job: identify patterns STILL PRESENT that could be deterministically stripped without
losing meaning. For each pattern, give:
  - category (one of the categories below)
  - a concrete pattern description (regex-style if possible, or a clear rule)
  - 2-4 verbatim examples copy-pasted from the transcript
  - estimated_occurrences (rough count in this transcript)
  - removal_safety: "safe" | "risky" | "context-dependent"

CATEGORIES:
  ack_only           - turns that contribute nothing: "Yep.", "Mhm.", "Got it.", "Okay.", "Right.", "Cool."
  audio_visual_ops   - "can you hear me", "let me share my screen", "I'll mute", "video on"
  household          - addressing kids/dogs/family mid-meeting (e.g. "Bob, finish your salad")
  side_smalltalk     - food, weather, traffic, kids' activities, sports asides
  transcription_garble - garbled phrases that mean nothing (e.g. unintelligible speaker)
  procedural         - "good morning", "thanks everyone", "let's wrap up", standardized openings/closings
  like_filler        - "like" used as a discourse particle, not a verb (e.g. "it's, like, expensive")
  speaker_self_address - speaker correcting themselves audibly ("oh wait, I meant—")
  other              - anything else worth flagging

Output a single JSON object — no prose, no fences:

{
  "transcript_meta": {"name": "<meeting name>", "occurred_at": "<iso>", "char_count": N},
  "patterns": [
    {
      "category": "ack_only",
      "pattern": "standalone turn matching ^(yep|mhm|got it|right|okay|cool)\\.?$",
      "examples": ["Yep.", "Mhm.", "Got it.", "Right."],
      "estimated_occurrences": 18,
      "removal_safety": "safe"
    }
  ]
}
"""


def invoke_haiku(prompt, claude_bin="claude", timeout=240):
    if not shutil.which(claude_bin):
        raise SystemExit(f"`{claude_bin}` not found on PATH.")
    cmd = [claude_bin, "-p", "--model", "claude-haiku-4-5-20251001"]
    proc = subprocess.run(cmd, input=prompt, text=True, capture_output=True, timeout=timeout)
    if proc.returncode != 0:
        raise SystemExit(f"haiku failed: {proc.stderr[:400]}")
    return proc.stdout


def parse_json(text):
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    s = text.find("{"); e = text.rfind("}")
    if s == -1 or e == -1 or e <= s:
        return None
    try:
        return json.loads(text[s:e+1])
    except json.JSONDecodeError:
        return None


def audit_one(guid, clean_fn, out_dir, claude_bin):
    meeting = fetch_meeting(guid)
    raw = render_transcript(meeting)
    cleaned = clean_fn(raw, strip_timestamps=False, aggressive=False) if raw else ""
    pre = len(raw); post = len(cleaned)
    print(f"  {guid}  '{meeting.get('name')}'  raw={pre} cleaned={post} ({100*(pre-post)/max(pre,1):.0f}% cut)")

    if post < 200:
        print(f"    skipping (transcript too short)")
        return None

    full_prompt = f"""{AUDIT_PROMPT}

================================================================
MEETING: {meeting.get('name')}  occurred_at={meeting.get('occurred_at')}
TRANSCRIPT (post-cleaner, {post} chars):

{cleaned}

================================================================
Emit the JSON object now. Begin with `{{` and end with `}}`."""
    try:
        raw_resp = invoke_haiku(full_prompt, claude_bin=claude_bin)
    except SystemExit as e:
        print(f"    haiku call failed: {e}")
        return None
    (out_dir / f"{guid}.response.json").write_text(raw_resp)
    parsed = parse_json(raw_resp)
    if parsed is None:
        print("    JSON parse failed; raw saved")
        return None
    (out_dir / f"{guid}.parsed.json").write_text(json.dumps(parsed, indent=2, ensure_ascii=False))
    return parsed


def aggregate(per_meeting):
    """Combine pattern findings across meetings into a category roll-up."""
    by_category = defaultdict(list)  # category -> [{pattern, examples, occurrences, safety, meeting_guid}]
    for guid, parsed in per_meeting.items():
        for p in (parsed.get("patterns") or []):
            by_category[p.get("category", "other")].append({
                "pattern": p.get("pattern"),
                "examples": p.get("examples") or [],
                "occurrences": p.get("estimated_occurrences"),
                "safety": p.get("removal_safety"),
                "meeting": guid,
            })
    return by_category


def render_aggregate(by_category):
    lines = []
    cat_sorted = sorted(by_category.items(),
                          key=lambda kv: -sum((x.get("occurrences") or 0) for x in kv[1]))
    for cat, items in cat_sorted:
        total_occ = sum((x.get("occurrences") or 0) for x in items)
        lines.append(f"\n## {cat}  (across {len(items)} meeting findings, ~{total_occ} occurrences)")
        # Group by pattern text (rough)
        by_pat = defaultdict(list)
        for it in items:
            by_pat[it.get("pattern") or "(no-pattern)"].append(it)
        for pat, group in sorted(by_pat.items(), key=lambda kv: -sum((x.get("occurrences") or 0) for x in kv[1])):
            occ = sum((x.get("occurrences") or 0) for x in group)
            safeties = {x.get("safety") for x in group if x.get("safety")}
            lines.append(f"  - PATTERN: {pat}  (≈{occ} occurrences, safety={','.join(sorted(safeties)) or '?'})")
            seen = set()
            for it in group:
                for ex in (it.get("examples") or [])[:3]:
                    if ex and ex not in seen:
                        lines.append(f"      example: {ex!r}")
                        seen.add(ex)
                        if len(seen) >= 4:
                            break
                if len(seen) >= 4:
                    break
    return "\n".join(lines)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--client-slug")
    p.add_argument("--meeting-guid", action="append", default=[])
    p.add_argument("--all-extracted", action="store_true",
                    help="Audit all source_meeting rows in the db (filtered by --client-slug if given)")
    p.add_argument("--db", default=os.path.join(os.path.dirname(__file__), "agent_memory.db"))
    p.add_argument("--out-dir", default="/tmp/filler_audit")
    p.add_argument("--claude-bin", default="claude")
    args = p.parse_args()

    out_dir = Path(args.out_dir); out_dir.mkdir(parents=True, exist_ok=True)
    clean_fn = load_clean()

    guids = list(args.meeting_guid)
    if args.all_extracted or (args.client_slug and not guids):
        conn = sqlite3.connect(os.path.expanduser(args.db))
        sql = "SELECT external_id FROM source_meeting"
        params = []
        if args.client_slug:
            sql += " WHERE client_id=(SELECT client_id FROM client WHERE slug=?)"
            params.append(args.client_slug)
        sql += " ORDER BY occurred_at"
        for r in conn.execute(sql, params):
            guids.append(r[0])

    if not guids:
        raise SystemExit("No meeting guids supplied (try --meeting-guid <guid> or --all-extracted)")

    print(f"Auditing {len(guids)} meeting(s)…")
    per_meeting = {}
    for guid in guids:
        try:
            parsed = audit_one(guid, clean_fn, out_dir, args.claude_bin)
            if parsed is not None:
                per_meeting[guid] = parsed
        except Exception as e:
            print(f"  {guid}: error {e}")

    by_category = aggregate(per_meeting)
    summary_path = out_dir / "AGGREGATE.md"
    summary_path.write_text("# Residual filler audit\n\n" + render_aggregate(by_category))
    print()
    print(render_aggregate(by_category))
    print(f"\nFull aggregate: {summary_path}")


if __name__ == "__main__":
    main()
