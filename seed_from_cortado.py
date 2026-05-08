"""
seed_from_cortado.py - Seed a client tenant's knowledge graph from a cortado account.

Walks a cortado account's contacts + projects + project team + stakeholders and writes
entities + edges + cortado-cited provenance into agent_memory.db. PII (email, phone)
is NEVER persisted; system identifiers (sfid, cortado guid, box folder id, asana url)
are persisted as entity_alias rows.

Idempotent: re-running same args reconciles to the same graph state.

Usage:
    seed_from_cortado.py \\
        --client-slug hig_growth_partners \\
        --client-name "H.I.G. Growth Partners, LLC" \\
        --account-guid c3955a7b-afee-4643-9ae9-38b95a6a7423 \\
        --project-guid 5732befb-9963-40e1-be93-a175cb88f827 \\
        --cortado-skill-dir ~/.clawdbot/skills/cortado-api \\
        --db ~/work/agent_memory/agent_memory.db

Pass --project-guid multiple times to seed multiple projects.
"""

import argparse
import importlib.util
import json
import os
import sqlite3
import sys
from pathlib import Path


# ---------------------------------------------------------------------------
# cortado API client — imports cortado_manager.py and uses its helpers directly
# (skips the CLI text-output path; goes straight to JSON via the API).
# ---------------------------------------------------------------------------

class Cortado:
    def __init__(self, skill_dir):
        self.skill_dir = Path(os.path.expanduser(skill_dir)).resolve()
        script = self.skill_dir / "scripts" / "cortado_manager.py"
        if not script.exists():
            raise SystemExit(f"cortado_manager.py not found at {script}")

        # cortado_manager.py expects credentials/ relative to cwd. Chdir before import.
        old_cwd = os.getcwd()
        os.chdir(self.skill_dir)
        try:
            spec = importlib.util.spec_from_file_location("cortado_manager", str(script))
            mod = importlib.util.module_from_spec(spec)
            # Defer module-level argparse if any: cortado_manager only argparses in __main__.
            spec.loader.exec_module(mod)
            self._mod = mod
            self.headers, self.auth = mod.get_auth()
            self.BASE = mod.BASE_URL
        finally:
            os.chdir(old_cwd)

    def _get(self, path):
        r = self._mod.api_request('GET', f"{self.BASE}{path}", self.headers, self.auth)
        if r.status_code != 200:
            raise SystemExit(f"cortado GET {path} -> {r.status_code}: {r.text[:200]}")
        return r.json()

    def _list(self, path, key='results'):
        return self._mod.fetch_all(f"{self.BASE}{path}", self.headers, self.auth, key=key)

    def get_account(self, guid):
        return self._get(f"/accounts/{guid}/")

    def contacts_for_account(self, account_guid):
        return [c for c in self._list("/contacts/") if c.get("account_guid") == account_guid]

    def get_project(self, guid):
        return self._get(f"/projects/{guid}/")

    def project_team(self, guid):
        return self._list(f"/projects/{guid}/team/")

    def project_stakeholders(self, guid):
        return self._list(f"/projects/{guid}/stakeholders/")


# ---------------------------------------------------------------------------
# DB helpers (load registered scripts; minimal extra logic here)
# ---------------------------------------------------------------------------

