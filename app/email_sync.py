"""Match sent-mail recipients to Queue companies so `email_track` can hold them for 90 days.

Pure logic (no network): scripts/sync_email_sends.py feeds it the sent-mail headers it fetched and
the Queue rows it read, and gets back the cells to write plus everything that needs a human look.

Only the recipient's DOMAIN is used to match, and it must equal the Queue row's website domain (or be
a subdomain of it): a name match is never enough, and shared mailbox providers (gmail.com ...) never
match anything. Message bodies are never read.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from email.utils import getaddresses

# A recipient at one of these says nothing about which company was emailed.
GENERIC_DOMAINS = frozenset({
    "gmail.com", "googlemail.com", "yahoo.com", "ymail.com", "outlook.com", "hotmail.com", "live.com", "msn.com",
    "aol.com", "icloud.com", "me.com", "mac.com", "comcast.net", "att.net", "sbcglobal.net", "verizon.net",
    "bellsouth.net", "cox.net", "charter.net", "earthlink.net", "protonmail.com", "proton.me", "mail.com",
    "gmx.com", "zoho.com", "optonline.net", "frontier.com", "windstream.net", "centurylink.net", "mchsi.com",
})

_CLAY_PREFIX = "clay_import_2026"


def norm_domain(value: str) -> str:
    """"https://www.Foo-Restoration.com/path?x=1" -> "foo-restoration.com"; "" if there is no host."""
    v = (value or "").strip().lower()
    if not v:
        return ""
    v = re.sub(r"^[a-z][a-z0-9+.-]*://", "", v)
    v = v.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    v = v.split("@")[-1].split(":", 1)[0].strip(".")
    if v.startswith("www."):
        v = v[4:]
    return v if "." in v else ""


def recipient_domains(header_values: list[str], own_addresses: set[str] | None = None) -> set[str]:
    """The company domains in a message's To/Cc/Bcc headers, minus the sender's own address and mailbox providers."""
    own = {a.lower() for a in (own_addresses or set())}
    own_domains = {a.split("@")[-1] for a in own if "@" in a}
    out = set()
    for _name, addr in getaddresses([h for h in header_values if h]):
        addr = addr.strip().lower()
        if "@" not in addr or addr in own:
            continue
        d = norm_domain(addr)
        if d and d not in GENERIC_DOMAINS and d not in own_domains:
            out.add(d)
    return out


def build_index(rows: list[dict]) -> dict[str, list[int]]:
    """Queue website domain -> sheet row numbers. rows need: row, domain."""
    idx: dict[str, list[int]] = {}
    for r in rows:
        d = norm_domain(str(r.get("domain", "")))
        if d:
            idx.setdefault(d, []).append(int(r["row"]))
    return idx


def match_rows(domain: str, index: dict[str, list[int]]) -> list[int]:
    """Rows whose website domain equals `domain`, or that `domain` is a subdomain of (mail.foo.com -> foo.com)."""
    parts = domain.split(".")
    for i in range(0, max(len(parts) - 1, 1)):
        cand = ".".join(parts[i:])
        if "." not in cand:
            break
        if cand in index:
            return index[cand]
    return []


def _iso(value) -> date | None:
    try:
        return date.fromisoformat(str(value).strip()[:10])
    except ValueError:
        return None


@dataclass
class Plan:
    writes: list[tuple[int, str]] = field(default_factory=list)          # (sheet row, YYYY-MM-DD)
    already_ok: int = 0                                                   # matched, existing date is the same or newer
    kept_text: list[int] = field(default_factory=list)                   # existing non-date text in email_track: left alone
    clay_held_back: list[tuple[int, str]] = field(default_factory=list)  # matched a Clay-table row: needs --include-clay
    unmatched: dict[str, tuple[int, str]] = field(default_factory=dict)  # domain -> (sends, last date)
    multi_row_domains: dict[str, list[int]] = field(default_factory=dict)
    matched_domains: int = 0
    sends_used: int = 0


def plan(sends: list[dict], rows: list[dict], include_clay: bool = False) -> Plan:
    """sends: [{"date": "YYYY-MM-DD", "domains": {...}}]; rows: [{"row", "domain", "source", "email_track"}]."""
    p = Plan()
    index = build_index(rows)
    by_row = {int(r["row"]): r for r in rows}
    latest: dict[str, str] = {}
    counts: dict[str, int] = {}
    for s in sends:
        if not s.get("domains"):
            continue
        p.sends_used += 1
        for d in s["domains"]:
            counts[d] = counts.get(d, 0) + 1
            if d not in latest or s["date"] > latest[d]:
                latest[d] = s["date"]
    wanted: dict[int, str] = {}
    for d, when in latest.items():
        hit = match_rows(d, index)
        if not hit:
            p.unmatched[d] = (counts[d], when)
            continue
        p.matched_domains += 1
        if len(hit) > 1:
            p.multi_row_domains[d] = hit
        for rn in hit:
            if wanted.get(rn, "") < when:
                wanted[rn] = when
    for rn, when in sorted(wanted.items()):
        r = by_row[rn]
        if str(r.get("source", "")).startswith(_CLAY_PREFIX) and not include_clay:
            p.clay_held_back.append((rn, when))
            continue
        cur = str(r.get("email_track", "")).strip()
        if cur and _iso(cur) is None:
            p.kept_text.append(rn)                       # "sent" / a quarter tag: never overwrite a human's note
            continue
        if cur and _iso(cur) >= date.fromisoformat(when):
            p.already_ok += 1
            continue
        p.writes.append((rn, when))
    return p
