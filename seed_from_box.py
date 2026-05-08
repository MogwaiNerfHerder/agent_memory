"""
seed_from_box.py - Seed prior engagements + Box documents from box_files.db.

Walks the per-client section of the Box index (e.g. "All Files/Clients/HIG Growth/")
and mints:
  - project entity for each Addendum / engagement folder
  - company entity for the target (parsed from folder name)
  - codename alias on the target (Colt -> Baxter Planning, Leopard -> Carebox, ...)
  - edges:
        client_company --engages_cortado_for--> project
        project        --commissioned_by-------> client_company
        project        --concerns---------------> target
        project        --target_of_diligence----> target
        project        --has_box_folder---------> "<full_path>"  (literal)
  - source_document rows for every file under each engagement folder
  - one cite per project edge to source_kind='box', source_external_id=<box_folder_id>

Box DB is read-only in containers at /data/box_files.db. We query via:
    docker exec <bot_container> python3 -c '...'

Usage:
    seed_from_box.py --client-slug hig_growth_partners \\
        --base-folder "All Files/Clients/HIG Growth/"
"""

import argparse
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path


# Pattern: "HIG Growth - Addendum E - Project Leopard DD (Carebox)"
#                         ^^         ^^^^^^^^         ^^^^^^^
#                       letter       codename           target
ENGAGEMENT_RE = re.compile(
    r"^.+?\s+-\s+Addendum\s+(?P<letter>[A-Z])\s+-\s+Project\s+(?P<codename>[A-Za-z0-9]+)"
    r"(?:\s+\w+)?"                                # optional engagement-type word: DD, CDD, GTM, etc.
    r"(?:\s*\((?P<target>[^)]+)\))?",
    re.IGNORECASE,
)


