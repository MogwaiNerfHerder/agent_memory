"""Retroactively re-attribute the already-processed batch50 meetings from the
shared fake 'batch-test' client to each meeting's real resolved client.

Entities are scoped per client_id and deduped WITHIN that scope
(resolve_or_create_entity matches on client_id + canonical_name) -- this
schema deliberately does not share one entity record across clients, since a
client's knowledge graph is meant to stay isolated from another's.

An entity referenced by meetings that resolve to more than one real client is
the normal case for Cortado staff (a consultant staffed on 10 engagements) and
cross-portfolio contacts (a PE firm partner across several of their portfolio
companies) -- not an anomaly. But naively cloning such an entity into each
client as a disconnected copy loses the fact that it's the same real person
every time -- exactly wrong for "Jessica worked on 5 clients, we should know
it's the same Jessica." Per migrate_v0_14_0_cross_client_identity.py: each
client still gets its own entity row (query isolation preserved), but rows
that are the same real person all point at one shared global_identity row.

Identification heuristic, since nothing on the entity row itself says "this
is Cortado staff": for each entity, look at the real participant list (email
+ name) of every meeting that cites it. A participant whose email is
@cortadogroup.com and whose name matches -> cortado_staff. A participant
whose email domain is a known cross-portfolio (multi-account) domain ->
cross_portfolio_contact. No match on any citing meeting -> treated as a
genuine client-specific person; if such an entity is STILL referenced by
meetings resolving to different real clients with no identity signal at all,
that's a real anomaly (a coincidental name collision, most likely) and is
left in place under batch-test rather than guessed at.

Memories (z_memory) and edges are effectively 1:1 with the meeting that
created them -- so they move outright with their meeting. Only entity
references (edge.subject_id/object_id, memory_entity.entity_id) need the
clone-and-link treatment.
"""
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from resolve_client import build_domain_keyword_index, resolve_client_for_meeting_with_fallback
from seed_from_cortado import Cortado

TESTDB = sys.argv[1] if len(sys.argv) > 1 else r"C:\Users\DAVID~1.RUS\AppData\Local\Temp\agent_memory_batch50_test.db"
SKILL_DIR = r"C:\Work\projectsheets\.claude\commands\cortado-api"
BATCH_TEST_SLUG = "batch-test"
LOCAL_TRANSCRIPT_GLOB = "C:/Work/marketing_commercial_intelligence/data/transcripts/*.json"
INTERNAL_DOMAIN = "cortadogroup.com"

# Mirrors run_batch50.py's ATTRIBUTION map. Meetings reassigned here were
# originally written with the pre-resolve_client.py default
# (attribution_source='manual', attribution_confidence='certain') because
# they all sat under the shared 'batch-test' client -- that overclaims
# certainty for whatever real resolve_client_for_meeting() status they
# actually get here (often 'likely' or 'guessed'). Without this map, a
# retroactively-reassigned meeting keeps a stale 'certain' label it never
# earned, silently miscalibrating attribution_confidence against the other
# two levels (entity_alias.confidence, attribution_event.confidence) that
# share this same certain|likely|guessed vocabulary.
ATTRIBUTION = {
    "resolved": ("cortado_account", "certain"),
    "resolved_via_domain": ("attendee_domain", "likely"),
    "resolved_via_title": ("content_inference", "guessed"),
    "cross_portfolio": ("manual", "guessed"),
    "unassigned": ("manual", "guessed"),
}


def load_local_meeting(guid):
    import glob
    matches = glob.glob(f"C:/Work/marketing_commercial_intelligence/data/transcripts/*{guid}*.json")
    return json.loads(Path(matches[0]).read_text(encoding="utf-8")) if matches else None


def get_or_create_global_identity(conn, identity_kind, canonical_name, cortado_staff_email=None):
    row = conn.execute(
        "SELECT global_identity_id FROM global_identity WHERE identity_kind=? AND canonical_name=?",
        (identity_kind, canonical_name),
    ).fetchone()
    if row:
        return row[0]
    cur = conn.execute(
        "INSERT INTO global_identity (identity_kind, canonical_name, cortado_staff_email) VALUES (?, ?, ?)",
        (identity_kind, canonical_name, cortado_staff_email),
    )
    return cur.lastrowid


