"""
extract_document.py - Multi-pass document extractor (parallel to extract_meeting.py).

Pulls a Box document's text content (from box_files.db) and runs the same three
focused passes used for meetings — resolve, structure, observe — emitting
entities/aliases/drift_hypotheses, edges/events, and notes. Citations link back
to the source_document row for clickable Box URL traceability.

Usage:
    extract_document.py --client-slug hig_growth_partners \\
        --box-file-id 2218725673530 \\
        --box-file-id 2218728278272 \\
        --execute --responses-dir /tmp/doc_extract/

Requires:
  - source_document row already exists (run seed_from_box.py first)
  - box_files.db accessible via `docker exec <container> ...`
  - claude CLI on PATH for --execute mode
"""

import argparse
import importlib.util
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

# Reuse helpers from extract_meeting.py
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import extract_meeting as _em
from extract_meeting import (
    GraphDB,
    quote_in_transcript,
    invoke_claude,
    invoke_claude_json,
    extract_json,
    apply_resolve,
    validate_edges,
    validate_notes,
    build_correction_prompt,
    apply_validated_edges,
    apply_validated_notes,
    render_entity_block,
    render_predicate_block,
    PII_RULE,
    _norm,
)


def quote_in_text_fuzzy(quote, text):
    """Fuzzy fallback: strict substring first; if fail, accept if a contiguous
    >=6-word span from the quote appears in the text. Catches paraphrased
    documents where the model wove real phrases into new sentences."""
    if not quote:
        return False
    if quote_in_transcript(quote, text):
        return True
    q = _norm(quote)
    if len(q) < 8:
        return True
    t = _norm(text)
    # Try 6-word contiguous spans
    words = q.split()
    if len(words) < 6:
        return False
    for i in range(0, len(words) - 5):
        span = " ".join(words[i:i+6])
        if span in t:
            return True
    return False


# ---------------------------------------------------------------------------
# Box content access via docker exec on box_files.db
# ---------------------------------------------------------------------------