def docker_exec_sql(container, query, *params):
    """Run an SQL query in the container against /data/box_files.db, return JSON rows."""
    code = f"""
import sqlite3, json, sys
con = sqlite3.connect('file:/data/box_files.db?mode=ro', uri=True)
params = {list(params)!r}
rows = list(con.execute({query!r}, params))
print(json.dumps(rows, default=str))
"""
    proc = subprocess.run(
        ["docker", "exec", container, "python3", "-c", code],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise SystemExit(f"docker exec failed: {proc.stderr[:500]}")
    return json.loads(proc.stdout)


class GraphDB:
    def __init__(self, db_path):
        self.conn = sqlite3.connect(db_path)
        self.conn.execute("PRAGMA foreign_keys = ON")
        self._scripts = {}
        for name in ("resolve_or_create_entity", "propose_predicate"):
            row = self.conn.execute(
                "SELECT script_body FROM z_script_catalog WHERE script_name=? AND is_active=1", (name,)
            ).fetchone()
            ns = {}; exec(row[0], ns); self._scripts[name] = ns[name]

    def call(self, name, *args, **kwargs):
        return self._scripts[name](self.conn, *args, **kwargs)

    def get_client(self, slug):
        row = self.conn.execute("SELECT client_id, name FROM client WHERE slug=?", (slug,)).fetchone()
        if not row:
            raise SystemExit(f"No client with slug '{slug}'")
        return row[0], row[1]

    def get_client_company_entity(self, client_id):
        # Find the company entity that represents the client itself
        row = self.conn.execute(
            "SELECT entity_id FROM entity WHERE client_id=? AND type='company' "
            "AND canonical_name=(SELECT name FROM client WHERE client_id=?) LIMIT 1",
            (client_id, client_id),
        ).fetchone()
        if not row:
            raise SystemExit("Client company entity not found; run seed_from_cortado first.")
        return row[0]

    def add_alias(self, client_id, entity_id, alias_text, alias_kind, confidence="likely"):
        if not alias_text:
            return
        self.conn.execute(
            """INSERT OR IGNORE INTO entity_alias (client_id, entity_id, alias_text, alias_kind, confidence, resolved_by)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (client_id, entity_id, str(alias_text), alias_kind, confidence, "seeder:box:v0.1.0"),
        )
        self.conn.commit()

    def ensure_predicate(self, name, **kw):
        self.call("propose_predicate", name, **kw)
        return self.conn.execute("SELECT predicate_id FROM predicate WHERE name=?", (name,)).fetchone()[0]

    def upsert_edge(self, client_id, subj, pid, *, object_id=None, object_literal=None,
                    object_literal_type=None, notes=None, justification=None,
                    confidence="factual"):
        if object_id is not None:
            existing = self.conn.execute(
                "SELECT edge_id FROM edge WHERE client_id=? AND subject_id=? AND predicate_id=? AND object_id=? AND status='active'",
                (client_id, subj, pid, object_id),
            ).fetchone()
        else:
            existing = self.conn.execute(
                "SELECT edge_id FROM edge WHERE client_id=? AND subject_id=? AND predicate_id=? AND object_literal=? AND status='active'",
                (client_id, subj, pid, object_literal),
            ).fetchone()
        if existing:
            return existing[0]
        cur = self.conn.execute(
            """INSERT INTO edge (client_id, subject_id, predicate_id, object_id, object_literal, object_literal_type,
                                  notes, justification, confidence, sensitivity, status,
                                  first_observed_ts, last_corroborated_ts)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'routine', 'active', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)""",
            (client_id, subj, pid, object_id, object_literal, object_literal_type,
             notes, justification, confidence),
        )
        self.conn.commit()
        return cur.lastrowid

    def cite_box(self, edge_id, box_external_id):
        existing = self.conn.execute(
            "SELECT citation_id FROM citation WHERE cited_kind='edge' AND cited_id=? AND source_kind='manual' AND source_external_id=?",
            (edge_id, f"box:{box_external_id}"),
        ).fetchone()
        if existing:
            return
        # NB: source_kind is restricted by trigger to (meeting,document,manual,cortado,salesforce,asana,slack)
        # Box doesn't have its own kind today; using 'manual' with prefix in source_external_id.
        # If we add a 'box' source_kind later, migrate via UPDATE.
        self.conn.execute(
            """INSERT INTO citation (cited_kind, cited_id, source_kind, source_id, source_external_id, extracted_by)
               VALUES ('edge', ?, 'manual', NULL, ?, 'seeder:box:v0.1.0')""",
            (edge_id, f"box:{box_external_id}"),
        )
        self.conn.commit()

    def upsert_source_document(self, client_id, box_file_id, folder_path, modified_at=None):
        url = f"https://cortadogroup.app.box.com/file/{box_file_id}"
        existing = self.conn.execute(
            "SELECT source_document_id FROM source_document WHERE external_id=?", (str(box_file_id),)
        ).fetchone()
        if existing:
            self.conn.execute(
                "UPDATE source_document SET client_id=?, folder_path=?, modified_at=COALESCE(?, modified_at), url=? WHERE source_document_id=?",
                (client_id, folder_path, modified_at, url, existing[0]),
            )
            self.conn.commit()
            return existing[0]
        cur = self.conn.execute(
            """INSERT INTO source_document (client_id, external_id, provider, folder_path, modified_at,
                                              attribution_source, attribution_confidence, url)
               VALUES (?, ?, 'box', ?, ?, 'box_folder', 'certain', ?)""",
            (client_id, str(box_file_id), folder_path, modified_at, url),
        )
        self.conn.commit()
        return cur.lastrowid


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--client-slug", required=True)
    p.add_argument("--base-folder", required=True, action="append", default=[],
                    help='Box-side root(s) to walk — repeat the flag for additional roots, '
                         'e.g. --base-folder "All Files/Clients/HIG Growth/" '
                         '--base-folder "HIG Growth - "')
    p.add_argument("--container", default="declawed-kiselgolem",
                    help="OrbStack container with /data/box_files.db")
    p.add_argument("--db", default=os.path.join(os.path.dirname(__file__), "agent_memory.db"))
    p.add_argument("--limit-files-per-engagement", type=int, default=10000,
                    help="Cap source_document rows minted per engagement (default 10000)")
    args = p.parse_args()
    args.db = os.path.expanduser(args.db)

    if not shutil.which("docker"):
        # PATH may not include docker on user shells; check/inject
        for cand in ("/opt/homebrew/bin/docker", "/usr/local/bin/docker"):
            if os.path.exists(cand):
                os.environ["PATH"] = os.path.dirname(cand) + ":" + os.environ.get("PATH", "")
                break
        else:
            raise SystemExit("docker CLI not found; needed to access box_files.db inside container")

    db = GraphDB(args.db)
    client_id, client_name = db.get_client(args.client_slug)
    client_company_eid = db.get_client_company_entity(client_id)
    print(f"client={client_name} (id={client_id})  client_company_entity={client_company_eid}")

    # Predicates we'll use (already approved in v0.6.0 except has_box_folder which exists from earlier)
    pred = {
        n: db.ensure_predicate(n) for n in
        ("engages_cortado_for", "commissioned_by", "concerns",
         "target_of_diligence", "has_box_folder")
    }

    # Step 1: discover engagement subfolders, across ALL configured base folders
    all_subfolders = []  # list of (subfolder_name, n_files, base_folder)
    for base in args.base_folder:
        subs = docker_exec_sql(
            args.container,
            """SELECT DISTINCT
                  CASE WHEN instr(substr(full_path, ?), '/') > 0
                       THEN substr(substr(full_path, ?), 1, instr(substr(full_path, ?), '/')-1)
                       ELSE substr(full_path, ?)
                  END AS subfolder,
                  COUNT(*) AS n_files
               FROM files WHERE full_path LIKE ?
               GROUP BY subfolder ORDER BY n_files DESC""",
            len(base) + 1, len(base) + 1, len(base) + 1,
            len(base) + 1, base + "%",
        )
        for s, n in subs:
            all_subfolders.append((s, n, base))
    print(f"\nDiscovered {len(all_subfolders)} candidate subfolders across {len(args.base_folder)} base path(s)")
    subfolders = [(s, n) for s, n, _ in all_subfolders]
    sub_to_base = {s: b for s, _, b in all_subfolders}

    n_projects = n_targets = n_files = n_skipped = 0
    for subfolder, file_count in subfolders:
        if subfolder == "" or subfolder.endswith(".boxnote") or subfolder.endswith(".pptx"):
            n_skipped += 1
            continue
        m = ENGAGEMENT_RE.search(subfolder)
        if not m:
            print(f"  SKIP (no engagement match): {subfolder!r}  ({file_count} files)")
            n_skipped += 1
            continue

        addendum = m.group("letter").upper()
        codename = m.group("codename")
        target_real = m.group("target")
        engagement_full = subfolder
        full_box_path = sub_to_base[subfolder] + engagement_full

        # Mint project entity (canonical name = full Box folder name)
        proj_canonical = engagement_full
        proj = db.call("resolve_or_create_entity", client_id, proj_canonical,
                        entity_type="project", canonical_name=proj_canonical,
                        alias_kind="name", min_alias_confidence="certain",
                        resolved_by="seeder:box:v0.1.0")
        proj_eid = proj["entity_id"]
        if proj.get("created"):
            n_projects += 1
        # Codename alias on the project too
        if codename:
            db.add_alias(client_id, proj_eid, f"Project {codename}", "codename", "likely")
            db.add_alias(client_id, proj_eid, f"Addendum {addendum}", "codename", "likely")

        # Mint target entity
        target_canonical = target_real or codename
        target = db.call("resolve_or_create_entity", client_id, target_canonical,
                          entity_type="company", canonical_name=target_canonical,
                          alias_kind="name", min_alias_confidence="likely",
                          resolved_by="seeder:box:v0.1.0")
        target_eid = target["entity_id"]
        if target.get("created"):
            n_targets += 1
        if codename and target_real and codename != target_real:
            db.add_alias(client_id, target_eid, codename, "codename", "likely")
            db.add_alias(client_id, target_eid, f"Project {codename}", "codename", "likely")

        # Edges
        e1 = db.upsert_edge(client_id, client_company_eid, pred["engages_cortado_for"],
                             object_id=proj_eid, confidence="factual",
                             notes=f"Inferred from Box folder structure under {args.base_folder!r}")
        db.cite_box(e1, full_box_path)
        e2 = db.upsert_edge(client_id, proj_eid, pred["commissioned_by"],
                             object_id=client_company_eid, confidence="factual")
        db.cite_box(e2, full_box_path)
        e3 = db.upsert_edge(client_id, proj_eid, pred["concerns"],
                             object_id=target_eid, confidence="factual",
                             notes="Target inferred from engagement folder name")
        db.cite_box(e3, full_box_path)
        e4 = db.upsert_edge(client_id, proj_eid, pred["target_of_diligence"],
                             object_id=target_eid, confidence="factual")
        db.cite_box(e4, full_box_path)
        e5 = db.upsert_edge(client_id, proj_eid, pred["has_box_folder"],
                             object_literal=full_box_path, object_literal_type="string",
                             confidence="factual")
        db.cite_box(e5, full_box_path)

        # Step 2: source_document rows for files in this engagement folder
        files = docker_exec_sql(
            args.container,
            """SELECT box_file_id, full_path FROM files
                WHERE full_path LIKE ? LIMIT ?""",
            full_box_path + "%", args.limit_files_per_engagement,
        )
        for box_file_id, full_path in files:
            db.upsert_source_document(client_id, box_file_id, full_path)
            n_files += 1

        print(f"  ENGAGEMENT  Addendum {addendum}  codename={codename!r}  target={target_canonical!r}  "
              f"files={file_count}  proj_eid={proj_eid}  target_eid={target_eid}")

    print()
    print(f"== seed_from_box summary ==")
    print(f"  new project entities:    {n_projects}")
    print(f"  new target entities:     {n_targets}")
    print(f"  source_document rows:    {n_files}")
    print(f"  subfolders skipped:      {n_skipped}")


if __name__ == "__main__":
    main()
