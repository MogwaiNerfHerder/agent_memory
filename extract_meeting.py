"""
extract_meeting.py - Multi-pass meeting extractor (v0.7.0).

Three focused passes per meeting, each a small, attention-rich LLM call:

  PASS A — Resolve
    Inputs : transcript + current entities + drift-aware identity rules.
    Outputs: new_entities, alias_proposals, drift_hypotheses, summary.
    Goal   : full attention on identity. Nothing structural.

  PASS B — Structure
    Inputs : transcript + entities (incl. A's additions) + predicate vocab + event/timing rules.
    Outputs: events (entities of type='event'), edges with timing.
    Goal   : full attention on relationships. No prose.

  PASS C — Observe
    Inputs : transcript + entity+event inventory + soft-knowledge rules.
    Outputs: notes (action_items, decisions, risks, themes, observations) with sensitivity.
    Goal   : full attention on the soft layer.

Between passes:
  - apply A's output to the db (mint entities, add aliases, file drift hypotheses)
  - validate every justification quote in B and C against the actual transcript text
    (substring check w/ whitespace normalization). Invalid rows are dropped, not retried.

Usage:
    extract_meeting.py --client-slug hig_growth_partners --meeting-guid <guid> \\
        --execute --responses-dir /tmp/extract/<guid>/

    --dry-run prints all three prompts without invoking the model.
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

sys.path.insert(0, str(Path(__file__).parent))
from sensitivity_gate import classify_sensitivity  # noqa: E402

# Distinct exit code for "held by the sensitivity gate" -- lets callers (e.g.
# run_batch50.py) tell "this meeting is permanently excluded from extraction"
# apart from a real transient failure that should be retried.
SENSITIVITY_HELD_EXIT_CODE = 3


# =============================================================================
# Cortado client (unchanged from v0.6)
# =============================================================================

def cortado_module(skill_dir):
    skill_dir = Path(os.path.expanduser(skill_dir)).resolve()
    script = skill_dir / "scripts" / "cortado_manager.py"
    if not script.exists():
        raise SystemExit(f"cortado_manager.py not found at {script}")
    old_cwd = os.getcwd(); os.chdir(skill_dir)
    try:
        spec = importlib.util.spec_from_file_location("cortado_manager", str(script))
        mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
        return mod, *mod.get_auth()
    finally:
        os.chdir(old_cwd)


def fetch_meeting(skill_dir, guid):
    m, h, a = cortado_module(skill_dir)
    r = m.api_request("GET", f"{m.BASE_URL}/meetings/{guid}/", h, a)
    if r.status_code != 200:
        raise SystemExit(f"cortado meeting {guid}: {r.status_code} {r.text[:200]}")
    return r.json()


# =============================================================================
# Transcript helpers
# =============================================================================

_CLEAN_TRANSCRIPT_FN = None


def _load_clean_transcript_fn():
    """Load the clean-transcript skill's clean() function once and cache."""
    global _CLEAN_TRANSCRIPT_FN
    if _CLEAN_TRANSCRIPT_FN is not None:
        return _CLEAN_TRANSCRIPT_FN
    candidates = [
        Path(os.path.expanduser("~/.clawdbot/skills/clean-transcript/scripts/clean_transcript.py")),
        Path("/skills/clean-transcript/scripts/clean_transcript.py"),
    ]
    for path in candidates:
        if path.exists():
            spec = importlib.util.spec_from_file_location("clean_transcript_skill", str(path))
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            _CLEAN_TRANSCRIPT_FN = mod.clean
            return _CLEAN_TRANSCRIPT_FN
    _CLEAN_TRANSCRIPT_FN = lambda text, **kwargs: text  # noqa: E731 — no-op fallback
    return _CLEAN_TRANSCRIPT_FN


def render_transcript(meeting, apply_filler_strip=True):
    clean = meeting.get("transcript_clean")
    if isinstance(clean, list):
        lines = []
        for turn in clean:
            if not isinstance(turn, dict):
                continue
            t = turn.get("t") or ""
            sp = turn.get("speaker") or ""
            tx = turn.get("text") or ""
            lines.append(f"[{t}] {sp}: {tx}")
        text = "\n".join(lines)
    else:
        text = meeting.get("transcript_text") or meeting.get("transcript") or ""
    if apply_filler_strip and text:
        try:
            fn = _load_clean_transcript_fn()
            text = fn(text, strip_timestamps=False, aggressive=False)
        except Exception:
            pass  # fail-soft; uncleaned text is still usable
    return text


_WS = re.compile(r"\s+")

# Map common Unicode punctuation that LLMs emit to ASCII so substring checks
# don't fail just because the model used a smart quote or ellipsis character.
_PUNCT_MAP = str.maketrans({
    "‘": "'", "’": "'", "‚": "'", "‛": "'",  # curly singles
    "“": '"', "”": '"', "„": '"', "‟": '"',  # curly doubles
    "–": "-", "—": "-", "−": "-", "‐": "-", "‑": "-",  # dashes
    "…": "...",                                              # ellipsis
    " ": " ", " ": " ", " ": " ", " ": " ",  # nbsp / thin / narrow
    "​": "",  "﻿": "",                                  # zero-width
})

import unicodedata as _uni


def _norm(s):
    if not s:
        return ""
    # NFKC handles many compatibility variants (full-width, ligatures, etc.)
    s = _uni.normalize("NFKC", s)
    s = s.translate(_PUNCT_MAP)
    s = s.lower()
    s = _WS.sub(" ", s).strip()
    return s


def quote_in_transcript(quote, transcript_text):
    """Substring check with normalization. Quotes shorter than 8 chars are accepted blindly."""
    if not quote:
        return False
    q = _norm(quote)
    if len(q) < 8:
        return True
    return q in _norm(transcript_text)


# =============================================================================
# DB layer
# =============================================================================

