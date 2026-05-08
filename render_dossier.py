"""
render_dossier.py - Bot-style answer template: "what's going on with X?"

Pulls everything we know about an entity (or matching set of entities) and
renders a sectioned, URL-decorated dossier suitable for Slack / email / docs.

Sections (in order):
  1. Identity — canonical name, type, key aliases (sfid, cortado guid, urls)
  2. Structural facts — high-confidence factual edges (employed_by, has_role,
     reports_to, project membership, dates) grouped by predicate
  3. Recent activity — events/action items by scheduled_for / occurred_at,
     respecting --since / --until filters
  4. Items of note — z_memory observations grouped by memory_type, sorted by
     importance descending; clickable meeting URLs
  5. Discrepancies — where the entity has parallel single-valued edges
  6. Linked Box documents — for project entities, list of source_document URLs
     under their folder; for person entities, docs they're cited in (if any)
  7. Sources — a roll-up of every meeting + box doc this dossier draws from

Sensitivity tiers honored via --max-sensitivity (default 'sensitive';
hr_grade requires explicit --include-hr-grade).

Usage:
    render_dossier.py --client-slug hig_growth_partners --entity "Hans Sherman"
    render_dossier.py --client-slug hig_growth_partners --entity "Project Violet" --format markdown
    render_dossier.py --client-slug hig_growth_partners --entity Rewind --include-hr-grade
"""

import argparse
import os
import sqlite3
import sys
from collections import defaultdict


SENS_RANK = {"routine": 0, "sensitive": 1, "hr_grade": 2}


def get_client(conn, slug):
    row = conn.execute("SELECT client_id, name FROM client WHERE slug=?", (slug,)).fetchone()
    if not row:
        raise SystemExit(f"No client with slug '{slug}'")
    return row[0], row[1]


def find_entity(conn, client_id, query):
    rows = conn.execute(
        """SELECT DISTINCT e.entity_id, e.type, e.canonical_name
             FROM entity e
        LEFT JOIN entity_alias a ON a.entity_id = e.entity_id
            WHERE e.client_id = ?
              AND (e.canonical_name LIKE ? COLLATE NOCASE OR a.alias_text LIKE ? COLLATE NOCASE)
         ORDER BY (CASE WHEN e.canonical_name LIKE ? COLLATE NOCASE THEN 0 ELSE 1 END), e.canonical_name""",
        (client_id, f"%{query}%", f"%{query}%", f"%{query}%"),
    ).fetchall()
    return rows


def aliases(conn, eid):
    return conn.execute(
        "SELECT alias_text, alias_kind, confidence FROM entity_alias WHERE entity_id=? ORDER BY alias_kind, alias_text",
        (eid,),
    ).fetchall()


def edges_for_entity(conn, client_id, eid, include_sensitive_max):
    """Active edges where eid is subject or object."""
    rank_max = SENS_RANK.get(include_sensitive_max, 1)
    sens_filter_clause = " AND e.sensitivity IN (" + ",".join(
        repr(s) for s, r in SENS_RANK.items() if r <= rank_max
    ) + ")"
    sql = f"""
    SELECT e.edge_id, e.subject_id, p.name AS predicate, p.cardinality,
           e.object_id, e.object_literal, e.object_literal_type,
           e.confidence, e.sensitivity, e.notes, e.justification,
           subj.canonical_name AS subj_name, subj.type AS subj_type,
           obj.canonical_name AS obj_name, obj.type AS obj_type
      FROM edge e
      JOIN predicate p ON p.predicate_id=e.predicate_id
      JOIN entity subj ON subj.entity_id=e.subject_id
 LEFT JOIN entity obj ON obj.entity_id=e.object_id
     WHERE e.client_id=? AND e.status='active'
       AND (e.subject_id=? OR e.object_id=?)
       {sens_filter_clause}
     ORDER BY p.name, e.confidence
    """
    return conn.execute(sql, (client_id, eid, eid)).fetchall()


def edge_citations(conn, edge_id):
    return conn.execute(
        """SELECT c.source_kind, c.source_external_id, c.source_ts, c.quote,
                  sm.url AS meeting_url, sd.url AS doc_url
             FROM citation c
        LEFT JOIN source_meeting sm ON c.source_kind='meeting' AND c.source_id=sm.source_meeting_id
        LEFT JOIN source_document sd ON c.source_kind='document' AND c.source_id=sd.source_document_id
            WHERE c.cited_kind='edge' AND c.cited_id=?
         ORDER BY c.source_ts""",
        (edge_id,),
    ).fetchall()


