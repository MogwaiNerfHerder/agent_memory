"""
query_graph.py - Retrieval helpers for the client knowledge graph.

CLI for inspecting what's in agent_memory.db for a given client tenant.
Used to sanity-check seeded data and (later) extracted soft knowledge.

Subcommands:
    dossier <client> <entity>         Everything we know about an entity
    stakeholders <client> <project>   Project stakeholders with soft data
    project <client> <project>        Project facts + people staffed
    proposed-predicates <client>      Predicates pending review with counts
    resolve <client> <text>           Test alias resolution
    discrepancies <client> [entity]   Edges that supersede / disagree
    recent <client> [--since DATE]    Recent hot observations (z_memory)
    stats <client>                    Graph summary

All output is human-readable text; pass --json for machine-readable.
"""

import argparse
import json
import os
import sqlite3
import sys
from collections import defaultdict


def get_client_id(conn, slug):
    row = conn.execute("SELECT client_id, name FROM client WHERE slug=?", (slug,)).fetchone()
    if not row:
        raise SystemExit(f"No client with slug '{slug}'")
    return row[0], row[1]


def find_entity(conn, client_id, query):
    """Resolve an entity by alias_text (case-insensitive), or canonical_name LIKE."""
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


def edges_for_entity(conn, client_id, entity_id, direction="both"):
    """Return all edges touching an entity, with predicate and the other side resolved."""
    sql = """
    SELECT e.edge_id, e.subject_id, p.name AS predicate, e.object_id, e.object_literal,
           e.object_literal_type, e.confidence, e.sensitivity, e.status, e.notes,
           subj.canonical_name AS subject_name, subj.type AS subject_type,
           obj.canonical_name AS object_name, obj.type AS object_type
      FROM edge e
      JOIN predicate p ON p.predicate_id = e.predicate_id
      JOIN entity subj ON subj.entity_id = e.subject_id
 LEFT JOIN entity obj ON obj.entity_id = e.object_id
     WHERE e.client_id = ?
       AND e.status = 'active'
       AND (e.subject_id = ? OR e.object_id = ?)
  ORDER BY p.name, e.edge_id
    """
    return conn.execute(sql, (client_id, entity_id, entity_id)).fetchall()


def citations_for_edge(conn, edge_id):
    return conn.execute(
        """SELECT source_kind, source_id, source_external_id, source_ts, quote, extracted_by
             FROM citation WHERE cited_kind='edge' AND cited_id=?
            ORDER BY citation_id""",
        (edge_id,),
    ).fetchall()


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------

def cmd_dossier(args, conn):
    client_id, client_name = get_client_id(conn, args.client)
    matches = find_entity(conn, client_id, args.entity)
    if not matches:
        print(f"No entity matches '{args.entity}' in {client_name}")
        return
    if len(matches) > 1 and not args.all:
        print(f"Multiple matches for '{args.entity}'; pass --all to dump every one, or be more specific:")
        for eid, etype, ename in matches:
            print(f"  [{etype}] {ename}  (entity_id={eid})")
        return
    for eid, etype, ename in matches:
        render_entity_dossier(conn, client_id, client_name, eid, etype, ename, json_out=args.json)


