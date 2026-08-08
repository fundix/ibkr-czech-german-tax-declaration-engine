## General Information
- **CSV Encoding:** All input CSV files are expected to be `utf-8-sig` encoded.
- **Decimal Parsing:** Numerical monetary values and quantities are parsed into Python's `Decimal` type, preserving precision from the string representation. Empty strings or unparsable numeric values typically default to `None` or `Decimal("0.0")` based on field definition and parsing logic (`safe_decimal` utility).
- **Date Parsing:** Date strings are parsed from various common formats (e.g., YYYY-MM-DD, YYYYMMDD) into Python `datetime.date` or `datetime.datetime` objects.

---

## 1. Trades File
- **Default Name (from `config.py`):** `trades.csv`
- **Sample File Provided:** `input_file_2.csv`
- **Associated Pydantic Model:** `RawTradeRecord`

**Column Specifications (based on `input_file_2.csv` headers):**

| CSV Header             | Model Field Name (Pydantic) | Model Data Type             | Description                                                                 | Notes (Optionality, Example, Parsing Detail)                                                                                                                               |
|------------------------|-----------------------------|-----------------------------|-----------------------------------------------------------------------------|----------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `ClientAccountID`      | `client_account_id`         | `Optional[str]`             | The client's account identifier.                                            | Optional. Example: "U7542366"                                                                                                                                              |
| `CurrencyPrimary`      | `currency_primary`          | `str`                       | The primary currency of the transaction or asset.                           | Required. Example: "EUR", "USD"                                                                                                                                            |
| `AssetClass`           | `asset_class`               | `str`                       | The asset class of the instrument (e.g., STK, OPT, CASH, BOND, CFD).        | Required. Example: "BOND", "CASH", "CFD", "OPT", "STK"                                                                                                                      |
| `SubCategory`          | `sub_category`              | `Optional[str]`             | Sub-category of the asset (e.g., COMMON, ETF, ADR for STK).                 | Optional. Example: "Corp", "COMMON", "ADR", "ETF"                                                                                                                          |
| `Symbol`               | `symbol`                    | `str`                       | The trading symbol of the instrument.                                       | Required. Example: "VW 0 3/4 06/15/23", "EUR.USD", "IWO", "TIO   230616C00001000"                                                                                          |
| `Description`          | `description`               | `str`                       | A textual description of the instrument or transaction.                     | Required. Example: "VW 0 3/4 06/15/23", "EUR.USD", "USD IWO"                                                                                                               |
| `ISIN`                 | `isin`                      | `Optional[str]`             | International Securities Identification Number.                             | Optional. Example: "XS1734548487", "US23292E1082"                                                                                                                          |
| `Strike`               | `strike`                    | `Optional[Decimal]`         | The strike price of an option.                                              | Optional, relevant for OPT. Example: "1.0", "225.0"                                                                                                                        |
| `Expiry`               | `expiry`                    | `Optional[str]`             | The expiry date of an option or future (YYYY-MM-DD).                        | Optional, relevant for OPT/FUT. Parsed as string. Example: "2023-06-16"                                                                                                     |
| `Put/Call`             | `put_call`                  | `Optional[str]`             | Indicates if an option is a Put ('P') or Call ('C').                        | Optional, relevant for OPT. Example: "C", "P"                                                                                                                              |
| `TradeDate`            | `trade_date`                | `str`                       | The date of the trade (YYYY-MM-DD).                                         | Required. Parsed as string. Example: "2023-03-13"                                                                                                                          |
| `Quantity`             | `quantity`                  | `Decimal`                   | The number of units traded. Positive for buy, negative for sell.            | Required. Example: "20000.0", "-22.0"                                                                                                                                      |
| `TradePrice`           | `trade_price`               | `Decimal`                   | The price per unit for the trade.                                           | Required. Example: "99.42", "1.05675", "0.03"                                                                                                                              |
| `IBCommission`         | `ib_commission`             | `Optional[Decimal]`         | Commission charged by Interactive Brokers for the trade.                    | Optional. Usually negative. Example: "-12.5", "-1.89394"                                                                                                                   |
| `IBCommissionCurrency` | `ib_commission_currency`    | `Optional[str]`             | The currency of the IB commission.                                          | Optional. Example: "EUR", "USD"                                                                                                                                            |
| `Buy/Sell`             | `buy_sell`                  | `Optional[str]`             | Indicates if the trade was a buy or sell.                                   | Optional. "BUY" or "SELL". Crucial for determining `FinancialEventType`. Code has fallbacks if missing.                                                                    |
| `TransactionID`        | `transaction_id`            | `Optional[str]`             | IBKR's unique identifier for the transaction.                               | Optional, but highly recommended. Example: "1728910410"                                                                                                                    |
| `Notes/Codes`          | `notes_codes`               | `Optional[str]`             | Codes related to the trade (e.g., P, A, Ex, Ep).                            | Optional. Used to identify exercises, assignments, expirations. Example: "P", "A", "Ep", "Ex"                                                                               |
| `UnderlyingSymbol`     | `underlying_symbol`         | `Optional[str]`             | The symbol of the underlying asset for derivatives.                         | Optional, relevant for OPT/FUT/CFD. Example: "TIO", "IWO"                                                                                                                  |
| `Conid`                | `conid`                     | `Optional[str]`             | IBKR's contract identifier for the instrument.                              | Optional. Example: "298918183", "12087792"                                                                                                                                 |
| `UnderlyingConid`      | `underlying_conid`          | `Optional[str]`             | IBKR's contract identifier for the underlying asset.                        | Optional, relevant for OPT/FUT/CFD. Example: "325898828.0" (parsed as string)                                                                                              |
| `Multiplier`           | `multiplier`                | `Optional[Decimal]`         | The contract multiplier (e.g., for options, futures).                     | Optional. Example: "1", "100"                                                                                                                                              |
| `Open/CloseIndicator`  | `open_close_indicator`      | `Optional[str]`             | Indicates if trade opens or closes a position ('O' or 'C').                 | Crucial for determining `FinancialEventType` for standard financial instrument trades in conjunction with `Buy/Sell` (refer to PRD Section 5, Step 7). Expected values: 'O' (Open), 'C' (Close). Missing or invalid values for relevant trades constitute a data inconsistency. Not applicable to currency pair trades (e.g., FX 'CASH' asset class trades like EUR.USD). The provided sample `input_file_2.csv` does not include this column; real input data for accurate trade classification requires it. Model field `open_close_indicator` maps to this. |

