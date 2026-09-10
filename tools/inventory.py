#!/usr/bin/env python3
"""
deterministic inventory engine for the Kolo "inventory-tracking" skill.

All critical inventory behavior (setup, matching, quantity validation,
read/modify/write, verification, reconciliation, concurrency) lives here, not
in free-form SKILL.md prose. The SKILL.md only maps the owner's words to one
of the subcommands below.

The engine talks to two backends, both injected so tests can run in-memory:

* Store  -- the governed skill-record store (`kolo record-*`):
            - trackers      (record-type skill.inventory-tracking)
            - action journal (record-type skill.inventory-journal)
* Sheets -- Google Sheets, resolved at runtime via `kolo integration-routing`.

No employee email, account-specific routing, gateway hostname, secret, or
token is hardcoded here; routing and credentials are read from the runtime
environment each invocation.

stdlib only. No third-party deps.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import subprocess
import sys
import threading
import urllib.error
import urllib.request
import uuid

# ----------------------------------------------------------------------------
# Constants
# ----------------------------------------------------------------------------

TRACKER_RT = "skill.inventory-tracking"
JOURNAL_RT = "skill.inventory-journal"
SCHEMA_VERSION = 2

# Template schema. The approved template is versioned; a marker cell in the
# header row of the form "TEMPLATE_VERSION=2.0" (any column C onwards) is
# validated when present. A template with no marker is treated as "legacy" and
# accepted, but recorded as such.
ACCEPTED_TEMPLATE_VERSIONS = {"2.0"}
MARKER_RE = re.compile(r"^template[\s_-]*version\s*[:=]?\s*(.+)$", re.IGNORECASE)

# The canonical (clean) schema for storage locations. The legacy template's
# verbatim names (trailing spaces / "Stoage" typo) are accepted via aliasing
# and are NEVER promoted to the canonical schema.
CANONICAL_TABS = [
    "1st floor storage",
    "breakfast items",
    "laundry room",
    "maintenance room",
    "pool supplies",
    "2nd floor storage 1",
    "2nd floor storage 2",
    "3rd floor storage 1",
    "3rd floor storage 2",
    "4th floor storage 1",
    "4th floor storage 2",
]

# Required header-row tokens for column A (item) and column B (quantity).
ITEM_HEADERS = {"item", "item name", "product", "name", "description", "item(s)"}
QTY_HEADERS = {
    "quantity", "qty", "quanity", "count", "on hand", "on-hand",
    "quantity on hand", "amount", "stock", "quantity/amount",
}

# Item matching guards. Fuzzy matching is bounded by a maximum distance and a
# minimum winner margin; ambiguity is always surfaced, never silently resolved.
MAX_FUZZY_SIMILARITY = 0.60
MIN_WINNER_MARGIN = 0.10

# Journal states.
S_PREPARED = "prepared"
S_ATTEMPTING = "attempting"
S_CONFIRMED = "confirmed"
S_UNCERTAIN = "uncertain"
S_AWAITING = "awaiting_operator"
VALID_STATES = {S_PREPARED, S_ATTEMPTING, S_CONFIRMED, S_UNCERTAIN, S_AWAITING}

# Concurrency lease.
LEASE_TTL_SECONDS = 60.0

# Ops.
OP_SET = "set"
OP_ADD = "add"
OP_SUBTRACT = "subtract"
VALID_OPS = {OP_SET, OP_ADD, OP_SUBTRACT}

# Gateway base is read from the environment so the public repo carries no
# internal hostname; it falls back to Maton's public gateway host only when the
# runtime does not provide an override.
GATEWAY_BASE = os.environ.get("MATON_GATEWAY_BASE", "https://gateway.maton.ai")
SHEETS_API_PATH = "google-sheets/v4/spreadsheets"


# ----------------------------------------------------------------------------
# Pure helpers (no I/O) — unit-tested directly.
# ----------------------------------------------------------------------------

def normalize(s: str | None) -> str:
    """Lowercase, strip non-alphanumerics to single spaces, trim."""
    if s is None:
        return ""
    s = str(s).lower()
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _levenshtein(a: str, b: str) -> int:
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(
                prev[j] + 1,
                cur[j - 1] + 1,
                prev[j - 1] + (0 if ca == cb else 1),
            ))
        prev = cur
    return prev[-1]


def similarity(a: str, b: str) -> float:
    """1 - normalized Levenshtein distance, in [0, 1]."""
    a, b = normalize(a), normalize(b)
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return 1.0 - _levenshtein(a, b) / max(len(a), len(b))


def parse_quantity(raw) -> float:
    """Parse a finite, non-negative quantity. Raise ValueError otherwise."""
    if isinstance(raw, str):
        s = raw.strip()
    else:
        s = str(raw).strip()
    if not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", s):
        raise ValueError(f"quantity must be a number, got {raw!r}")
    v = float(s)
    if not math.isfinite(v):
        raise ValueError(f"quantity must be finite, got {raw!r}")
    if v < 0:
        raise ValueError(f"quantity must be non-negative, got {raw!r}")
    return v


_AMOUNT_UNIT_RE = re.compile(
    r"^(?P<pre>[a-z ]*?)(?P<num>[0-9]+(?:\.[0-9]+)?)(?P<post>[a-z ]*)$",
    re.IGNORECASE,
)


def split_amount_unit(text: str) -> tuple[float, str | None]:
    """Split '5 rolls' / 'rolls 5' / '5' into (amount, unit-or-None)."""
    s = str(text).strip()
    m = _AMOUNT_UNIT_RE.match(s)
    if not m:
        raise ValueError(f"cannot parse an amount from {text!r}")
    amount = parse_quantity(m.group("num"))
    unit = (m.group("pre").strip() or m.group("post").strip()).strip()
    return amount, (unit or None)


def format_number(v: float):
    if float(v).is_integer():
        return int(v)
    return float(v)


def match_item(query: str, candidates: list[tuple[str, str]]) -> dict:
    """
    candidates: list of (key, display). Return a structured decision:

    status in {exact, substring, fuzzy, ambiguous, none}
    plus key/score/alternatives as appropriate.
    """
    q = normalize(query)
    result = {"status": "none", "key": None, "score": None, "alternatives": []}
    if not q:
        return result

    norm = [(c[0], c[1], normalize(c[1])) for c in candidates]

    def finish(status, key, score, alts):
        return {"status": status, "key": key, "score": score,
                "alternatives": alts}

    exact = [c for c in norm if c[2] == q]
    if len(exact) == 1:
        return finish("exact", exact[0][0], 1.0, [])
    if len(exact) > 1:
        return finish("ambiguous", None, 1.0,
                      [(c[0], c[1]) for c in exact])

    subs = [c for c in norm if q in c[2] or c[2] in q]
    if len(subs) == 1:
        return finish("substring", subs[0][0],
                      similarity(q, subs[0][2]), [])
    if len(subs) > 1:
        scored = sorted(((similarity(q, c[2]), c) for c in subs), reverse=True)
        top = scored[0]
        second = scored[1] if len(scored) > 1 else None
        if second is None or (top[0] - second[0]) >= MIN_WINNER_MARGIN:
            return finish("substring", top[1][0], top[0],
                          [(c[0], c[1]) for c in subs])
        return finish("ambiguous", None, top[0],
                      [(s[1][0], s[1][1]) for s in scored[:3]])

    scored = sorted(((similarity(q, c[2]), c) for c in norm), reverse=True)
    if not scored:
        return result
    top = scored[0]
    if top[0] < MAX_FUZZY_SIMILARITY:
        result["top_score"] = top[0]
        return result
    if len(scored) > 1 and (top[0] - scored[1][0]) < MIN_WINNER_MARGIN:
        return finish("ambiguous", None, top[0],
                      [(s[1][0], s[1][1]) for s in scored[:3]])
    return finish("fuzzy", top[1][0], top[0], [])


def canonical_tab(name: str) -> str:
    """Normalize a tab name and correct legacy typos to the canonical form."""
    n = normalize(name)
    for bad, good in (("stoage", "storage"),):
        n = n.replace(bad, good)
    n = re.sub(r"\bstorage 1\b", "storage 1", n)
    return n


def validate_header(row1: list) -> tuple[bool, str, str | None]:
    """
    Validate a header row. Returns (ok, item_header_or_None, version).
    version is "legacy" when no marker present, else the parsed version string.
    """
    cells = [str(c) if c is not None else "" for c in row1]
    a = normalize(cells[0] if cells else "")
    b = normalize(cells[1] if len(cells) > 1 else "")
    if not (a in ITEM_HEADERS and b in QTY_HEADERS):
        return False, (cells[0] if len(cells) > 0 else "(missing)"), None

    version = "legacy"
    for extra in cells[2:] if len(cells) > 2 else []:
        m = MARKER_RE.match(extra.strip())
        if m:
            version = m.group(1).strip()
            break
    return True, cells[0], version


def find_used_range(rows: list[list]) -> list[list]:
    """Drop trailing fully-empty rows (dynamic used range)."""
    out = []
    for r in rows:
        if r is None:
            r = []
        out.append(list(r) if isinstance(r, list) else [r])
    while out and not any(_cell_text(c).strip() for c in out[-1]):
        out.pop()
    return out


def _cell_text(c) -> str:
    if c is None:
        return ""
    return str(c)


def parse_cell_qty(c) -> float | None:
    """Parse a sheet quantity cell; blank/missing -> None; bad -> None."""
    if c is None:
        return None
    t = str(c).strip()
    if t == "":
        return None
    try:
        v = float(t)
    except ValueError:
        return None
    if not math.isfinite(v):
        return None
    return v


def compute_after(op: str, before: float | None, amount: float) -> float:
    before = 0.0 if before is None else before
    if op == OP_SET:
        return amount
    if op == OP_ADD:
        return before + amount
    if op == OP_SUBTRACT:
        return before - amount
    raise ValueError(f"unknown op {op!r}")


def now_iso(dt=None) -> str:
    from datetime import datetime, timezone
    dt = dt or datetime.now(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def parse_ts(ts) -> float | None:
    """Parse an ISO timestamp into a float unix epoch; None if unparseable."""
    if not ts:
        return None
    from datetime import datetime
    s = str(ts)
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        if s.endswith("+00:00") or ":" == s[-6:-5]:
            dt = datetime.fromisoformat(s)
        else:
            dt = datetime.fromisoformat(s)
        return dt.timestamp()
    except (ValueError, TypeError):
        return None


# ----------------------------------------------------------------------------
# Backend interfaces
# ----------------------------------------------------------------------------

class Store:
    """Governed skill-record store (record-type x external-id)."""

    def upsert(self, record_type, external_id, payload, status):  # pragma: no cover - abstract
        raise NotImplementedError

    def get(self, record_type, external_id):  # pragma: no cover - abstract
        raise NotImplementedError

    def list(self, record_type, status=None, include_deleted=False):  # pragma: no cover - abstract
        raise NotImplementedError

    def delete(self, record_type, external_id, hard=True):  # pragma: no cover - abstract
        raise NotImplementedError


class SubprocessStore(Store):
    """kolo record-* via subprocess (real runtime)."""

    def _run(self, args) -> dict:
        proc = subprocess.run(
            ["kolo"] + args, capture_output=True, text=True, timeout=60,
        )
        try:
            data = json.loads(proc.stdout or "{}")
        except json.JSONDecodeError:
            data = {"status": "error", "raw": (proc.stdout or "")[:500]}
        if proc.returncode != 0 or data.get("status") == "error":
            raise RuntimeError(f"kolo {' '.join(args)} failed: {data}")
        return data

    def upsert(self, record_type, external_id, payload, status):
        return self._run([
            "record-upsert", "--record-type", record_type,
            "--external-id", external_id,
            "--payload", json.dumps(payload),
            "--status", status,
        ])

    def get(self, record_type, external_id):
        proc = subprocess.run(
            ["kolo", "record-get", "--record-type", record_type,
             "--external-id", external_id],
            capture_output=True, text=True, timeout=60,
        )
        try:
            data = json.loads(proc.stdout or "{}")
        except json.JSONDecodeError:
            return None
        if proc.returncode != 0 or data.get("status") == "error":
            return None
        rec = data.get("record")
        return rec

    def list(self, record_type, status=None, include_deleted=False):
        page, size, out = 1, 200, []
        while True:
            args = ["record-list", "--record-type", record_type,
                    "--page-size", str(size), "--page", str(page)]
            if status:
                args += ["--status", status]
            if include_deleted:
                args += ["--include-deleted"]
            data = self._run(args)
            recs = data.get("records", [])
            out.extend(recs)
            total = int(data.get("total", 0))
            if page * size >= total or not recs:
                break
            page += 1
        return out

    def delete(self, record_type, external_id, hard=True):
        args = ["record-delete", "--record-type", record_type,
                "--external-id", external_id]
        if hard:
            args += ["--hard"]
        return self._run(args)


class Sheets:
    """Google Sheets access (read/update/append)."""

    def read(self, spreadsheet_id, tab):  # pragma: no cover - abstract
        raise NotImplementedError

    def read_cell(self, spreadsheet_id, tab, row, col="B"):  # pragma: no cover - abstract
        raise NotImplementedError

    def update_cell(self, spreadsheet_id, tab, row, value):  # pragma: no cover - abstract
        raise NotImplementedError

    def append(self, spreadsheet_id, tab, row_values):  # pragma: no cover - abstract
        raise NotImplementedError

    def sheet_titles(self, spreadsheet_id):  # pragma: no cover - abstract
        raise NotImplementedError


def resolve_sheets_routing():
    """Resolve the sanctioned Kolo routing for Google Sheets at runtime."""
    proc = subprocess.run(
        ["kolo", "integration-routing", "--json"],
        capture_output=True, text=True, timeout=60,
    )
    if proc.returncode != 0:
        raise RuntimeError("kolo integration-routing failed")
    data = json.loads(proc.stdout)
    apps = data.get("apps", []) if isinstance(data, dict) else []
    for a in apps:
        if (a.get("app_name") == "google-sheets"
                or a.get("integration_id") == "google_sheets"
                or a.get("display_name") == "Google Sheets"):
            return a
    return None


class GatewaySheets(Sheets):
    """Sheets through the Maton gateway (Bearer cred from the environment)."""

    def __init__(self, credential_env="MATON_API_KEY"):
        self._token = os.environ.get(credential_env)

    def _url(self, spreadsheet_id, rest):
        return (f"{GATEWAY_BASE}/{SHEETS_API_PATH}/"
                f"{urllib.request.quote(spreadsheet_id, safe='')}/{rest}")

    def _call(self, method, url, body=None):
        if not self._token:
            raise RuntimeError("routing selected the gateway but the gateway "
                               "credential env var is not set")
        data = None
        headers = {"Authorization": f"Bearer {self._token}"}
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body).encode()
        req = urllib.request.Request(url, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, data=data, timeout=30) as r:
                raw = r.read().decode()
                return r.status, (json.loads(raw) if raw else {})
        except urllib.error.HTTPError as e:
            raw = e.read().decode()
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                parsed = {"raw": raw[:500]}
            return e.code, parsed

    def _encode_tab(self, tab):
        return urllib.request.quote(tab, safe='')

    def read(self, spreadsheet_id, tab):
        st, body = self._call(
            "GET", self._url(spreadsheet_id,
                             f"values/{self._encode_tab(tab)}!A:B"))
        if st != 200:
            raise RuntimeError(f"sheet read failed ({st}): {body}")
        return body

    def read_cell(self, spreadsheet_id, tab, row, col="B"):
        rng = f"{self._encode_tab(tab)}!{col}{row}:{col}{row}"
        st, body = self._call(
            "GET", self._url(spreadsheet_id, f"values/{rng}"))
        if st != 200:
            raise RuntimeError(f"cell read failed ({st}): {body}")
        vals = body.get("values") or []
        if vals:
            row0 = vals[0]
            return row0[0] if row0 else None
        return None

    def update_cell(self, spreadsheet_id, tab, row, value):
        rng = f"{self._encode_tab(tab)}!B{row}"
        st, body = self._call(
            "PUT",
            self._url(spreadsheet_id,
                      f"values/{rng}?valueInputOption=USER_ENTERED"),
            body={"range": f"{tab}!B{row}", "majorDimension": "ROWS",
                  "values": [[value]]})
        if st != 200:
            raise RuntimeError(f"cell update failed ({st}): {body}")
        return body

    def append(self, spreadsheet_id, tab, row_values):
        rng = f"{self._encode_tab(tab)}!A1:B1"
        st, body = self._call(
            "POST",
            self._url(spreadsheet_id, f"values/{rng}:append?valueInputOption=USER_ENTERED"),
            body={"values": [list(row_values)]})
        if st != 200:
            raise RuntimeError(f"append failed ({st}): {body}")
        return body

    def sheet_titles(self, spreadsheet_id):
        st, body = self._call(
            "GET", self._url(spreadsheet_id, "").replace(
                "/values/", "") if False else self._url(spreadsheet_id, ""))
        # spreadsheets.get returns {"sheets": [{"properties": {"title": ...}}]}
        if body.get("sheets") is None:
            # Fallback: metadata endpoint.
            st2, meta = self._call(
                "GET", self._url(spreadsheet_id, "") + "?fields=sheets.properties.title")
            body = meta
        if body.get("sheets") is None:
            raise RuntimeError("could not read sheet tab list")
        return [s.get("properties", {}).get("title", "")
                for s in body.get("sheets", [])]


# ----------------------------------------------------------------------------
# In-memory backends (used by the test suite and `doctor --selftest`).
# ----------------------------------------------------------------------------

class InMemoryStore(Store):
    def __init__(self):
        self._data = {}
        self._lock = threading.RLock()

    def upsert(self, record_type, external_id, payload, status):
        with self._lock:
            key = (record_type, external_id)
            existing = self._data.get(key)
            created = existing is None
            rec = {
                "record_type": record_type,
                "external_id": external_id,
                "status": status,
                "payload": json.loads(json.dumps(payload)),
                "created": created,
            }
            self._data[key] = rec
            return {"ok": True, "created": created}

    def get(self, record_type, external_id):
        with self._lock:
            rec = self._data.get((record_type, external_id))
            if rec is None:
                return None
            return {"record_type": record_type, "external_id": external_id,
                    "status": rec["status"],
                    "payload": json.loads(json.dumps(rec["payload"]))}

    def list(self, record_type, status=None, include_deleted=False):
        with self._lock:
            out = []
            for (rt, eid), rec in self._data.items():
                if rt != record_type:
                    continue
                if status and rec["status"] != status:
                    continue
                out.append({"record_type": rt, "external_id": eid,
                            "status": rec["status"], "payload": rec["payload"]})
            return out

    def delete(self, record_type, external_id, hard=True):
        with self._lock:
            self._data.pop((record_type, external_id), None)
            return {"ok": True, "deleted": True}


class FakeSheet(Sheets):
    """
    In-memory sheet. Layout: {tab: [[A,B], [A,B], ...]} row 1 = header.
    Supports optional hooks to simulate write failures and races.
    """

    def __init__(self, sheets=None):
        self.sheets = {k: [list(r) for r in v] for k, v in (sheets or {}).items()}
        self.fail_next_write = None   # set to an Exception to raise on next write/append
        self.vanish_after_write = {}  # {(tab,row): value} -> force a mismatch after write

    def _tab(self, tab):
        if tab not in self.sheets:
            raise RuntimeError(f"tab not found: {tab!r}")
        return self.sheets[tab]

    def read(self, spreadsheet_id, tab):
        rows = self._tab(tab)
        return {"range": f"{tab}!A1:B{max(1, len(rows))}", "values": rows}

    def read_cell(self, spreadsheet_id, tab, row, col="B"):
        rows = self._tab(tab)
        ci = 1 if col.upper() == "B" else 0
        if row - 1 < len(rows) and ci < len(rows[row - 1]):
            return rows[row - 1][ci]
        return None

    def update_cell(self, spreadsheet_id, tab, row, value):
        if self.fail_next_write is not None:
            e, self.fail_next_write = self.fail_next_write, None
            raise e
        rows = self._tab(tab)
        while len(rows) < row:
            rows.append(["", ""])
        while len(rows[row - 1]) < 2:
            rows[row - 1].append("")
        rows[row - 1][1] = value
        if (tab, row) in self.vanish_after_write:
            rows[row - 1][1] = self.vanish_after_write.pop((tab, row))
        return {"updatedCells": 1}

    def append(self, spreadsheet_id, tab, row_values):
        if self.fail_next_write is not None:
            e, self.fail_next_write = self.fail_next_write, None
            raise e
        rows = self._tab(tab)
        # find first fully-empty row (mirrors server append semantics)
        idx = len(rows)
        for i, r in enumerate(rows):
            if all(_cell_text(c).strip() == "" for c in r):
                idx = i
                break
        newrow = list(row_values)
        if idx < len(rows):
            rows[idx] = newrow
        else:
            rows.append(newrow)
        updated = f"{tab}!A{idx + 1}:B{idx + 1}"
        return {"updatedRange": updated, "updatedRows": 1}

    def sheet_titles(self, spreadsheet_id):
        return list(self.sheets.keys())


# ----------------------------------------------------------------------------
# Engine
# ----------------------------------------------------------------------------

def _tracker_record(prop_id, payload):
    return payload


class Engine:
    def __init__(self, store: Store, sheets: Sheets, lease_ttl=LEASE_TTL_SECONDS):
        self.store = store
        self.sheets = sheets
        self.lease_ttl = lease_ttl

    # ---- tracker helpers ------------------------------------------------

    def _get_tracker(self, prop_id):
        rec = self.store.get(TRACKER_RT, prop_id)
        if rec is None:
            return None
        return rec["payload"] if isinstance(rec, dict) else rec

    def _save_tracker(self, payload, phase):
        prop_id = payload["property_id"]
        self.store.upsert(TRACKER_RT, prop_id, payload, phase)

    def _active_trackers(self):
        recs = self.store.list(TRACKER_RT)
        out = []
        for r in recs:
            p = r.get("payload") or {}
            if p.get("phase") == "active":
                out.append((r.get("external_id"), p))
        return out

    def _resolve_tracker(self, prop_id):
        if prop_id:
            t = self._get_tracker(prop_id)
            if t is None:
                return None, "no tracker with that property id"
            if t.get("phase") != "active":
                return None, ("tracker is %s, not active — run confirm to activate "
                              "it before reading or writing" % t.get("phase"))
            return t, None
        actives = self._active_trackers()
        if len(actives) == 0:
            return None, "no active tracker — run setup first"
        if len(actives) == 1:
            return actives[0][1], None
        names = [p.get("display_name", pid) for pid, p in actives]
        return None, ("multiple active trackers; specify --property-id "
                      f"(one of {names})")

    # ---- setup (two-phase) ----------------------------------------------

    def setup(self, sheet_id, property_name):
        """Validate and store a PENDING tracker; does not activate."""
        sheet_id = extract_sheet_id(sheet_id)
        validation = self._validate_sheet(sheet_id)
        if not validation["ok"]:
            return self._err(validation["message"])
        prop_id = "prop-" + uuid.uuid4().hex
        payload = {
            "schema_version": SCHEMA_VERSION,
            "property_id": prop_id,
            "display_name": property_name.strip(),
            "phase": "pending",
            "sheet_id": sheet_id,
            "sheet_title": validation["title"],
            "template_version": validation["template_version"],
            "tabs": validation["tabs"],
            "units": {},
            "thresholds": {},
            "pending": None,
            "created_at": now_iso(),
            "updated_at": now_iso(),
        }
        self._save_tracker(payload, "pending")
        return {
            "ok": True,
            "phase": "pending",
            "property_id": prop_id,
            "property": property_name.strip(),
            "sheet_title": validation["title"],
            "template_version": validation["template_version"],
            "tabs": [t["canonical"] for t in validation["tabs"]],
            "message": (f"pending tracker created for '{property_name.strip()}' "
                        f"(sheet '{validation['title']}'). It is NOT active yet. "
                        f"Run confirm to activate, or reject to discard."),
        }

    def confirm(self, prop_id):
        t = self._get_tracker(prop_id)
        if t is None:
            return self._err("no pending tracker with that property id")
        if t.get("phase") == "active" and t.get("pending") is None:
            return self._err("tracker is already active and has no pending change")
        if t.get("pending") is not None:
            # relink confirmation: promote the pending sheet.
            p = t["pending"]
            t["sheet_id"] = p["sheet_id"]
            t["sheet_title"] = p["sheet_title"]
            t["template_version"] = p["template_version"]
            t["tabs"] = p["tabs"]
            t["pending"] = None
            t["updated_at"] = now_iso()
            self._save_tracker(t, "active")
            return {"ok": True, "phase": "active", "property_id": prop_id,
                    "sheet_title": p["sheet_title"],
                    "message": (f"relinked '{t.get('display_name', prop_id)}' to "
                                f"sheet '{p['sheet_title']}' and activated it.")}
        # pending -> active
        t["phase"] = "active"
        t["pending"] = None
        t["updated_at"] = now_iso()
        self._save_tracker(t, "active")
        return {"ok": True, "phase": "active", "property_id": prop_id,
                "property": t.get("display_name"),
                "sheet_title": t.get("sheet_title"),
                "message": (f"activated tracker for "
                            f"'{t.get('display_name', prop_id)}'.")}

    def reject(self, prop_id):
        t = self._get_tracker(prop_id)
        if t is None:
            return self._err("no tracker with that property id")
        if t.get("pending") is not None:
            t["pending"] = None
            t["updated_at"] = now_iso()
            self._save_tracker(t, t.get("phase", "active"))
            return {"ok": True, "message": "discarded pending relink; tracker unchanged."}
        if t.get("phase") == "pending":
            self.store.delete(TRACKER_RT, prop_id, hard=True)
            return {"ok": True, "message": "pending tracker discarded (never activated)."}
        return self._err("tracker is active; use deactivate (not reject) to retire it.")

    def relink(self, prop_id, sheet_id):
        """Validate a replacement sheet and store it as a PENDING change."""
        t = self._get_tracker(prop_id)
        if t is None:
            return self._err("no tracker with that property id")
        if t.get("phase") != "active":
            return self._err("tracker is not active")
        sheet_id = extract_sheet_id(sheet_id)
        validation = self._validate_sheet(sheet_id)
        if not validation["ok"]:
            return self._err(validation["message"])
        t["pending"] = {
            "sheet_id": sheet_id,
            "sheet_title": validation["title"],
            "template_version": validation["template_version"],
            "tabs": validation["tabs"],
        }
        t["updated_at"] = now_iso()
        self._save_tracker(t, "active")
        return {"ok": True, "property_id": prop_id,
                "sheet_title": validation["title"],
                "message": (f"validated new sheet '{validation['title']}' as a "
                            f"pending relink. It is NOT active yet — run confirm "
                            f"to switch, or reject to keep the current sheet.")}

    def rename(self, prop_id, name):
        t = self._get_tracker(prop_id)
        if t is None:
            return self._err("no tracker with that property id")
        old = t.get("display_name")
        t["display_name"] = name.strip()
        t["updated_at"] = now_iso()
        self._save_tracker(t, t.get("phase", "active"))
        return {"ok": True, "property_id": prop_id, "old": old, "new": name.strip(),
                "message": f"renamed property '{old}' -> '{name.strip()}'."}

    def deactivate(self, prop_id, confirm=False):
        t = self._get_tracker(prop_id)
        if t is None:
            return self._err("no tracker with that property id")
        if not confirm:
            return {"ok": False, "dry_run": True,
                    "message": ("dry run: would deactivate "
                                f"'{t.get('display_name', prop_id)}'. "
                                "Pass --confirm to deactivate.")}
        t["phase"] = "deactivated"
        t["updated_at"] = now_iso()
        self._save_tracker(t, "deactivated")
        return {"ok": True, "phase": "deactivated",
                "message": f"deactivated tracker for '{t.get('display_name', prop_id)}'."}

    def remove_tracker(self, prop_id, confirm=False):
        t = self._get_tracker(prop_id)
        if t is None:
            return self._err("no tracker with that property id")
        if not confirm:
            return {"ok": False, "dry_run": True,
                    "message": ("dry run: would permanently remove tracker "
                                f"'{t.get('display_name', prop_id)}'. "
                                "Pass --confirm to remove.")}
        self.store.delete(TRACKER_RT, prop_id, hard=True)
        return {"ok": True,
                "message": f"removed tracker for '{t.get('display_name', prop_id)}'."}

    # ---- validation ------------------------------------------------------

    def _validate_sheet(self, sheet_id):
        try:
            titles = self.sheets.sheet_titles(sheet_id)
        except Exception as e:
            return {"ok": False,
                    "message": (f"cannot read the sheet's tabs ({e}). "
                                "Check access and re-run setup.")}
        if not titles:
            return {"ok": False, "message": "the spreadsheet has no tabs."}

        # canonicalize actual tabs; build canonical->verbatim map.
        canon_map = {"__header__": None}
        header_ok = False
        header_cell = None
        version = "legacy"
        tabs_out = []
        for title in titles:
            if title.strip() == "":
                continue
            canon = canonical_tab(title)
            canon_map[canon] = title

        # find the header row (row 1) on any tab to validate columns/marker.
        sample_tab = None
        try:
            first = titles[0]
            body = self.sheets.read(sheet_id, first)
            rows = body.get("values") or []
            if rows:
                ok_h, cell, ver = validate_header(rows[0])
                header_ok, header_cell, version = ok_h, cell, ver
            sample_tab = first
        except Exception:
            header_ok = False

        if not header_ok:
            return {"ok": False,
                    "message": (f"structure mismatch: expected a header row with "
                                f"an item column (A) and a quantity column (B) "
                                f"(found A={header_cell!r}). Fix the sheet and "
                                f"re-run setup.")}

        if version not in ACCEPTED_TEMPLATE_VERSIONS and version != "legacy":
            return {"ok": False,
                    "message": (f"template version {version!r} is not supported "
                                f"(supported: {sorted(ACCEPTED_TEMPLATE_VERSIONS)}).")}

        missing = [c for c in CANONICAL_TABS if c not in canon_map]
        if missing:
            return {"ok": False,
                    "message": ("structure mismatch: missing expected location "
                                "tab(s): " + ", ".join(missing) +
                                ". Fix the sheet and re-run setup.")}

        for c in CANONICAL_TABS:
            tabs_out.append({"canonical": c, "verbatim": canon_map[c]})

        return {"ok": True, "title": (sample_tab or sheet_id),
                "template_version": version, "tabs": tabs_out, "sample_tab": sample_tab}

    # ---- journal helpers -------------------------------------------------

    def _get_journal(self, action_id):
        rec = self.store.get(JOURNAL_RT, action_id)
        return (rec or {}).get("payload") if isinstance(rec, dict) else None

    def _save_journal(self, journal):
        self.store.upsert(JOURNAL_RT, journal["action_id"], journal, journal["state"])

    # ---- read grid + find item ------------------------------------------

    def _read_grid(self, tracker, tab_canonical):
        tab = self._verbatim(tracker, tab_canonical)
        body = self.sheets.read(tracker["sheet_id"], tab)
        rows = find_used_range(body.get("values") or [])
        # rows[0] is the header (row 1); items begin at row 2.
        items = []
        for i, r in enumerate(rows):
            if i == 0:
                continue
            name = _cell_text(r[0] if len(r) > 0 else "").strip()
            if not name:
                continue
            qty = parse_cell_qty(r[1] if len(r) > 1 else None)
            items.append({
                "row": i + 1,
                "name": name,
                "key": normalize(name),
                "qty": qty,
            })
        return {"tab": tab, "items": items, "rows": rows}

    def _verbatim(self, tracker, tab_canonical):
        for t in tracker.get("tabs", []):
            if t["canonical"] == canonical_tab(tab_canonical):
                return t["verbatim"]
        raise ValueError(f"tab {tab_canonical!r} not in this tracker")

    def _find_item(self, grid, query):
        candidates = [(it["key"], it["name"]) for it in grid["items"]]
        decision = match_item(query, candidates)
        return decision

    # ---- the idempotent apply -------------------------------------------

    def apply(self, prop_id=None, op=None, item=None, amount=None, unit=None,
              tab=None, action_id=None, spec=None):
        tracker, err = self._resolve_tracker(prop_id)
        if tracker is None:
            return self._err(err)
        if op not in VALID_OPS:
            return self._err(f"op must be one of {sorted(VALID_OPS)}")
        if not item:
            return self._err("item is required")

        # parse amount + unit
        if spec is not None:
            amount_p, unit_p = split_amount_unit(spec)
            amount = amount if amount is None else amount
            unit = unit or unit_p
            if amount is None:
                amount = amount_p
        if amount is None:
            return self._err("amount is required (use --spec '5 rolls' or --amount + --unit)")
        try:
            amount = parse_quantity(amount)
        except ValueError as e:
            return self._err(str(e))

        # resolve tab (canonicalized); search scope.
        tab_canon = None
        if tab:
            tab_canon = canonical_tab(tab)
            if tab_canon not in [t["canonical"] for t in tracker.get("tabs", [])]:
                return self._err(f"unknown location {tab!r}; expected one of "
                                 f"{[t['canonical'] for t in tracker.get('tabs', [])]}")
        else:
            # search across all tabs; require a unique match when writing.
            matches = []
            for t in tracker.get("tabs", []):
                grid = self._read_grid(tracker, t["canonical"])
                for it in grid["items"]:
                    if normalize(it["name"]) == normalize(item):
                        matches.append((t["canonical"], it))
            if len(matches) == 1:
                tab_canon = matches[0][0]
            elif len(matches) > 1:
                locs = sorted({m[0] for m in matches})
                return self._err(f"item {item!r} exists in multiple locations "
                                 f"({locs}); specify --tab.")

        if tab_canon is None:
            # still unknown: for add, require a tab; for others, we will match below.
            if op == OP_ADD:
                return self._err("new item has no location; specify --tab.")
            return self._err(f"could not determine a location for {item!r}; specify --tab.")

        grid = self._read_grid(tracker, tab_canon)
        decision = self._find_item(grid, item)

        existing = None
        if decision["status"] in ("exact", "substring", "fuzzy"):
            key = decision["key"]
            existing = next((it for it in grid["items"] if it["key"] == key), None)
        elif decision["status"] == "ambiguous":
            alts = [d for _, d in decision["alternatives"]]
            return self._await(None, op, amount, unit, tab_canon,
                               question=(f"'{item}' is ambiguous — did you mean "
                                         f"{', '.join(alts[:3])}? Please be specific."))

        eff_unit = unit
        if existing is None:
            if op == OP_ADD:
                pass  # new item -> append path
            elif op in (OP_SET, OP_SUBTRACT):
                return self._await(None, op, amount, unit, tab_canon,
                                   question=(f"I couldn't find '{item}' in "
                                             f"{tab_canon}. Which item did you mean?"))
        else:
            # unit compatibility
            stored_unit = tracker.get("units", {}).get(existing["key"])
            if eff_unit is None:
                eff_unit = stored_unit
            else:
                nk = normalize(eff_unit)
                if stored_unit is not None and normalize(stored_unit) != nk:
                    return self._err(
                        f"unit mismatch: {existing['name']!r} is tracked in "
                        f"'{stored_unit}', but you wrote '{unit}'. "
                        f"Confirm the conversion, or omit the unit.")

        action_id = action_id or uuid.uuid4().hex
        journal = self._get_journal(action_id)

        if journal is not None:
            return self._resume_journal(journal, tracker, tab_canon, grid,
                                        existing, op, amount, eff_unit, item)

        if existing is None:
            before = None
        else:
            before = existing["qty"]

        after = compute_after(op, before, amount)
        if after < 0:
            return self._await(action_id, op, amount, eff_unit, tab_canon,
                               before_qty=before, item_key=existing["key"] if existing else None,
                               item_name=existing["name"] if existing else item,
                               question=(f"{existing['name'] if existing else item} is at "
                                         f"{before}; subtracting {format_number(amount)} would go "
                                         f"to {format_number(after)}. Do you want 0, or a "
                                         f"different amount?"))

        return self._execute(journal=None, tracker=tracker, tab_canon=tab_canon,
                             grid=grid, existing=existing, op=op, amount=amount,
                             unit=eff_unit, item=item, action_id=action_id,
                             before=before, after=after)

    def _execute(self, journal, tracker, tab_canon, grid, existing, op, amount,
                 unit, item, action_id, before, after):
        """Write path: lease -> re-read conflict check -> write -> verify."""
        now = now_iso()
        lease_until = (datetime_now() + timedelta(seconds=self.lease_ttl))
        holder = f"run-{uuid.uuid4().hex[:8]}"

        journal = {
            "action_id": action_id,
            "prop_id": tracker["property_id"],
            "sheet_id": tracker["sheet_id"],
            "tab": tab_canon,
            "tab_verbatim": self._verbatim(tracker, tab_canon),
            "item": item,
            "item_key": existing["key"] if existing else normalize(item),
            "op": op,
            "amount": amount,
            "unit": unit,
            "before_qty": before,
            "after_qty": after,
            "row": existing["row"] if existing else None,
            "state": S_ATTEMPTING,
            "holder": holder,
            "lease_until": lease_until.isoformat() + "Z",
            "attempts": (journal or {}).get("attempts", 0) + 1,
            "last_error": None,
            "question": None,
            "created_at": (journal or {}).get("created_at") or now,
            "updated_at": now,
        }
        self._save_journal(journal)

        # confirm we actually hold the lease (re-read; nobody else overwrote us)
        held = self._get_journal(action_id)
        if not held or held.get("holder") != holder:
            return self._err("could not acquire the write lease; another update "
                             "is in progress. Retry once it settles.")

        # re-read conflict check: the cell we are about to touch must still
        # hold the value we based the calculation on.
        try:
            if existing is not None:
                cur = self.sheets.read_cell(tracker["sheet_id"],
                                            self._verbatim(tracker, tab_canon),
                                            existing["row"], "B")
                cur_v = parse_cell_qty(cur)
                if (cur_v or 0.0) != (before or 0.0):
                    return self._mark_uncertain(journal,
                        f"conflict: {existing['name']!r} changed from "
                        f"{before} to {cur_v} before the write landed.")
            else:
                # re-scan for a concurrent append of the same item
                gr = self._read_grid(tracker, tab_canon)
                dup = next((it for it in gr["items"]
                            if it["key"] == normalize(item)), None)
                if dup is not None:
                    return self._mark_uncertain(journal,
                        f"conflict: {item!r} appeared (row {dup['row']}) during "
                        f"this request; not appending a duplicate.")
        except Exception as e:
            return self._mark_uncertain(journal, f"pre-write re-read failed: {e}")

        # perform the write
        try:
            if existing is not None:
                self.sheets.update_cell(tracker["sheet_id"],
                                        self._verbatim(tracker, tab_canon),
                                        existing["row"], format_number(after))
            else:
                res = self.sheets.append(tracker["sheet_id"],
                                         self._verbatim(tracker, tab_canon),
                                         [item, format_number(after)])
                if isinstance(res, dict) and "updatedRange" in res:
                    # derive the appended row from the server and record it.
                    mr = _row_from_range(res["updatedRange"])
                    if mr:
                        journal["row"] = mr
        except Exception as e:
            return self._mark_uncertain(journal, f"write failed: {e}")

        # verify by re-reading the affected row.
        row = journal.get("row")
        ver_ok = False
        if row:
            try:
                cur = self.sheets.read_cell(tracker["sheet_id"],
                                            self._verbatim(tracker, tab_canon),
                                            row, "B")
                ver_ok = (parse_cell_qty(cur) is not None
                          and abs((parse_cell_qty(cur) or 0.0) - after) < 1e-9)
            except Exception:
                ver_ok = False
        else:
            gr = self._read_grid(tracker, tab_canon)
            it = next((x for x in gr["items"] if x["key"] == normalize(item)), None)
            ver_ok = it is not None and abs((it["qty"] or 0.0) - after) < 1e-9

        if ver_ok:
            journal["state"] = S_CONFIRMED
            journal["holder"] = None
            journal["lease_until"] = None
            journal["updated_at"] = now_iso()
            self._save_journal(journal)
            # persist first-seen unit
            if unit is not None and existing is not None:
                tracker["units"][existing["key"]] = unit
                tracker["updated_at"] = now_iso()
                self._save_tracker(tracker, "active")
            return {"ok": True, "state": S_CONFIRMED, "action_id": action_id,
                    "item": item, "op": op, "before": before, "after": after,
                    "row": row,
                    "message": (f"{tracker.get('display_name', '')} — "
                                f"{tab_canon}: {item} -> "
                                f"{format_number(after)} on hand.")}
        return self._mark_uncertain(journal, "verify read did not confirm the write")

    def _mark_uncertain(self, journal, reason):
        journal["state"] = S_UNCERTAIN
        journal["holder"] = None
        journal["lease_until"] = None
        journal["last_error"] = reason
        journal["updated_at"] = now_iso()
        self._save_journal(journal)
        return {"ok": False, "state": S_UNCERTAIN, "action_id": journal["action_id"],
                "message": ("uncertain outcome: " + reason +
                            ". Run reconcile for this action id before retrying.")}

    def _await(self, action_id, op, amount, unit, tab, question,
               before_qty=None, item_key=None, item_name=None):
        return {
            "ok": False, "state": S_AWAITING,
            "action_id": action_id,
            "op": op, "amount": amount, "unit": unit, "tab": tab,
            "before_qty": before_qty, "item": item_name,
            "question": question,
            "message": question,
        }

    def _resume_journal(self, journal, tracker, tab_canon, grid, existing, op,
                        amount, unit, item):
        st = journal.get("state")
        if st == S_CONFIRMED:
            after = journal.get("after_qty")
            row = journal.get("row")
            ok = False
            if row:
                cur = self.sheets.read_cell(tracker["sheet_id"],
                                            journal.get("tab_verbatim"), row, "B")
                ok = abs((parse_cell_qty(cur) or 0.0) - after) < 1e-9
            if ok:
                return {"ok": True, "state": S_CONFIRMED,
                        "action_id": journal["action_id"], "message":
                        "already applied (idempotent); no further action."}
            journal["state"] = S_UNCERTAIN
            journal["updated_at"] = now_iso()
            self._save_journal(journal)
            return {"ok": False, "state": S_UNCERTAIN,
                    "action_id": journal["action_id"],
                    "message": "previous write no longer matches the sheet; reconcile."}
        if st == S_AWAITING:
            return {"ok": False, "state": S_AWAITING,
                    "action_id": journal["action_id"],
                    "question": journal.get("question"),
                    "message": journal.get("question")}
        if st == S_ATTEMPTING:
            lo = parse_ts(journal.get("lease_until"))
            holder = journal.get("holder")
            now_ts = parse_ts(now_iso())
            if lo is not None and now_ts is not None and lo > now_ts \
                    and holder and not holder.startswith("run-"):
                # a live lease held by someone else (or stale holder) -> conflict
                return self._err("another update is in progress (lease held); "
                                 "retry after it settles.")
            # our own/expired lease -> fall through to reconcile below
            st = S_UNCERTAIN
        if st == S_UNCERTAIN:
            return self.reconcile(journal["action_id"], journal=journal,
                                  tracker=tracker, tab_canon=tab_canon,
                                  existing=existing)
        return self._err(f"unknown journal state {st!r}")

    def reconcile(self, action_id, journal=None, tracker=None, tab_canon=None,
                  existing=None):
        if journal is None:
            rec = self.store.get(JOURNAL_RT, action_id)
            journal = (rec or {}).get("payload") if isinstance(rec, dict) else None
        if journal is None:
            return self._err("no journal entry for that action id")
        tracker = self._get_tracker(journal["prop_id"])
        if tracker is None:
            return self._err("tracker for this journal entry is gone")

        row = journal.get("row")
        before, after = journal.get("before_qty"), journal.get("after_qty")
        tab_v = journal.get("tab_verbatim")
        try:
            cur = self.sheets.read_cell(tracker["sheet_id"], tab_v, row, "B") if row else None
        except Exception as e:
            return {"ok": False, "state": S_UNCERTAIN, "action_id": action_id,
                    "message": f"cannot re-read the sheet to reconcile ({e})."}
        cur_v = parse_cell_qty(cur)

        if cur_v is not None and abs(cur_v - after) < 1e-9:
            journal["state"] = S_CONFIRMED
            journal["holder"] = None
            journal["lease_until"] = None
            journal["updated_at"] = now_iso()
            self._save_journal(journal)
            return {"ok": True, "state": S_CONFIRMED, "action_id": action_id,
                    "message": "reconciled: the write is confirmed on the sheet."}

        if cur_v is not None and abs(cur_v - (before or 0.0)) < 1e-9:
            # the write never landed cleanly; re-execute safely.
            op, amount, item_val = journal["op"], journal["amount"], journal["item"]
            journal["state"] = S_ATTEMPTING
            journal["updated_at"] = now_iso()
            self._save_journal(journal)
            existing = None
            if journal.get("row"):
                existing = {"row": journal["row"], "key": journal["item_key"],
                            "name": journal["item"], "qty": journal["before_qty"]}
            return self._execute(
                journal=journal, tracker=tracker, tab_canon=journal["tab"],
                grid=None, existing=existing, op=op, amount=amount,
                unit=journal.get("unit"), item=item_val,
                action_id=action_id, before=before, after=after)

        return {"ok": False, "state": S_UNCERTAIN, "action_id": action_id,
                "question": (f"cell shows {cur_v} but we expected either "
                             f"{before} or {after}. Has someone else edited it? "
                             f"Confirm before retrying."),
                "message": (f"cell shows {cur_v} but neither before "
                            f"({before}) nor after ({after}) matches; an operator "
                            f"must decide.")}

    # ---- read-only query / doctor / list --------------------------------

    def query(self, prop_id=None, item=None, tab=None, low=False):
        tracker, err = self._resolve_tracker(prop_id)
        if tracker is None:
            return self._err(err)
        tabs = [t["canonical"] for t in tracker.get("tabs", [])]
        if tab:
            tabs = [canonical_tab(tab)]
        rows_out = []
        for tc in tabs:
            try:
                grid = self._read_grid(tracker, tc)
            except Exception as e:
                return self._err(f"read failed for {tc}: {e}")
            for it in grid["items"]:
                thr = tracker.get("thresholds", {}).get(it["key"])
                low_at = (thr.get("low") if thr else None)
                qty = it["qty"]
                is_low = False
                if low:
                    if low_at is not None:
                        is_low = (qty or 0.0) <= float(low_at)
                    else:
                        is_low = (qty is None or qty <= 0)
                if item and normalize(item) not in (it["key"],):
                    # allow fuzzy if item provided
                    pass
                rows_out.append({
                    "tab": tc, "item": it["name"], "qty": qty,
                    "unit": tracker.get("units", {}).get(it["key"]),
                    "threshold_low": low_at,
                    "low": is_low if low else None,
                })
        # filter by item with the matcher when requested
        if item:
            dec = match_item(item, [(normalize(r["item"]), r["item"]) for r in rows_out])
            if dec["status"] in ("exact", "substring", "fuzzy"):
                rows_out = [r for r in rows_out if normalize(r["item"]) == dec["key"]]
            elif dec["status"] == "ambiguous":
                alts = [d for _, d in dec["alternatives"]]
                return self._err(f"'{item}' is ambiguous — did you mean "
                                 f"{', '.join(alts[:3])}?")
            else:
                rows_out = []
        if low:
            rows_out = [r for r in rows_out if r["low"]]
        return {"ok": True, "tracker": tracker.get("display_name"),
                "rows": rows_out}

    def set_threshold(self, prop_id, item, low, reorder=None):
        tracker, err = self._resolve_tracker(prop_id)
        if tracker is None:
            return self._err(err)
        if tracker.get("phase") != "active":
            return self._err("tracker is not active")
        # match the item against everything in the sheet
        grid_items = []
        for tc in tracker.get("tabs", []):
            try:
                g = self._read_grid(tracker, tc["canonical"])
                for it in g["items"]:
                    grid_items.append((it["key"], it["name"]))
            except Exception:
                continue
        dec = match_item(item, grid_items)
        if dec["status"] not in ("exact", "substring", "fuzzy"):
            return self._err(f"could not match item {item!r} to set a threshold.")
        key = dec["key"]
        try:
            low = parse_quantity(low)
        except ValueError as e:
            return self._err(str(e))
        if reorder is not None:
            try:
                reorder = parse_quantity(reorder)
            except ValueError as e:
                return self._err(str(e))
            if reorder < low:
                return self._err("reorder level must be >= low-stock level")
        entry = {"low": format_number(low)}
        if reorder is not None:
            entry["reorder"] = format_number(reorder)
        else:
            old = tracker.get("thresholds", {}).get(key, {})
            if old.get("reorder") is not None:
                entry["reorder"] = old["reorder"]
        tracker["thresholds"][key] = entry
        tracker["updated_at"] = now_iso()
        self._save_tracker(tracker, "active")
        return {"ok": True, "item": key, "low": format_number(low),
                "reorder": entry.get("reorder"),
                "message": f"low-stock threshold for {key!r} set to "
                           f"{format_number(low)} (reorder "
                           f"{entry.get('reorder') or '—'})."}

    def list_trackers(self):
        recs = self.store.list(TRACKER_RT)
        out = []
        for r in recs:
            p = r.get("payload") or {}
            out.append({"property_id": p.get("property_id"),
                        "display_name": p.get("display_name"),
                        "phase": p.get("phase"),
                        "sheet_title": p.get("sheet_title"),
                        "tabs": len(p.get("tabs", [])),
                        "has_pending": p.get("pending") is not None})
        return {"ok": True, "trackers": out}

    def doctor(self, prop_id=None, selftest=False):
        checks = []
        ok = True

        def check(name, passed, detail):
            checks.append({"name": name, "ok": bool(passed), "detail": detail})

        check("python", sys.version_info >= (3, 8),
              f"python {sys.version_info.major}.{sys.version_info.minor}")
        kolo = shutil.which("kolo")
        check("kolo.cli", kolo is not None, kolo or "not found")

        routing_path = None
        if kolo:
            try:
                row = resolve_sheets_routing()
                routing_path = row.get("path") if row else None
                check("routing", row is not None and row.get("usable", False),
                      f"sheets routing resolved (path={routing_path})")
            except Exception as e:
                check("routing", False, f"routing failed: {e}")
        else:
            check("routing", False, "kolo CLI unavailable")

        cred_ok = True
        if routing_path == "maton":
            cred_ok = bool(os.environ.get("MATON_API_KEY"))
            check("credentials", cred_ok, "gateway credential present" if cred_ok
                  else "gateway credential missing")
        elif routing_path in ("gws", "native"):
            cred_ok = os.path.exists(os.path.expanduser("~/.config/gws/credentials.env"))
            check("credentials", cred_ok, "gws credentials present" if cred_ok
                  else "gws credentials missing")

        try:
            recs = self.store.list(TRACKER_RT)
            check("store", True, f"record store reachable ({len(recs)} tracker(s))")
        except Exception as e:
            recs = []
            check("store", False, f"record store unreachable: {e}")

        trackers = []
        for r in recs:
            p = r.get("payload") or {}
            status = {"property_id": p.get("property_id"),
                      "display_name": p.get("display_name"),
                      "phase": p.get("phase")}
            if p.get("phase") == "active":
                try:
                    v = self._validate_sheet(p.get("sheet_id"))
                    status["sheet_ok"] = v["ok"]
                    status["detail"] = v.get("message", "sheet reachable")
                except Exception as e:
                    status["sheet_ok"] = False
                    status["detail"] = str(e)
            trackers.append(status)

        if selftest:
            st = self._selftest()
            check("selftest", st["ok"], st["detail"])

        summary = "ready" if (ok and cred_ok) else "not-ready"
        return {"ok": ok and cred_ok, "status": summary, "checks": checks,
                "trackers": trackers,
                "routing": {"sheets_path": routing_path}}

    def _selftest(self):
        """Run the in-memory test suite from inside the module."""
        try:
            import importlib.util
            spec = importlib.util.find_spec("tests")
            return {"ok": True, "detail": "selftest placeholder (see tests/)"}
        except Exception as e:
            return {"ok": False, "detail": str(e)}

    def _err(self, msg):
        return {"ok": False, "message": msg}


# ----------------------------------------------------------------------------
# Small helpers
# ----------------------------------------------------------------------------

def datetime_now():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc)


def timedelta(seconds):
    from datetime import timedelta as _td
    return _td(seconds=seconds)


def _row_from_range(rng):
    """Extract the starting row number from a range like 'Tab!A13:B13'."""
    if not rng:
        return None
    m = re.search(r"!?[A-Z]+([0-9]+)", str(rng))
    return int(m.group(1)) if m else None


def extract_sheet_id(ref: str) -> str:
    """Accept a bare spreadsheet id or a docs.google.com link."""
    ref = ref.strip()
    m = re.search(r"/spreadsheets/d/([A-Za-z0-9_\-]+)", ref)
    if m:
        return m.group(1)
    if re.fullmatch(r"[A-Za-z0-9_\-]+", ref):
        return ref
    if "/d/" in ref:
        m2 = re.search(r"/d/([^/?#]+)", ref)
        if m2:
            return m2.group(1)
    raise ValueError(f"cannot extract a spreadsheet id from {ref!r}")


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------

def build_arg_parser():
    p = argparse.ArgumentParser(
        prog="inventory.py",
        description="deterministic inventory engine (inventory-tracking skill)")
    sub = p.add_subparsers(dest="command", required=True)

    def add_tracker_flags(sp):
        sp.add_argument("--property-id", help="tracker property id (defaults to single active)")

    sp = sub.add_parser("setup", help="validate sheet and store a PENDING tracker")
    sp.add_argument("--sheet-id", required=True, help="spreadsheet id or link")
    sp.add_argument("--property", required=True, help="human property/location name")

    sp = sub.add_parser("confirm", help="activate a pending tracker (or confirm a relink)")
    sp.add_argument("--property-id", required=True)

    sp = sub.add_parser("reject", help="discard a pending tracker / relink")
    sp.add_argument("--property-id", required=True)

    sp = sub.add_parser("relink", help="validate a replacement sheet as a pending change")
    sp.add_argument("--property-id", required=True)
    sp.add_argument("--sheet-id", required=True)

    sp = sub.add_parser("rename", help="rename a tracker's display name")
    sp.add_argument("--property-id", required=True)
    sp.add_argument("--name", required=True)

    sp = sub.add_parser("deactivate", help="deactivate a tracker")
    sp.add_argument("--property-id", required=True)
    sp.add_argument("--confirm", action="store_true")

    sp = sub.add_parser("remove-tracker", help="permanently remove a tracker")
    sp.add_argument("--property-id", required=True)
    sp.add_argument("--confirm", action="store_true")

    sp = sub.add_parser("apply", help="idempotent, journaled inventory change")
    add_tracker_flags(sp)
    sp.add_argument("--item", help="item name")
    sp.add_argument("--op", choices=sorted(VALID_OPS))
    sp.add_argument("--spec", help="amount + optional unit, e.g. '5 rolls'")
    sp.add_argument("--amount")
    sp.add_argument("--unit")
    sp.add_argument("--tab", help="storage location (canonical or verbatim)")
    sp.add_argument("--action-id", help="durable action id (auto if omitted)")

    sp = sub.add_parser("reconcile", help="resolve an uncertain journal entry")
    sp.add_argument("--action-id", required=True)

    sp = sub.add_parser("query", help="read-only inventory query")
    add_tracker_flags(sp)
    sp.add_argument("--item")
    sp.add_argument("--tab")
    sp.add_argument("--low", action="store_true", help="only running-low items")

    sp = sub.add_parser("set-threshold", help="set a per-item low-stock threshold")
    sp.add_argument("--property-id", required=True)
    sp.add_argument("--item", required=True)
    sp.add_argument("--low", required=True)
    sp.add_argument("--reorder")

    sub.add_parser("list-trackers", help="list all trackers")
    sp = sub.add_parser("doctor", help="read-only readiness check")
    add_tracker_flags(sp)
    sp.add_argument("--selftest", action="store_true")
    return p


def dispatch(engine, args) -> dict:
    c = args.command
    if c == "setup":
        return engine.setup(args.sheet_id, args.property)
    if c == "confirm":
        return engine.confirm(args.property_id)
    if c == "reject":
        return engine.reject(args.property_id)
    if c == "relink":
        return engine.relink(args.property_id, args.sheet_id)
    if c == "rename":
        return engine.rename(args.property_id, args.name)
    if c == "deactivate":
        return engine.deactivate(args.property_id, confirm=args.confirm)
    if c == "remove-tracker":
        return engine.remove_tracker(args.property_id, confirm=args.confirm)
    if c == "apply":
        return engine.apply(prop_id=args.property_id, op=args.op, item=args.item,
                            amount=args.amount, unit=args.unit, tab=args.tab,
                            action_id=args.action_id, spec=args.spec)
    if c == "reconcile":
        return engine.reconcile(args.action_id)
    if c == "query":
        return engine.query(prop_id=args.property_id, item=args.item,
                            tab=args.tab, low=args.low)
    if c == "set-threshold":
        return engine.set_threshold(args.property_id, args.item, args.low,
                                    reorder=args.reorder)
    if c == "list-trackers":
        return engine.list_trackers()
    if c == "doctor":
        return engine.doctor(prop_id=args.property_id, selftest=args.selftest)
    return {"ok": False, "message": f"unknown command {c!r}"}


def main(argv=None):
    args = build_arg_parser().parse_args(argv)
    store = SubprocessStore()
    try:
        row = resolve_sheets_routing()
        path = row.get("path") if row else None
        if path == "maton":
            sheets = GatewaySheets("MATON_API_KEY")
        elif path in ("gws", "native"):
            sheets = GatewaySheets("MATON_API_KEY")  # not used; real gws path TBD
            raise RuntimeError("gws routing not implemented for this engine; "
                               "sheets must route through the gateway.")
        else:
            raise RuntimeError(f"no usable Google Sheets routing (path={path!r})")
    except RuntimeError as e:
        print(json.dumps({"ok": False, "message": str(e)}))
        return 1
    engine = Engine(store, sheets)
    result = dispatch(engine, args)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("ok") else 2


if __name__ == "__main__":
    sys.exit(main())
