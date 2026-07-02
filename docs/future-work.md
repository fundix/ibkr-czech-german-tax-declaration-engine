# Future Work

## High priority
- treaty-by-treaty FTC verification
- pre-2014 time test rule
- uniform FX mode

## Medium
- DE plugin documentation
- API reference

## Low
- UI / web interface
## Open audit findings (AUDIT_REPORT_2026-07, awaiting design/data)

- **M11 — cash-in-lieu for split fractions**: a reverse split leaving a
  fraction keeps it in the ledger forever; the CIL cash is never taxed and
  the fraction's cost basis is lost. Needs a sample of how CIL appears in
  real IBKR data (cash transaction row vs. corporate action detail) to wire
  the fraction disposal.
- **M17/M18 — mixed FX dates on premium/repayment components**: an option
  premium folded into the stock basis carries the ECB rate of the option's
  opening day but is converted to CZK at the stock trade date (capital
  repayments analogously). Error ∝ component size × EUR/CZK drift between
  the dates. Proper fix = per-component cash-flow dates on RealizedGainLoss.
- **L14 — WHT linker tolerances**: amount-relationship windows are generous
  (up to 100 % of income in the proximity strategy); tightening should be
  validated against real statements to avoid breaking legitimate links.