class GraphDB:
    def __init__(self, db_path):
        self.conn = sqlite3.connect(db_path)
        self.conn.execute("PRAGMA foreign_keys = ON")
        self._scripts = {}
        for name in ("resolve_or_create_entity", "propose_predicate", "attribute_fact", "cite"):
            self._scripts[name] = self._load_script(name)

    def _load_script(self, name):
        row = self.conn.execute(
            "SELECT script_body FROM z_script_catalog WHERE script_name=? AND is_active=1",
            (name,),
        ).fetchone()
        if not row:
            raise SystemExit(f"Script '{name}' not found in z_script_catalog. Run migrations first.")
        ns = {}
        exec(row[0], ns)
        return ns[name]

    def call(self, name, *args, **kwargs):
        return self._scripts[name](self.conn, *args, **kwargs)

    # ----- client -----
    def upsert_client(self, slug, name, sfid=None, cortado_account_guid=None):
        cur = self.conn.execute(
            "INSERT INTO client (slug, name) VALUES (?, ?) ON CONFLICT(slug) DO UPDATE SET name=excluded.name",
            (slug, name),
        )
        client_id = self.conn.execute("SELECT client_id FROM client WHERE slug=?", (slug,)).fetchone()[0]
        self.conn.commit()
        return client_id

    # ----- alias -----
    def add_alias(self, client_id, entity_id, alias_text, alias_kind, confidence="certain", resolved_by="seeder:cortado:v0.3.0"):
        if not alias_text:
            return
        self.conn.execute(
            """INSERT OR IGNORE INTO entity_alias (client_id, entity_id, alias_text, alias_kind, confidence, resolved_by)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (client_id, entity_id, str(alias_text), alias_kind, confidence, resolved_by),
        )
        self.conn.commit()

    # ----- predicate -----
    def ensure_predicate(self, name, description=None, subject_types=None, object_types=None, inverse_name=None):
        self.call("propose_predicate", name, description=description,
                  subject_types=subject_types, object_types=object_types,
                  inverse_name=inverse_name)
        row = self.conn.execute("SELECT predicate_id FROM predicate WHERE name=?", (name,)).fetchone()
        return row[0]

    # ----- edge -----
    def upsert_edge(self, client_id, subject_id, predicate_id, object_id=None,
                     object_literal=None, object_literal_type=None,
                     notes=None, justification=None,
                     confidence="factual", sensitivity="routine"):
        """Idempotent: if an active edge with the same (client, subject, predicate, object[_literal]) exists, return its id."""
        if object_id is not None:
            existing = self.conn.execute(
                """SELECT edge_id FROM edge WHERE client_id=? AND subject_id=? AND predicate_id=? AND object_id=? AND status='active'""",
                (client_id, subject_id, predicate_id, object_id),
            ).fetchone()
        else:
            existing = self.conn.execute(
                """SELECT edge_id FROM edge WHERE client_id=? AND subject_id=? AND predicate_id=? AND object_literal=? AND status='active'""",
                (client_id, subject_id, predicate_id, object_literal),
            ).fetchone()
        if existing:
            return existing[0]
        cur = self.conn.execute(
            """INSERT INTO edge (client_id, subject_id, predicate_id, object_id, object_literal, object_literal_type,
                                  notes, justification, confidence, sensitivity, status,
                                  first_observed_ts, last_corroborated_ts)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)""",
            (client_id, subject_id, predicate_id, object_id, object_literal, object_literal_type,
             notes, justification, confidence, sensitivity),
        )
        self.conn.commit()
        return cur.lastrowid

    def cite_external(self, cited_kind, cited_id, source_kind, source_external_id, quote=None, extracted_by="seeder:cortado:v0.3.0"):
        existing = self.conn.execute(
            """SELECT citation_id FROM citation
                WHERE cited_kind=? AND cited_id=? AND source_kind=? AND source_external_id=?""",
            (cited_kind, cited_id, source_kind, source_external_id),
        ).fetchone()
        if existing:
            return existing[0]
        cur = self.conn.execute(
            """INSERT INTO citation
                (cited_kind, cited_id, source_kind, source_id, source_external_id, source_ts, quote, extracted_by)
               VALUES (?, ?, ?, NULL, ?, NULL, ?, ?)""",
            (cited_kind, cited_id, source_kind, source_external_id, quote, extracted_by),
        )
        self.conn.commit()
        return cur.lastrowid


# ---------------------------------------------------------------------------
# Predicate seed (HIG pilot scope)
# ---------------------------------------------------------------------------

PREDICATES = [
    # org / role
    ("employed_by",        "Person works for company",                  ["person"], ["company"]),
    ("has_role",           "Person has a role/title (literal)",         ["person"], ["literal"]),
    ("reports_to",         "Person reports to person",                  ["person"], ["person"]),
    ("manages",            "Person manages person",                     ["person"], ["person"]),
    # project structure
    ("commissioned_by",    "Project is paid for by company",            ["project"], ["company"]),
    ("engages_cortado_for","Project engages Cortado Group on behalf of company", ["company"], ["project"]),
    ("concerns",           "Project concerns / acts on a target",       ["project"], ["company", "person", "project"]),
    ("target_of_diligence","Company is the target of a DD project",     ["project"], ["company"]),
    ("staffed_on",         "Person is staffed on project (Cortado side)", ["person"], ["project"]),
    ("engagement_role",    "Person plays an engagement role on project (literal: seller/partner/EM/consultant)", ["person"], ["literal"]),
    ("stakeholder_on",     "Person is a client-side stakeholder on project", ["person"], ["project"]),
    # corporate hierarchy
    ("parent_of",          "Company is parent of company",              ["company"], ["company"]),
    ("part_of",            "Entity is part of larger entity (fund -> firm)", ["company"], ["company"]),
    # links
    ("has_box_folder",     "Project has Box folder (literal: folder id or url)", ["project"], ["literal"]),
    ("has_asana_board",    "Project has Asana board (literal: url)",    ["project"], ["literal"]),
    # project schedule
    ("scheduled_start",    "Project scheduled start date (literal: date)", ["project"], ["literal"]),
    ("scheduled_end",      "Project scheduled end date (literal: date)",   ["project"], ["literal"]),
    ("project_status",     "Project status (literal: cortado status string)", ["project"], ["literal"]),
]


# ---------------------------------------------------------------------------
# Seeder
# ---------------------------------------------------------------------------

CORTADO_GROUP_SLUG_NAME = "Cortado Group"


def seed(args):
    db = GraphDB(args.db)
    cortado = Cortado(args.cortado_skill_dir)

    print(f"== Seeding tenant '{args.client_slug}' from cortado account {args.account_guid} ==")

    client_id = db.upsert_client(args.client_slug, args.client_name)
    print(f"  client_id={client_id}")

    # ---------------- predicates --------------------------------------------
    pred_ids = {}
    for name, desc, subj, obj in PREDICATES:
        pred_ids[name] = db.ensure_predicate(name, description=desc, subject_types=subj, object_types=obj)
    print(f"  predicates registered: {len(pred_ids)}")

    # ---------------- core entities -----------------------------------------
    # Cortado Group (we always need this; type=company)
    cortado_entity = db.call("resolve_or_create_entity", client_id, CORTADO_GROUP_SLUG_NAME,
                              entity_type="company", canonical_name=CORTADO_GROUP_SLUG_NAME,
                              alias_kind="name", min_alias_confidence="certain",
                              resolved_by="seeder:cortado:v0.3.0")
    cortado_eid = cortado_entity["entity_id"]
    db.add_alias(client_id, cortado_eid, "cortadogroup", "slug")

    # Client company entity
    account = cortado.get_account(args.account_guid)
    if not isinstance(account, dict):
        raise SystemExit(f"Could not parse cortado account {args.account_guid}: {account!r}")
    sfid = (account.get("external_ids") or {}).get("salesforce")
    client_company = db.call("resolve_or_create_entity", client_id, account["name"],
                              entity_type="company", canonical_name=account["name"],
                              alias_kind="name", min_alias_confidence="certain",
                              resolved_by="seeder:cortado:v0.3.0")
    client_eid = client_company["entity_id"]
    db.add_alias(client_id, client_eid, args.account_guid, "cortado_account_guid")
    if sfid:
        db.add_alias(client_id, client_eid, sfid, "sfid")
    if account.get("aliases"):
        for a in str(account["aliases"]).split(","):
            a = a.strip()
            if a:
                db.add_alias(client_id, client_eid, a, "name", confidence="likely")
    print(f"  client_company entity_id={client_eid}, sfid={sfid}")

    # ---------------- contacts (no PII; sfid + name only) -------------------
    contacts = cortado.contacts_for_account(args.account_guid)
    n_contacts = 0
    for contact in (contacts or []):
        if not isinstance(contact, dict):
            continue
        cname = contact.get("full_name") or contact.get("name")
        if not cname:
            continue
        contact_guid = contact.get("guid")
        contact_sfid = (contact.get("external_ids") or {}).get("salesforce") if isinstance(contact.get("external_ids"), dict) else None
        title = contact.get("job_title") or contact.get("title")
        person = db.call("resolve_or_create_entity", client_id, cname,
                          entity_type="person", canonical_name=cname,
                          alias_kind="name", min_alias_confidence="certain",
                          resolved_by="seeder:cortado:v0.3.0")
        person_eid = person["entity_id"]
        if contact_guid:
            db.add_alias(client_id, person_eid, contact_guid, "cortado_contact_guid")
        if contact_sfid:
            db.add_alias(client_id, person_eid, contact_sfid, "sfid")

        edge_id = db.upsert_edge(client_id, person_eid, pred_ids["employed_by"],
                                  object_id=client_eid,
                                  notes="Cortado lists this contact under the account",
                                  justification="cortado contact.account = this account",
                                  confidence="factual")
        if contact_guid:
            db.cite_external("edge", edge_id, "cortado", contact_guid)

        if title:
            edge_id = db.upsert_edge(client_id, person_eid, pred_ids["has_role"],
                                      object_literal=str(title),
                                      object_literal_type="string",
                                      notes="Title from cortado contact record (may be stale)",
                                      confidence="factual")
            if contact_guid:
                db.cite_external("edge", edge_id, "cortado", contact_guid)
        n_contacts += 1
    print(f"  contacts seeded: {n_contacts}")

    # ---------------- projects ----------------------------------------------
    for proj_guid in (args.project_guid or []):
        seed_project(db, cortado, client_id, client_eid, cortado_eid, pred_ids, proj_guid)

    print("\n== Seeding complete ==")


def seed_project(db, cortado, client_id, client_company_eid, cortado_group_eid, pred_ids, project_guid):
    print(f"\n  -- project {project_guid} --")
    proj = cortado.get_project(project_guid)
    if not isinstance(proj, dict):
        raise SystemExit(f"Could not parse project {project_guid}: {proj!r}")

    proj_name = proj.get("name") or f"project_{project_guid}"
    proj_sfid = (proj.get("external_ids") or {}).get("salesforce")
    proj_external = proj.get("external_links") or {}
    proj_box_folder = proj_external.get("box")
    proj_asana = proj_external.get("asana")

    project_entity = db.call("resolve_or_create_entity", client_id, proj_name,
                              entity_type="project", canonical_name=proj_name,
                              alias_kind="name", min_alias_confidence="certain",
                              resolved_by="seeder:cortado:v0.3.0")
    project_eid = project_entity["entity_id"]
    db.add_alias(client_id, project_eid, project_guid, "cortado_project_guid")
    if proj_sfid:
        db.add_alias(client_id, project_eid, proj_sfid, "sfid")
    if proj_box_folder:
        # store the folder id (last numeric) as box_folder_id alias too
        try:
            box_folder_id = proj_box_folder.rstrip("/").split("/")[-1]
            if box_folder_id.isdigit():
                db.add_alias(client_id, project_eid, box_folder_id, "box_folder_id")
        except Exception:
            pass
    print(f"    project entity_id={project_eid}, name='{proj_name}'")

    # commissioned_by -> client company
    eid = db.upsert_edge(client_id, project_eid, pred_ids["commissioned_by"],
                          object_id=client_company_eid,
                          notes="Cortado project.account links here",
                          confidence="factual")
    db.cite_external("edge", eid, "cortado", project_guid)

    # CDD detection: mint placeholder target company so observations have somewhere to land
    name_upper = (proj_name or "").upper()
    is_diligence = " CDD" in name_upper or "DUE DILIGENCE" in name_upper or " DD " in name_upper or name_upper.endswith(" DD")
    if is_diligence:
        # Extract a codename if there's a "Project <Codename>" pattern in the name
        import re
        m = re.search(r"PROJECT\s+([A-Z][A-Za-z0-9\-]+)", proj_name or "", flags=re.IGNORECASE)
        codename = m.group(1) if m else None
        target_canonical = f"{(codename or proj_name)} Target"
        target = db.call("resolve_or_create_entity", client_id, target_canonical,
                          entity_type="company", canonical_name=target_canonical,
                          alias_kind="codename", min_alias_confidence="guessed",
                          resolved_by="seeder:cortado:placeholder:v0.4.0")
        target_eid = target["entity_id"]
        if codename:
            db.add_alias(client_id, target_eid, codename, "codename", confidence="likely")
            db.add_alias(client_id, target_eid, f"Project {codename}", "codename", confidence="likely")
        # Mark this entity as a placeholder via descriptive aliases so future extraction can rename
        db.add_alias(client_id, target_eid, "the target", "role_descriptor", confidence="likely")
        eid_concerns = db.upsert_edge(client_id, project_eid, pred_ids["concerns"],
                                       object_id=target_eid,
                                       notes=f"Placeholder target for diligence project; canonical name unknown until extraction discovers it",
                                       justification="cortado project name signals CDD/DD; target is concerns object by definition",
                                       confidence="factual")
        db.cite_external("edge", eid_concerns, "cortado", project_guid)
        # also: target_of_diligence
        eid_tod = db.upsert_edge(client_id, project_eid, pred_ids["target_of_diligence"],
                                  object_id=target_eid,
                                  confidence="factual")
        db.cite_external("edge", eid_tod, "cortado", project_guid)
        print(f"    pre-minted target placeholder entity_id={target_eid}, codename={codename!r}")

    # company engages_cortado_for project
    eid = db.upsert_edge(client_id, client_company_eid, pred_ids["engages_cortado_for"],
                          object_id=project_eid,
                          notes="Inverse of commissioned_by; client engages Cortado for this project",
                          confidence="factual")
    db.cite_external("edge", eid, "cortado", project_guid)

    # project external links as literal edges
    if proj_box_folder:
        eid = db.upsert_edge(client_id, project_eid, pred_ids["has_box_folder"],
                              object_literal=proj_box_folder, object_literal_type="url",
                              confidence="factual")
        db.cite_external("edge", eid, "cortado", project_guid)
    if proj_asana:
        eid = db.upsert_edge(client_id, project_eid, pred_ids["has_asana_board"],
                              object_literal=proj_asana, object_literal_type="url",
                              confidence="factual")
        db.cite_external("edge", eid, "cortado", project_guid)
    if proj.get("start_date"):
        eid = db.upsert_edge(client_id, project_eid, pred_ids["scheduled_start"],
                              object_literal=proj["start_date"], object_literal_type="date",
                              confidence="factual")
        db.cite_external("edge", eid, "cortado", project_guid)
    if proj.get("end_date"):
        eid = db.upsert_edge(client_id, project_eid, pred_ids["scheduled_end"],
                              object_literal=proj["end_date"], object_literal_type="date",
                              confidence="factual")
        db.cite_external("edge", eid, "cortado", project_guid)
    status = proj.get("status_option") or proj.get("status")
    if status:
        eid = db.upsert_edge(client_id, project_eid, pred_ids["project_status"],
                              object_literal=str(status), object_literal_type="string",
                              confidence="factual")
        db.cite_external("edge", eid, "cortado", project_guid)

    # ---------------- engagement roles (seller/partner/EM) ------------------
    for role_field, role_label in [("seller", "seller"), ("partner", "partner"),
                                    ("engagement_manager", "engagement_manager")]:
        person_email = proj.get(role_field)
        person_name = proj.get(f"{role_field}_name")
        if not (person_email and person_name):
            continue
        # PII: the email is just used to dedupe staff against entities; not persisted
        person = db.call("resolve_or_create_entity", client_id, person_name,
                          entity_type="person", canonical_name=person_name,
                          alias_kind="name", min_alias_confidence="certain",
                          resolved_by="seeder:cortado:v0.3.0")
        person_eid = person["entity_id"]
        # employed_by Cortado Group
        eid = db.upsert_edge(client_id, person_eid, pred_ids["employed_by"],
                              object_id=cortado_group_eid,
                              notes=f"Inferred from cortado project {role_field} email domain",
                              confidence="factual")
        db.cite_external("edge", eid, "cortado", project_guid)
        # staffed_on this project
        eid = db.upsert_edge(client_id, person_eid, pred_ids["staffed_on"],
                              object_id=project_eid,
                              confidence="factual")
        db.cite_external("edge", eid, "cortado", project_guid)
        # engagement_role literal
        eid = db.upsert_edge(client_id, person_eid, pred_ids["engagement_role"],
                              object_literal=role_label, object_literal_type="string",
                              confidence="factual",
                              notes=f"From cortado project.{role_field}")
        db.cite_external("edge", eid, "cortado", project_guid)

    # ---------------- team --------------------------------------------------
    team = cortado.project_team(project_guid)
    if isinstance(team, list):
        for member in team:
            if not isinstance(member, dict):
                continue
            mname = member.get("person_name") or member.get("name")
            if not mname:
                continue
            person = db.call("resolve_or_create_entity", client_id, mname,
                              entity_type="person", canonical_name=mname,
                              alias_kind="name", min_alias_confidence="certain",
                              resolved_by="seeder:cortado:v0.3.0")
            person_eid = person["entity_id"]
            person_guid = member.get("person_guid")
            if person_guid:
                db.add_alias(client_id, person_eid, person_guid, "cortado_staff_guid")
            eid = db.upsert_edge(client_id, person_eid, pred_ids["employed_by"],
                                  object_id=cortado_group_eid,
                                  confidence="factual")
            db.cite_external("edge", eid, "cortado", project_guid)
            eid = db.upsert_edge(client_id, person_eid, pred_ids["staffed_on"],
                                  object_id=project_eid,
                                  confidence="factual")
            db.cite_external("edge", eid, "cortado", project_guid)
            project_role = member.get("project_role") or member.get("role")
            if project_role:
                eid = db.upsert_edge(client_id, person_eid, pred_ids["engagement_role"],
                                      object_literal=str(project_role), object_literal_type="string",
                                      confidence="factual")
                db.cite_external("edge", eid, "cortado", project_guid)

    # ---------------- stakeholders ------------------------------------------
    stakeholders = cortado.project_stakeholders(project_guid)
    if isinstance(stakeholders, list):
        for sh in stakeholders:
            if not isinstance(sh, dict):
                continue
            sname = sh.get("contact_name") or sh.get("name")
            if not sname:
                continue
            person = db.call("resolve_or_create_entity", client_id, sname,
                              entity_type="person", canonical_name=sname,
                              alias_kind="name", min_alias_confidence="certain",
                              resolved_by="seeder:cortado:v0.3.0")
            person_eid = person["entity_id"]
            contact_guid = sh.get("contact_guid")
            if contact_guid:
                db.add_alias(client_id, person_eid, contact_guid, "cortado_contact_guid")
            eid = db.upsert_edge(client_id, person_eid, pred_ids["stakeholder_on"],
                                  object_id=project_eid,
                                  confidence="factual")
            db.cite_external("edge", eid, "cortado", project_guid)
            eid = db.upsert_edge(client_id, person_eid, pred_ids["employed_by"],
                                  object_id=client_company_eid,
                                  confidence="factual",
                                  notes="Inferred: stakeholder on a project commissioned by this company")
            db.cite_external("edge", eid, "cortado", project_guid)
            inf = sh.get("influence_level")
            if inf:
                pred_id = db.ensure_predicate("stakeholder_influence",
                                               description="Stakeholder influence level on a project (literal: high|medium|low)",
                                               subject_types=["person"], object_types=["literal"])
                eid = db.upsert_edge(client_id, person_eid, pred_id,
                                      object_literal=str(inf), object_literal_type="string",
                                      confidence="factual")
                db.cite_external("edge", eid, "cortado", project_guid)
            disp = sh.get("disposition")
            if disp:
                pred_id = db.ensure_predicate("stakeholder_disposition",
                                               description="Stakeholder disposition toward project (cortado uses + / = / -)",
                                               subject_types=["person"], object_types=["literal"])
                eid = db.upsert_edge(client_id, person_eid, pred_id,
                                      object_literal=str(disp), object_literal_type="string",
                                      confidence="factual",
                                      sensitivity="sensitive")
                db.cite_external("edge", eid, "cortado", project_guid)


# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--client-slug", required=True)
    parser.add_argument("--client-name", required=True)
    parser.add_argument("--account-guid", required=True)
    parser.add_argument("--project-guid", action="append", default=[])
    parser.add_argument("--cortado-skill-dir", default="~/.clawdbot/skills/cortado-api")
    parser.add_argument("--db", default=os.path.join(os.path.dirname(__file__), "agent_memory.db"))
    args = parser.parse_args()
    args.db = os.path.expanduser(args.db)
    seed(args)


if __name__ == "__main__":
    main()