def render_entity_dossier(conn, client_id, client_name, entity_id, etype, ename, json_out=False):
    aliases = conn.execute(
        "SELECT alias_text, alias_kind, confidence FROM entity_alias WHERE entity_id=? ORDER BY alias_kind, alias_text",
        (entity_id,),
    ).fetchall()
    edges = edges_for_entity(conn, client_id, entity_id)

    out_edges = []
    for e in edges:
        cites = citations_for_edge(conn, e[0])
        side = "subject" if e[1] == entity_id else "object"
        other = e[12] if side == "subject" else e[10]
        other_type = e[13] if side == "subject" else e[11]
        if e[3] is None and e[4] is not None:
            other = e[4]
            other_type = f"literal:{e[5] or '?'}"
        out_edges.append({
            "edge_id": e[0], "side": side,
            "predicate": e[2],
            "other": other, "other_type": other_type,
            "confidence": e[6], "sensitivity": e[7], "status": e[8],
            "notes": e[9],
            "citations": [
                {"source_kind": c[0], "source_id": c[1], "source_external_id": c[2],
                 "source_ts": c[3], "quote": c[4]}
                for c in cites
            ],
        })

    if json_out:
        print(json.dumps({
            "client": client_name, "entity": ename, "type": etype,
            "entity_id": entity_id,
            "aliases": [{"alias": a[0], "kind": a[1], "confidence": a[2]} for a in aliases],
            "edges": out_edges,
        }, indent=2, default=str))
        return

    print(f"\n=== {ename}  [{etype}]  (client={client_name}, entity_id={entity_id}) ===")
    if aliases:
        print("Aliases:")
        for txt, kind, conf in aliases:
            print(f"  • {txt}  ({kind}, {conf})")
    if not out_edges:
        print("  (no edges)")
        return
    by_pred = defaultdict(list)
    for e in out_edges:
        by_pred[e["predicate"]].append(e)
    for pred in sorted(by_pred):
        print(f"\n  {pred}:")
        for e in by_pred[pred]:
            arrow = "→" if e["side"] == "subject" else "←"
            sens = "" if e["sensitivity"] == "routine" else f" [{e['sensitivity']}]"
            print(f"    {arrow} {e['other']}  ({e['other_type']}, {e['confidence']}{sens})")
            if e["notes"]:
                print(f"        note: {e['notes']}")
            for c in e["citations"]:
                ext = c["source_external_id"] or c["source_id"]
                ts = f" @ {c['source_ts']}" if c["source_ts"] else ""
                q = f' — "{c["quote"]}"' if c["quote"] else ""
                print(f"        cite: {c['source_kind']}:{ext}{ts}{q}")


def cmd_stakeholders(args, conn):
    client_id, client_name = get_client_id(conn, args.client)
    matches = find_entity(conn, client_id, args.project)
    proj_matches = [m for m in matches if m[1] == "project"]
    if not proj_matches:
        print(f"No project matches '{args.project}'")
        return
    for proj_id, _, proj_name in proj_matches:
        print(f"\n=== Stakeholders on {proj_name} ===")
        rows = conn.execute(
            """SELECT subj.entity_id, subj.canonical_name
                 FROM edge e
                 JOIN entity subj ON subj.entity_id=e.subject_id
                 JOIN predicate p ON p.predicate_id=e.predicate_id
                WHERE e.client_id=? AND e.object_id=? AND p.name='stakeholder_on'
             ORDER BY subj.canonical_name""",
            (client_id, proj_id),
        ).fetchall()
        if not rows:
            print("  (none)")
            continue
        for sh_id, sh_name in rows:
            soft = conn.execute(
                """SELECT p.name, e.object_literal
                     FROM edge e JOIN predicate p ON p.predicate_id=e.predicate_id
                    WHERE e.client_id=? AND e.subject_id=? AND p.name LIKE 'stakeholder_%' AND p.name!='stakeholder_on'""",
                (client_id, sh_id),
            ).fetchall()
            extras = ", ".join(f"{p}={v}" for p, v in soft) if soft else ""
            print(f"  • {sh_name}" + (f"  ({extras})" if extras else ""))


def cmd_project(args, conn):
    client_id, client_name = get_client_id(conn, args.client)
    matches = [m for m in find_entity(conn, client_id, args.project) if m[1] == "project"]
    if not matches:
        print(f"No project matches '{args.project}'")
        return
    for proj_id, _, proj_name in matches:
        render_entity_dossier(conn, client_id, client_name, proj_id, "project", proj_name, json_out=args.json)
        if args.json:
            continue
        # Add a "people" rollup
        team = conn.execute(
            """SELECT subj.canonical_name, e.object_literal
                 FROM edge e JOIN predicate p ON p.predicate_id=e.predicate_id
                 JOIN entity subj ON subj.entity_id=e.subject_id
                WHERE e.client_id=? AND e.object_id=? AND p.name='staffed_on' """,
            (client_id, proj_id),
        ).fetchall()
        roles = {}
        for r in conn.execute(
            """SELECT subj.canonical_name, e.object_literal
                 FROM edge e JOIN predicate p ON p.predicate_id=e.predicate_id
                 JOIN entity subj ON subj.entity_id=e.subject_id
                WHERE e.client_id=? AND p.name='engagement_role'
                  AND subj.entity_id IN (
                      SELECT subject_id FROM edge WHERE client_id=? AND object_id=?
                                                    AND predicate_id=(SELECT predicate_id FROM predicate WHERE name='staffed_on')
                  )""",
            (client_id, client_id, proj_id),
        ).fetchall():
            roles.setdefault(r[0], []).append(r[1])
        if team:
            print("\n  Staffed (Cortado side):")
            for name, _ in team:
                role_str = ", ".join(roles.get(name, [])) or "—"
                print(f"    • {name}  [{role_str}]")