def docker_exec_query(container, code):
    proc = subprocess.run(
        ["docker", "exec", container, "python3", "-c", code],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise SystemExit(f"docker exec failed: {proc.stderr[:500]}")
    return proc.stdout


def fetch_box_file(container, box_file_id):
    """Return dict with name, full_path, content."""
    code = f"""
import sqlite3, json, sys
con = sqlite3.connect('file:/data/box_files.db?mode=ro', uri=True)
fid = {box_file_id!r}
file_row = con.execute('SELECT name, full_path, file_type, size FROM files WHERE box_file_id=?', (fid,)).fetchone()
content_row = con.execute('SELECT content FROM file_content WHERE box_file_id=?', (fid,)).fetchone()
out = {{
    'name': file_row[0] if file_row else None,
    'full_path': file_row[1] if file_row else None,
    'file_type': file_row[2] if file_row else None,
    'size': file_row[3] if file_row else None,
    'content': content_row[0] if content_row else None,
}}
print(json.dumps(out))
"""
    return json.loads(docker_exec_query(container, code))


# ---------------------------------------------------------------------------
# Document-specific prompts (adapted from extract_meeting.py)
# ---------------------------------------------------------------------------

PROMPT_A_DOC = f"""You are extracting ENTITY REFERENCES from a Box document (not a meeting transcript).

Your ONLY job in this pass is identity:
  • Who/what is mentioned in this document?
  • The entity inventory below lists each entity with its aliases tagged with KIND. ANY alias kind (including transcription_drift) means "this string already resolves to this entity." When you see such a string, treat it as a reference to the existing entity_id. DO NOT re-propose as drift; DO NOT mint new entity for it.
  • Mint NEW entities for: clearly named real-world organizations/people/products/projects/concepts, even peripherally; documents/studies referenced; repeatedly-referenced unnamed actors (mint a placeholder, confidence=guessed).
  • drift_hypothesis is for new unknown tokens that look like transcription artifacts of an existing entity (rare in documents — these are usually carefully-spelled).

{PII_RULE}

Output a single JSON object — no prose, no fences:

{{
  "summary": "1-3 sentence summary of what this document is",
  "new_entities": [
    {{"temp_id": "n1", "type": "person|company|project|product|document|topic|department|deal|fund|event|concept",
      "canonical_name": "...", "rationale": "..."}}
  ],
  "alias_proposals": [
    {{"entity_ref": "<entity_id or n1>", "alias_text": "...",
      "alias_kind": "name|nickname|formal_name|codename|sfid|...", "confidence": "certain|likely", "rationale": "..."}}
  ],
  "drift_hypotheses": []
}}
"""


PROMPT_B_DOC = f"""You are extracting STRUCTURAL FACTS from a Box document.

Identity has already been resolved in pass A. Your ONLY job in this pass is RELATIONSHIPS and EVENTS:

  • EDGES (subject, predicate, object_or_literal) using the predicate vocabulary.
    Every edge MUST include `justification` — a 10-200-char VERBATIM excerpt from the document. No quote = no edge.
  • EVENTS (entity.type='event'): mint events for things that happen in time — dated milestones, deadlines, scheduled actions, payments, contractual events. Attach timing via the right predicate (scheduled_for/occurred_at/occurred_around/started_at/ended_at).

DOCUMENTS often contain TABLES — if the document is structured (rows × columns), each table row is typically a fact. Examples: a partner-fee table is a set of (rewind --pays_fee_to--> partner) edges with rate as object_literal; a commission plan table is (role --has_commission_rate--> rate) edges.

Edge confidence:
  factual    = the document states it as fact (most document-derived edges)
  stated     = quote/claim attributed to a person within the document
  implied    = clear inference from multiple cells/sentences

Edge sensitivity:
  routine     = standard business facts
  sensitive   = comp numbers, deal terms, internal metrics
  hr_grade    = personnel risk, conflicts, terminations

{PII_RULE}

Output ONE JSON object:

{{
  "events": [
    {{"temp_id": "e1", "canonical_name": "...", "rationale": "..."}}
  ],
  "edges": [
    {{"subject": "<entity_id or e1>",
      "predicate": "predicate_name (must be in approved/proposed vocab)",
      "object": "<entity_id or e1>" OR null,
      "object_literal": "literal value when object is null",
      "object_literal_type": "string|date|datetime|currency|int|percent|url|...",
      "confidence": "stated|implied|factual",
      "sensitivity": "routine|sensitive|hr_grade",
      "notes": "1-line elaboration if helpful, else null",
      "justification": "verbatim document excerpt, 10-200 chars"}}
  ]
}}
"""


PROMPT_C_DOC = f"""You are extracting ITEMS OF NOTE from a Box document.

Identity and structural facts have been captured in earlier passes. Your ONLY job in this pass is the SOFT LAYER — observations a structured edge-graph would miss:
  • decisions / policies the document defines
  • action items implied (e.g., 'Forge migration required by Apr 1, 2026')
  • risks identified (terms with deadlines, asymmetric clauses, concentration risk)
  • themes / patterns across the doc
  • recommendations / required actions
  • observations about structure / outliers / inconsistencies
  • lessons / learnings that should inform future work

Each note MUST include a verbatim 10-200 char `justification` from the document.

{PII_RULE}

Output ONE JSON object:

{{
  "notes": [
    {{"memory_type": "decision|action_item|open_question|risk|observation|theme|recommendation|insight|lesson|policy",
      "content": "1-3 sentences in your own words, precise and self-contained",
      "subject_entities": ["<entity_id>", ...],
      "mentioned_entities": ["<entity_id>", ...],
      "importance": 1..10,
      "sensitivity": "routine|sensitive|hr_grade",
      "tags": "comma,separated,short,tags",
      "justification": "verbatim document excerpt, 10-200 chars"}}
  ]
}}
"""


def build_prompt_doc_resolve(doc, entities):
    return (PROMPT_A_DOC
            + "\n================================================================\n"
            + render_entity_block(entities)
            + "\n================================================================\n"
            + f"DOCUMENT METADATA:\n  box_file_id: {doc['box_file_id']}\n  name: {doc['name']}\n"
            + f"  full_path: {doc['full_path']}\n  file_type: {doc.get('file_type')}\n"
            + "\n================================================================\nDOCUMENT CONTENT:\n"
            + doc["content"]
            + "\n================================================================\n"
            + "Emit the JSON object now.")


def build_prompt_doc_structure(doc, entities, predicates):
    return (PROMPT_B_DOC
            + "\n================================================================\n"
            + render_entity_block(entities)
            + "\n================================================================\n"
            + render_predicate_block(predicates)
            + "\n================================================================\n"
            + f"DOCUMENT METADATA:\n  box_file_id: {doc['box_file_id']}\n  name: {doc['name']}\n  full_path: {doc['full_path']}\n"
            + "\n================================================================\nDOCUMENT CONTENT:\n"
            + doc["content"]
            + "\n================================================================\n"
            + "Emit the JSON object now.")


def build_prompt_doc_observe(doc, entities):
    return (PROMPT_C_DOC
            + "\n================================================================\n"
            + render_entity_block(entities)
            + "\n================================================================\n"
            + f"DOCUMENT METADATA:\n  box_file_id: {doc['box_file_id']}\n  name: {doc['name']}\n  full_path: {doc['full_path']}\n"
            + "\n================================================================\nDOCUMENT CONTENT:\n"
            + doc["content"]
            + "\n================================================================\n"
            + "Emit the JSON object now.")


# ---------------------------------------------------------------------------
# Document-specific apply (writes citations referencing source_document)
# ---------------------------------------------------------------------------

class DocGraphDB(GraphDB):
    def get_source_document(self, box_file_id):
        row = self.conn.execute(
            "SELECT source_document_id, client_id, url FROM source_document WHERE external_id=?",
            (str(box_file_id),),
        ).fetchone()
        if not row:
            return None
        return {"source_document_id": row[0], "client_id": row[1], "url": row[2]}

    def cite_edge_document(self, edge_id, source_document_id, source_external_id, quote, extracted_by):
        existing = self.conn.execute(
            "SELECT citation_id FROM citation WHERE cited_kind='edge' AND cited_id=? AND source_kind='document' AND source_id=?",
            (edge_id, source_document_id),
        ).fetchone()
        if existing:
            return existing[0]
        cur = self.conn.execute(
            """INSERT INTO citation (cited_kind, cited_id, source_kind, source_id, source_external_id, quote, extracted_by)
               VALUES ('edge', ?, 'document', ?, ?, ?, ?)""",
            (edge_id, source_document_id, source_external_id, quote, extracted_by),
        )
        self.conn.commit()
        return cur.lastrowid

    def cite_memory_document(self, memory_id, source_document_id, source_external_id, quote, extracted_by):
        existing = self.conn.execute(
            "SELECT citation_id FROM citation WHERE cited_kind='memory' AND cited_id=? AND source_kind='document' AND source_id=?",
            (memory_id, source_document_id),
        ).fetchone()
        if existing:
            return existing[0]
        cur = self.conn.execute(
            """INSERT INTO citation (cited_kind, cited_id, source_kind, source_id, source_external_id, quote, extracted_by)
               VALUES ('memory', ?, 'document', ?, ?, ?, ?)""",
            (memory_id, source_document_id, source_external_id, quote, extracted_by),
        )
        self.conn.commit()
        return cur.lastrowid


def apply_structure_doc(db, client_id, source_document_id, source_external_id,
                         result, transcript_text, vocab_names, claude_bin, model, rdir=None):
    stats = {"events_created": 0, "edges_created": 0, "edges_existing": 0,
             "edges_dropped_after_correction": 0, "edges_corrected": 0, "citations": 0}
    temp_to_eid = {}

    for ev in (result.get("events") or []):
        cname = (ev.get("canonical_name") or "").strip()
        if not cname or len(cname) <= 3:
            continue
        res = db.call("resolve_or_create_entity", client_id, cname,
                       entity_type="event", canonical_name=cname,
                       alias_kind="name", min_alias_confidence="likely",
                       resolved_by="extractor:document:v0.1.0")
        if res.get("created"):
            stats["events_created"] += 1
        if ev.get("temp_id"):
            temp_to_eid[ev["temp_id"]] = res["entity_id"]

    good, bad = validate_edges(result.get("edges") or [], transcript_text, vocab_names, temp_to_eid)
    print(f"  Pass B validate: {len(good)} good, {len(bad)} need correction")

    for attempt in range(1, 3):
        if not bad:
            break
        prompt = build_correction_prompt("edges", bad, transcript_text, vocab_names=vocab_names)
        if rdir:
            (rdir / f"prompt_B_correct_{attempt}.txt").write_text(prompt)
        try:
            cresult, _ = invoke_claude_json(prompt, claude_bin, model,
                raw_path=(rdir / f"response_B_correct_{attempt}.json") if rdir else None)
            corrections = cresult.get("corrections") or []
        except SystemExit:
            corrections = []
        idx_map = {c.get("index"): c for c in corrections if isinstance(c, dict)}
        retry, dropped = [], 0
        for i, (row, _) in enumerate(bad, start=1):
            corr = idx_map.get(i)
            if not corr or corr.get("action") == "drop":
                dropped += 1
                continue
            if corr.get("action") == "fix" and isinstance(corr.get("corrected"), dict):
                retry.append(corr["corrected"])
        stats["edges_dropped_after_correction"] += dropped
        good_now, still_bad = validate_edges(retry, transcript_text, vocab_names, temp_to_eid)
        stats["edges_corrected"] += len(good_now)
        good.extend(good_now)
        bad = still_bad
        print(f"    attempt {attempt}: {len(good_now)} fixed, {dropped} dropped, {len(still_bad)} still bad")

    if bad:
        stats["edges_dropped_after_correction"] += len(bad)

    # Write edges (use registered scripts via db.call where possible)
    pred_id_cache = {}
    def pred_id(name):
        if name in pred_id_cache:
            return pred_id_cache[name]
        row = db.conn.execute("SELECT predicate_id FROM predicate WHERE name=? AND status IN ('approved','proposed')", (name,)).fetchone()
        pred_id_cache[name] = row[0] if row else None
        return pred_id_cache[name]

    for ed in good:
        subj = ed.get("_resolved_subject")
        obj = ed.get("_resolved_object")
        pid = pred_id(ed["predicate"])
        if not pid or subj is None:
            continue
        edge_id, created = db.upsert_edge(
            client_id, subj, pid,
            object_id=obj, object_literal=ed.get("object_literal"),
            object_literal_type=ed.get("object_literal_type"),
            notes=ed.get("notes"), justification=ed.get("justification"),
            confidence=ed.get("confidence") or "factual",
            sensitivity=ed.get("sensitivity") or "routine",
        )
        stats["edges_created" if created else "edges_existing"] += 1
        db.cite_edge_document(edge_id, source_document_id, source_external_id,
                               ed.get("justification"), "extractor:document:v0.1.0")
        stats["citations"] += 1
    return stats


def apply_observe_doc(db, client_id, source_document_id, source_external_id,
                       result, transcript_text, claude_bin, model, rdir=None):
    stats = {"notes_dropped_after_correction": 0, "notes_corrected": 0,
             "notes_created": 0, "notes_existing": 0, "memory_entity_links": 0, "citations": 0}

    good, bad = validate_notes(result.get("notes") or [], transcript_text)
    print(f"  Pass C validate: {len(good)} good, {len(bad)} need correction")

    for attempt in range(1, 3):
        if not bad:
            break
        prompt = build_correction_prompt("notes", bad, transcript_text)
        if rdir:
            (rdir / f"prompt_C_correct_{attempt}.txt").write_text(prompt)
        try:
            cresult, _ = invoke_claude_json(prompt, claude_bin, model,
                raw_path=(rdir / f"response_C_correct_{attempt}.json") if rdir else None)
            corrections = cresult.get("corrections") or []
        except SystemExit:
            corrections = []
        idx_map = {c.get("index"): c for c in corrections if isinstance(c, dict)}
        retry, dropped = [], 0
        for i, (row, _) in enumerate(bad, start=1):
            corr = idx_map.get(i)
            if not corr or corr.get("action") == "drop":
                dropped += 1
                continue
            if corr.get("action") == "fix" and isinstance(corr.get("corrected"), dict):
                retry.append(corr["corrected"])
        stats["notes_dropped_after_correction"] += dropped
        good_now, still_bad = validate_notes(retry, transcript_text)
        stats["notes_corrected"] += len(good_now)
        good.extend(good_now)
        bad = still_bad
        print(f"    attempt {attempt}: {len(good_now)} fixed, {dropped} dropped, {len(still_bad)} still bad")

    if bad:
        stats["notes_dropped_after_correction"] += len(bad)

    def resolve_ref(ref):
        if ref is None:
            return None
        if isinstance(ref, int):
            return ref
        if isinstance(ref, str) and ref.isdigit():
            return int(ref)
        return None

    for note in good:
        content = (note.get("content") or "").strip()
        if not content:
            continue
        existing = db.memory_exists_for_source(client_id, content, source_external_id)
        if existing:
            stats["notes_existing"] += 1
            memory_id = existing
        else:
            mtype = note.get("memory_type") or "observation"
            importance = max(1, min(10, int(note.get("importance") or 5)))
            tags = note.get("tags")
            sens = note.get("sensitivity") or "routine"
            if sens not in ("routine", "sensitive", "hr_grade"):
                sens = "routine"
            memory_id = db.write_memory(
                client_id, content, mtype, source="document",
                source_id=source_external_id, importance=importance, tags=tags,
                sensitivity=sens,
            )
            stats["notes_created"] += 1
            db.cite_memory_document(memory_id, source_document_id, source_external_id,
                                     note.get("justification"), "extractor:document:v0.1.0")
            stats["citations"] += 1
        for ref in (note.get("subject_entities") or []):
            eid = resolve_ref(ref)
            if eid:
                db.link_memory_entity(memory_id, eid, role="subject")
                stats["memory_entity_links"] += 1
        for ref in (note.get("mentioned_entities") or []):
            eid = resolve_ref(ref)
            if eid:
                db.link_memory_entity(memory_id, eid, role="mentioned")
                stats["memory_entity_links"] += 1
    return stats


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

def extract_one(db, client_id, client_name, container, box_file_id, claude_bin, model,
                 base_responses_dir):
    print(f"\n=== box_file_id={box_file_id} ===")
    sd = db.get_source_document(box_file_id)
    if not sd:
        print(f"  no source_document row for {box_file_id}; skipping (run seed_from_box first)")
        return
    if sd["client_id"] != client_id:
        print(f"  source_document client_id mismatch ({sd['client_id']} vs {client_id}); skipping")
        return

    doc_meta = fetch_box_file(container, box_file_id)
    if not doc_meta.get("content"):
        print(f"  no extracted content in box_files.db; skipping")
        return
    doc_meta["box_file_id"] = box_file_id
    print(f"  name: {doc_meta['name']}")
    print(f"  content: {len(doc_meta['content'])} chars")

    rdir = None
    if base_responses_dir:
        rdir = Path(base_responses_dir) / box_file_id
        rdir.mkdir(parents=True, exist_ok=True)

    # Pass A: Resolve
    entities = db.snapshot_entities(client_id)
    prompt_a = build_prompt_doc_resolve(doc_meta, entities)
    print(f"\n[Pass A — Resolve] prompt={len(prompt_a)} chars (~{len(prompt_a)//4} tokens) entities={len(entities)}")
    if rdir:
        (rdir / "prompt_A.txt").write_text(prompt_a)
    result_a, _ = invoke_claude_json(prompt_a, claude_bin, model,
        raw_path=(rdir / "response_A.json") if rdir else None)
    stats_a, _ = apply_resolve(db, client_id, str(box_file_id), None, result_a)
    print(f"  applied: {stats_a}")
    print(f"  summary: {result_a.get('summary')!r}")

    # Pass B: Structure
    entities = db.snapshot_entities(client_id)
    predicates = db.snapshot_predicates(status_filter=["approved", "proposed"])
    vocab_names = {p["name"] for p in predicates}
    prompt_b = build_prompt_doc_structure(doc_meta, entities, predicates)
    print(f"\n[Pass B — Structure] prompt={len(prompt_b)} chars entities={len(entities)} predicates={len(predicates)}")
    if rdir:
        (rdir / "prompt_B.txt").write_text(prompt_b)
    result_b, _ = invoke_claude_json(prompt_b, claude_bin, model,
        raw_path=(rdir / "response_B.json") if rdir else None)
    stats_b = apply_structure_doc(db, client_id, sd["source_document_id"], str(box_file_id),
                                    result_b, doc_meta["content"], vocab_names,
                                    claude_bin, model, rdir)
    print(f"  applied: {stats_b}")

    # Pass C: Observe
    entities = db.snapshot_entities(client_id)
    prompt_c = build_prompt_doc_observe(doc_meta, entities)
    print(f"\n[Pass C — Observe] prompt={len(prompt_c)} chars entities={len(entities)}")
    if rdir:
        (rdir / "prompt_C.txt").write_text(prompt_c)
    result_c, _ = invoke_claude_json(prompt_c, claude_bin, model,
        raw_path=(rdir / "response_C.json") if rdir else None)
    stats_c = apply_observe_doc(db, client_id, sd["source_document_id"], str(box_file_id),
                                  result_c, doc_meta["content"],
                                  claude_bin, model, rdir)
    print(f"  applied: {stats_c}")

    # Mark this source_document as freshly extracted
    db.conn.execute(
        "UPDATE source_document SET content_extracted_at=CURRENT_TIMESTAMP WHERE source_document_id=?",
        (sd["source_document_id"],),
    )
    db.conn.commit()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--client-slug", required=True)
    p.add_argument("--box-file-id", action="append", required=True,
                    help="Repeat for multiple files")
    p.add_argument("--container", default="declawed-kiselgolem",
                    help="OrbStack container with /data/box_files.db")
    p.add_argument("--db", default=os.path.join(os.path.dirname(__file__), "agent_memory.db"))
    p.add_argument("--claude-bin", default="claude")
    p.add_argument("--model", help="Optional model id")
    p.add_argument("--responses-dir", help="Base dir to save prompts/responses (subdir per file)")
    p.add_argument("--lenient-quotes", action="store_true",
                    help="Allow fuzzy quote-validation: accept if a 6-word contiguous span from the proposed quote appears verbatim in the document, even if the full quote does not. Useful for documents with paraphrased content.")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true")
    g.add_argument("--execute", action="store_true")
    args = p.parse_args()
    args.db = os.path.expanduser(args.db)

    if args.dry_run:
        raise SystemExit("--dry-run not yet implemented for documents; use --execute (or omit Pass invocations).")

    if not shutil.which("docker"):
        for cand in ("/opt/homebrew/bin/docker", "/usr/local/bin/docker"):
            if os.path.exists(cand):
                os.environ["PATH"] = os.path.dirname(cand) + ":" + os.environ.get("PATH", "")
                break

    if args.lenient_quotes:
        # Swap the quote validator used by validate_edges / validate_notes (imported from extract_meeting)
        _em.quote_in_transcript = quote_in_text_fuzzy
        print("(lenient-quotes mode: 6-word contiguous span fallback enabled)")

    db = DocGraphDB(args.db)
    client_id, client_name = db.get_client(args.client_slug)
    print(f"client={client_name} (id={client_id})")

    for fid in args.box_file_id:
        extract_one(db, client_id, client_name, args.container, fid,
                     args.claude_bin, args.model, args.responses_dir)


if __name__ == "__main__":
    main()
