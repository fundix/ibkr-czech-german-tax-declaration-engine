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
| Jednotný kurz (annual uniform rate) | **PARTIAL** — `CzFxMode.UNIFORM` uses the GFŘ rates from `uniform_rates.py` (pokyny D-49/D-66/D-75); §10 disposal legs still route through the EUR-enriched amounts, so they inherit the EUR intermediate (M17/M18) |
| `--cz-fx-mode compare` — both modes scored by final tax | **IMPLEMENTED** — `fx_mode_compare.py` |
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
| Lot pairing choice (FIFO / LIFO / weighted average / optimal) | **IMPLEMENTED** — `pairing.py`, `pairing_solver.py`; a private investor may pick, the tool only recommends. `optimal` covers long securities; options, shorts and assets with a mid-year corporate action stay FIFO |
| Currency disposal (FX conversion) → CZ_10_CURRENCY | **IMPLEMENTED** — each disposal of a foreign currency becomes a `CURRENCY_CONVERSION` item. Conversions *out of* CZK are not disposals — they only establish an acquisition rate |
| FX gain on a currency disposal (the actual §10 figure) | **IMPLEMENTED** — `engine/currency_ledger.py` runs a FIFO over cash from the Statement of Funds (merged across every year on file); `countries/cz/currency_gains.py` rules on what §10 reaches. Per a tax advisor on 2026-08-15: **narrow reading is the default** (paying a share's purchase price is payment, not an exchange of money), with a broad switch; both readings share ONE FIFO, since a purchase consumes layers either way. A negative balance is a **debt**, not a negative lot — its own queue, its own mirrored result, outside the §10 base and never netted against gains. Netting inside the kind is floored at zero (§10 odst. 4 and 5) and the occasional-income exemption is tested on gross positive gains; both settled by the same advisor ruling, so the figures now reach the tax base |


### Currency disposals — settled rules

A tax advisor answered both blocking questions on **2026-08-15** for a Czech
resident individual, private account outside business assets, keeping no books.
Everything below is implemented and in the tax base.

**Rate and layer date: settlement.** `CzTaxConfig.currency_movement_date`
defaults to `SETTLE_DATE`. §38 wants the daily rate for the day an item arises,
and a taxpayer keeping no books is on the cash basis — income has to reach his
estate, typically on credit to the account (NSS 2 Afs 21/2010-118; extended
chamber 1 Afs 208/2023-57, point 33). A spot exchange creates a receivable and
a payable on the trade day; the currency balances themselves only move on
settlement.

**A conversion is one event on one date.** Its legs take a single shared date
and stay contiguous, so nothing is consumed between the two sides of one
exchange. This is not cosmetic: IBKR books the two legs a day apart on **19 of
89** conversions of the reference book, while their settlement dates have never
been seen to differ. The execution-level `trades.csv` carries a single TradeDate
for those 19 — always the later of the two — so the earlier leg is a reporting
artefact and `_conversion_date` takes the maximum. The advisory `DATE` scenario
obeys the same rule; per-leg dating was simply wrong and is gone.

**Netting inside the kind, floored at zero.** §10 odst. 5 calls an FX loss an
expense and odst. 4 disregards the difference where a kind's expenses exceed its
income. So losses net against gains across currencies, pairs and accounts of one
taxpayer — the "kind" is none of those — and the result never goes below zero.
`CzCurrencyNetting` keeps `raw_net`, `net_taxable` and `unutilized_loss` apart,
because the unused loss is not a tax loss: it reaches no other kind of §10, does
not carry to another year, and `compute_combined` contributes only the floored
figure.

**Occasional-income exemption.** Tested on the **sum of positive gains**, not on
the net and not on the volume converted; losses neither lower the amount tested
nor earn the exemption; it is a cliff. `currency_occasional_exempt_limit_czk`
defaults to 50 000 CZK.

**Still outside the base, deliberately:** the mirrored result of repaying
borrowed currency (computed, reported, flagged for review — §10 covers
exchanging one's own money, and NSS 5 Afs 45/2011-94 allowed an FX result on
repaying a debt only for a bookkeeping legal person), and FX on a demonstrably
regulated European market, which these statements cannot identify and which the
note says is not carved out automatically.

**2026 caveat**, carried in the note: amendment 360/2025 Sb. renumbered the §10
letters and left the odst. 4-5 references inconsistent with the new q/r, so the
letter is checked per year rather than hardcoded.

A note on provenance: the reconstruction is verifiable rather than merely
plausible. The Statement of Funds carries a running `Balance` on every movement
row, so replaying the amounts must reproduce IBKR's own figure on every row —
`verify_against_statement` does exactly that in file order (a completeness
check, independent of the settlement ordering the FIFO consumes in), and a
mismatch blocks the note from quoting any number.

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
| Pre-2014 acquisition rule (6-month test) | **PARTIAL** — `pre_2014_rule_enabled`, measured from `holding_period_start` (čl. II bod 5, 344/2013 Sb.); assumes the direct issuer share stayed ≤ 5 %, which the statement cannot confirm — noted on the item |
| Fund-specific time test rules | **NOT IMPLEMENTED** — fund units take the same 3-year test as any other security |

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
| Source country for the cap: the item's own `source_country`, else the first WHT record carrying one | **IMPLEMENTED** — the item's country comes from `IssuerCountryCode` and outranks the tax row, which on an ADR names the depositary |
| Cap rate defaulted rather than verified is flagged (`cap_rate_defaulted`) | **IMPLEMENTED** — 15 % default is indistinguishable from a real 15 % cap otherwise; see below |
| FTC invariant: `paid = creditable + non_creditable` | **IMPLEMENTED** |
| Split of `non_creditable` into `treaty_excess_ftc` + `uncredited_ftc` | **IMPLEMENTED** — see below |
| Final FTC = `min(preliminary, czech_tax_on_foreign_income)` | **IMPLEMENTED** |
| Czech tax on foreign income: proportional method `gross_tax × (foreign_income / combined_base)` | **IMPLEMENTED** |
| Per-country FTC aggregation in summary | **IMPLEMENTED** |
| Treaty-by-treaty verification of cap rates | **IMPLEMENTED** for the 16 treaties in `country_credit_caps` (AT, AU, CA, CH, CN, DE, FR, GB, HK, IE, JP, KZ, LU, NL, SE, US); others fall back to the 15 % default |

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
| Official form line references (ř. XX) | **IMPLEMENTED** — verified 2026-07-03 against DAP vzor č. 30 and Příloha 2/3 vzor č. 21 for period 2025; re-verify when a new vzor is published. `official_line_ref` stays `None` on the lines that have no counterpart on the form |
| Per-state Příloha 3 sheets (§38f odst. 8) | **IMPLEMENTED** — `per_state_credit` on the liability summary; the sheets sum to ř. 328 |
| PDF filing-support report | **IMPLEMENTED** — `exporters/pdf_exporter.py`, `--output-pdf`; a podklad for manual filing, not the form itself |
| Official DAP form (PDF vzor / EPO XML) generation | **NOT IMPLEMENTED** — figures must be typed into the form or EPO by hand |
