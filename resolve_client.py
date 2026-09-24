"""Resolve a meeting's real Cortado account to a client row, instead of
bucketing everything under one shared test client (what the batch50 validation
run did for convenience).

Per-meeting flow:
  1. meeting["account"] (an account guid) is present -> find-or-create a
     `client` row keyed on cortado_client_id = account_guid, fetching the real
     account name from Cortado the first time a given account is seen.
  2. meeting["account"] is missing/null -> route to a single, explicit,
     clearly-labeled "unassigned" client bucket. Never guess an account and
     never merge unattributed content into a real client's entity graph --
     matches the design doc's requirement (R21) to preserve a missing client
     association as unknown rather than inventing one.
  3. The account guid resolves but the live account-detail fetch itself fails
     (404, network error) -> also held as unresolved, NOT silently folded into
     "unassigned" -- that would misrepresent a resolvable-but-failed lookup as
     genuinely unknown. Logged with the real reason so it can be retried.

Reuses Cortado (the account/project API wrapper) and GraphDB.upsert_client
from seed_from_cortado.py rather than reimplementing the API client -- but
does NOT use seed_from_cortado.py's upsert_client() directly: that function
accepts cortado_account_guid as a parameter but its INSERT statement never
actually writes it to the cortado_client_id column, so a second call for the
same account would never find the first client row and would mint a
duplicate. Fixed here rather than silently relied upon.
"""

from __future__ import annotations

import glob
import json
import re
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path
from typing import NamedTuple

sys.path.insert(0, str(Path(__file__).parent))
from seed_from_cortado import Cortado  # noqa: E402  (reuse the proven API wrapper)

UNASSIGNED_SLUG = "unassigned-no-account"  # kept for logging/back-compat; no longer used as an actual client slug -- see _ensure_unassigned_client
UNASSIGNED_NAME = "Unassigned (no account on meeting -- not a real client, holding bucket only)"
CROSS_PORTFOLIO_SLUG = "cross-portfolio-pe-relationship"
CROSS_PORTFOLIO_NAME = (
    "Cross-portfolio PE/investor relationship (holding bucket only -- a PE or "
    "investor firm is the parent of multiple portfolio companies, and a single "
    "conversation with them can legitimately touch several at once; this is NOT "
    "the same as 'unknown', and must never be auto-attributed to any one "
    "portfolio company just because it happened to have the most meetings)."
)
INTERNAL_DOMAIN = "cortadogroup.com"


class ResolutionResult(NamedTuple):
    status: str  # "resolved" | "resolved_via_domain" | "resolved_via_title" |
    # "cross_portfolio" | "unassigned" | "account_fetch_failed"
    client_slug: str | None
    client_id: int | None
    reason: str


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "account"


def _ensure_holding_client(conn: sqlite3.Connection, slug: str, name: str) -> int:
    row = conn.execute("SELECT client_id FROM client WHERE slug=?", (slug,)).fetchone()
    if row:
        return row[0]
    cur = conn.execute(
        "INSERT INTO client (slug, name, status) VALUES (?, ?, 'holding')", (slug, name),
    )
    conn.commit()
    return cur.lastrowid


def _ensure_unassigned_client(conn: sqlite3.Connection, meeting_guid: str | None = None) -> tuple[int, str]:
    """One isolated holding client PER unassigned meeting, not one shared
    bucket for all of them.

    Entities are deduped within a client_id's scope -- pooling every
    genuinely-unrelated "no signal at all" meeting into one shared
    unassigned-no-account client repeats the exact mistake the original
    fake shared 'batch-test' client made (11+ different real companies'
    entire casts piling into one entity pool), just smaller-scale and
    recurring. Confirmed on real data: 22 meetings shared one client_id and
    had accumulated 394 pooled entities, inflating every subsequent Codex
    call's context (and cost/latency) for every meeting landing there,
    while also risking two unrelated people (e.g. two different "David"s
    across two unrelated internal meetings) being silently merged into one
    entity.

    Falls back to the shared UNASSIGNED_SLUG bucket only if no meeting_guid
    is available at all (defensive; every real caller has one)."""
    if not meeting_guid:
        return _ensure_holding_client(conn, UNASSIGNED_SLUG, UNASSIGNED_NAME), UNASSIGNED_SLUG
    slug = f"unassigned-{meeting_guid[:8]}"
    name = f"Unassigned (meeting {meeting_guid[:8]}, no account/domain/title signal -- single-meeting holding bucket, not a real client)"
    return _ensure_holding_client(conn, slug, name), slug