class GraphDB:
    def __init__(self, db_path):
        self.conn = sqlite3.connect(db_path)
        self.conn.execute("PRAGMA foreign_keys = ON")
        self._scripts = {}
        self._embedder = None
        for name in ("resolve_or_create_entity", "propose_predicate"):
            row = self.conn.execute(
                "SELECT script_body FROM z_script_catalog WHERE script_name=? AND is_active=1", (name,)
            ).fetchone()
            ns = {}; exec(row[0], ns); self._scripts[name] = ns[name]

    def call(self, name, *args, **kwargs):
        return self._scripts[name](self.conn, *args, **kwargs)

    @property
    def embedder(self):
        if self._embedder is None:
            from sentence_transformers import SentenceTransformer
            self._embedder = SentenceTransformer("all-MiniLM-L6-v2")
        return self._embedder

    def embed(self, text):
        import numpy as np
        return self.embedder.encode(text).astype(np.float32).tobytes()

    # ----- client / source_meeting -----
    def get_client(self, slug):
        row = self.conn.execute("SELECT client_id, name FROM client WHERE slug=?", (slug,)).fetchone()
        if not row:
            raise SystemExit(f"No client with slug '{slug}'")
        return row[0], row[1]

    def upsert_source_meeting(self, client_id, external_id, occurred_at,
                               attribution_source="manual", attribution_confidence="certain"):
        """attribution_source/attribution_confidence default to the previous
        hardcoded values ('manual'/'certain') for callers that don't pass them,
        but a caller that resolved this meeting's client via resolve_client.py
        (direct account guid, attendee-domain match, or title-keyword match)
        should pass through what that resolution actually found -- a
        domain/keyword guess is not the same certainty as a direct account
        link, and the schema already has the vocabulary
        (lookup_attribution_source / lookup_identity_confidence) to say so."""
        url = f"https://cg.cortadogroup.ai/meetings/console/{external_id}/"
        existing = self.conn.execute(
            "SELECT source_meeting_id FROM source_meeting WHERE external_id=?", (external_id,)
        ).fetchone()
        if existing:
            self.conn.execute(
                """UPDATE source_meeting SET client_id=?, occurred_at=?, url=?,
                       attribution_source=?, attribution_confidence=? WHERE source_meeting_id=?""",
                (client_id, occurred_at, url, attribution_source, attribution_confidence, existing[0]),
            )
            self.conn.commit()
            return existing[0]
        cur = self.conn.execute(
            """INSERT INTO source_meeting (client_id, external_id, occurred_at, url,
                                            attribution_source, attribution_confidence)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (client_id, external_id, occurred_at, url, attribution_source, attribution_confidence),
        )
        self.conn.commit()
        return cur.lastrowid

    # ----- snapshot -----
    def snapshot_entities(self, client_id):
        rows = self.conn.execute(
            "SELECT entity_id, type, canonical_name FROM entity WHERE client_id=? ORDER BY type, canonical_name",
            (client_id,),
        ).fetchall()
        out = []
        for eid, etype, ename in rows:
            aliases = self.conn.execute(
                """SELECT alias_text, alias_kind FROM entity_alias
                    WHERE entity_id=? AND alias_kind IN ('name','nickname','codename','transcription_drift','initials','formal_name','role_descriptor')
                    ORDER BY alias_kind, alias_text""",
                (eid,),
            ).fetchall()
            alist = [(a[0], a[1]) for a in aliases if a[0] != ename]
            out.append({"entity_id": eid, "type": etype, "name": ename, "aliases": alist})
        return out

    def snapshot_predicates(self, status_filter=None):
        sql = "SELECT name, status, description, subject_types, object_types FROM predicate"
        if status_filter:
            sql += " WHERE status IN (" + ",".join("?" * len(status_filter)) + ")"
            params = status_filter
        else:
            params = []
        sql += " ORDER BY status, name"
        return [
            {"name": r[0], "status": r[1], "description": r[2],
             "subject_types": json.loads(r[3]) if r[3] else None,
             "object_types": json.loads(r[4]) if r[4] else None}
            for r in self.conn.execute(sql, params).fetchall()
        ]

    # ----- alias / predicate -----
    def add_alias(self, client_id, entity_id, alias_text, alias_kind="name", confidence="likely",
                  resolved_by="extractor:multipass:v0.7.0"):
        if not alias_text:
            return
        self.conn.execute(
            """INSERT OR IGNORE INTO entity_alias (client_id, entity_id, alias_text, alias_kind, confidence, resolved_by)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (client_id, entity_id, str(alias_text), alias_kind, confidence, resolved_by),
        )
        self.conn.commit()

    # ----- edges -----
    def upsert_edge(self, client_id, subject_id, predicate_id, object_id=None,
                     object_literal=None, object_literal_type=None,
                     notes=None, justification=None,
                     confidence="stated", sensitivity="routine"):
        if object_id is not None:
            existing = self.conn.execute(
                "SELECT edge_id FROM edge WHERE client_id=? AND subject_id=? AND predicate_id=? AND object_id=? AND status='active'",
                (client_id, subject_id, predicate_id, object_id),
            ).fetchone()
        else:
            existing = self.conn.execute(
                "SELECT edge_id FROM edge WHERE client_id=? AND subject_id=? AND predicate_id=? AND object_literal=? AND status='active'",
                (client_id, subject_id, predicate_id, object_literal),
            ).fetchone()
        if existing:
            return existing[0], False
        cur = self.conn.execute(
            """INSERT INTO edge (client_id, subject_id, predicate_id, object_id, object_literal, object_literal_type,
                                  notes, justification, confidence, sensitivity, status,
                                  first_observed_ts, last_corroborated_ts)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)""",
            (client_id, subject_id, predicate_id, object_id, object_literal, object_literal_type,
             notes, justification, confidence, sensitivity),
        )
        self.conn.commit()
        return cur.lastrowid, True

    def cite_edge_meeting(self, edge_id, source_meeting_id, source_external_id, source_ts, quote, extracted_by):
        existing = self.conn.execute(
            "SELECT citation_id FROM citation WHERE cited_kind='edge' AND cited_id=? AND source_kind='meeting' AND source_id=?",
            (edge_id, source_meeting_id),
        ).fetchone()
        if existing:
            return existing[0]
        cur = self.conn.execute(
            """INSERT INTO citation (cited_kind, cited_id, source_kind, source_id, source_external_id, source_ts, quote, extracted_by)
               VALUES ('edge', ?, 'meeting', ?, ?, ?, ?, ?)""",
            (edge_id, source_meeting_id, source_external_id, source_ts, quote, extracted_by),
        )
        self.conn.commit()
        return cur.lastrowid

    # ----- memory -----
    def write_memory(self, client_id, content, memory_type, source, source_id=None,
                     importance=5, tags=None, summary=None, sensitivity="routine"):
        embedding = self.embed(content)
        cur = self.conn.execute(
            """INSERT INTO z_memory
                (content, summary, memory_type, source, source_id, importance, tags,
                 embedding, embedding_model, client_id, sensitivity)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'all-MiniLM-L6-v2', ?, ?)""",
            (content, summary, memory_type, source, source_id, importance, tags,
             embedding, client_id, sensitivity),
        )
        self.conn.commit()
        return cur.lastrowid

    def memory_exists_for_source(self, client_id, content, source_external_id):
        row = self.conn.execute(
            """SELECT m.memory_id FROM z_memory m
                 JOIN citation c ON c.cited_kind='memory' AND c.cited_id=m.memory_id
                WHERE m.client_id=? AND m.content=?
                  AND c.source_kind='meeting' AND c.source_external_id=?""",
            (client_id, content, source_external_id),
        ).fetchone()
        return row[0] if row else None

    def link_memory_entity(self, memory_id, entity_id, role="mentioned"):
        self.conn.execute(
            "INSERT OR IGNORE INTO memory_entity (memory_id, entity_id, role) VALUES (?, ?, ?)",
            (memory_id, entity_id, role),
        )
        self.conn.commit()

    def cite_memory_meeting(self, memory_id, source_meeting_id, source_external_id, source_ts, quote, extracted_by):
        existing = self.conn.execute(
            "SELECT citation_id FROM citation WHERE cited_kind='memory' AND cited_id=? AND source_kind='meeting' AND source_id=?",
            (memory_id, source_meeting_id),
        ).fetchone()
        if existing:
            return existing[0]
        cur = self.conn.execute(
            """INSERT INTO citation (cited_kind, cited_id, source_kind, source_id, source_external_id, source_ts, quote, extracted_by)
               VALUES ('memory', ?, 'meeting', ?, ?, ?, ?, ?)""",
            (memory_id, source_meeting_id, source_external_id, source_ts, quote, extracted_by),
        )
        self.conn.commit()
        return cur.lastrowid


# =============================================================================
# Prompt builders — three small focused prompts
# =============================================================================

PII_RULE = "PII: Never extract email addresses, phone numbers, or home addresses. Skip even if present."

ENTITY_BLOCK_HEADER = "ENTITIES IN SCOPE (resolve to existing where possible):"


