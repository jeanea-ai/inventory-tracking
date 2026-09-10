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
  columns exist.
- **Per-property trackers.** Each validated sheet is stored against its
  property via Kolo's `kolo record-*` governed store, and reused on every
  later request — never guessed.
- **Structure validation.** Column A = item name, Column B = quantity on hand,
  one tab per storage location. A sheet that's inaccessible, moved, or
  mismatched is reported clearly with one actionable question — inventory is
  never half-applied.
- **Conversational updates.** Add / receive, use / remove, set-to, and query
  phrasing are matched, with fuzzy item matching that tolerates the template's
  typos.

## Files

```
SKILL.md                       ← main skill documentation
README.md                      ← this file
.gitignore                     ← registry metadata + local secrets excluded
```

## Install

Publish or install through the Kolo Skills Marketplace:

```bash
openclaw skills install inventory-tracking
```

> The `SKILL.md` references internal gateway routing for the owner's Google
> Sheets connection. This repo is kept private by default.