---

## 2. Cash Transactions File
- **Default Name (from `config.py`):** `cash_transactions.csv`
- **Sample File Provided:** `input_file_1.csv` (structure inferred from image and code)
- **Associated Pydantic Model:** `RawCashTransactionRecord`

**Column Specifications (based on `input_file_1.csv` headers):**

| CSV Header         | Model Field Name (Pydantic) | Model Data Type   | Description                                                                          | Notes (Optionality, Example, Parsing Detail)                                                                                                |
|--------------------|-----------------------------|-------------------|--------------------------------------------------------------------------------------|---------------------------------------------------------------------------------------------------------------------------------------------|
| `(index)`          | (Ignored by model)          | N/A               | Row index from pandas DataFrame export, if present.                                  | Ignored by `RawCashTransactionRecord` due to `Config.extra = 'ignore'`.                                                                     |
| `ClientAccountID`  | `client_account_id`         | `Optional[str]`   | The client's account identifier.                                                     | Optional. Example: "U7542366"                                                                                                               |
| `CurrencyPrimary`  | `currency_primary`          | `str`             | The currency of the cash transaction.                                                | Required. Example: "CAD", "JPY", "EUR"                                                                                                      |
| `AssetClass`       | `asset_class`               | `Optional[str]`   | Asset class related to the cash transaction (e.g., STK, BOND). Can be empty/null.    | Optional. Example: "STK", "BOND", or empty.                                                                                                 |
| `SubCategory`      | `sub_category`              | `Optional[str]`   | Sub-category of the asset (e.g., COMMON). Can be empty/null.                         | Optional. Example: "COMMON", or empty.                                                                                                      |
| `Symbol`           | `symbol`                    | `Optional[str]`   | Symbol of the instrument related to the cash flow. Can be empty/null.                | Optional. Example: "BNS", "9022.T", or empty.                                                                                               |
| `Description`      | `description`               | `str`             | Detailed description of the cash transaction. Crucial for type determination.        | Required. Example: "BNS (CA0641491075) CASH DIVIDEND CAD 1.03 - CA TAX"                                                                   |
| `SettleDate`       | `settle_date`               | `str`             | The settlement date of the cash transaction (YYYY-MM-DD).                            | Required. Parsed as string. Example: "2023-01-27"                                                                                           |
| `Amount`           | `amount`                    | `Decimal`         | The monetary amount of the cash transaction. Positive for inflow, negative for outflow. | Required. Example: "-30.9", "0.12", "7000.0"                                                                                                |
| `Type`             | `type`                      | `str`             | The type of cash transaction (e.g., Dividends, Withholding Tax).                     | Required. Example: "Withholding Tax", "Dividends", "Broker Interest Received"                                                               |
| `Conid`            | `conid`                     | `Optional[str]`   | IBKR's contract identifier for the related instrument. Can be empty/null.            | Optional. Example: "4457153.0" (parsed as string), or empty.                                                                                |
| `UnderlyingConid`  | `underlying_conid`          | `Optional[str]`   | IBKR Conid of the underlying for derivative-related cash flows. Can be empty/null.   | Optional. Empty in sample.                                                                                                                  |
| `ISIN`             | `isin`                      | `Optional[str]`   | ISIN of the related instrument. Can be empty/null.                                   | Optional. Example: "CA0641491075", or empty.                                                                                               |
| `IssuerCountryCode`| `issuer_country_code`       | `Optional[str]`   | ISO country code of the issuer or tax authority.                                     | Optional. Example: "CA", "JP", or empty. Used for WHT source country.                                                                       |

