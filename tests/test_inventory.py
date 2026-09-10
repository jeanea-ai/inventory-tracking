#!/usr/bin/env python3
"""
Test suite for the deterministic inventory engine (tools/inventory.py).

stdlib unittest only (pytest-compatible: `python -m unittest` or `pytest`).
Runs entirely against the in-memory backends; no network, no Google Sheets,
no kolo CLI. Covers the required hardening scenarios:

  - two-phase setup (pending -> explicit confirm; reject discards)
  - ambiguous fuzzy matches (ask, never guess)
  - concurrent updates (version-conflict detection via pre-write re-read)
  - timeout-after-write reconciliation + uncertain-vs-operator decision
  - duplicate action ids (idempotent replay)
  - negative stock (no flooring at zero; ask one actionable question)
  - unit mismatch (incompatible-change rejection)
  - low-stock / reorder thresholds and "running low"
  - sheets over 200 rows (dynamic used range)
  - append races (concurrency-safe appends, no duplicate rows)
  - tracker relinking (pending change -> confirm)
  - error questions (structure mismatch, unknown tab, missing item)
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
TOOLS = HERE.parent / "tools"
sys.path.insert(0, str(TOOLS))

import inventory as inv


HEADER = ["Item", "Quantity", "TEMPLATE_VERSION=2.0"]

# Verbatim tab names from the approved (legacy) template, including the
# trailing spaces and the "Stoage" typo. These are what a live sheet actually
# contains and what the engine must canonicalize without promoting typos.
VERBATIM_TABS = [
    "1st Floor Storage ",
    "Breakfast Items ",
    "Laundry Room",
    "Maintenance Room",
    "Pool Supplies ",
    "2nd Floor Stoage 1",
    "2nd Floor Storage 2",
    "3rd Floor Storage 1",
    "3rd Floor Storage 2",
    "4th Floor Storage 1",
    "4th Floor Storage 2",
]


def make_sheet(extra_items=None):
    """Build a template-conformant sheet dict (verbatim tab -> rows)."""
    tabs = {t: [list(HEADER)] for t in VERBATIM_TABS}
    tabs["Laundry Room"] = [list(HEADER), ["Bath Towels", "5"], ["Hand Soap", "4"]]
    tabs["Breakfast Items "] = [list(HEADER), ["Applw Juice", "6"], ["Cereal", "3"]]
    tabs["1st Floor Storage "] = [list(HEADER), ["Bath Towels", "12"]]
    if extra_items:
        for tab, col in extra_items:
            tabs[tab].append(list(col))
    return tabs


def make_engine(sheets=None, store=None):
    store = store or inv.InMemoryStore()
    sheets = sheets or inv.FakeSheet(make_sheet())
    return inv.Engine(store, sheets), store, sheets


def setup_active(engine, property_name="Sunrise Inn"):
    """Run two-phase setup and confirm, returning the property id."""
    r = engine.setup("https://docs.google.com/spreadsheets/d/ABC123EXAMPLEID/edit",
                     property_name)
    assert r["ok"] and r["phase"] == "pending", r
    pid = r["property_id"]
    c = engine.confirm(pid)
    assert c["ok"] and c["phase"] == "active", c
    return pid


class TestTwoPhaseSetup(unittest.TestCase):
    def test_setup_creates_pending_not_active(self):
        eng, store, sheets = make_engine()
        r = eng.setup("https://docs.google.com/spreadsheets/d/ABC123EXAMPLEID/edit",
                      "Sunrise Inn")
        self.assertTrue(r["ok"])
        self.assertEqual(r["phase"], "pending")
        # tracker must NOT be usable for writes yet
        q = eng.query(prop_id=r["property_id"])
        self.assertFalse(q["ok"])

    def test_confirm_activates(self):
        eng, store, sheets = make_engine()
        pid = setup_active(eng)
        lt = eng.list_trackers()
        self.assertTrue(any(t["property_id"] == pid and t["phase"] == "active"
                            for t in lt["trackers"]))

    def test_reject_discards_pending(self):
        eng, store, sheets = make_engine()
        r = eng.setup("https://docs.google.com/spreadsheets/d/ABC123EXAMPLEID/edit",
                      "Sunrise Inn")
        pid = r["property_id"]
        rej = eng.reject(pid)
        self.assertTrue(rej["ok"])
        self.assertIsNone(eng._get_tracker(pid))

    def test_no_tracker_means_no_write(self):
        eng, store, sheets = make_engine()
        a = eng.apply(op="add", item="Pillows", spec="6", tab="laundry room")
        self.assertFalse(a["ok"])
        self.assertIn("setup", a["message"].lower())

    def test_structure_mismatch_rejected(self):
        eng, store, sheets = make_engine()
        # remove one required tab -> missing location tab(s)
        bad = inv.FakeSheet({k: v for k, v in make_sheet().items()
                             if k != "2nd Floor Stoage 1"})
        eng2 = inv.Engine(inv.InMemoryStore(), bad)
        r = eng2.setup("https://docs.google.com/spreadsheets/d/ABC123EXAMPLEID/edit",
                       "Sunrise Inn")
        self.assertFalse(r["ok"])
        self.assertIn("structure mismatch", r["message"])

    def test_unsupported_template_version_rejected(self):
        tabs = {t: [["Item", "Quantity", "TEMPLATE_VERSION=9.9"]] for t in VERBATIM_TABS}
        eng = inv.Engine(inv.InMemoryStore(), inv.FakeSheet(tabs))
        r = eng.setup("https://docs.google.com/spreadsheets/d/ABC123EXAMPLEID/edit",
                      "Sunrise Inn")
        self.assertFalse(r["ok"])
        self.assertIn("not supported", r["message"])


class TestMatchingSafety(unittest.TestCase):
    def test_exact_match_preferred(self):
        eng, store, sheets = make_engine()
        pid = setup_active(eng)
        d = eng._find_item(eng._read_grid(eng._get_tracker(pid), "laundry room"),
                           "bath towels")
        self.assertEqual(d["status"], "exact")

    def test_typo_fuzzy_match_single_candidate(self):
        eng, store, sheets = make_engine()
        pid = setup_active(eng)
        d = eng._find_item(eng._read_grid(eng._get_tracker(pid), "laundry room"),
                           "bath towells")
        self.assertIn(d["status"], ("substring", "fuzzy"))

    def test_ambiguous_fuzzy_asks_no_guess(self):
        # two candidates at identical similarity must resolve to "ambiguous",
        # never to an arbitrary nearest match.
        d = inv.match_item("soap bar", [("a", "Dish Soap Bar"),
                                        ("b", "Hand Soap Bar")])
        self.assertEqual(d["status"], "ambiguous")

    def test_fuzzy_below_threshold_is_none_not_a_guess(self):
        # a query far from every candidate must never silently latch on.
        d = inv.match_item("bath towls", [("a", "Coffee Cups"),
                                          ("b", "Trash Liners")])
        self.assertEqual(d["status"], "none")

    def test_adjust_missing_item_returns_question(self):
        eng, store, sheets = make_engine()
        pid = setup_active(eng)
        r = eng.apply(prop_id=pid, op="subtract", item="zzz no such item zzz",
                      spec="1", tab="laundry room")
        self.assertFalse(r["ok"])
        self.assertEqual(r["state"], "awaiting_operator")
        self.assertIsNotNone(r.get("question"))


class TestIdempotencyAndJournal(unittest.TestCase):
    def test_duplicate_action_id_replays_without_double_apply(self):
        eng, store, sheets = make_engine()
        pid = setup_active(eng)
        aid = "action-dup-1"
        r1 = eng.apply(prop_id=pid, op="add", item="Cereal", spec="1",
                       tab="breakfast items", action_id=aid)
        self.assertTrue(r1["ok"])
        self.assertEqual(r1["state"], "confirmed")
        r2 = eng.apply(prop_id=pid, op="add", item="Cereal", spec="1",
                       tab="breakfast items", action_id=aid)
        self.assertTrue(r2["ok"])
        self.assertIn("idempotent", r2["message"])
        # quantity must have incremented exactly once: 3 -> 4
        q = eng.query(prop_id=pid, item="cereal")
        self.assertEqual(q["rows"][0]["qty"], 4.0)

    def test_write_failure_marks_uncertain_then_reconciles(self):
        eng, store, sheets = make_engine()
        pid = setup_active(eng)
        aid = "action-timeout-1"
        sheets.fail_next_write = RuntimeError("gateway timeout")
        r = eng.apply(prop_id=pid, op="add", item="Cereal", spec="2",
                      tab="breakfast items", action_id=aid)
        self.assertFalse(r["ok"])
        self.assertEqual(r["state"], "uncertain")
        # reconcile: re-read shows before (3) -> safe re-execute -> confirmed
        rec = eng.reconcile(aid)
        self.assertTrue(rec["ok"], rec)
        self.assertEqual(rec["state"], "confirmed")

    def test_uncertain_neither_before_nor_after_needs_operator(self):
        eng, store, sheets = make_engine()
        pid = setup_active(eng)
        aid = "action-uncertain-1"
        r = eng.apply(prop_id=pid, op="add", item="Cereal", spec="2",
                      tab="breakfast items", action_id=aid)
        self.assertTrue(r["ok"])
        # someone else edited the cell to a value matching neither before nor after
        row = r["row"]
        tab_v = eng._verbatim(eng._get_tracker(pid), "breakfast items")
        sheets.sheets[tab_v][row - 1][1] = 77
        rec = eng.reconcile(aid)
        self.assertFalse(rec["ok"])
        self.assertEqual(rec["state"], "uncertain")
        self.assertIn("operator", rec["message"])


class TestConcurrency(unittest.TestCase):
    class RaceSheet(inv.FakeSheet):
        """Tamper with a cell the first time it is re-read (simulating a
        concurrent writer landing between the initial grid read and the
        pre-write conflict check)."""

        def __init__(self, sheets, tamper):
            super().__init__(sheets)
            self.tamper = tamper  # (tab, row, value)

        def read_cell(self, sid, tab, row, col="B"):
            if self.tamper:
                t_tab, t_row, t_val = self.tamper
                rows = self._tab(t_tab)
                rows[t_row - 1][1] = t_val
                self.tamper = None
            return super().read_cell(sid, tab, row, col)

    def test_conflict_detected_on_prewrite_reread(self):
        # bath towels in laundry room originally at 5, row 2
        sheets = self.RaceSheet(make_sheet(), tamper=("Laundry Room", 2, 999))
        eng = inv.Engine(inv.InMemoryStore(), sheets)
        pid = setup_active(eng)
        r = eng.apply(prop_id=pid, op="add", item="bath towels", spec="2",
                      tab="laundry room", action_id="race-1")
        self.assertFalse(r["ok"])
        self.assertEqual(r["state"], "uncertain")
        self.assertIn("conflict", r["message"])
        # the sheet value must be unchanged (999), not 7
        self.assertEqual(sheets.sheets["Laundry Room"][1][1], 999)

    def test_append_does_not_duplicate_on_replay(self):
        eng, store, sheets = make_engine()
        pid = setup_active(eng)
        aid = "append-replay-1"
        r1 = eng.apply(prop_id=pid, op="add", item="Pillows", spec="4",
                       tab="laundry room", action_id=aid)
        self.assertTrue(r1["ok"])
        r2 = eng.apply(prop_id=pid, op="add", item="Pillows", spec="4",
                       tab="laundry room", action_id=aid)
        self.assertTrue(r2["ok"])
        grid = eng._read_grid(eng._get_tracker(pid), "laundry room")
        pillows = [i for i in grid["items"] if i["name"].strip() == "Pillows"]
        self.assertEqual(len(pillows), 1)


class TestQuantityAndUnits(unittest.TestCase):
    def test_negative_stock_asks_question_not_floored(self):
        eng, store, sheets = make_engine()
        pid = setup_active(eng)
        r = eng.apply(prop_id=pid, op="subtract", item="Hand Soap", spec="10",
                      tab="laundry room", action_id="neg-1")
        self.assertFalse(r["ok"])
        self.assertEqual(r["state"], "awaiting_operator")
        self.assertIsNotNone(r.get("question"))
        # value must NOT have been written (still 4)
        self.assertEqual(sheets.sheets["Laundry Room"][2][1], "4")

    def test_non_numeric_and_negative_amounts_rejected(self):
        eng, store, sheets = make_engine()
        pid = setup_active(eng)
        with self.assertRaises(ValueError):
            inv.parse_quantity("-3")
        with self.assertRaises(ValueError):
            inv.parse_quantity("abc")
        with self.assertRaises(ValueError):
            inv.parse_quantity("1e999")  # inf

    def test_unit_recorded_then_incompatible_change_rejected(self):
        eng, store, sheets = make_engine()
        pid = setup_active(eng)
        # first write establishes unit "rolls"
        r1 = eng.apply(prop_id=pid, op="add", item="Cereal", spec="1 roll",
                       tab="breakfast items")
        self.assertTrue(r1["ok"])
        # incompatible unit now rejected
        r2 = eng.apply(prop_id=pid, op="add", item="Cereal", spec="2 boxes",
                       tab="breakfast items")
        self.assertFalse(r2["ok"])
        self.assertIn("unit mismatch", r2["message"])

    def test_fractional_quantity_flows_through(self):
        eng, store, sheets = make_engine()
        pid = setup_active(eng)
        r = eng.apply(prop_id=pid, op="set", item="Applw Juice", spec="2.5",
                      tab="breakfast items")
        self.assertTrue(r["ok"])
        self.assertEqual(r["after"], 2.5)


class TestThresholds(unittest.TestCase):
    def test_set_threshold_and_running_low(self):
        eng, store, sheets = make_engine()
        pid = setup_active(eng)
        st = eng.set_threshold(pid, "cereal", "5", reorder="10")
        self.assertTrue(st["ok"])
        # cereal is at 3 -> below 5 -> low
        q = eng.query(prop_id=pid, low=True)
        low_items = {r["item"]: r for r in q["rows"]}
        self.assertIn("Cereal", low_items)

    def test_reorder_must_be_ge_low(self):
        eng, store, sheets = make_engine()
        pid = setup_active(eng)
        r = eng.set_threshold(pid, "cereal", "10", reorder="5")
        self.assertFalse(r["ok"])


class TestDynamicRangeAndAppendRaces(unittest.TestCase):
    def test_find_used_range_drops_trailing_empty_rows(self):
        rows = [list(HEADER), ["A", "1"], ["B", "2"],
                ["", ""], ["", ""], ["", ""]]
        used = inv.find_used_range(rows)
        self.assertEqual(len(used), 3)  # header + 2 items

    def test_sheet_over_200_rows_reads_correctly(self):
        # 250 items in one tab; engine must read dynamically, not A2:B200
        tabs = make_sheet()
        rows = [list(HEADER)] + [[f"Item {i}", str(i)] for i in range(1, 251)]
        tabs["Maintenance Room"] = rows
        eng = inv.Engine(inv.InMemoryStore(), inv.FakeSheet(tabs))
        pid = setup_active(eng)
        grid = eng._read_grid(eng._get_tracker(pid), "maintenance room")
        self.assertEqual(len(grid["items"]), 250)

    def test_append_lands_on_first_empty_row_after_used_range(self):
        eng, store, sheets = make_engine()
        pid = setup_active(eng)
        r1 = eng.apply(prop_id=pid, op="add", item="Pillows", spec="4",
                       tab="laundry room")
        r2 = eng.apply(prop_id=pid, op="add", item="Blankets", spec="2",
                       tab="laundry room")
        self.assertTrue(r1["ok"] and r2["ok"])
        grid = eng._read_grid(eng._get_tracker(pid), "laundry room")
        names = [i["name"] for i in grid["items"]]
        self.assertIn("Pillows", names)
        self.assertIn("Blankets", names)
        # no duplicate row for the same logical item
        self.assertEqual(names.count("Pillows"), 1)


class TestTrackerLifecycle(unittest.TestCase):
    def test_relink_requires_confirm(self):
        eng, store, sheets = make_engine()
        pid = setup_active(eng, "Sunrise Inn")
        r = eng.relink(pid, "https://docs.google.com/spreadsheets/d/NEWSHEET123456/edit")
        self.assertTrue(r["ok"])
        # tracker still active against old sheet until confirm
        t = eng._get_tracker(pid)
        self.assertEqual(t["phase"], "active")
        self.assertIsNotNone(t["pending"])
        self.assertNotEqual(t["sheet_id"], "NEWSHEET123456")
        c = eng.confirm(pid)
        self.assertTrue(c["ok"])
        t = eng._get_tracker(pid)
        self.assertEqual(t["sheet_id"], "NEWSHEET123456")
        self.assertIsNone(t["pending"])

    def test_rename_and_deactivate_and_remove(self):
        eng, store, sheets = make_engine()
        pid = setup_active(eng, "Sunrise Inn")
        rn = eng.rename(pid, "Sunrise Inn West")
        self.assertTrue(rn["ok"])
        self.assertEqual(eng._get_tracker(pid)["display_name"], "Sunrise Inn West")
        deact = eng.deactivate(pid, confirm=True)
        self.assertTrue(deact["ok"])
        self.assertEqual(eng._get_tracker(pid)["phase"], "deactivated")
        rm = eng.remove_tracker(pid, confirm=True)
        self.assertTrue(rm["ok"])
        self.assertIsNone(eng._get_tracker(pid))

    def test_deactivate_without_confirm_is_dry_run(self):
        eng, store, sheets = make_engine()
        pid = setup_active(eng)
        r = eng.deactivate(pid)
        self.assertFalse(r["ok"])
        self.assertTrue(r.get("dry_run"))
        self.assertEqual(eng._get_tracker(pid)["phase"], "active")

    def test_unknown_tab_rejected(self):
        eng, store, sheets = make_engine()
        pid = setup_active(eng)
        r = eng.apply(prop_id=pid, op="add", item="Pillows", spec="4",
                      tab="rooftop bar")
        self.assertFalse(r["ok"])
        self.assertIn("unknown location", r["message"])


class TestPureHelpers(unittest.TestCase):
    def test_normalize(self):
        self.assertEqual(inv.normalize("  Hello   World!! "), "hello world")
        self.assertEqual(inv.normalize(None), "")

    def test_extract_sheet_id(self):
        self.assertEqual(
            inv.extract_sheet_id("https://docs.google.com/spreadsheets/d/ABC123/edit#gid=0"),
            "ABC123")
        self.assertEqual(inv.extract_sheet_id("ABC123"), "ABC123")

    def test_canonical_tab_fixes_typo_never_promotes(self):
        self.assertEqual(inv.canonical_tab("2nd Floor Stoage 1"),
                         "2nd floor storage 1")
        self.assertEqual(inv.canonical_tab("1st Floor Storage "),
                         "1st floor storage")

    def test_format_number(self):
        self.assertEqual(inv.format_number(3.0), 3)
        self.assertEqual(inv.format_number(2.5), 2.5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