def _ensure_cross_portfolio_client(conn: sqlite3.Connection) -> int:
    return _ensure_holding_client(conn, CROSS_PORTFOLIO_SLUG, CROSS_PORTFOLIO_NAME)


def _find_client_by_account_guid(conn: sqlite3.Connection, account_guid: str) -> tuple[int, str] | None:
    row = conn.execute(
        "SELECT client_id, slug FROM client WHERE cortado_client_id=?", (account_guid,)
    ).fetchone()
    return (row[0], row[1]) if row else None


def _unique_slug(conn: sqlite3.Connection, base_slug: str, account_guid: str) -> str:
    """base_slug might already be taken by a DIFFERENT account (two accounts
    with the same/similar name) -- disambiguate with a short guid suffix
    rather than silently colliding onto the wrong client."""
    row = conn.execute("SELECT cortado_client_id FROM client WHERE slug=?", (base_slug,)).fetchone()
    if row is None or row[0] == account_guid:
        return base_slug
    return f"{base_slug}-{account_guid[:8]}"


def resolve_client_for_meeting(cortado: Cortado, conn: sqlite3.Connection, meeting: dict) -> ResolutionResult:
    account_guid = meeting.get("account")
    if not account_guid:
        client_id, slug = _ensure_unassigned_client(conn, meeting.get("guid"))
        return ResolutionResult("unassigned", slug, client_id, "meeting has no account field")

    existing = _find_client_by_account_guid(conn, account_guid)
    if existing:
        client_id, slug = existing
        return ResolutionResult("resolved", slug, client_id, "existing client row")

    try:
        account = cortado.get_account(account_guid)
    except SystemExit as exc:
        return ResolutionResult("account_fetch_failed", None, None, str(exc)[:200])

    name = (account.get("name") or account.get("account_name") or account_guid).strip()
    slug = _unique_slug(conn, _slugify(name), account_guid)
    cur = conn.execute(
        "INSERT INTO client (slug, name, cortado_client_id) VALUES (?, ?, ?)",
        (slug, name, account_guid),
    )
    conn.commit()
    return ResolutionResult("resolved", slug, cur.lastrowid, f"created new client for account {account_guid}")


# ---------------------------------------------------------------------------
# Fallback tiers for meetings with no direct account link on the meeting
# record itself. Both tiers are lower-confidence than a direct account guid
# and are recorded as such (attribution_confidence='likely'/'guessed', using
# the schema's existing lookup_identity_confidence vocabulary) rather than
# silently treated the same as a certain resolution. Neither tier can ever
# MINT a brand-new client from a guess -- they only route a meeting to a
# client we already have concrete evidence exists (a real account-guid link
# elsewhere, or a domain seen on an already-linked meeting). If nothing
# clears the bar, the meeting still falls through to the unassigned bucket,
# same as before -- ambiguous evidence is treated as no evidence.
# ---------------------------------------------------------------------------

class DomainKeywordIndex(NamedTuple):
    domain_to_account: dict[str, str]              # unambiguous domains only
    domain_to_multi_accounts: dict[str, set[str]]  # PE/investor-style domains: >1 real account
    account_to_client: dict[str, tuple[int, str]]  # account_guid -> (client_id, slug)
    client_names: list[tuple[int, str, str]]        # (client_id, slug, name) for known clients