def get_or_create_entity_clone(conn, entity_id, old_client_id, target_client_id, global_identity_id=None):
    """Return an entity_id under target_client_id matching entity_id's
    type/canonical_name, cloning it (with aliases) the first time a given
    target client needs it. If global_identity_id is set, both the clone and
    the original get linked to it (retroactively for the original), so a
    cross-client query can recognize them as the same real person."""
    row = conn.execute(
        "SELECT type, canonical_name, attributes FROM entity WHERE entity_id=?", (entity_id,)
    ).fetchone()
    if row is None:
        return None
    etype, cname, attrs = row

    if global_identity_id is not None:
        conn.execute("UPDATE entity SET global_identity_id=? WHERE entity_id=? AND global_identity_id IS NULL",
                     (global_identity_id, entity_id))

    existing = conn.execute(
        "SELECT entity_id FROM entity WHERE client_id=? AND type=? AND canonical_name=?",
        (target_client_id, etype, cname),
    ).fetchone()
    if existing:
        if global_identity_id is not None:
            conn.execute("UPDATE entity SET global_identity_id=? WHERE entity_id=? AND global_identity_id IS NULL",
                         (global_identity_id, existing[0]))
        return existing[0]

    cur = conn.execute(
        "INSERT INTO entity (client_id, type, canonical_name, attributes, global_identity_id) VALUES (?, ?, ?, ?, ?)",
        (target_client_id, etype, cname, attrs, global_identity_id),
    )
    new_entity_id = cur.lastrowid
    for alias_text, alias_kind, confidence in conn.execute(
        "SELECT alias_text, alias_kind, confidence FROM entity_alias WHERE entity_id=? AND client_id=?",
        (entity_id, old_client_id),
    ):
        conn.execute(
            """INSERT OR IGNORE INTO entity_alias
                   (client_id, entity_id, alias_text, alias_kind, confidence, resolved_by)
               VALUES (?, ?, ?, ?, ?, 'reassign_batch_clients:clone')""",
            (target_client_id, new_entity_id, alias_text, alias_kind, confidence),
        )
    return new_entity_id


