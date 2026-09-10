---
name: inventory-tracking
description: "Track and update hotel inventory in the Google Sheets inventory tracker from casual conversation — no tool name needed. Trigger when the owner mentions stock or supplies: 'add 5 bath towels to the laundry room', 'we got a delivery of paper towels', 'we used 3 rolls', 'set the coffee cups to 40', 'we have 12 plates now', 'how many towels do we have', 'what's running low', 'restock the breakfast items', 'take inventory', 'count the pool supplies', 'log that shipment', 'update stock'. A required first-time setup links the property's Google Sheet (created from the approved inventory template) before any write; Kolo validates access and structure and never guesses which spreadsheet to use."
version: 1.1.0
tags: [inventory, stock, google-sheets, hotel, supplies, setup]
---

# Inventory Tracking

Track hotel inventory in the Google Sheets inventory template. The owner
describes inventory changes in plain English; you resolve the property and
location, match the item, update its quantity in the linked sheet, and confirm
in one line.

## First rule: never guess the spreadsheet

There is **no hardcoded/default spreadsheet**. Inventory is always tracked in a
sheet the owner has explicitly linked for a specific property/location. If no
linked sheet exists for the property in question (and the prompt does not
itself supply a fresh `docs.google.com/spreadsheets/d/<ID>` link), **do not
update anything** — run first-time setup and ask for the link. Guessing a
spreadsheet ID is the failure mode this skill exists to prevent.

## The per-property tracker (governed records)

Each property's linked sheet is stored as a governed record:

- **record-type:** `skill.inventory-tracking`
- **external-id:** the property slug (lowercase, kebab-case — e.g. `sunrise-inn`,
  `main-hotel`, `north-tower`)

The record `payload` holds:

```json
{
  "spreadsheet_id": "<ID>",
  "title": "<sheet title>",
  "property": "<human property/location name>",
  "tabs": ["1st Floor Storage ", "Breakfast Items ", "..."],
  "sheet_lineage": "approved inventory template"
}
```

Read the tracker for a property before every request:

```bash
kolo record-list --record-type skill.inventory-tracking
# then, for the one property you need:
kolo record-get --record-type skill.inventory-tracking --external-id <property-slug>
```

`record-list` returns metadata only; use `record-get` for the full payload.
If the property has no record, you must run setup — there is nothing to write
against and nothing to fall back to.

## First-time setup (required before any write)

If the resolved property has no linked tracker (or the prompt points at a sheet
you have never validated), walk setup one step at a time. **No inventory write
happens until setup completes.**

1. **Ask for the link.** Ask the owner for the Google Sheets link for the
   spreadsheet they created from the **approved inventory template**, and which
   property/location it is for. That is the one actionable question — ask it and
   stop. Do not go hunting through Drive and pick a sheet yourself.
2. **Resolve the property slug.** Turn the property/location they name into a
   stable kebab-case slug for `--external-id`.
3. **Extract the ID.** Take the segment after `/spreadsheets/d/` from the link.
4. **Validate access.** Resolve routing (`kolo integration-routing`), then read
   spreadsheet metadata and the sheet list through the gateway (below). A 403 or
   "no active connection" means Kolo cannot reach the sheet for this account —
   explain and guide the owner to **Settings → Integrations → Connect Google**,
   then stop. A 404 means the link is wrong or the sheet was moved/deleted.
5. **Validate structure.** Confirm each expected location tab exists and that
   **Column A = item name, Column B = quantity on hand**, with a header row 1.
   The approved template's tabs are:
   `1st Floor Storage `, `Breakfast Items `, `Laundry Room`, `Maintenance Room`,
   `Pool Supplies `, `2nd Floor Stoage 1`, `2nd Floor Storage 2`,
   `3rd Floor Storage 1`, `3rd Floor Storage 2`, `4th Floor Storage 1`,
   `4th Floor Storage 2`. If tabs or the A/B column layout do not match the
   template, do not proceed — tell the owner what is off and ask them to fix the
   sheet (or confirm a corrected link).
6. **Save the tracker.**

```bash
kolo record-upsert --record-type skill.inventory-tracking --external-id <property-slug> \
  --status active \
  --payload '{"spreadsheet_id":"<ID>","title":"<sheet title>","property":"<human name>","tabs":[...]}'
```

7. **Confirm before accepting updates.** Echo back the linked property/location
   and the sheet title (with its tab list) and get an explicit "yes" that this
   is the right sheet for that property. Only then is setup complete and the
   tracker live for future requests.