def notes_for_entity(conn, client_id, eid, include_sensitive_max):
    rank_max = SENS_RANK.get(include_sensitive_max, 1)
    sens_in = ",".join(repr(s) for s, r in SENS_RANK.items() if r <= rank_max)
    rows = conn.execute(
        f"""SELECT m.memory_id, m.memory_type, m.importance, m.sensitivity, m.content, m.created_at, me.role,
                  c.source_kind, c.source_external_id, c.source_ts, c.quote, sm.url AS meeting_url
             FROM memory_entity me
             JOIN z_memory m ON m.memory_id = me.memory_id
        LEFT JOIN citation c ON c.cited_kind='memory' AND c.cited_id = m.memory_id
        LEFT JOIN source_meeting sm ON c.source_kind='meeting' AND c.source_id=sm.source_meeting_id
            WHERE me.entity_id=? AND m.client_id=? AND m.is_active=1 AND m.deleted_at IS NULL
              AND m.sensitivity IN ({sens_in})
         ORDER BY m.importance DESC, m.created_at DESC""",
        (eid, client_id),
    ).fetchall()
    # Dedupe memory_id (multiple citations show as multiple rows)
    seen = set()
    out = []
    for r in rows:
        if r[0] in seen:
            continue
        seen.add(r[0])
        out.append(r)
    return out


def docs_for_project(conn, client_id, eid):
    """If entity is a project with a Box folder edge, fetch source_document rows."""
    folder_path = conn.execute(
        """SELECT object_literal FROM edge e
             JOIN predicate p ON p.predicate_id=e.predicate_id
            WHERE e.client_id=? AND e.subject_id=? AND p.name='has_box_folder'
              AND e.status='active' AND e.object_literal IS NOT NULL
            ORDER BY length(e.object_literal) DESC LIMIT 1""",
        (client_id, eid),
    ).fetchone()
    if not folder_path:
        return [], None
    fp = folder_path[0]
    # fetch documents whose folder_path begins with this folder path
    rows = conn.execute(
        """SELECT external_id, folder_path, url, modified_at FROM source_document
            WHERE client_id=? AND folder_path LIKE ?
            ORDER BY folder_path
            LIMIT 200""",
        (client_id, fp.rstrip("/") + "%"),
    ).fetchall()
    return rows, fp


