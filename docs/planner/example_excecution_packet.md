> **Open questions** (shown on the board, never sent to the coder)
> - Should a sent invoice still allow duplicating lines into a credit note, or is duplication simply off once the invoice is locked? Assumed: off.

# Task
Add duplicate invoice line functionality.

## Goal
Allow a user to duplicate an existing invoice line from the invoice editor. The copy appears
immediately below the source line.

## Current behavior
Invoice lines can be added, edited and deleted, but not duplicated. A new line is always appended
at the end, and line order is only stored when the invoice is saved.

## Decisions
- The copy is made through the existing line-create flow -- one validation path, over a client-side clone that skips it.
- Product, description, quantity, unit, unit price and VAT are copied; the line ID and timestamps are not -- the copy is a new line, over a shallow copy that shares identity.
- The copy is inserted directly after its source -- what the user expects, over appending at the end like Add does.
- Duplication is unavailable on locked or sent invoices, using the same lock rule that already disables editing -- over a separate rule that could drift from it.
- The invoice API's response shapes stay as they are -- other clients read them.
- The action sits in the line row's existing action menu -- over a new toolbar button.

## Acceptance criteria
1. Clicking Duplicate on a line creates exactly one new line, directly below it.
2. The new line's product, description, quantity, unit, unit price and VAT equal the source's, and its ID differs.
3. After saving and reloading the invoice, the copy is still there, in the same position.
4. On a locked or sent invoice, no Duplicate action is offered and none can be triggered.
5. Editing the copy does not change the source line.