def render_entity_block(entities):
    lines = [ENTITY_BLOCK_HEADER]
    for e in entities:
        line = f"  id={e['entity_id']} type={e['type']} name=\"{e['name']}\""
        if e["aliases"]:
            alias_strs = [f"\"{a[0]}\"({a[1]})" for a in e["aliases"]]
            line += f" aliases=[{', '.join(alias_strs)}]"
        lines.append(line)
    return "\n".join(lines)


def render_predicate_block(predicates):
    lines = ["APPROVED PREDICATE VOCABULARY:"]
    for p in predicates:
        line = f"  {p['name']}"
        if p["description"]:
            line += f" — {p['description']}"
        if p.get("subject_types") or p.get("object_types"):
            line += f"  [subj: {p.get('subject_types')} → obj: {p.get('object_types')}]"
        lines.append(line)
    return "\n".join(lines)


# ---------- Pass A: Resolve ----------

PROMPT_A_INSTRUCTIONS = f"""You are extracting ENTITY REFERENCES from a meeting transcript.

Your ONLY job in this pass is identity:
  • Who/what is mentioned in this transcript?
  • The entity inventory below lists each entity with its aliases. Aliases are tagged with
    a KIND in parentheses, e.g. `"Mark"(transcription_drift)`, `"Odie"(nickname)`,
    `"William"(formal_name)`. ANY alias kind — including `transcription_drift` — means
    "this string already resolves to this entity." When you see such a string in the
    transcript, treat it as a reference to the existing entity_id. DO NOT re-propose it
    as a drift_hypothesis. DO NOT mint a new entity for it.
  • Which NEW references warrant minting an entity? Mint when:
      - named real-world entity (firm, exec, product, document, fund), even peripherally
      - repeatedly-referenced unnamed actor ("the target", "their CEO") — mint a placeholder
        with descriptive canonical_name like "Project X Target CEO" and confidence=guessed
      - documents/studies/reports referenced by name or author
    Don't mint for: pronouns alone, ≤3-char tokens that may be initials, or wild guesses,
    OR tokens that already appear as aliases on an existing entity (resolve those instead).
  • drift_hypothesis is ONLY for NEW unknown tokens that look like they might be a
    transcription artifact for an existing entity (and aren't already on that entity's
    alias list). The example "Dedle"→"D. Dale" applies when "Dedle" is NOT yet a
    known alias of D. Dale. Once approved, future transcripts must resolve directly.

{PII_RULE}

Output a single JSON object with exactly this shape — no prose, no fences:

{{
  "summary": "1-3 sentence summary of what this meeting was about",
  "new_entities": [
    {{"temp_id": "n1", "type": "person|company|project|product|document|topic|department|deal|fund|event",
      "canonical_name": "...", "rationale": "..."}}
  ],
  "alias_proposals": [
    {{"entity_ref": "<entity_id or n1>", "alias_text": "...",
      "alias_kind": "name|nickname|initials|codename|formal_name|sfid|cortado_*_guid",
      "confidence": "certain|likely", "rationale": "..."}}
  ],
  "drift_hypotheses": [
    {{"observed_token": "...", "candidate_entity_ref": "<existing entity_id>",
      "rationale": "why this might be a mishearing/typo of the candidate",
      "supporting_quote": "verbatim quote, 10-200 chars"}}
  ]
}}

Only emit alias_proposals when you are CERTAIN of the identity (e.g. speaker explicitly says
'Marc, sometimes called Mark'). For uncertain cases, use drift_hypotheses.
"""


def build_prompt_resolve(meeting, entities):
    """Dynamic (per-call) content only. The meeting metadata + transcript used to
    be re-included here and duplicated across all three passes -- moved to
    render_meeting_prefix(), which invoke_codex_cached() writes once to a temp
    file reused across all three passes for this meeting instead of re-embedding
    the transcript in every call."""
    return (
        PROMPT_A_INSTRUCTIONS
        + "\n================================================================\n"
        + render_entity_block(entities)
        + "\n================================================================\n"
        + "Emit the JSON object now. Begin with `{` and end with `}`."
    )


# ---------- Pass B: Structure ----------

PROMPT_B_INSTRUCTIONS = f"""You are extracting STRUCTURAL FACTS from a meeting transcript.

Identity has already been resolved — use the entity inventory provided. Your ONLY job in
this pass is relationships and events:

  • EDGES (subject, predicate, object_or_literal) using the predicate vocabulary.
    Every edge MUST include `justification` — a 10-200-char VERBATIM quote from the
    transcript. No quote = no edge.
  • EVENTS (entity.type='event'): mint events for things that happen in time —
    scheduled meetings, action items with deadlines, decisions tied to a date,
    deliverables, deal closings, departures, milestones. Give descriptive
    canonical_names like "HIG/Violet IC Readout 2026-05-12" or "Bill: schedule
    Hans/Marc/Max access call".
    For each event, emit the event entity AND the structural edges that pin it down:
      - has_event_type → "meeting|action_item|decision|deliverable|deadline|milestone|party|product_launch|deal_close|departure|activity"
      - scheduled_for → date literal (planned future, may shift)  -- use only if a date is genuinely stated
      - occurred_at → datetime literal (precise past)
      - occurred_around → fuzzy past ('2024-Q3', 'last year') with object_literal_type=year|quarter|date_range
      - started_at / ended_at → bounded period
      - discovered_at → '{{meeting_occurred_at}}' (when WE learned of it; use the meeting's occurred_at)
      - event_status → "scheduled|occurred|cancelled|missed|in_progress"
      - assigned_to → person entity (action_item ownership)
      - attended_by → person entity (meeting attendance)
      - precedes / follows → other event
    DO NOT INVENT DATES. If the transcript doesn't state a due date, omit the
    scheduled_for edge entirely. Better to have an event with no date than a fake date.

Resolve all relative time references against the meeting's occurred_at.
"Tomorrow" said in a 2026-05-01 meeting = 2026-05-02. "Last quarter" depends on month.

Edge confidence:
  stated   = speaker said it explicitly
  implied  = clear inference from multiple cues
  factual  = procedural truth (who attended, what was discussed)

Edge sensitivity:
  routine  = professional facts
  sensitive = interpersonal observations, dispositions, personal context
  hr_grade = personnel risk, confidentiality, conflicts. Sparingly.

{PII_RULE}

Output a single JSON object — no prose, no fences:

{{
  "events": [
    {{"temp_id": "e1", "canonical_name": "...", "rationale": "..."}}
  ],
  "edges": [
    {{"subject": "<entity_id or e1>",
      "predicate": "predicate_name (must be in vocab; if needed and missing, this is wrong pass)",
      "object": "<entity_id or e1>" OR null,
      "object_literal": "literal value when object is null",
      "object_literal_type": "string|date|datetime|currency|int|url|year|quarter|date_range",
      "confidence": "stated|implied|factual",
      "sensitivity": "routine|sensitive|hr_grade",
      "notes": "1-line elaboration if helpful, else null",
      "justification": "verbatim transcript quote, 10-200 chars"}}
  ]
}}

Note: predicates outside the approved vocab will be DROPPED. Use only the listed predicates.
"""


def build_prompt_structure(meeting, entities, predicates):
    """Dynamic content only -- see build_prompt_resolve's docstring."""
    return (
        PROMPT_B_INSTRUCTIONS.replace("{{meeting_occurred_at}}", meeting.get("occurred_at") or "")
        + "\n================================================================\n"
        + render_entity_block(entities)
        + "\n================================================================\n"
        + render_predicate_block(predicates)
        + "\n================================================================\n"
        + "Emit the JSON object now. Begin with `{` and end with `}`."
    )


# ---------- Pass C: Observe ----------