def render_text(conn, client_id, client_name, entity_id, etype, ename, args):
    out = []
    al = aliases(conn, entity_id)
    out.append(f"# {ename}  ({etype})")
    out.append(f"_client: {client_name}_  ·  _entity_id: {entity_id}_\n")

    # ---- Identity / aliases -------------------------------------------------
    if al:
        ident_lines = []
        url_lines = []
        for a_text, a_kind, a_conf in al:
            if a_kind in ("cortado_account_guid", "cortado_project_guid", "cortado_contact_guid",
                          "cortado_staff_guid"):
                kind_short = a_kind.replace("cortado_", "").replace("_guid", "")
                url_lines.append(f"- cortado/{kind_short}: `{a_text}`  → https://cg.cortadogroup.ai/")
            elif a_kind == "box_folder_id":
                url_lines.append(f"- box folder: https://cortadogroup.app.box.com/folder/{a_text}")
            elif a_kind == "sfid":
                url_lines.append(f"- salesforce: `{a_text}`")
            else:
                ident_lines.append(f"- {a_text}  _({a_kind}, {a_conf})_")
        if ident_lines:
            out.append("## Aliases")
            out.extend(ident_lines)
            out.append("")
        if url_lines:
            out.append("## System IDs")
            out.extend(url_lines)
            out.append("")

    # ---- Structural facts (factual confidence) ------------------------------
    edges = edges_for_entity(conn, client_id, entity_id, args.max_sensitivity)
    factual_edges = [e for e in edges if e[7] == "factual"]
    other_edges = [e for e in edges if e[7] != "factual"]

    if factual_edges:
        out.append("## Facts")
        by_pred = defaultdict(list)
        for e in factual_edges:
            by_pred[e[2]].append(e)
        for pred in sorted(by_pred):
            out.append(f"### {pred}")
            for e in by_pred[pred]:
                side = "→" if e[1] == entity_id else "←"
                other = (e[13] or e[5] or "—") if e[1] == entity_id else (e[11] or "—")
                other_type = (e[14] or e[6] or "literal") if e[1] == entity_id else e[12]
                line = f"- {side} {other}"
                if other_type and other_type != "literal":
                    line += f"  _({other_type})_"
                if e[8] and e[8] != "routine":
                    line += f"  **[{e[8]}]**"
                out.append(line)
            out.append("")

    # ---- Inferred / soft edges (stated/implied) -----------------------------
    if other_edges:
        out.append("## Inferred (stated/implied)")
        for e in other_edges:
            side = "→" if e[1] == entity_id else "←"
            other = (e[13] or e[5] or "—") if e[1] == entity_id else (e[11] or "—")
            line = f"- {side} **{e[2]}** {other}  _({e[7]}"
            if e[8] and e[8] != "routine":
                line += f", {e[8]}"
            line += ")_"
            out.append(line)
            if e[10]:
                out.append(f'    > _"{e[10][:200]}"_')
            cites = edge_citations(conn, e[0])
            for c in cites[:2]:
                src_kind, src_ext, src_ts, quote, m_url, d_url = c
                if m_url:
                    out.append(f"    cite: [meeting]({m_url})")
                elif d_url:
                    out.append(f"    cite: [doc]({d_url})")
                else:
                    out.append(f"    cite: {src_kind}:{src_ext}")
        out.append("")

    # ---- Items of note ------------------------------------------------------
    notes = notes_for_entity(conn, client_id, entity_id, args.max_sensitivity)
    if notes:
        out.append(f"## Items of note  ({len(notes)} total)")
        # Group by memory_type
        by_type = defaultdict(list)
        for n in notes:
            by_type[n[1]].append(n)
        type_order = ["risk", "decision", "open_question", "action_item", "recommendation",
                       "theme", "insight", "lesson", "observation"]
        for mt in type_order + sorted(set(by_type) - set(type_order)):
            if mt not in by_type:
                continue
            out.append(f"### {mt}  ({len(by_type[mt])})")
            for n in by_type[mt][:args.notes_per_type]:
                _, mtype, imp, sens, content, ts, role, sk, sext, sts, quote, m_url = n
                badge = f"imp={imp}"
                if sens and sens != "routine":
                    badge += f", **{sens}**"
                date_str = ts[:10] if ts else ""
                out.append(f"- _[{date_str}, {badge}]_  {content}")
                if m_url:
                    out.append(f"    [meeting]({m_url})")
            if len(by_type[mt]) > args.notes_per_type:
                out.append(f"  …and {len(by_type[mt]) - args.notes_per_type} more.")
        out.append("")

    # ---- Project-only: Box documents ---------------------------------------
    if etype == "project":
        docs, folder_path = docs_for_project(conn, client_id, entity_id)
        if docs:
            out.append(f"## Linked Box documents  ({len(docs)})")
            out.append(f"_root: {folder_path}_")
            for ext_id, fp, url, modified in docs[:25]:
                rel = fp[len(folder_path):] if fp.startswith(folder_path) else fp
                out.append(f"- [{rel}]({url})")
            if len(docs) > 25:
                out.append(f"  …and {len(docs) - 25} more.")
            out.append("")

    return "\n".join(out)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--client-slug", required=True)
    p.add_argument("--entity", required=True, help="entity name, alias, or substring to look up")
    p.add_argument("--db", default=os.path.join(os.path.dirname(__file__), "agent_memory.db"))
    p.add_argument("--max-sensitivity", default="sensitive",
                    choices=["routine", "sensitive", "hr_grade"],
                    help="Highest sensitivity tier to include (default: sensitive)")
    p.add_argument("--include-hr-grade", action="store_true",
                    help="Shortcut for --max-sensitivity hr_grade")
    p.add_argument("--notes-per-type", type=int, default=5,
                    help="Cap notes shown per memory_type (default 5)")
    p.add_argument("--all", action="store_true",
                    help="If multiple entity matches, render all instead of refusing")
    args = p.parse_args()
    if args.include_hr_grade:
        args.max_sensitivity = "hr_grade"

    conn = sqlite3.connect(os.path.expanduser(args.db))
    client_id, client_name = get_client(conn, args.client_slug)
    matches = find_entity(conn, client_id, args.entity)
    if not matches:
        print(f"No entity matches {args.entity!r}", file=sys.stderr)
        sys.exit(1)
    if len(matches) > 1 and not args.all:
        print(f"Multiple matches — pass --all or be more specific:")
        for eid, etype, ename in matches:
            print(f"  [{etype}] {ename}  (id={eid})")
        sys.exit(2)
    for eid, etype, ename in matches:
        print(render_text(conn, client_id, client_name, eid, etype, ename, args))
        if len(matches) > 1:
            print("\n---\n")


if __name__ == "__main__":
    main()
