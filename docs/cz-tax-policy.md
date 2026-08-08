# CZ Tax Policy Reference

Definitive reference for Czech tax rules as implemented in this project. Each rule is marked:
- **IMPLEMENTED** — fully functional with tests
- **PARTIAL** — functional but with known simplifications
- **NOT IMPLEMENTED** — architecture prepared, logic missing

---

## FX Policy

| Rule | Status |
|------|--------|
| Daily ČNB rate as default | **IMPLEMENTED** |
| Per-event conversion (not aggregate) | **IMPLEMENTED** |
| Direct foreign→CZK (not via EUR intermediate) for §8 events | **IMPLEMENTED** |
| EUR→CZK for RGL disposals (core pipeline converts to EUR first) | **PARTIAL** — EUR intermediate unavoidable for disposal amounts |
| Disposal cost basis converted at ACQUISITION-date rate, proceeds at DISPOSAL-date rate (captures FX gain/loss; per NSS 2 Afs 4/2019-35) | **IMPLEMENTED** |
| Weekend/holiday fallback to last valid rate | **IMPLEMENTED** — `max_fallback_days=7` |
| Jednotný kurz (annual uniform rate) | **NOT IMPLEMENTED** — `CzFxMode.UNIFORM` raises `NotImplementedError` |
| FxConversionRecord audit trail on every conversion | **IMPLEMENTED** |

---

## §8 ZDP — Příjmy z kapitálového majetku

| Rule | Status |
|------|--------|
| Dividends (DIVIDEND_CASH) → CZ_8_DIVIDENDS | **IMPLEMENTED** |
| Fund distributions → CZ_8_DIVIDENDS | **IMPLEMENTED** |
| Interest (INTEREST_RECEIVED) → CZ_8_INTEREST | **IMPLEMENTED** |
| §8 income is always taxable (no time test, no annual limit) | **IMPLEMENTED** |
| WHT linked to parent dividend/interest via event ID, asset+date, or ±3 day proximity | **IMPLEMENTED** |
| Unlinked WHT → standalone item (item_type=OTHER), NOT counted as income | **IMPLEMENTED** |

---

## §10 ZDP — Ostatní příjmy

| Rule | Status |
|------|--------|
| Stocks, bonds, ETFs → CZ_10_SECURITIES | **IMPLEMENTED** |
| Options, CFDs → CZ_10_OPTIONS | **IMPLEMENTED** |
| Options are derivative instruments, NOT securities under §4/1/u | **IMPLEMENTED** — no time test for options |
| PrivateSaleAsset → CZ_10_SECURITIES | **IMPLEMENTED** |

---

## Time Test (§4 odst. 1 písm. u ZDP)

The letter is year-mapped in `CzTaxConfig.paragraph_4_letters_by_year` — it was
renumbered, and this engine cited písm. w) until a tax advisor corrected it on
2026-08-05 (time test = u, the 100k limit = t). Years before the earliest
confirmed entry cite the paragraph without a letter rather than guessing.

| Rule | Status |
|------|--------|
| Securities held > 3 years (1095 days) → exempt | **IMPLEMENTED** |
| Threshold: `holding_period_days > holding_test_years × 365` (strict >) | **IMPLEMENTED** |
| Missing acquisition_date → PENDING_MANUAL_REVIEW, conservatively taxable | **IMPLEMENTED** |
| Unparseable dates → PENDING_MANUAL_REVIEW | **IMPLEMENTED** |
| Holding period computed from acquisition_date and event_date when not preset | **IMPLEMENTED** |
| Time test configurable (enable/disable, custom years) | **IMPLEMENTED** |
| Pre-2014 acquisition rule (6-month test) | **NOT IMPLEMENTED** |
| Fund-specific time test rules | **NOT IMPLEMENTED** |

---

## Annual Exempt Limit (§4 odst. 1 písm. t ZDP, 2025+ Amendment)