PROMPT_C_INSTRUCTIONS = f"""You are extracting ITEMS OF NOTE from a meeting transcript.

Identity and structural facts have been captured in earlier passes. Your ONLY job in this
pass is the SOFT LAYER — observations a structured graph misses but that are valuable to
remember:

  • decisions made (and why)
  • action items (and who, what, by when — if a date was stated)
  • open questions / unresolved tensions
  • risks identified (target risks, project risks, market risks)
  • themes / threads of discussion that recur across passes
  • recommendations (proposed approaches the team articulated)
  • observations about people / dynamics / preferences / interpersonal tensions
  • lessons / learnings that should inform future work
  • insights that change how the team is thinking about something

Each note MUST have:
  • content: 1-3 sentences in YOUR OWN WORDS, precise enough that a reader 6 months
    from now understands it without the transcript
  • justification: 10-200-char VERBATIM quote from the transcript supporting it
  • subject_entities / mentioned_entities: link to entity_ids from the inventory
  • importance: 1-10 (1=trivial, 5=normal, 10=critical/HR-grade-confidential)
  • sensitivity: routine | sensitive | hr_grade

Be liberal — write a note for anything worth carrying forward. Don't write notes for
things already captured as edges (e.g., "X attended" is an edge, not a note).

{PII_RULE}

Output a single JSON object — no prose, no fences:

{{
  "notes": [
    {{"memory_type": "decision|action_item|open_question|risk|observation|theme|recommendation|insight|lesson",
      "content": "...",
      "subject_entities": ["<entity_id>", ...],
      "mentioned_entities": ["<entity_id>", ...],
      "importance": 1..10,
      "sensitivity": "routine|sensitive|hr_grade",
      "tags": "comma,separated,short,tags",
      "justification": "verbatim transcript quote, 10-200 chars"}}
  ]
}}
"""


def build_prompt_observe(meeting, entities):
    """Dynamic content only -- see build_prompt_resolve's docstring."""
    return (
        PROMPT_C_INSTRUCTIONS
        + "\n================================================================\n"
        + render_entity_block(entities)
        + "\n================================================================\n"
        + "Emit the JSON object now. Begin with `{` and end with `}`."
    )


# =============================================================================
# LLM invocation + JSON parsing
# =============================================================================

def invoke_claude(prompt, claude_bin="claude", model=None, timeout=900):
    """Bare `claude -p` CLI subprocess call. No longer called anywhere in this
    file -- the Pass B/C correction-prompt path (invoke_claude_json) and the
    three main passes both now go through codex exec (invoke_codex /
    invoke_codex_cached). Left defined, unused, rather than deleted, in case
    anything outside this file imports it directly."""
    if not shutil.which(claude_bin):
        raise SystemExit(f"`{claude_bin}` not found on PATH.")
    cmd = [claude_bin, "-p"]
    if model:
        cmd += ["--model", model]
    proc = subprocess.run(cmd, input=prompt, text=True, capture_output=True, timeout=timeout)
    if proc.returncode != 0:
        raise SystemExit(f"claude -p failed (rc={proc.returncode}): {proc.stderr[:500]}")
    return proc.stdout


def render_meeting_prefix(meeting):
    """Stable, per-meeting content: metadata + transcript. Byte-identical across
    all three (resolve/structure/observe) passes for one meeting -- written once
    to a temp file by invoke_codex_cached() and reused across all three passes,
    instead of being re-embedded in every call's prompt.

    Previously each build_prompt_* function re-included the full transcript text
    after pass-specific instructions (which differ per pass), so it was resent
    at full length on every one of the 3 main-pass subprocess calls, AND the
    varying instructions coming first would have defeated prefix-based caching
    even if the invocation mechanism supported it (caching only credits an
    identical leading *prefix*; content after the first differing byte doesn't
    help). This keeps the transcript out of the per-call prompt/argv entirely.
    """
    return (
        f"MEETING METADATA:\n  guid: {meeting.get('guid')}\n  name: {meeting.get('name')}\n"
        f"  occurred_at: {meeting.get('occurred_at')}\n"
        "\n================================================================\nTRANSCRIPT:\n"
        + render_transcript(meeting)
        + "\n================================================================\n"
    )


def invoke_codex(prompt, model=None, timeout=900):
    """`codex exec` non-interactive CLI call (OpenAI, not Anthropic). Used for
    every main-pass invocation and JSON-repair retry in the cached path below --
    no Anthropic SDK, no `claude -p`, anywhere in this path.

    codex resolves to a .cmd shim on Windows (npm global install). Win32's
    CreateProcess (what subprocess.run uses with shell=False) can only launch
    real PE executables directly -- it can't interpret .cmd/.bat, which is why
    a bare ["codex", "exec"] argv list fails with WinError 2 even though the
    same command works fine typed into an interactive shell (the shell does its
    own PATHEXT resolution that CreateProcess doesn't). shell=True routes the
    call through cmd.exe, which does resolve .cmd shims correctly; the prompt
    itself still goes through `input=`/stdin, not the shell command line, so
    quoting/escaping the (large) transcript-adjacent prompt text is a non-issue.

    encoding="utf-8" is explicit because text=True without it defaults to the
    process's locale encoding -- cp1252 on this Windows box -- which raises
    UnicodeEncodeError on any transcript containing e.g. curly quotes or the
    <=/>= symbols that show up in ordinary meeting notes.
    """
    codex_path = shutil.which("codex")
    if not codex_path:
        raise SystemExit("`codex` not found on PATH.")
    cmd = "codex exec"
    if model:
        cmd += f" -m {model}"
    proc = subprocess.run(
        cmd, input=prompt, text=True, encoding="utf-8", capture_output=True,
        timeout=timeout, shell=True,
    )
    if proc.returncode != 0:
        raise SystemExit(f"codex exec failed (rc={proc.returncode}): {proc.stderr[:500]}")
    return proc.stdout


# guid -> Path. Module-level (not a per-call mutable-default-arg) so BOTH the
# three main passes (via get_meeting_prefix_path) AND the Pass B/C correction
# retries (via get_meeting_prefix_path_for_guid) reuse the same on-disk file
# for a given meeting within one process run, instead of each having its own
# private cache. Previously the correction path had no cache at all -- it
# re-embedded the full transcript_text (measured at 51,000-67,000 chars,
# ~13,000-17,000 tokens) directly into every correction prompt, on top of the
# 3 main passes already paying for it once each -- the single largest
# contributor to per-meeting cost/time, worse than the original 3x-resend bug
# this whole caching mechanism was built to fix.
_meeting_prefix_files = {}


def get_meeting_prefix_path(meeting):
    """Write (once per guid per process) and return the path to this meeting's
    stable transcript+metadata prefix file."""
    guid = meeting.get("guid") or "unknown"
    if guid not in _meeting_prefix_files:
        tmp = Path(os.environ.get("TEMP", "/tmp")) / f"codex_meeting_prefix_{guid}.txt"
        tmp.write_text(render_meeting_prefix(meeting), encoding="utf-8")
        _meeting_prefix_files[guid] = tmp
    return _meeting_prefix_files[guid]


def get_meeting_prefix_path_for_guid(guid):
    """Look up a prefix file already written by get_meeting_prefix_path() earlier
    in this same process. The correction-retry path only has the guid (see
    apply_structure/apply_observe's signature), not the full meeting dict -- but
    the corresponding main pass for this meeting always writes the file first
    within the same run, so a lookup miss here means a real ordering bug, not a
    normal condition to paper over with a fallback re-fetch."""
    if guid not in _meeting_prefix_files:
        raise RuntimeError(
            f"No cached transcript prefix file for guid {guid!r} -- expected the "
            "main pass (Pass A/B/C) for this meeting to have written one first."
        )
    return _meeting_prefix_files[guid]


