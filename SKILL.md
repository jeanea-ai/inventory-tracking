---
name: inventory-tracking
description: "Track hotel inventory in the linked Google Sheets template from casual conversation. Triggers on stock/supply talk ('add 5 bath towels', 'we used 3 rolls', 'how many do we have', 'what's running low'). Requires first-time per-property sheet setup before any write; a deterministic engine performs matching, quantity math, write verification, and concurrency control."
version: 1.2.0
tags: [inventory, stock, google-sheets, hotel, supplies, setup]
---

# Inventory Tracking

A deterministic engine (`tools/inventory.py`) owns every inventory change.
Your only job is to map the owner's words to one subcommand and relay the
result. Never hand-edit the sheet, never guess a spreadsheet, never guess an
item.

Run every command from this skill's directory:

```bash
python3 tools/inventory.py <subcommand> [flags]
```

All output is one line of JSON. Read its `state`, not the exit code:

- `confirmed` → report `message`.
- `awaiting_operator` → ask the owner the `question` (one short question) and stop.
- `uncertain` → run `reconcile --action-id <id>` before retrying.
- `attempting` → transient; wait for the lease, then re-check or `reconcile`.

## Golden rules

- Never guess a spreadsheet or an item.
- No write without an **active** tracker; a pending setup is read/write-blocked.
- Every action is journaled and idempotent; the engine re-reads after every
  write to verify before it ever reports success.
- A quantity that would go negative, an ambiguous item, or a unit mismatch is
  returned as `awaiting_operator` — relay the question and wait, never decide.

## First-time setup (two-phase, per property)

1. Owner names a property and pastes its Google Sheets link (created from the
   approved inventory template):

   ```bash
   python3 tools/inventory.py setup --sheet-id "<id or link>" --property "Sunrise Inn"
   ```

   This validates access and structure against the template and stores a
   **pending** tracker. An inaccessible sheet, a wrong/moved link, or tabs/
   columns that don't match the template each return an explanation and one
   actionable question — never a guess.

2. Owner confirms → activate; owner declines → discard:

   ```bash
   python3 tools/inventory.py confirm --property-id <id>
   python3 tools/inventory.py reject --property-id <id>
   ```

   After `confirm`, echo the property + sheet title + tab list. Trackers are
   stored as Kolo `skill.inventory-tracking` records; routing and credentials
   are resolved by the engine at runtime, so nothing account- or
   gateway-specific lives in this file.

## Daily use

| Owner says | Command |
|---|---|
| "set X to N" / "we have N X" | `apply --item X --op set --spec N [--tab loc]` |
| "add N X" / "got a delivery of N X" | `apply --item X --op add --spec N [--tab loc]` |
| "use/remove N X" / "sold N X" | `apply --item X --op subtract --spec N [--tab loc]` |
| "how many X" / "what's in <loc>" | `query [--item X] [--tab loc]` |
| "what's running low" | `query --low` |

`--spec` takes `"5"`, `"5 rolls"`, or `"rolls 5"` (amount plus optional unit).
Use `--amount 5 --unit rolls` as an alternative. Omit `--tab` when the item
lives in one location; if the item appears in several tabs the engine asks
which one. `--property-id` is optional when exactly one active tracker exists;
otherwise resolve it with `list-trackers` and pass it explicitly.

## Tracker management

```bash
python3 tools/inventory.py list-trackers
python3 tools/inventory.py relink --property-id <id> --sheet-id "<new sheet>"  # pending until confirm
python3 tools/inventory.py rename --property-id <id> --name "New Name"
python3 tools/inventory.py deactivate --property-id <id>   # soft; add --confirm to apply
python3 tools/inventory.py remove-tracker --property-id <id>  # destructive; add --confirm
```

`deactivate` and `remove-tracker` are dry runs without `--confirm`; run with
`--confirm` only after the owner explicitly approves.

## Readiness

```bash
python3 tools/inventory.py doctor
```

Read-only check: Python version, Kolo CLI, routing, credentials, record store,
and each tracker's sheet. Use before a session of writes; if `status` is
`not-ready`, fix the named check (or re-run setup for a drifted sheet) before
writing.

## Before writing — two checks

1. `doctor` returns `ready`.
2. The property has an **active** tracker (`list-trackers`); none for this
   property → run setup (ask for the link) and stop.

## Errors & questions

- No active tracker for the property → setup (ask for the link) and stop.
- Sheet 403 → owner reconnects (Settings → Integrations); 404 → corrected link.
- Structure/tabs/columns mismatch → the engine says exactly what is off; re-run setup.
- Ambiguous or would-go-negative → relay the engine's question; never decide.

## Approval

Writing the owner's own linked, validated tracker at their direct request is
the instruction — no separate strategic brief. Any outbound action (ordering,
emailing a vendor) still follows the normal approval flow.