| Rule | Status |
|------|--------|
| Threshold: CZK 100,000 of gross disposal proceeds | **IMPLEMENTED** |
| Metric: `proceeds_czk` (gross proceeds), NOT gain/loss | **IMPLEMENTED** |
| Applies only to SECURITY_DISPOSAL items | **IMPLEMENTED** |
| Options NOT eligible | **IMPLEMENTED** |
| Dividends/interest NOT eligible | **IMPLEMENTED** |
| ALL disposal proceeds counted in the sum, time-test-exempt included | **IMPLEMENTED** — the two exemption titles cannot be combined; netting exempt sales out of the sum under-taxed the year (per advisor 2026-08-05) |
| Only still-taxable disposals can be flipped by this title | **IMPLEMENTED** |
| All-or-nothing: if total exceeds threshold, ALL eligible items taxable | **IMPLEMENTED** |
| Items without `proceeds_czk` (no FX converter) excluded from test | **IMPLEMENTED** |
| Configurable (enable/disable, custom threshold) | **IMPLEMENTED** |

---

## Mergers — stock-for-stock exchange (§23b / §23c ZDP)

Per a tax advisor's answer of 2026-08-05 (question in
`docs/otazky-danovy-poradce-fuze.md`) these transactions split in two, with
opposite consequences, and **the statement cannot tell them apart** — what
decides is the tax residence and legal form of the companies involved, not the
venue, ticker or ISIN.