def invoke_codex_cached(meeting, dynamic_prompt, model=None, timeout=900):
    """codex exec call with the meeting transcript written once to a temp file
    and reused across all three passes for this meeting (and their correction
    retries -- see get_meeting_prefix_path_for_guid), instead of re-embedding
    the full transcript text in every call's prompt/argv. This both avoids
    resending the transcript repeatedly in the request itself and avoids
    putting a large transcript blob directly in a shell argument.

    Caveat, stated plainly rather than assumed: OpenAI's API does automatic
    prefix-based prompt caching (no manual cache_control breakpoint needed,
    unlike Anthropic's), but `codex exec` is a full agent harness -- I do not
    have visibility into whether its own internal request construction (system
    prompt, tool-call plumbing, session bookkeeping) preserves a stable prefix
    across separate cold invocations the way a raw Chat Completions/Responses
    API call would. This still gets the file written once instead of the
    transcript text re-embedded in every prompt/argv, and keeps the ordering
    (stable content first, varying instructions after) that caching -- if
    codex's harness allows it -- depends on. Flagging this rather than
    overclaiming a guaranteed discount the way the Anthropic version could.
    """
    prefix_path = get_meeting_prefix_path(meeting)
    prompt = (
        f"Read the meeting transcript + metadata from this file: {prefix_path}\n"
        "Do not modify that file or any other file. Then complete this task using its content:\n\n"
        + dynamic_prompt
    )
    return invoke_codex(prompt, model=model, timeout=timeout)


def invoke_claude_json_cached(meeting, dynamic_prompt, model, raw_path=None, max_repairs=1):
    """Same contract as invoke_claude_json, but for the three main passes: uses
    invoke_codex_cached() (Codex/OpenAI, not Anthropic) for the primary call and
    every repair retry.
    """
    raw = invoke_codex_cached(meeting, dynamic_prompt, model)
    if raw_path:
        Path(raw_path).write_text(raw)
    try:
        return extract_json(raw), raw
    except json.JSONDecodeError as exc:
        for attempt in range(1, max_repairs + 1):
            repair = (
                "Your previous response was not valid JSON. The parser reported:\n"
                f"  {exc.msg} (line {exc.lineno} col {exc.colno})\n\n"
                "Common mistakes: bare token values without quotes, trailing commas, single quotes, "
                "comments, or text outside the JSON object.\n\n"
                "Re-emit a SINGLE valid JSON object with the SAME content. No prose, no fences, "
                "no commentary outside the object. Begin with `{` and end with `}`.\n\n"
                "PREVIOUS (invalid) RESPONSE:\n" + raw[:6000]
            )
            raw = invoke_codex(repair, model)
            if raw_path:
                Path(str(raw_path) + f".repair{attempt}").write_text(raw)
            try:
                return extract_json(raw), raw
            except json.JSONDecodeError as exc2:
                exc = exc2
                continue
        raise SystemExit(
            f"Could not get valid JSON after {max_repairs} repair attempt(s). Last error: {exc}\n"
            f"Raw head: {raw[:400]}"
        )


def extract_json(text):
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
        raise json.JSONDecodeError("No JSON object in response", text, 0)
    return json.loads(text[s:e+1])


def invoke_claude_json(prompt, claude_bin, model, raw_path=None, max_repairs=1):
    """Used by the Pass B/C correction-prompt retry path (build_correction_prompt
    call sites). Despite the name (kept to avoid touching those call sites'
    signature), this now calls codex exec, not claude -p -- claude_bin is
    accepted but ignored. Parses JSON; on parse failure, sends a repair prompt
    up to max_repairs times."""
    raw = invoke_codex(prompt, model)
    if raw_path:
        Path(raw_path).write_text(raw)
    try:
        return extract_json(raw), raw
    except json.JSONDecodeError as exc:
        for attempt in range(1, max_repairs + 1):
            repair = (
                "Your previous response was not valid JSON. The parser reported:\n"
                f"  {exc.msg} (line {exc.lineno} col {exc.colno})\n\n"
                "Common mistakes: bare token values without quotes, trailing commas, single quotes, "
                "comments, or text outside the JSON object.\n\n"
                "Re-emit a SINGLE valid JSON object with the SAME content. No prose, no fences, "
                "no commentary outside the object. Begin with `{` and end with `}`.\n\n"
                "PREVIOUS (invalid) RESPONSE:\n" + raw[:6000]
            )
            raw = invoke_codex(repair, model)
            if raw_path:
                Path(str(raw_path) + f".repair{attempt}").write_text(raw)
            try:
                return extract_json(raw), raw
            except json.JSONDecodeError as exc2:
                exc = exc2
                continue
        raise SystemExit(
            f"Could not get valid JSON after {max_repairs} repair attempt(s). Last error: {exc}\n"
            f"Raw head: {raw[:400]}"
        )


# =============================================================================
# Apply functions
# =============================================================================

def apply_resolve(db, client_id, source_external_id, occurred_at, result):
    stats = {"new_entities": 0, "aliases": 0, "drift_hypotheses": 0, "skipped_short_token": 0}
    temp_to_eid = {}

    for ne in (result.get("new_entities") or []):
        cname = (ne.get("canonical_name") or "").strip()
        if not cname:
            continue
        if len(cname) <= 3:
            stats["skipped_short_token"] += 1
            continue
        etype = ne.get("type") or "person"
        res = db.call("resolve_or_create_entity", client_id, cname,
                       entity_type=etype, canonical_name=cname,
                       alias_kind="name", min_alias_confidence="likely",
                       resolved_by="extractor:multipass:v0.7.0")
        if res.get("created"):
            stats["new_entities"] += 1
        if ne.get("temp_id"):
            temp_to_eid[ne["temp_id"]] = res["entity_id"]

    def resolve_ref(ref):
        if ref is None:
            return None
        if isinstance(ref, int):
            return ref
        if isinstance(ref, str):
            if ref.isdigit():
                return int(ref)
            if ref in temp_to_eid:
                return temp_to_eid[ref]
        return None

    for ap in (result.get("alias_proposals") or []):
        eid = resolve_ref(ap.get("entity_ref"))
        if not eid:
            continue
        atext = (ap.get("alias_text") or "").strip()
        if not atext or len(atext) <= 3 or "@" in atext:
            continue
        akind = ap.get("alias_kind") or "name"
        conf = ap.get("confidence") or "likely"
        db.add_alias(client_id, eid, atext, alias_kind=akind, confidence=conf)
        stats["aliases"] += 1

    for dh in (result.get("drift_hypotheses") or []):
        token = (dh.get("observed_token") or "").strip()
        cand = resolve_ref(dh.get("candidate_entity_ref"))
        if not token or not cand:
            stats.setdefault("drifts_skipped", 0)
            stats["drifts_skipped"] += 1
            continue
        # Tier-1 (7): if observed_token already equals candidate canonical or any alias, it's not a drift
        norm_token = _norm(token)
        cand_canonical = db.conn.execute("SELECT canonical_name FROM entity WHERE entity_id=?", (cand,)).fetchone()
        cand_aliases = [r[0] for r in db.conn.execute(
            "SELECT alias_text FROM entity_alias WHERE entity_id=?", (cand,)).fetchall()]
        all_known = set(_norm(s) for s in ([cand_canonical[0]] if cand_canonical else []) + cand_aliases)
        if norm_token in all_known:
            stats.setdefault("drifts_skipped_not_a_drift", 0)
            stats["drifts_skipped_not_a_drift"] += 1
            continue
        try:
            db.conn.execute(
                """INSERT INTO drift_hypothesis
                    (client_id, observed_token, candidate_entity_id, rationale, supporting_quote,
                     source_kind, source_external_id, source_ts, proposed_by, status)
                   VALUES (?, ?, ?, ?, ?, 'meeting', ?, ?, 'extractor:multipass:v0.7.0', 'pending')""",
                (client_id, token, cand, dh.get("rationale"), dh.get("supporting_quote"),
                 source_external_id, occurred_at),
            )
            db.conn.commit()
            stats["drift_hypotheses"] += 1
        except sqlite3.IntegrityError:
            pass

    return stats, temp_to_eid


