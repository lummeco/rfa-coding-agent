# Task
Add duplicate invoice line functionality.

## Goal
Allow a user to duplicate an existing invoice line from the invoice editor.
The new line should appear immediately below the source line.

## Current behavior
Invoice lines can be added, edited and deleted, but not duplicated.

## Required behavior
- Add a duplicate action to each editable invoice line.
- Copy:
  - product
  - description
  - quantity
  - unit
  - unit price
  - VAT
- Insert the copy immediately below the original.
- Generate a new line ID.
- Do not allow duplication when the invoice is locked/sent.

## Constraints
- Preserve the existing invoice editing architecture.
- Do not introduce a new backend endpoint unless necessary.
- Follow existing UI component patterns.
- Do not change existing API response shapes.
- Avoid unrelated refactoring.

## Relevant areas
Likely relevant:
- invoice editor
- invoice line row component
- invoice state/update logic
- invoice locking rules

These are hints. Inspect the repository before deciding what needs changing.

## Acceptance criteria
1. Clicking Duplicate creates exactly one new line.
2. Copied business fields equal the source line.
3. New line has its own identity.
4. New line appears directly after the source.
5. Locked/sent invoices cannot duplicate lines.
6. Existing add/edit/delete functionality still works.
7. Existing tests pass.
8. Add or update tests covering duplication.

## Verification
Run the relevant unit/integration tests and the repository's normal
lint/typecheck/test commands for the affected area.

## Non-goals
- Redesigning the invoice editor.
- Changing invoice persistence architecture.
- Adding bulk duplication.