def main():
    # Generalized beyond the original 'batch-test' shared fake client: the
    # exact same shared-bucket entity-pooling mistake recurred in
    # 'unassigned-no-account' after resolve_client.py's per-meeting-isolation
    # fix (each unassigned meeting now gets its own client going forward) --
    # this same clone/relink machinery retroactively splits whatever
    # already-pooled shared client is named here.
    source_slug = sys.argv[2] if len(sys.argv) > 2 else BATCH_TEST_SLUG
    conn = sqlite3.connect(TESTDB)
    conn.execute("PRAGMA foreign_keys = ON")

    old_client = conn.execute("SELECT client_id FROM client WHERE slug=?", (source_slug,)).fetchone()
    if not old_client:
        print(f"No '{source_slug}' client found -- nothing to reassign.")
        return
    old_client_id = old_client[0]

    cortado = Cortado(SKILL_DIR)
    index = build_domain_keyword_index(conn, LOCAL_TRANSCRIPT_GLOB)

    rows = conn.execute(
        "SELECT source_meeting_id, external_id FROM source_meeting WHERE client_id=?", (old_client_id,)
    ).fetchall()
    print(f"{len(rows)} meetings currently under the shared '{source_slug}' client.")

    meeting_new_client = {}
    meeting_attribution = {}  # source_meeting_id -> (attribution_source, attribution_confidence)
    meeting_participants = {}  # source_meeting_id -> participants list
    for source_meeting_id, guid in rows:
        meeting = load_local_meeting(guid)
        if meeting is None:
            print(f"  {guid}: SKIP (no local transcript cache)")
            continue
        meeting_participants[source_meeting_id] = meeting.get("participants") or []
        resolution = resolve_client_for_meeting_with_fallback(cortado, conn, meeting, index)
        index = build_domain_keyword_index(conn, LOCAL_TRANSCRIPT_GLOB)
        if resolution.client_slug is None:
            print(f"  {guid}: SKIP (client resolution failed: {resolution.reason})")
            continue
        meeting_new_client[source_meeting_id] = resolution.client_id
        meeting_attribution[source_meeting_id] = ATTRIBUTION[resolution.status]
        print(f"  {guid}: -> {resolution.client_slug} ({resolution.status})")

    # entity_id -> set of source_meeting_id that cite it, across all meetings
    # being reassigned -- used to identify cross-client people by looking at
    # every meeting that mentions them, not just the current one.
    entity_citing_meetings: dict[int, set[int]] = {}

    def record_citation(entity_id, source_meeting_id):
        entity_citing_meetings.setdefault(entity_id, set()).add(source_meeting_id)

    for source_meeting_id in meeting_new_client:
        for (memory_id,) in conn.execute(
            "SELECT cited_id FROM citation WHERE cited_kind='memory' AND source_kind='meeting' AND source_id=?",
            (source_meeting_id,),
        ):
            for (entity_id,) in conn.execute("SELECT entity_id FROM memory_entity WHERE memory_id=?", (memory_id,)):
                record_citation(entity_id, source_meeting_id)
        for (edge_id,) in conn.execute(
            "SELECT cited_id FROM citation WHERE cited_kind='edge' AND source_kind='meeting' AND source_id=?",
            (source_meeting_id,),
        ):
            subj, obj = conn.execute("SELECT subject_id, object_id FROM edge WHERE edge_id=?", (edge_id,)).fetchone()
            if subj:
                record_citation(subj, source_meeting_id)
            if obj:
                record_citation(obj, source_meeting_id)

    identity_cache: dict[int, tuple[str, str | None] | None] = {}

    def identify_entity(entity_id):
        if entity_id in identity_cache:
            return identity_cache[entity_id]
        row = conn.execute("SELECT type, canonical_name FROM entity WHERE entity_id=?", (entity_id,)).fetchone()
        etype, cname = (row[0], row[1].strip().lower()) if row else (None, "")
        if etype != "person":
            # Cross-client identity linking only applies to actual people.
            # An 'event' entity like "Bill Piacitelli / David Farrell Meeting
            # 2025-11-25" would otherwise substring-match "Bill Piacitelli"
            # and get incorrectly linked to his global_identity -- found and
            # fixed after the first run showed exactly this on real data.
            identity_cache[entity_id] = None
            return None
        result = None
        for source_meeting_id in entity_citing_meetings.get(entity_id, ()):
            for p in meeting_participants.get(source_meeting_id, []):
                pname = (p.get("name") or "").strip().lower()
                email = (p.get("email") or "").strip().lower()
                if not pname or "@" not in email:
                    continue
                if pname != cname and pname not in cname and cname not in pname:
                    continue
                domain = email.rsplit("@", 1)[1]
                if domain == INTERNAL_DOMAIN:
                    result = ("cortado_staff", email)
                    break
                if domain in index.domain_to_multi_accounts:
                    result = result or ("cross_portfolio_contact", None)
            if result and result[0] == "cortado_staff":
                break
        identity_cache[entity_id] = result
        return result

    moved_meetings = moved_memories = moved_edges = 0
    entity_clones_created = 0
    global_identities_used = 0
    clone_cache: dict[tuple[int, int], int] = {}

    def clone_for(entity_id, target_client_id):
        nonlocal entity_clones_created, global_identities_used
        key = (entity_id, target_client_id)
        if key not in clone_cache:
            identity = identify_entity(entity_id)
            global_identity_id = None
            if identity:
                kind, email = identity
                cname_row = conn.execute("SELECT canonical_name FROM entity WHERE entity_id=?", (entity_id,)).fetchone()
                global_identity_id = get_or_create_global_identity(conn, kind, cname_row[0], email)
                global_identities_used += 1
            before = conn.execute("SELECT COUNT(*) FROM entity WHERE client_id=?", (target_client_id,)).fetchone()[0]
            clone_cache[key] = get_or_create_entity_clone(conn, entity_id, old_client_id, target_client_id, global_identity_id)
            after = conn.execute("SELECT COUNT(*) FROM entity WHERE client_id=?", (target_client_id,)).fetchone()[0]
            entity_clones_created += (after - before)
        return clone_cache[key]

    try:
        for source_meeting_id, new_client_id in meeting_new_client.items():
            attribution_source, attribution_confidence = meeting_attribution[source_meeting_id]
            conn.execute(
                "UPDATE source_meeting SET client_id=?, attribution_source=?, attribution_confidence=? "
                "WHERE source_meeting_id=?",
                (new_client_id, attribution_source, attribution_confidence, source_meeting_id),
            )
            moved_meetings += 1

            for (memory_id,) in conn.execute(
                "SELECT cited_id FROM citation WHERE cited_kind='memory' AND source_kind='meeting' AND source_id=?",
                (source_meeting_id,),
            ):
                cur = conn.execute("UPDATE z_memory SET client_id=? WHERE memory_id=? AND client_id=?",
                                    (new_client_id, memory_id, old_client_id))
                moved_memories += cur.rowcount
                for entity_id, role in conn.execute(
                    "SELECT entity_id, role FROM memory_entity WHERE memory_id=?", (memory_id,)
                ):
                    clone_id = clone_for(entity_id, new_client_id)
                    if clone_id and clone_id != entity_id:
                        conn.execute("DELETE FROM memory_entity WHERE memory_id=? AND entity_id=?",
                                     (memory_id, entity_id))
                        conn.execute(
                            "INSERT OR IGNORE INTO memory_entity (memory_id, entity_id, role) VALUES (?, ?, ?)",
                            (memory_id, clone_id, role),
                        )

            for (edge_id,) in conn.execute(
                "SELECT cited_id FROM citation WHERE cited_kind='edge' AND source_kind='meeting' AND source_id=?",
                (source_meeting_id,),
            ):
                subj, obj = conn.execute(
                    "SELECT subject_id, object_id FROM edge WHERE edge_id=?", (edge_id,)
                ).fetchone()
                new_subj = clone_for(subj, new_client_id) if subj else None
                new_obj = clone_for(obj, new_client_id) if obj else None
                cur = conn.execute(
                    "UPDATE edge SET client_id=?, subject_id=?, object_id=? WHERE edge_id=? AND client_id=?",
                    (new_client_id, new_subj or subj, new_obj if obj else None, edge_id, old_client_id),
                )
                moved_edges += cur.rowcount

        conn.commit()
    except Exception:
        conn.rollback()
        raise

    print(f"\nApplied: {moved_meetings} meetings, {moved_memories} notes, {moved_edges} edges moved; "
          f"{entity_clones_created} client-scoped entity rows created, "
          f"{global_identities_used} of those clone-lookups matched a shared real-person identity "
          f"(Cortado staff / cross-portfolio contact) rather than being a plain unlinked clone.")

    print("\n=== Cross-client identities found ===")
    for gid, kind, cname, email in conn.execute(
        "SELECT global_identity_id, identity_kind, canonical_name, cortado_staff_email FROM global_identity ORDER BY identity_kind, canonical_name"
    ).fetchall():
        n_clients = conn.execute("SELECT COUNT(DISTINCT client_id) FROM entity WHERE global_identity_id=?", (gid,)).fetchone()[0]
        print(f"  [{kind}] {cname}{' <'+email+'>' if email else ''} -- appears in {n_clients} client(s)")

    print("\n=== Post-reassignment summary ===")
    for client_id, slug, name in conn.execute("SELECT client_id, slug, name FROM client ORDER BY slug").fetchall():
        n_meetings = conn.execute("SELECT COUNT(*) FROM source_meeting WHERE client_id=?", (client_id,)).fetchone()[0]
        n_entities = conn.execute("SELECT COUNT(*) FROM entity WHERE client_id=?", (client_id,)).fetchone()[0]
        n_edges = conn.execute("SELECT COUNT(*) FROM edge WHERE client_id=?", (client_id,)).fetchone()[0]
        n_memories = conn.execute("SELECT COUNT(*) FROM z_memory WHERE client_id=?", (client_id,)).fetchone()[0]
        if n_meetings or n_entities or n_edges or n_memories:
            print(f"  {slug:35} meetings={n_meetings:3} entities={n_entities:4} edges={n_edges:4} notes={n_memories:4}")


if __name__ == "__main__":
    main()