def _resolve_factory(temp_to_eid):
    def resolve(ref):
        if ref is None:
            return None
        if isinstance(ref, int):
            return ref
        if isinstance(ref, str):
            if ref.isdigit():
                return int(ref)
            if ref in temp_to_eid:
                return temp_to_eid[ref]
        return None
    return resolve


_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DATETIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2})?(Z|[+-]\d{2}:?\d{2})?$")
_YEAR_RE = re.compile(r"^\d{4}$")
_QUARTER_RE = re.compile(r"^\d{4}-Q[1-4]$")
_MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
_DATE_RANGE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}\.\.\d{2}-\d{2}$|^\d{4}-\d{2}-\d{2}\.\.\d{4}-\d{2}-\d{2}$")


def _literal_matches_type(lit, lit_type):
    """Return None if OK, else a reason dict."""
    if not lit or not lit_type:
        return None
    s = str(lit).strip()
    if lit_type == "date":
        if not _DATE_RE.match(s):
            return {"code": "date_literal_unparseable", "detail": f"object_literal_type=date requires YYYY-MM-DD, got {s!r}"}
        try:
            from datetime import date as _date
            y, m, d = map(int, s.split("-"))
            _date(y, m, d)
        except (ValueError, TypeError):
            return {"code": "date_literal_invalid", "detail": f"date {s!r} is not a real calendar date"}
    elif lit_type == "datetime":
        if not _DATETIME_RE.match(s):
            return {"code": "datetime_literal_unparseable", "detail": f"object_literal_type=datetime requires ISO 8601 (YYYY-MM-DDTHH:MM[:SS][Z|±HH:MM]), got {s!r}"}
    elif lit_type == "year":
        if not _YEAR_RE.match(s):
            return {"code": "year_literal_unparseable", "detail": f"object_literal_type=year requires YYYY, got {s!r}"}
    elif lit_type == "quarter":
        if not _QUARTER_RE.match(s):
            return {"code": "quarter_literal_unparseable", "detail": f"object_literal_type=quarter requires YYYY-Qn (n=1..4), got {s!r}"}
    elif lit_type == "month":
        if not _MONTH_RE.match(s):
            return {"code": "month_literal_unparseable", "detail": f"object_literal_type=month requires YYYY-MM, got {s!r}"}
    elif lit_type == "date_range":
        if not _DATE_RANGE_RE.match(s):
            return {"code": "date_range_literal_unparseable", "detail": f"object_literal_type=date_range requires YYYY-MM-DD..[YYYY-MM-]DD, got {s!r}"}
    return None


def validate_edges(rows, transcript_text, vocab_names, temp_to_eid):
    """Split edge rows into (good, bad). Each bad row is (row, reasons[])."""
    resolve = _resolve_factory(temp_to_eid)
    good, bad = [], []
    for ed in rows or []:
        reasons = []
        subj = resolve(ed.get("subject"))
        if subj is None:
            reasons.append({"code": "unresolved_subject", "detail": f"subject={ed.get('subject')!r} did not resolve to any entity"})
        pname = ed.get("predicate")
        if not pname:
            reasons.append({"code": "missing_predicate"})
        elif pname not in vocab_names:
            reasons.append({"code": "unknown_predicate", "detail": f"'{pname}' is not in approved/proposed vocabulary"})
        obj = resolve(ed.get("object"))
        obj_lit = ed.get("object_literal")
        # Tier-1 (1): both set is mutually exclusive
        if obj is not None and obj_lit not in (None, ""):
            reasons.append({"code": "object_ambiguity", "detail": "both object entity_ref and object_literal supplied; pick one"})
        if obj is None and (obj_lit is None or obj_lit == ""):
            reasons.append({"code": "missing_object", "detail": "neither object entity_ref nor object_literal supplied"})
        # Tier-1 (5): self-edge
        if subj is not None and obj is not None and subj == obj:
            reasons.append({"code": "self_edge", "detail": f"subject and object are the same entity_id={subj}"})
        if obj_lit and "@" in str(obj_lit) and ed.get("object_literal_type") != "url":
            reasons.append({"code": "pii_email_in_literal", "detail": "object_literal looks like an email address"})
        # Tier-1 (3): date parse check
        lit_type = ed.get("object_literal_type")
        if lit_type and obj_lit:
            r = _literal_matches_type(obj_lit, lit_type)
            if r:
                reasons.append(r)
        quote = ed.get("justification") or ""
        if not quote:
            reasons.append({"code": "missing_quote"})
        elif not quote_in_transcript(quote, transcript_text):
            reasons.append({"code": "quote_not_in_transcript",
                             "detail": "the supplied justification quote does not appear in the meeting transcript text"})
        if reasons:
            bad.append((ed, reasons))
        else:
            good.append({**ed, "_resolved_subject": subj, "_resolved_object": obj})
    return good, bad


def validate_notes(rows, transcript_text):
    good, bad = [], []
    for note in rows or []:
        reasons = []
        content = (note.get("content") or "").strip()
        if not content:
            reasons.append({"code": "missing_content"})
        if "@" in content and "." in content.split("@")[-1].split()[0] if content else False:
            reasons.append({"code": "pii_email_in_content"})
        quote = note.get("justification") or ""
        if not quote:
            reasons.append({"code": "missing_quote"})
        elif not quote_in_transcript(quote, transcript_text):
            reasons.append({"code": "quote_not_in_transcript",
                             "detail": "the supplied justification quote does not appear in the meeting transcript text"})
        try:
            imp = int(note.get("importance") or 5)
            if not (1 <= imp <= 10):
                reasons.append({"code": "importance_out_of_range", "detail": f"importance={imp}"})
        except (TypeError, ValueError):
            reasons.append({"code": "importance_not_int"})
        if note.get("sensitivity") and note["sensitivity"] not in ("routine", "sensitive", "hr_grade"):
            reasons.append({"code": "invalid_sensitivity", "detail": f"sensitivity={note['sensitivity']!r}"})
        if reasons:
            bad.append((note, reasons))
        else:
            good.append(note)
    return good, bad