---

## 3. Positions File (Start of Year / End of Year)
- **Default Names (from `config.py`):** `positions_start_of_year.csv`, `positions_end_of_year.csv`
- **Sample Files Provided:** `input_file_3.csv` (Start), `input_file_4.csv` (End)
- **Associated Pydantic Model:** `RawPositionRecord`

**Column Specifications (based on `input_file_3.csv` / `input_file_4.csv` headers):**

| CSV Header         | Model Field Name (Pydantic) | Model Data Type   | Description                                                                 | Notes (Optionality, Example, Parsing Detail)                                                                                                                                           |
|--------------------|-----------------------------|-------------------|-----------------------------------------------------------------------------|----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `ClientAccountID`  | (Effectively ignored)       | `N/A`             | The client's account identifier.                                            | Present in CSV. `RawPositionRecord.account_id` (alias `AccountId`) does not map to this header. `ClientAccountID` data from CSV is ignored by the Pydantic model as currently defined. Example: "U7542366" |
| `CurrencyPrimary`  | `currency_primary`          | `str`             | The currency of the position.                                               | Required. Example: "CAD", "EUR", "SGD"                                                                                                                                                 |
| `AssetClass`       | `asset_class`               | `str`             | The asset class of the instrument (e.g., STK, OPT).                         | Required. Example: "STK", "OPT"                                                                                                                                                        |
| `SubCategory`      | `sub_category`              | `Optional[str]`   | Sub-category of the asset (e.g., COMMON, ETF, ADR).                         | Optional. Example: "COMMON", "ETF", "ADR". `ADR` suppresses the ISIN-prefix country guess — an ADR's ISIN names the depositary's country (US), not the issuer's.                        |
| `Symbol`           | `symbol`                    | `str`             | The trading symbol of the instrument.                                       | Required. Example: "BNS", "4GLDd", "P LEG  20230120 63 M"                                                                                                                              |
| `Description`      | `description`               | `str`             | A textual description of the instrument.                                    | Required. Example: "BANK OF NOVA SCOTIA", "XETRA-GOLD"                                                                                                                                 |
| `ISIN`             | `isin`                      | `Optional[str]`   | International Securities Identification Number.                             | Optional. Example: "CA0641491075", "DE000A0S9GB0"                                                                                                                                      |
| `IssuerCountryCode`| `issuer_country_code`       | `Optional[str]`   | ISO country code of the issuer — the authoritative source for a holding's geography. | Optional, and **not exported by default**: enable "Issuer Country Code" in the Open Positions section of the Flex query, then re-download and recompute. Without it the country falls back to income rows, then the ISIN prefix, then "unknown". Example: "CN", "KY" |
| `Quantity`         | `position`                  | `Decimal`         | The number of units held. Positive for long, negative for short.            | Required. (Aliased from `Quantity` in CSV to `position` in model). Example: "200", "-100"                                                                                                 |
| `PositionValue`    | `position_value`            | `Optional[Decimal]`| The market value of the position in `CurrencyPrimary`.                      | Optional. Example: "13268", "22032"                                                                                                                                                    |
| `MarkPrice`        | `mark_price`                | `Optional[Decimal]`| The mark-to-market price per unit.                                          | Optional. Example: "66.34", "55.08", "2.31"                                                                                                                                            |
| `CostBasisMoney`   | `cost_basis_money`          | `Optional[Decimal]`| The total cost basis of the position in `CurrencyPrimary`.                  | Optional. Example: "13527", "22279.134", "-787.8" (negative for short proceeds)                                                                                                      |
| `UnderlyingSymbol` | `underlying_symbol`         | `Optional[str]`   | The symbol of the underlying asset for derivatives.                         | Optional, relevant for OPT. Example: "LEG"                                                                                                                                             |
| `Conid`            | `conid`                     | `Optional[str]`   | IBKR's contract identifier for the instrument.                              | Optional. Example: "4457153", "50784405", "604172754"                                                                                                                                  |
| `UnderlyingConid`  | `underlying_conid`          | `Optional[str]`   | IBKR's contract identifier for the underlying asset.                        | Optional, relevant for OPT. Example: "121764205"                                                                                                                                       |
| `Multiplier`       | `multiplier`                | `Optional[Decimal]`| The contract multiplier (e.g., for options).                              | Optional. Example: "1", "100"                                                                                                                                                          |