def cmd_proposed_predicates(args, conn):
    client_id, _ = get_client_id(conn, args.client)
    rows = conn.execute(
        """SELECT p.name, p.status, p.proposed_count, COUNT(e.edge_id) AS edge_count
             FROM predicate p
        LEFT JOIN edge e ON e.predicate_id = p.predicate_id AND e.client_id = ?
            WHERE p.status='proposed'
         GROUP BY p.predicate_id
         ORDER BY edge_count DESC, p.proposed_count DESC""",
        (client_id,),
    ).fetchall()
    if not rows:
        print("No proposed predicates.")
        return
    print(f"{'predicate':<30}  {'edges':>6}  {'proposed_count':>15}")
    print("-" * 56)
    for name, _, pc, ec in rows:
        print(f"{name:<30}  {ec:>6}  {pc:>15}")


def cmd_resolve(args, conn):
    client_id, _ = get_client_id(conn, args.client)
    matches = find_entity(conn, client_id, args.text)
    if not matches:
        print(f"No matches for '{args.text}'.")
        return
    for eid, etype, ename in matches:
        print(f"  [{etype}] {ename}  (entity_id={eid})")
        for txt, kind, conf in conn.execute(
            "SELECT alias_text, alias_kind, confidence FROM entity_alias WHERE entity_id=?", (eid,)):
            print(f"      • {txt}  ({kind}, {conf})")


def cmd_discrepancies(args, conn):
    client_id, _ = get_client_id(conn, args.client)
    sql = """
    SELECT new_e.edge_id, p.name, subj.canonical_name,
           COALESCE(new_obj.canonical_name, new_e.object_literal),
           old_e.edge_id, COALESCE(old_obj.canonical_name, old_e.object_literal)
      FROM edge new_e
      JOIN edge old_e ON old_e.edge_id = new_e.supersedes_id
      JOIN predicate p ON p.predicate_id = new_e.predicate_id
      JOIN entity subj ON subj.entity_id = new_e.subject_id
 LEFT JOIN entity new_obj ON new_obj.entity_id = new_e.object_id
 LEFT JOIN entity old_obj ON old_obj.entity_id = old_e.object_id
     WHERE new_e.client_id = ? AND new_e.status='active'
    """
    params = [client_id]
    if args.entity:
        ents = find_entity(conn, client_id, args.entity)
        if not ents:
            print(f"No entity matches '{args.entity}'")
            return
        ids = [e[0] for e in ents]
        sql += " AND (new_e.subject_id IN ({0}) OR new_e.object_id IN ({0}))".format(",".join("?" * len(ids)))
        params.extend(ids * 2)
    rows = conn.execute(sql, params).fetchall()
    if not rows:
        print("No supersessions yet (no inferred contradictions).")
        return
    for new_id, pred, subj, new_obj, old_id, old_obj in rows:
        print(f"  {subj} --{pred}-->")
        print(f"    was: {old_obj}  (edge {old_id}, superseded)")
        print(f"    now: {new_obj}  (edge {new_id}, active)")


def cmd_recent(args, conn):
    client_id, _ = get_client_id(conn, args.client)
    sql = "SELECT memory_id, content, memory_type, importance, created_at FROM z_memory WHERE client_id=? AND is_active=1 AND deleted_at IS NULL"
    params = [client_id]
    if args.since:
        sql += " AND created_at >= ?"
        params.append(args.since)
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(args.limit)
    rows = conn.execute(sql, params).fetchall()
    if not rows:
        print("No hot observations yet (z_memory empty for this client).")
        return
    for mid, content, mtype, imp, ts in rows:
        print(f"  [{mtype} imp={imp} {ts}] {content[:200]}")