def build_correction_prompt(pass_kind, bad_rows, prefix_path, vocab_names=None, meeting_occurred_at=None):
    """One bundled re-prompt: list each rejected row with reasons; ask FIX or DROP.

    Takes prefix_path (the same cached transcript+metadata file written by
    get_meeting_prefix_path for this meeting's main passes) instead of the raw
    transcript_text string. Previously this embedded the full transcript inline
    on every correction call -- measured at 51,000-67,000 chars (~13,000-17,000
    tokens) -- on top of the 3 main passes already sending it once each. That
    was the single largest cost driver per meeting, worse than the original
    3x-resend bug the cached-file approach was built to fix in the first place.
    """
    items = []
    for i, (row, reasons) in enumerate(bad_rows, start=1):
        reasons_str = "; ".join(f"{r['code']}: {r.get('detail', '')}" for r in reasons)
        items.append(f"  [{i}] {json.dumps(row, ensure_ascii=False)}\n      REJECTED: {reasons_str}")
    items_block = "\n".join(items)

    vocab_block = ""
    if vocab_names:
        vocab_block = "\n\nAPPROVED PREDICATE VOCAB (use only these): " + ", ".join(sorted(vocab_names))

    return f"""You previously emitted {pass_kind} rows. Validation rejected the following.

For EACH rejected item, return either:
  - "fix": a corrected version of the row that addresses the reasons. (Most common case is a quote_not_in_transcript — find the actual verbatim text that supports the row, with no paraphrasing. If no quote can be found, DROP the row instead.)
  - "drop": acknowledge the row was wrong and should not be included.

Rules:
  - Quotes must be VERBATIM from the transcript (case insensitive, but words and punctuation as spoken).
  - Predicates must be from the approved vocab. {vocab_block if pass_kind == 'edges' else ''}
  - Do not invent dates not stated in the transcript.
  - Meeting occurred at: {meeting_occurred_at}

REJECTED ITEMS ({len(bad_rows)}):
{items_block}

================================================================
For verbatim-quote lookup, read the meeting transcript + metadata from this
file: {prefix_path}
Do not modify that file or any other file.

================================================================
Output a single JSON object — no prose, no fences:

{{
  "corrections": [
    {{"index": 1, "action": "fix", "corrected": {{...same shape as the original row...}}, "rationale": "one short sentence"}},
    {{"index": 2, "action": "drop", "rationale": "one short sentence"}}
  ]
}}
"""


def apply_validated_edges(db, client_id, source_meeting_id, source_external_id, occurred_at,
                           good_edges, vocab_names):
    """Write the validated edges. Assumes _resolved_subject / _resolved_object already attached."""
    stats = {"edges_created": 0, "edges_existing": 0, "citations": 0}

    pred_id_cache = {}
    def pred_id(name):
        if name in pred_id_cache:
            return pred_id_cache[name]
        row = db.conn.execute("SELECT predicate_id FROM predicate WHERE name=? AND status IN ('approved','proposed')", (name,)).fetchone()
        pred_id_cache[name] = row[0] if row else None
        return pred_id_cache[name]

    for ed in good_edges:
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
            confidence=ed.get("confidence") or "stated",
            sensitivity=ed.get("sensitivity") or "routine",
        )
        stats["edges_created" if created else "edges_existing"] += 1
        db.cite_edge_meeting(edge_id, source_meeting_id, source_external_id, occurred_at,
                              ed.get("justification"), "extractor:multipass:v0.7.0")
        stats["citations"] += 1
    return stats


def apply_structure(db, client_id, source_meeting_id, source_external_id, occurred_at,
                    result, transcript_text, vocab_names, claude_bin, model, occurred_at_str,
                    rdir=None):
    """Mint events first; then validate edges; if any bad, ONE correction re-prompt; apply."""
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
                       resolved_by="extractor:multipass:v0.7.0")
        if res.get("created"):
            stats["events_created"] += 1
        if ev.get("temp_id"):
            temp_to_eid[ev["temp_id"]] = res["entity_id"]

    good, bad = validate_edges(result.get("edges") or [], transcript_text, vocab_names, temp_to_eid)
    print(f"  Pass B validate: {len(good)} good, {len(bad)} need correction")

    MAX_RETRIES = 2
    for attempt in range(1, MAX_RETRIES + 1):
        if not bad:
            break
        prompt = build_correction_prompt("edges", bad, get_meeting_prefix_path_for_guid(source_external_id),
                                          vocab_names=vocab_names,
                                          meeting_occurred_at=occurred_at_str)
        if rdir:
            (rdir / f"prompt_B_correct_{attempt}.txt").write_text(prompt)
        print(f"  Pass B correction attempt {attempt}/{MAX_RETRIES}: {len(prompt)} chars (~{len(prompt)//4} tokens)")
        try:
            cresult, _raw = invoke_claude_json(
                prompt, claude_bin, model,
                raw_path=(rdir / f"response_B_correct_{attempt}.json") if rdir else None,
            )
            corrections = cresult.get("corrections") or []
        except SystemExit:
            corrections = []
        idx_map = {c.get("index"): c for c in corrections if isinstance(c, dict)}
        retry_rows = []
        dropped_by_model = 0
        for i, (row, _reasons) in enumerate(bad, start=1):
            corr = idx_map.get(i)
            if not corr or corr.get("action") == "drop":
                dropped_by_model += 1
                continue
            if corr.get("action") == "fix" and isinstance(corr.get("corrected"), dict):
                retry_rows.append(corr["corrected"])
        stats["edges_dropped_after_correction"] += dropped_by_model
        good_now, still_bad = validate_edges(retry_rows, transcript_text, vocab_names, temp_to_eid)
        stats["edges_corrected"] += len(good_now)
        good.extend(good_now)
        bad = still_bad
        print(f"    attempt {attempt}: {len(good_now)} fixed, {dropped_by_model} dropped, {len(still_bad)} still bad")

    if bad:
        stats["edges_dropped_after_correction"] += len(bad)
        if rdir:
            (rdir / "edges_unrecoverable.json").write_text(json.dumps(
                [{"row": r, "reasons": rs} for r, rs in bad], indent=2, ensure_ascii=False))

    apply_stats = apply_validated_edges(db, client_id, source_meeting_id, source_external_id,
                                          occurred_at, good, vocab_names)
    stats.update(apply_stats)
    return stats


def apply_validated_notes(db, client_id, source_meeting_id, source_external_id, occurred_at, good_notes):
    stats = {"notes_created": 0, "notes_existing": 0, "memory_entity_links": 0, "citations": 0}
    def resolve_ref(ref):
        if ref is None:
            return None
        if isinstance(ref, int):
            return ref
        if isinstance(ref, str) and ref.isdigit():
            return int(ref)
        return None
    for note in good_notes:
        content = (note.get("content") or "").strip()
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
                client_id, content, mtype, source="meeting",
                source_id=source_external_id, importance=importance, tags=tags,
                sensitivity=sens,
            )
            stats["notes_created"] += 1
            db.cite_memory_meeting(memory_id, source_meeting_id, source_external_id,
                                    occurred_at, note.get("justification"), "extractor:multipass:v0.7.0")
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