def build_domain_keyword_index(conn: sqlite3.Connection, transcript_glob: str) -> DomainKeywordIndex:
    """Bootstraps a domain -> account map from locally cached transcripts that
    already carry a real account link (this is a pragmatic point-in-time
    build from what's on disk, not a live query -- rebuild periodically as
    more meetings get resolved for real, rather than treating this as a
    permanent source of truth).

    A domain that maps to more than one real account is NOT treated as "no
    signal" -- that conflates two genuinely different situations. Most
    multi-account domains are PE/investor firms who are the parent of several
    portfolio companies; a single call with them can legitimately discuss more
    than one portfolio company at once. That's a real, distinct case (see
    domain_to_multi_accounts / CROSS_PORTFOLIO_SLUG below), not an error to
    silently exclude and fall back to "unknown" the same way as a domain with
    truly no evidence at all.
    """
    domain_votes: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for path in glob.glob(transcript_glob):
        try:
            meeting = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        account_guid = meeting.get("account")
        if not account_guid:
            continue
        for participant in meeting.get("participants") or []:
            email = str(participant.get("email") or "").strip().lower()
            if "@" not in email or email.endswith("@" + INTERNAL_DOMAIN):
                continue
            domain_votes[email.rsplit("@", 1)[1]][account_guid] += 1

    domain_to_account = {
        domain: next(iter(accounts)) for domain, accounts in domain_votes.items() if len(accounts) == 1
    }
    domain_to_multi_accounts = {
        domain: set(accounts) for domain, accounts in domain_votes.items() if len(accounts) > 1
    }

    account_to_client: dict[str, tuple[int, str]] = {}
    client_names: list[tuple[int, str, str]] = []
    for client_id, slug, name, cortado_client_id in conn.execute(
        "SELECT client_id, slug, name, cortado_client_id FROM client WHERE cortado_client_id IS NOT NULL"
    ).fetchall():
        account_to_client[cortado_client_id] = (client_id, slug)
        client_names.append((client_id, slug, name))

    return DomainKeywordIndex(domain_to_account, domain_to_multi_accounts, account_to_client, client_names)


_STOPWORDS = {
    "meeting", "call", "review", "weekly", "sync", "check", "update", "catch",
    "up", "with", "and", "the", "minute", "minutes", "intro", "monthly", "re",
    # Generic corporate-suffix words: too weak a signal alone since they're
    # common across many unrelated company names and everyday titles.
    # Confirmed false positives on real data even after the word-boundary
    # fix: "Quick Connect" matched "Amusement Connect" via "connect";
    # "Private Company ICP" matched "Frontenac Company" via "company";
    # "Jennings Executive Search <> Cortado Group" matched "Reservoir
    # Communications Group" via "group". Safe to exclude broadly: every
    # affected client name still has a distinctive token left (e.g.
    # "Aventiv Technologies" still matches via "aventiv" alone).
    "group", "company", "connect", "communications", "partners", "solutions",
    "holdings", "enterprises", "associates", "consulting", "systems",
    "technologies", "technology", "capital", "advisors", "international",
    "global", "services",
    # Generic assessment/engagement terminology, not corporate suffixes but
    # the same failure mode: common enough as standalone business jargon to
    # be a weak signal even when it happens to also be part of one client's
    # own name. Confirmed false positive: "Sharon <> George | Pricing
    # Diagnostic" matched Diagnostic Imaging Centers of Texas (DICOT) via
    # "diagnostic" -- DICOT still resolves fine via its other distinctive
    # tokens (dicot, imaging, centers, texas).
    "diagnostic", "assessment", "engagement", "planning", "strategy",
    "project", "program", "initiative",
    # Two more confirmed false positives on real data: "internal" (matches
    # ANY internal-meeting title against a client literally named
    # "Internal" -- about as generic a word as exists) and "leadership"
    # (matched "Territory Plan...sales leadership" against Center for
    # Creative Leadership via ordinary business usage of the word, nothing
    # to do with the client). Both clients lose title-keyword resolution
    # entirely as a result -- correct: neither had a more distinctive token
    # to fall back on, and no resolution beats a wrong one.
    "internal", "leadership", "growth",
    # "development" matched "Sierra Development" via the generic business
    # phrase "Report Development"; "point" matched "Wind Point Partners"
    # via "Point of Contact", an extremely common phrase. Both clients keep
    # a distinctive token (sierra, wind) so this only removes the weak
    # signal, not all resolution.
    "development", "point",
    # "health" matched "Cox Health" (an unrelated real company) against
    # "Mobile Health Consumer" -- also revealed that "HHIT"/"Harmony
    # Healthcare IT" is a real, separate Cortado platform account, not the
    # same company as Mobile Health Consumer at all. Mobile Health Consumer
    # keeps "mobile"/"consumer" as distinctive tokens.
    "health",
}


def _resolve_via_domain(index: DomainKeywordIndex, meeting: dict) -> tuple[int, str] | None:
    candidates = set()
    for participant in meeting.get("participants") or []:
        email = str(participant.get("email") or "").strip().lower()
        if "@" not in email or email.endswith("@" + INTERNAL_DOMAIN):
            continue
        account_guid = index.domain_to_account.get(email.rsplit("@", 1)[1])
        if account_guid and account_guid in index.account_to_client:
            candidates.add(index.account_to_client[account_guid])
    return next(iter(candidates)) if len(candidates) == 1 else None


