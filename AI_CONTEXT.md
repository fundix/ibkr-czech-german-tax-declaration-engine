# AI Context (Read Before Making Changes)

This project enforces strict separation between:
- core logic (broker-agnostic, country-agnostic)
- country plugins (CZ, DE)
- output layers (form mapping, exporters)

Before making any change, you MUST follow:
- docs/engineering-decisions.md
- docs/cz-tax-policy.md
- docs/ai-guidelines.md

## Critical Rules

1. Never add country-specific logic into core.
2. Never recompute tax logic in exporters or form mapping.
3. Always operate on CzTaxItem (single source of truth).
4. Never lose data (especially WHT, pending items).
5. Preserve auditability (event_id, FX date, source_country).
6. tax_liability.py is the only source of final tax numbers.

## If unsure:
Prefer adding a new pipeline step instead of modifying existing logic.