---

## 4. Corporate Actions File
- **Default Name (from `config.py`):** `corporate_actions.csv`
- **Sample File Provided:** `input_file_0.csv`
- **Associated Pydantic Model:** `RawCorporateActionRecord`

**Column Specifications (based on `input_file_0.csv` headers):**

| CSV Header         | Model Field Name (Pydantic) | Model Data Type   | Description                                                                   | Notes (Optionality, Example, Parsing Detail)                                                                                                |
|--------------------|-----------------------------|-------------------|-------------------------------------------------------------------------------|---------------------------------------------------------------------------------------------------------------------------------------------|
| `ClientAccountID`  | `client_account_id`         | `Optional[str]`   | The client's account identifier.                                              | Optional. Example: "U7542366"                                                                                                               |
| `Symbol`           | `symbol`                    | `str`             | The trading symbol of the instrument affected by the corporate action.        | Required. Example: "9022.T", "ATVI", "D05"                                                                                                  |
| `Description`      | `description`               | `str`             | Detailed description of the corporate action and the instrument.              | Required. Example: "9022.T(JP3566800003) SPLIT 5 FOR 1..."                                                                                 |
| `ISIN`             | `isin`                      | `Optional[str]`   | ISIN of the affected instrument.                                              | Optional. Example: "JP3566800003", "US00507V1098"                                                                                           |
| `Report Date`      | `report_date`               | `str`             | The date the corporate action was reported (YYYY-MM-DD).                      | Required. Parsed as string. Example: "2023-09-28"                                                                                           |
| `Code`             | `code`                      | `Optional[str]`   | IBKR code related to the corporate action (e.g., for specific CA subtypes).   | Optional. Empty in sample.                                                                                                                  |
| `Type`             | `type_ca`                   | `str`             | IBKR's type code for the corporate action (e.g., FS, TC, HI).                 | Required (model field `type_ca`). Example: "FS", "TC", "HI"                                                                                 |
| `ActionID`         | `action_id_ibkr`            | `Optional[str]`   | IBKR's unique identifier for the corporate action event.                      | Optional. Example: "126233647"                                                                                                              |
| `Conid`            | `conid`                     | `Optional[str]`   | IBKR's contract identifier for the affected instrument.                       | Optional. Example: "14018918", "52424577"                                                                                                   |
| `UnderlyingConid`  | `underlying_conid`          | `Optional[str]`   | IBKR Conid of the underlying if the CA affects a derivative.                  | Optional. Empty in sample.                                                                                                                  |
| `UnderlyingSymbol` | `underlying_symbol`         | `Optional[str]`   | Symbol of the underlying if the CA affects a derivative.                      | Optional. Empty in sample.                                                                                                                  |
| `CurrencyPrimary`  | `currency_primary`          | `Optional[str]`   | The currency of monetary amounts involved in the CA.                          | Optional (model allows None). Example: "JPY", "USD", "EUR"                                                                                  |
| `Amount`           | (Ignored by model)          | `N/A`             | A monetary amount related to the CA.                                          | Present in CSV (value "0"). Ignored by `RawCorporateActionRecord`; `Value` or `Proceeds` are used for monetary impact.                  |
| `Proceeds`         | `proceeds`                  | `Optional[Decimal]`| Monetary proceeds from the CA (e.g., cash from merger).                       | Optional. Example: "0", "19000"                                                                                                             |
| `Value`            | `value`                     | `Optional[Decimal]`| Monetary value related to the CA (e.g., FMV of stock dividend).               | Optional. Example: "0", "-18884", "149.85" (negative value seems to indicate cost/value given up in sample)                                   |
| `Quantity`         | `quantity`                  | `Optional[Decimal]`| Quantity of shares/units involved (e.g., new shares from split/dividend).   | Optional. Example: "400", "-200" (negative for shares disposed in merger), "5"                                                              |