def _cross_portfolio_candidates(index: DomainKeywordIndex, meeting: dict) -> set[str] | None:
    """Returns the set of candidate account guids if any attendee domain is a
    known multi-account (PE/investor-parent) domain, else None. Distinguished
    from _resolve_via_domain returning None (which means no domain signal at
    all) -- this means real signal, just spanning more than one company."""
    candidates: set[str] = set()
    for participant in meeting.get("participants") or []:
        email = str(participant.get("email") or "").strip().lower()
        if "@" not in email or email.endswith("@" + INTERNAL_DOMAIN):
            continue
        multi = index.domain_to_multi_accounts.get(email.rsplit("@", 1)[1])
        if multi:
            candidates |= multi
    return candidates or None


# Cortado's own client row (created because Cortado has an account entry in
# its own platform, e.g. for internal-business meetings). Excluded from
# title-keyword candidacy: "Cortado" is the company's own name, and shows up
# in a huge share of meeting titles as pure boilerplate (Cortado's own
# naming convention is "{Client} | Cortado // {Type}") -- not as a signal
# that the meeting is actually about Cortado itself. Left in, its token
# ("cortado") collided with the real, unambiguous single match on almost
# every title following that convention, creating false ambiguity and
# silently dropping the meeting to unassigned even when e.g. "Aventiv" alone
# was a clean, correct answer. Confirmed on real data: ~114 cached
# transcripts follow this naming pattern.
SELF_CLIENT_SLUG = "cortado-group"


def _resolve_via_title_keyword(index: DomainKeywordIndex, meeting: dict) -> tuple[int, str] | None:
    title = (meeting.get("name") or "").lower()
    if not title:
        return None
    title_words = set(re.split(r"[^a-z0-9]+", title))
    candidates = set()
    for client_id, slug, name in index.client_names:
        if slug == SELF_CLIENT_SLUG:
            continue
        tokens = [t for t in re.split(r"[^a-z0-9]+", name.lower()) if len(t) >= 4 and t not in _STOPWORDS]
        # Whole-word match against the title's own tokens, not substring
        # containment -- `in` matched "point" (a token of "Wind Point
        # Partners") inside "touchpoint", a completely unrelated word.
        # Confirmed on real data: "Cortado initial assessment touchpoint"
        # false-matched Wind Point Partners this way.
        if tokens and any(t in title_words for t in tokens):
            candidates.add((client_id, slug))
    return next(iter(candidates)) if len(candidates) == 1 else None


def resolve_client_for_meeting_with_fallback(
    cortado: Cortado, conn: sqlite3.Connection, meeting: dict, index: DomainKeywordIndex
) -> ResolutionResult:
    """Same as resolve_client_for_meeting, but when the meeting has no direct
    account link, tries: unambiguous domain match (likely) -> title-keyword
    match (guessed) -> cross-portfolio PE/investor domain (real signal, but
    spans multiple companies -- routed to its own holding bucket, never
    force-attributed to one of them) -> generic unassigned (no signal at all).
    """
    if meeting.get("account"):
        return resolve_client_for_meeting(cortado, conn, meeting)

    via_domain = _resolve_via_domain(index, meeting)
    if via_domain:
        client_id, slug = via_domain
        return ResolutionResult("resolved_via_domain", slug, client_id,
                                 "unambiguous attendee-domain match (confidence=likely)")

    via_title = _resolve_via_title_keyword(index, meeting)
    if via_title:
        client_id, slug = via_title
        return ResolutionResult("resolved_via_title", slug, client_id,
                                 "unambiguous meeting-title keyword match (confidence=guessed)")

    cross_portfolio = _cross_portfolio_candidates(index, meeting)
    if cross_portfolio:
        client_id = _ensure_cross_portfolio_client(conn)
        candidate_names = sorted(
            index.account_to_client.get(g, (None, g))[1] for g in cross_portfolio
        )
        return ResolutionResult(
            "cross_portfolio", CROSS_PORTFOLIO_SLUG, client_id,
            f"attendee domain is shared by multiple portfolio companies -- candidates for human "
            f"review, none auto-attributed: {', '.join(candidate_names)}",
        )

    client_id, slug = _ensure_unassigned_client(conn, meeting.get("guid"))
    return ResolutionResult("unassigned", slug, client_id,
                             "no account field, no domain match, no title match")