def cmd_notes(args, conn):
    client_id, _ = get_client_id(conn, args.client)
    if args.entity:
        ents = find_entity(conn, client_id, args.entity)
        if not ents:
            print(f"No entity matches '{args.entity}'")
            return
        for eid, etype, ename in ents:
            print(f"\n=== Notes about {ename} [{etype}] ===")
            sql = """
            SELECT m.memory_id, m.memory_type, m.importance, m.content, m.created_at, me.role,
                   c.source_kind, c.source_external_id, c.source_ts, c.quote
              FROM memory_entity me
              JOIN z_memory m ON m.memory_id = me.memory_id
         LEFT JOIN citation c ON c.cited_kind='memory' AND c.cited_id = m.memory_id
             WHERE me.entity_id = ? AND m.is_active=1 AND m.deleted_at IS NULL
            """
            params = [eid]
            if args.type:
                sql += " AND m.memory_type=?"; params.append(args.type)
            sql += " ORDER BY m.created_at DESC LIMIT ?"; params.append(args.limit)
            rows = conn.execute(sql, params).fetchall()
            if not rows:
                print("  (no notes)")
                continue
            seen = set()
            for mid, mtype, imp, content, ts, role, sk, sext, sts, quote in rows:
                if mid in seen:
                    continue
                seen.add(mid)
                print(f"\n  [{mtype} imp={imp} role={role} {ts[:10] if ts else ''}]")
                print(f"  {content}")
                if sk:
                    cite_id = sext or "manual"
                    cite_str = f"  cite: {sk}:{cite_id}"
                    if sts:
                        cite_str += f" @ {sts[:10]}"
                    print(cite_str)
                    if quote:
                        print(f"  quote: \"{quote[:200]}\"")
        return

    # No entity given: just list recent notes for client
    sql = "SELECT memory_id, memory_type, importance, content, created_at FROM z_memory WHERE client_id=? AND is_active=1 AND deleted_at IS NULL"
    params = [client_id]
    if args.type:
        sql += " AND memory_type=?"; params.append(args.type)
    sql += " ORDER BY created_at DESC LIMIT ?"; params.append(args.limit)
    rows = conn.execute(sql, params).fetchall()
    if not rows:
        print("No notes for this client.")
        return
    for mid, mtype, imp, content, ts in rows:
        print(f"  [{mtype} imp={imp} {ts[:10] if ts else ''}] {content[:200]}")


def cmd_drifts(args, conn):
    client_id, _ = get_client_id(conn, args.client)
    sql = """SELECT dh.drift_hypothesis_id, dh.observed_token, e.entity_id, e.canonical_name, e.type,
                    dh.rationale, dh.supporting_quote, dh.source_kind, dh.source_external_id,
                    dh.status, dh.proposed_by, dh.created_at
               FROM drift_hypothesis dh JOIN entity e ON e.entity_id = dh.candidate_entity_id
              WHERE dh.client_id = ?"""
    params = [client_id]
    if not args.all:
        sql += " AND dh.status='pending'"
    sql += " ORDER BY dh.status, dh.created_at"
    rows = conn.execute(sql, params).fetchall()
    if not rows:
        print("No drift hypotheses." + ("" if args.all else "  (none pending)"))
        return
    for r in rows:
        hid, tok, eid, ename, etype, rat, quote, sk, sext, st, pb, ts = r
        print(f"\n[{st}] hypothesis_id={hid}  ({ts[:19] if ts else ''})")
        print(f"  observed:    {tok!r}")
        print(f"  candidate:   [{etype}] {ename}  (entity_id={eid})")
        if rat:
            print(f"  rationale:   {rat}")
        if quote:
            print(f"  quote:       \"{quote}\"")
        if sk:
            print(f"  source:      {sk}:{sext}")
        print(f"  proposed_by: {pb}")
    print()
    print("Approve:  python3 query_graph.py approve-drift <hypothesis_id>")
    print("Reject:   python3 query_graph.py reject-drift  <hypothesis_id>")


def cmd_approve_drift(args, conn):
    row = conn.execute(
        "SELECT client_id, observed_token, candidate_entity_id, status FROM drift_hypothesis WHERE drift_hypothesis_id=?",
        (args.hypothesis_id,),
    ).fetchone()
    if not row:
        print(f"No drift_hypothesis with id={args.hypothesis_id}")
        return
    client_id, tok, cand, status = row
    if status != "pending":
        print(f"hypothesis {args.hypothesis_id} is already {status}; refusing")
        return
    conn.execute(
        """INSERT OR IGNORE INTO entity_alias (client_id, entity_id, alias_text, alias_kind, confidence, resolved_by)
           VALUES (?, ?, ?, 'transcription_drift', 'certain', ?)""",
        (client_id, cand, tok, f"approve-drift:{args.by}"),
    )
    conn.execute(
        "UPDATE drift_hypothesis SET status='approved', reviewed_by=?, reviewed_at=CURRENT_TIMESTAMP WHERE drift_hypothesis_id=?",
        (args.by, args.hypothesis_id),
    )
    conn.commit()
    print(f"Approved: '{tok}' is now a transcription_drift alias on entity_id={cand}")