def apply_observe(db, client_id, source_meeting_id, source_external_id, occurred_at,
                  result, transcript_text, claude_bin, model, occurred_at_str, rdir=None):
    stats = {"notes_dropped_after_correction": 0, "notes_corrected": 0,
             "notes_created": 0, "notes_existing": 0, "memory_entity_links": 0, "citations": 0}

    good, bad = validate_notes(result.get("notes") or [], transcript_text)
    print(f"  Pass C validate: {len(good)} good, {len(bad)} need correction")

    MAX_RETRIES = 2
    for attempt in range(1, MAX_RETRIES + 1):
        if not bad:
            break
        prompt = build_correction_prompt("notes", bad, get_meeting_prefix_path_for_guid(source_external_id),
                                          meeting_occurred_at=occurred_at_str)
        if rdir:
            (rdir / f"prompt_C_correct_{attempt}.txt").write_text(prompt)
        print(f"  Pass C correction attempt {attempt}/{MAX_RETRIES}: {len(prompt)} chars (~{len(prompt)//4} tokens)")
        try:
            cresult, _raw = invoke_claude_json(
                prompt, claude_bin, model,
                raw_path=(rdir / f"response_C_correct_{attempt}.json") if rdir else None,
            )
            corrections = cresult.get("corrections") or []
        except SystemExit:
            corrections = []
        idx_map = {c.get("index"): c for c in corrections if isinstance(c, dict)}
        retry_rows = []
        dropped_by_model = 0
        for i, (row, _reasons) in enumerate(bad, start=1):
            corr = idx_map.get(i)
            if not corr or corr.get("action") == "drop":
                dropped_by_model += 1
                continue
            if corr.get("action") == "fix" and isinstance(corr.get("corrected"), dict):
                retry_rows.append(corr["corrected"])
        stats["notes_dropped_after_correction"] += dropped_by_model
        good_now, still_bad = validate_notes(retry_rows, transcript_text)
        stats["notes_corrected"] += len(good_now)
        good.extend(good_now)
        bad = still_bad
        print(f"    attempt {attempt}: {len(good_now)} fixed, {dropped_by_model} dropped, {len(still_bad)} still bad")

    if bad:
        stats["notes_dropped_after_correction"] += len(bad)
        if rdir:
            (rdir / "notes_unrecoverable.json").write_text(json.dumps(
                [{"row": r, "reasons": rs} for r, rs in bad], indent=2, ensure_ascii=False))

    apply_stats = apply_validated_notes(db, client_id, source_meeting_id, source_external_id,
                                          occurred_at, good)
    stats.update(apply_stats)
    return stats


# =============================================================================
# Driver
# =============================================================================

def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--client-slug", required=True)
    p.add_argument("--meeting-guid", required=True)
    p.add_argument("--cortado-skill-dir", default="~/.clawdbot/skills/cortado-api")
    p.add_argument("--db", default=os.path.join(os.path.dirname(__file__), "agent_memory.db"))
    p.add_argument("--claude-bin", default="claude")
    p.add_argument("--model", help="Optional model id to pass to claude -p")
    p.add_argument("--attribution-source", default="manual",
                    help="How --client-slug was determined (see resolve_client.py): "
                         "'cortado_account' for a direct account guid, 'attendee_domain' or "
                         "'content_inference' for a fallback match, 'manual' (default) otherwise.")
    p.add_argument("--attribution-confidence", default="certain",
                    help="certain|likely|guessed -- pair with --attribution-source; "
                         "resolve_client.py's fallback tiers are 'likely'/'guessed', not 'certain'.")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true")
    g.add_argument("--execute", action="store_true")
    p.add_argument("--responses-dir", help="If set, save each pass's prompt + raw response there")
    p.add_argument("--no-clean-transcript", action="store_true",
                    help="Skip the clean-transcript skill's filler-strip preprocessing.")
    args = p.parse_args()
    args.db = os.path.expanduser(args.db)

    db = GraphDB(args.db)
    client_id, client_name = db.get_client(args.client_slug)
    meeting = fetch_meeting(args.cortado_skill_dir, args.meeting_guid)
    if not meeting.get("transcript_clean") and not meeting.get("transcript"):
        raise SystemExit(f"Meeting {args.meeting_guid} has no transcript")

    gate = classify_sensitivity(meeting)
    if gate.decision != "eligible":
        print(f"HELD by sensitivity gate: decision={gate.decision} reason={gate.reason}")
        sys.exit(SENSITIVITY_HELD_EXIT_CODE)

    transcript_text = render_transcript(meeting, apply_filler_strip=not args.no_clean_transcript)
    occurred_at = meeting.get("occurred_at")

    rdir = None
    if args.responses_dir:
        rdir = Path(os.path.expanduser(args.responses_dir))
        rdir.mkdir(parents=True, exist_ok=True)

    print(f"client={client_name}  meeting='{meeting.get('name')}'  turns={len(meeting.get('transcript_clean') or [])}")

    # ---------- Pass A: Resolve ----------
    entities = db.snapshot_entities(client_id)
    prompt_a = build_prompt_resolve(meeting, entities)
    print(f"\n[Pass A — Resolve]  dynamic_chars={len(prompt_a)} (~{len(prompt_a)//4} tokens, transcript sent separately as a cached prefix)  entities_in_scope={len(entities)}")
    if rdir:
        (rdir / "prompt_A_resolve.txt").write_text(render_meeting_prefix(meeting) + prompt_a)

    if args.dry_run:
        print("  (dry-run; skipping LLM)")
    else:
        result_a, _raw = invoke_claude_json_cached(
            meeting, prompt_a, args.model,
            raw_path=(rdir / "response_A_resolve.json") if rdir else None,
        )
        stats_a, _temp_a = apply_resolve(db, client_id, args.meeting_guid, occurred_at, result_a)
        print(f"  applied: {stats_a}")
        print(f"  summary: {result_a.get('summary')!r}")

    # ---------- Pass B: Structure ----------
    entities = db.snapshot_entities(client_id)
    predicates = db.snapshot_predicates(status_filter=["approved", "proposed"])
    vocab_names = {p["name"] for p in predicates}
    source_meeting_id = db.upsert_source_meeting(
        client_id, args.meeting_guid, occurred_at,
        attribution_source=args.attribution_source, attribution_confidence=args.attribution_confidence,
    )
    prompt_b = build_prompt_structure(meeting, entities, predicates)
    print(f"\n[Pass B — Structure]  dynamic_chars={len(prompt_b)} (~{len(prompt_b)//4} tokens, transcript sent separately as a cached prefix)  entities_in_scope={len(entities)}  predicates={len(predicates)}")
    if rdir:
        (rdir / "prompt_B_structure.txt").write_text(render_meeting_prefix(meeting) + prompt_b)

    if args.dry_run:
        print("  (dry-run; skipping LLM)")
    else:
        result_b, _raw = invoke_claude_json_cached(
            meeting, prompt_b, args.model,
            raw_path=(rdir / "response_B_structure.json") if rdir else None,
        )
        stats_b = apply_structure(db, client_id, source_meeting_id, args.meeting_guid,
                                    occurred_at, result_b, transcript_text, vocab_names,
                                    args.claude_bin, args.model, occurred_at, rdir)
        print(f"  applied: {stats_b}")

    # ---------- Pass C: Observe ----------
    entities = db.snapshot_entities(client_id)
    prompt_c = build_prompt_observe(meeting, entities)
    print(f"\n[Pass C — Observe]  dynamic_chars={len(prompt_c)} (~{len(prompt_c)//4} tokens, transcript sent separately as a cached prefix)  entities_in_scope={len(entities)}")
    if rdir:
        (rdir / "prompt_C_observe.txt").write_text(render_meeting_prefix(meeting) + prompt_c)

    if args.dry_run:
        print("  (dry-run; skipping LLM)")
        return

    result_c, _raw = invoke_claude_json_cached(
        meeting, prompt_c, args.model,
        raw_path=(rdir / "response_C_observe.json") if rdir else None,
    )
    stats_c = apply_observe(db, client_id, source_meeting_id, args.meeting_guid,
                              occurred_at, result_c, transcript_text,
                              args.claude_bin, args.model, occurred_at, rdir)
    print(f"  applied: {stats_c}")

    # Mark this source_meeting as freshly extracted
    db.conn.execute(
        "UPDATE source_meeting SET content_extracted_at=CURRENT_TIMESTAMP WHERE source_meeting_id=?",
        (source_meeting_id,),
    )
    db.conn.commit()


if __name__ == "__main__":
    main()
