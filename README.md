# Inventory Tracking

A Kolo skill that tracks hotel inventory in a Google Sheets template from
casual conversation — no tool name needed. The owner says "add 5 bath towels to
the laundry room" or "how many towels do we have", and Kolo resolves the
property and location, matches the item, updates the quantity, and confirms in
one line.

## What it does

- **First-time setup, required.** Before any inventory write, Kolo asks for the
  property's Google Sheet link (created from the approved inventory template),
  validates that it can reach the sheet, and confirms the expected tabs and
  columns exist. Setup is two-phase: a validated sheet is stored as a pending
  tracker and goes live only after the owner confirms.
- **Per-property trackers.** Each validated sheet is stored against its
  property and reused on every later request — never guessed.
- **Deterministic engine.** `tools/inventory.py` owns matching, quantity math,
  write verification, idempotent action journaling, and concurrency control.
  Routing and credentials are resolved at runtime by the engine — this repo
  contains no account names, gateway hosts, or secrets.
- **Structure validation.** Column A = item name, Column B = quantity on hand,
  one tab per storage location. A sheet that's inaccessible, moved, or
  mismatched is reported clearly with one actionable question — inventory is
  never half-applied.
- **Conversational updates.** Add / receive, use / remove, set-to, query, and
  low-stock phrasing are matched against a list of deterministic subcommands.

## Files

```
SKILL.md                       ← main skill documentation
README.md                      ← this file
.gitignore                     ← registry metadata + local secrets excluded
tools/inventory.py             ← the deterministic engine (stdlib only)
tests/test_inventory.py        ← unittest suite (also pytest-compatible)
```

## Development

```bash
# run the test suite
python3 -m unittest discover -s tests -v

# read-only readiness check (needs a configured Kolo environment)
python3 tools/inventory.py doctor
```

## Install

Publish or install through the Kolo Skills Marketplace:

```bash
openclaw skills install inventory-tracking
```