def cmd_reject_drift(args, conn):
    row = conn.execute(
        "SELECT status FROM drift_hypothesis WHERE drift_hypothesis_id=?", (args.hypothesis_id,)
    ).fetchone()
    if not row:
        print(f"No drift_hypothesis with id={args.hypothesis_id}")
        return
    if row[0] != "pending":
        print(f"hypothesis {args.hypothesis_id} is already {row[0]}; refusing")
        return
    conn.execute(
        "UPDATE drift_hypothesis SET status='rejected', reviewed_by=?, reviewed_at=CURRENT_TIMESTAMP WHERE drift_hypothesis_id=?",
        (args.by, args.hypothesis_id),
    )
    conn.commit()
    print(f"Rejected hypothesis {args.hypothesis_id}.")


def cmd_timeline(args, conn):
    client_id, _ = get_client_id(conn, args.client)
    timing_predicates = ("scheduled_for", "occurred_at", "occurred_around",
                          "started_at", "ended_at", "discovered_at")
    placeholders = ",".join("?" * len(timing_predicates))
    sql = f"""
    SELECT e.edge_id, e.object_literal AS dt, e.object_literal_type AS dt_type,
           p.name AS timing, subj.canonical_name AS subj_name, subj.type AS subj_type,
           subj.entity_id AS subj_id,
           (SELECT object_literal FROM edge x JOIN predicate xp ON xp.predicate_id=x.predicate_id
              WHERE x.client_id=e.client_id AND x.subject_id=e.subject_id AND xp.name='has_event_type'
                AND x.status='active' LIMIT 1) AS event_type,
           (SELECT object_literal FROM edge x JOIN predicate xp ON xp.predicate_id=x.predicate_id
              WHERE x.client_id=e.client_id AND x.subject_id=e.subject_id AND xp.name='event_status'
                AND x.status='active' LIMIT 1) AS event_status
      FROM edge e
      JOIN predicate p ON p.predicate_id=e.predicate_id
      JOIN entity subj ON subj.entity_id=e.subject_id
     WHERE e.client_id=? AND e.status='active' AND e.object_literal IS NOT NULL
       AND p.name IN ({placeholders})
    """
    params = [client_id, *timing_predicates]
    if args.frm:
        sql += " AND e.object_literal >= ?"; params.append(args.frm)
    if args.to:
        sql += " AND e.object_literal <= ?"; params.append(args.to)
    if args.type:
        sql += " AND EXISTS (SELECT 1 FROM edge xx JOIN predicate xp ON xp.predicate_id=xx.predicate_id WHERE xx.client_id=e.client_id AND xx.subject_id=e.subject_id AND xp.name='has_event_type' AND xx.object_literal=? AND xx.status='active')"
        params.append(args.type)
    if args.entity:
        ents = find_entity(conn, client_id, args.entity)
        if not ents:
            print(f"No entity matches '{args.entity}'")
            return
        ids = [e[0] for e in ents]
        sql += " AND e.subject_id IN (" + ",".join("?" * len(ids)) + ")"
        params.extend(ids)
    sql += " ORDER BY e.object_literal, p.name"
    rows = conn.execute(sql, params).fetchall()
    if not rows:
        print("No timeline entries.")
        return
    for r in rows:
        eid, dt, dt_type, timing, sname, stype, sid, etype, estatus = r
        bits = [f"  {dt}"]
        if dt_type and dt_type != "date":
            bits.append(f"({dt_type})")
        bits.append(f"[{timing}]")
        bits.append(f"{sname}")
        if etype:
            bits.append(f":: {etype}")
        if estatus:
            bits.append(f"({estatus})")
        print(" ".join(bits))