| Rule | Status |
|------|--------|
| Regime recorded per event in `cache/merger_treatments.json` (hand-edited, like the classification cache) | **IMPLEMENTED** |
| Unclassified merger → run refuses, naming the key, the choices and the evidence needed | **IMPLEMENTED** |
| Regime → ledger mechanics (§23b/§23c → carry-over; otherwise → taxable disposal) | **IMPLEMENTED** |
| Applying carry-over (lot transfer, cost and holding period preserved) | **IMPLEMENTED** — one target lot per source lot; total basis verbatim, unit re-derived; `acquisition_date` = merger date, `holding_period_start` carried |
| Carried quantity rescaled by the ratio | **IMPLEMENTED** — largest-remainder allocation against one whole-transfer target, residual to the oldest holding (per-lot rounding drifts 1e-8, and a shortfall aborts the run) |
| Short lots carried with their opening date | **IMPLEMENTED** — the proceeds figure is the tax attribute; leaving them behind breaks the eventual cover |
| Merger applied twice / self-merger / receiving leg / empty source / bad ratio or date | **IMPLEMENTED** — each refuses before either ledger is touched |
| Fractional credited share (cash in lieu) | **NOT IMPLEMENTED** — refused. Makes carry-over unusable for a ratio that does not divide the position evenly; the fix is to book the fraction's disposal from the cash-in-lieu row |
| Prior-year merger (SOY replay) | **NOT IMPLEMENTED** — refused (was silently dropped, which handed the target an invented holding start and a confident taxable verdict) |
| Applying a taxable disposal (RGL at the consideration's fair value) | **IMPLEMENTED** — one RGL per source lot at the new share's close on the merger date (§19 look-back), converted at the CLOSE date; replacement lots start a fresh basis and holding period |
| Price source reachable by the engine | **IMPLEMENTED** — `src/utils/historical_price_provider.py`, injected into the processor context like the FX converter; `src/webapp/quotes.py` keeps the live-quote service and re-exports it |
| Short position on the disposing side of a taxable merger | **NOT IMPLEMENTED** — refused; realising a short against the acquirer's price is not the same transaction as closing it |
| `holding_period_start` separate from `acquisition_date` | **IMPLEMENTED** — see below |
| FIFO queue position of a carried lot | back of the queue (`acquisition_date` = merger date), holding-seniority tie-break among carried lots. Matches the broker; if the advisor disagrees the remedy is LIFO/OPTIMAL, not a second sort rule |
| Cash doplatek / `cash in lieu` for fractional shares | **NOT IMPLEMENTED** |
| §23d odst. 1 notice to the tax office before the transaction | **NOT IMPLEMENTED** — compliance flag only |
| Merger replayed during SOY reconstruction | **NOT IMPLEMENTED** — `initialize_lots_from_soy` handles splits and stock dividends, not mergers |

### Why the holding period needs its own field

For a qualified transaction the advisor confirmed two things that pull in
opposite directions:

- the previous holding period **counts** toward the 3-year test, and
- the pre-2014 six-month grandfathering **does not** carry to a share issued
  after 2014 (NSS 3 Afs 249/2024-45).

`time_test.py` picks the regime from `acquisition_date < 2014-01-01` and used to
measure the period from the same field. Carrying an old date over would keep the
holding period correctly but also wrongly hand the new share the pre-2014
six-month test. The two decisions now have two dates:

| Field | Question it answers | Used by |
|---|---|---|
| `acquisition_date` | when was THIS security acquired | regime selection, FIFO consumption order, "Nabytí" in every report |
| `holding_period_start` | when did the holding begin | `holding_period_days`, the deadline, the CZK cost-basis FX date |

Both live on `FifoLot`, `RealizedGainLoss` and `CzTaxItem`, each with its own
`*_estimated` flag; `holding_period_start` normalises to `acquisition_date` when
absent, so it is never null downstream. `time_test_deadline()` takes both as
**required keyword arguments** on purpose — a default would let a call site
silently measure from the wrong date, and the distinction is invisible in the
common case where they coincide.

`ShortFifoLot` deliberately keeps a single `opening_date`: a short position can
never pass the holding-period test, so the evaluator short-circuits before any
date is read.

**Open questions this raised** (worth a line in the advisor's next round):

- **Resolved:** IBKR's `HoldingPeriodDateTime` is no longer used as a date. It is
  a **US-rules** holding basis — pushed forward by wash sales, which Czech law
  has no equivalent of, and carried back over IRC §368 reorganisations, which the
  advisor classifies as a *taxable* pozbytí in CZ — so it is wrong for both Czech
  questions. `acquisition_date` now comes from `OpenDateTime` alone. The broker
  date is still read, because a divergence carries information: when the two
  differ the lot's `holding_period_start_estimated` is set, and when
  `OpenDateTime` is missing entirely both flags are set, so the time test routes
  the item to `PENDING_MANUAL_REVIEW` instead of deciding from a date the data
  does not support. Both cases are logged at the point of seeding with the
  specific cause. No effect on this account: all 175 lot rows report the two
  dates identically.
- Should a carried-over lot keep its old FIFO queue position, or go to the back?
  The engine keys consumption order on `acquisition_date` (back of the queue),
  which matches the broker; `holding_period_start` is not used for ordering.
- The pre-2014 branch measures from `holding_period_start` too, for consistency.
  That cannot change any figure reachable today (it needs a disposal on or
  before 2014-06-30), but it is an unverified extension of čl. II bod 5.

---

## Loss Offsetting (§10)

| Rule | Status |
|------|--------|
| Securities gains/losses netted separately | **IMPLEMENTED** |
| Options gains/losses netted separately | **IMPLEMENTED** |
| Combined §10 net = securities net + options net | **IMPLEMENTED** |
| Only `included_in_tax_base=True` items participate | **IMPLEMENTED** |
| Exempt losses do NOT reduce tax base | **IMPLEMENTED** |
| Pending items conservatively included with warning | **IMPLEMENTED** |
| Negative net floored at zero for tax base | **IMPLEMENTED** |
| Loss carryforward | **NOT IMPLEMENTED** |
| Expense deduction (§10/4 ZDP) | **NOT IMPLEMENTED** — `cost_basis_czk` on items, rule not applied |

---

## Foreign Tax Credit (§38f ZDP)

| Rule | Status |
|------|--------|
| Per-item preliminary cap: `min(wht_paid, cap_rate × gross_income)` | **IMPLEMENTED** |
| Default cap rate: 15% (`default_max_credit_rate`) | **IMPLEMENTED** |
| Per-country treaty cap: `country_credit_caps` dict | **IMPLEMENTED** — 16 treaties with Sb. citations (12 verified 2026-07-03, SE/CN/HK/KZ added 2026-08-08) |
| Missing source_country → PENDING_MANUAL_REVIEW, default cap applied | **IMPLEMENTED** |
| No linked WHT → zero credit record, no crash | **IMPLEMENTED** |
| Multiple WHT records on one item: first source_country used for cap | **IMPLEMENTED** |
| FTC invariant: `paid = creditable + non_creditable` | **IMPLEMENTED** |
| Split of `non_creditable` into `treaty_excess_ftc` + `uncredited_ftc` | **IMPLEMENTED** — see below |
| Final FTC = `min(preliminary, czech_tax_on_foreign_income)` | **IMPLEMENTED** |
| Czech tax on foreign income: proportional method `gross_tax × (foreign_income / combined_base)` | **IMPLEMENTED** |
| Per-country FTC aggregation in summary | **IMPLEMENTED** |
| Treaty-by-treaty verification of cap rates | **IMPLEMENTED** for the 12 treaties in `country_credit_caps`; others fall back to the 15% default |

### What "not creditable" is made of

`non_creditable_ftc` (`paid − final_creditable`) holds two amounts with
completely different consequences, so it is reported as both:

| Field | Meaning | Where it goes |
|-------|---------|---------------|
| `treaty_excess_ftc` | `paid − preliminary` — withheld **above** the treaty rate | **Not on the return.** §38f odst. 5 counts foreign tax only up to the treaty rate, so this is a refund claim against the source state. Usually signals a missing form (e.g. W-8BEN for US). |
| `uncredited_ftc` | `preliminary − final_creditable` — treaty-eligible credit lost to the §38f/1 proportional cap or the §38f/8 per-state cap | **Příloha 3, ř. 329** (defined as ř. 323 − ř. 328). Deductible as an expense under §24 odst. 2 písm. ch) in the following period. |

The two always sum back to `non_creditable_ftc`. Mapping the *total* onto
ř. 329 — as the engine did before — contradicts that line's own definition
and would let over-withheld foreign tax be carried forward as if it were a
Czech credit.

### A missing treaty cap understates the tax

A country absent from `country_credit_caps` falls back to
`default_max_credit_rate` (15 %). That default equals several real caps, so the
displayed number cannot be told apart from a verified one — which is why the
FTC record carries `cap_rate_defaulted` and the dividends page marks such rows.

Where the real treaty caps **lower**, the fallback credits too much and the
Czech tax comes out too low. Found on the real book 2026-08-08: SE, CN, HK and
KZ were all defaulting to 15 % against treaty caps of 10 / 10 / 5 / 10 %.
Sweden is the case that bit — its domestic kupongskatt is 30 % and relief at
source is usually not applied to a Czech retail holder, so the statement showed
30 % withheld and 15 % was being credited where the treaty allows 10 %. Fixing
it moved the 2025 credit from 209,33 to 139,55 CZK and the final tax from
28 850 to 28 920 CZK.

Adding a cap therefore changes filed figures: bump `_FINGERPRINT_VERSION` so
cached runs are recomputed rather than served.

---

## Tax Liability (§16 ZDP)

| Rule | Status |
|------|--------|
| Base rate: 15% | **IMPLEMENTED** — configurable |
| Elevated rate: 23% above threshold | **IMPLEMENTED** — configurable |
| Default threshold: CZK 1,935,552 (2024 value) | **IMPLEMENTED** — configurable |
| Combined base = dividends + interest + max(0, sec_net) + max(0, opt_net) | **IMPLEMENTED** |
| FTC finalization against CZ tax on foreign income | **IMPLEMENTED** |
| `final_tax = gross_tax - final_creditable_ftc` | **IMPLEMENTED** |
| Threshold applies to TOTAL taxpayer income (not just IBKR) | **PARTIAL** — limitation note: IBKR-only view |
| Solidarity surcharge | **NOT IMPLEMENTED** |
| Sparer-Pauschbetrag equivalent | **NOT IMPLEMENTED** (CZ has no equivalent) |

---

## Form Mapping

| Rule | Status |
|------|--------|
| DAP-oriented line codes (CZ_DAP_8_*, CZ_DAP_10_*, etc.) | **IMPLEMENTED** |
| No recomputation in form mapping layer | **IMPLEMENTED** |
| Official form line references (ř. XX) | **NOT IMPLEMENTED** — `official_line_ref=None` |
| PDF/XML DAP generation | **NOT IMPLEMENTED** |