---

## 5. Statement of Funds File (optional)

- **Default Name:** `statement_of_funds.csv`
- **Associated Pydantic Model:** `RawStatementOfFundsRecord`
- **Parser:** `src/parsers/statement_of_funds_parser.py` — standalone, like `positions_parser`; readable without running the engine
- **Flex setup:** Query 5 in [docs/ibkr-flex-query-setup.md](docs/ibkr-flex-query-setup.md)

The per-currency cash ledger, and the **only** statement that carries cash
balances at all — `positions_end_of_year.csv` holds STK and OPT only. It exists
for the §10 FX gain on a currency disposal, which needs the rate at which the
disposed currency was acquired. **Optional and not yet consumed by the engine:**
the file is downloaded and stored, but no processor reads it, so its presence
changes no tax figure today.

**Two row shapes share the schema:**

| Shape | How to recognise it | What it carries |
|---|---|---|
| **Balance marker** | `ActivityCode` empty **and** `ActivityDescription` is `Starting Balance` / `Ending Balance`. `Amount` is 0 | the figure is in `Balance`. `LevelOfDetail` is `Currency` on *every* row, so it cannot be used to tell markers apart |
| **Movement** | everything else | `Amount == Debit + Credit`, `Debit` negative, `Credit` positive |

| CSV Header | Model Field Name | Model Data Type | Description | Notes |
|---|---|---|---|---|
| `ClientAccountID` | `client_account_id` | `Optional[str]` | Account identifier. | Optional. |
| `CurrencyPrimary` | `currency_primary` | `str` | The currency this block belongs to. | **Required.** The file is grouped by currency. |
| `AssetClass` | `asset_class` | `Optional[str]` | Asset class of the related instrument. | Optional. Example: "STK", empty on pure cash rows. |
| `SubCategory` | `sub_category` | `Optional[str]` | Sub-category (COMMON, ETF, ADR…). | Optional. |
| `Symbol` | `symbol` | `Optional[str]` | Instrument symbol, or the **currency pair** on one leg of a conversion. | Optional. Example: "RHM", "USD.CZK". The pair symbol is what identifies a conversion's two legs. |
| `Description` | `description` | `Optional[str]` | Instrument description. | Optional. |
| `Conid` | `conid` | `Optional[str]` | IBKR contract identifier. | Optional. |
| `ISIN` | `isin` | `Optional[str]` | ISIN of the related instrument. | Optional. |
| `UnderlyingSymbol` | `underlying_symbol` | `Optional[str]` | Underlying for derivatives. | Optional. |
| `ReportDate` | `report_date` | `Optional[str]` | When the line hit the statement. | Optional. **Not** the rate date. |
| `Date` | `date` | `Optional[str]` | The **economic** date. | Optional but this is the ČNB rate date. The two diverge: a withholding-tax correction reported 2026-07-30 carried date 2026-06-25. |
| `SettleDate` | `settle_date` | `Optional[str]` | Cash settlement date. | Optional. Use for the cash movement, not the rate. |
| `ActivityCode` | `activity_code` | `Optional[str]` | Movement type. | Seen in practice: `BUY`, `SELL`, `FOREX`, `DINT` (debit interest), `DIV`, `PIL`, `OFEE`, `FRTAX`; **empty** on balance markers. |
| `ActivityDescription` | `activity_description` | `Optional[str]` | Human-readable movement label. | Carries `Starting Balance` / `Ending Balance` on markers and `Commission from Forex Trade` on a conversion's fee row. |
| `TradeID` | `trade_id` | `Optional[str]` | Groups the rows of one trade. | **The join key for a currency conversion** — see below. |
| `TradeQuantity` | `trade_quantity` | `Optional[Decimal]` | Units traded. | Optional. |
| `TradeGross` | `trade_gross` | `Optional[Decimal]` | Gross trade amount. | Optional. Example: "-100.00" |
| `TradeCommission` | `trade_commission` | `Optional[Decimal]` | Commission, signed as IBKR reports it. | **May be positive** — an IBKR rebate, the same trap as in the Trades file. Do not assume a fee is negative. |
| `TradeTax` | `trade_tax` | `Optional[Decimal]` | Transaction tax. | Optional. |
| `Debit` | `debit` | `Optional[Decimal]` | Negative side of the movement. | Optional. |
| `Credit` | `credit` | `Optional[Decimal]` | Positive side of the movement. | Optional. |
| `Amount` | `amount` | `Optional[Decimal]` | Net movement. | `Debit + Credit`; **0** on balance markers. |
| `Balance` | `balance` | `Optional[Decimal]` | Running balance after the row. | The figure on a marker row. **May legitimately be negative** — on a margin account a debit balance is *borrowed* currency, not an error, and a FIFO must handle the sign rather than clamp it. |
| `LevelOfDetail` | `level_of_detail` | `Optional[str]` | Flex detail level. | `Currency` on every row — carries no information here. |
| `TransactionID` | `transaction_id` | `Optional[str]` | IBKR transaction identifier. | Empty on balance markers. **Not** a reliable conversion key — see below. |

> `FX Rate To Base` must be left **off** in the Flex query: it is IBKR's own
> rate, and §10 requires the ČNB daily rate.

### Currency conversions

A conversion is **two or three** rows sharing `TradeID`: one per currency leg,
plus usually a commission row in whatever currency the fee is billed in. All
carry `ActivityCode == FOREX`.

Neither `TransactionID` nor the description joins them:

- the commission shares the legs' transaction id on some trades and has its own
  on others (real trades 1295097860 and 1466896593), so grouping on it split
  every fee off as a bogus one-legged conversion — 26 apparent conversions where
  the book held 17;
- the legs already describe themselves differently ("Trad**ed**" vs
  "Trad**ing** Currency Leg").

The legs are identified from the **pair symbol** one leg always carries:
`USD.CZK` names both currencies, so the legs are the rows in those currencies
and anything else on the trade is a charge. The commission must be removed
*before* picking the second leg — a fee billed in exactly the other leg's
currency otherwise competes for the slot and iteration order decides the winner.