def cmd_stats(args, conn):
    client_id, client_name = get_client_id(conn, args.client)
    print(f"Client: {client_name}  (client_id={client_id})\n")
    n_entities = conn.execute("SELECT type, COUNT(*) FROM entity WHERE client_id=? GROUP BY type", (client_id,)).fetchall()
    print("Entities:")
    for t, n in n_entities:
        print(f"  {t}: {n}")
    n_aliases = conn.execute("SELECT COUNT(*) FROM entity_alias WHERE client_id=?", (client_id,)).fetchone()[0]
    print(f"  aliases: {n_aliases}")
    print()
    n_edges = conn.execute("SELECT COUNT(*) FROM edge WHERE client_id=?", (client_id,)).fetchone()[0]
    print(f"Edges: {n_edges}")
    for r in conn.execute("""
        SELECT confidence, COUNT(*) FROM edge WHERE client_id=? GROUP BY confidence ORDER BY 2 DESC
    """, (client_id,)):
        print(f"  confidence={r[0]}: {r[1]}")
    for r in conn.execute("""
        SELECT sensitivity, COUNT(*) FROM edge WHERE client_id=? GROUP BY sensitivity ORDER BY 2 DESC
    """, (client_id,)):
        print(f"  sensitivity={r[0]}: {r[1]}")
    print()
    sources = conn.execute("""
        SELECT c.source_kind, COUNT(*)
          FROM citation c JOIN edge e ON e.edge_id=c.cited_id AND c.cited_kind='edge'
         WHERE e.client_id=? GROUP BY c.source_kind ORDER BY 2 DESC
    """, (client_id,)).fetchall()
    if sources:
        print("Edges by source kind:")
        for k, n in sources:
            print(f"  {k}: {n}")
    n_mem = conn.execute("SELECT COUNT(*) FROM z_memory WHERE client_id=?", (client_id,)).fetchone()[0]
    print(f"\nHot facts (z_memory): {n_mem}")


# ---------------------------------------------------------------------------

def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", default=os.path.join(os.path.dirname(__file__), "agent_memory.db"))
    p.add_argument("--json", action="store_true", help="Machine-readable JSON output where applicable")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("dossier"); s.add_argument("client"); s.add_argument("entity"); s.add_argument("--all", action="store_true")
    s = sub.add_parser("stakeholders"); s.add_argument("client"); s.add_argument("project")
    s = sub.add_parser("project"); s.add_argument("client"); s.add_argument("project")
    s = sub.add_parser("proposed-predicates"); s.add_argument("client")
    s = sub.add_parser("resolve"); s.add_argument("client"); s.add_argument("text")
    s = sub.add_parser("discrepancies"); s.add_argument("client"); s.add_argument("entity", nargs="?")
    s = sub.add_parser("recent"); s.add_argument("client"); s.add_argument("--since"); s.add_argument("--limit", type=int, default=20)
    s = sub.add_parser("stats"); s.add_argument("client")
    s = sub.add_parser("notes"); s.add_argument("client"); s.add_argument("entity", nargs="?"); s.add_argument("--type"); s.add_argument("--limit", type=int, default=20)
    s = sub.add_parser("drifts"); s.add_argument("client"); s.add_argument("--all", action="store_true", help="Include approved/rejected (default: pending only)")
    s = sub.add_parser("timeline"); s.add_argument("client"); s.add_argument("--from", dest="frm"); s.add_argument("--to"); s.add_argument("--type"); s.add_argument("--entity")
    s = sub.add_parser("approve-drift"); s.add_argument("hypothesis_id", type=int); s.add_argument("--by", default=os.environ.get("USER", "manual"))
    s = sub.add_parser("reject-drift"); s.add_argument("hypothesis_id", type=int); s.add_argument("--by", default=os.environ.get("USER", "manual"))

    args = p.parse_args()
    conn = sqlite3.connect(os.path.expanduser(args.db))
    conn.execute("PRAGMA foreign_keys = ON")

    handlers = {
        "dossier": cmd_dossier,
        "stakeholders": cmd_stakeholders,
        "project": cmd_project,
        "proposed-predicates": cmd_proposed_predicates,
        "resolve": cmd_resolve,
        "discrepancies": cmd_discrepancies,
        "recent": cmd_recent,
        "stats": cmd_stats,
        "notes": cmd_notes,
        "drifts": cmd_drifts,
        "approve-drift": cmd_approve_drift,
        "reject-drift": cmd_reject_drift,
        "timeline": cmd_timeline,
    }
    handlers[args.cmd](args, conn)


if __name__ == "__main__":
    main()