## Routing

Resolve the Google Sheets access path with `kolo integration-routing`. For this
owner Google Sheets routes through the **Maton gateway** (see the `api-gateway`
skill for full mechanics). All calls below use that path with
`Authorization: Bearer $MATON_API_KEY`. If a routing row instead names `gws`,
use `gws sheets` equivalents.

## Approval

Updating the owner's own inventory sheet at their direct request is the whole
point of this skill — the prompt itself is the instruction, so inventory
read/write to a **linked, validated** tracker runs directly without a separate
strategic brief. Any other sheet, an unvalidated link, or any outbound action
(ordering, emailing a vendor) still follows the normal approval flow.

## Workflow

1. **Resolve the property and tracker.** If the prompt names a property, look up
   `kolo record-get --record-type skill.inventory-tracking --external-id <slug>`.
   If the prompt supplies a `docs.google.com/spreadsheets/d/<ID>` link for a
   property with no tracker, that link is a *candidate* — run setup validation on
   it before writing, never accept it blind. If there is exactly one configured
   property and the prompt doesn't say otherwise, use it. If there are several
   and the prompt is ambiguous, ask which property — don't guess. **No tracker
   for the property → run setup (ask for the link), then stop.**
2. **Re-validate before every session of writes.** Confirm the sheet is still
   reachable and its tab/column structure still matches the template (a 404/403,
   a renamed/moved sheet, or changed headers invalidates the tracker). If it has
   drifted, do not write — explain and re-run setup.
3. **Resolve the location.** Match the owner's words against the tracker's tab
   names (case-insensitive; tolerate "storage"/"room"/floor typos). If ambiguous,
   read the sheet list and confirm — don't guess.
4. **Read current state:**
   `GET https://gateway.maton.ai/google-sheets/v4/spreadsheets/{SHEET_ID}/values/{SHEET}!A2:B200`
   (URL-encode the sheet name; keep the trailing space where present).
5. **Match the item.** Normalize both sides (lowercase, strip, collapse
   spaces). The template item names contain typos ("Quanity", "Applw Juice",
   "Cranberry Jucie", "facuet"), so use substring/fuzzy matching — exact
   substring first, then nearest edit distance. New item + no match → append a
   row. Adjust + no match → ask which item they mean, don't guess.
6. **Apply the change:**
   - **Set** — "set X to N", "we have N X", "X is at N" → write N.
   - **Add / receive** — "got N X", "N X arrived", "add N X", "delivered" →
     `quantity += N`. Blank quantity counts as 0.
   - **Use / remove** — "used N X", "take N X", "remove N X", "sold N X" →
     `quantity -= N`, floor at 0.
   - **Query** — "how many X", "what's in <location>", "running low", "report"
     → read only, report. Treat blank quantity as 0.
7. **Write back.** For an existing item, update column B:
   `PUT https://gateway.maton.ai/google-sheets/v4/spreadsheets/{SHEET_ID}/values/{SHEET}!B{row}?valueInputOption=USER_ENTERED`
   body `{"range": "{SHEET}!B{row}", "majorDimension": "ROWS", "values": [[<qty>]]}`.
   For a new item, append:
   `POST https://gateway.maton.ai/google-sheets/v4/spreadsheets/{SHEET_ID}/values/{SHEET}!A{nextRow}:append?valueInputOption=USER_ENTERED`
   body `{"values": [[<item>, <qty>]]}`.
8. **Verify.** Re-read the affected cell(s) and confirm the written value before
   telling the owner it's done. Never report success from the write response
   alone.

## Errors & access problems

If the request cannot be completed, say plainly why and what one thing is
needed next — never guess and never half-apply:

- **No linked sheet for the property** → run setup; ask for the Google Sheets
  link from the approved template.
- **Sheet inaccessible (403)** → reconnect in Settings → Integrations → Connect
  Google.
- **Link wrong / sheet moved (404)** → ask for a corrected link.
- **Structure doesn't match the template** → explain which tab/column is off and
  ask them to fix it (tabs and A/B columns must match the template).
- **Ambiguous property/location/item** → ask one short clarifying question.

## Success format

One short line naming the property, location, item, and new quantity —
"Sunrise Inn — Laundry Room: Bath Towels → 14 on hand." For a query, list item
+ quantity per line. When asked what's low, flag anything at 0 or blank.
