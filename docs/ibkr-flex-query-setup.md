# Návod: IBKR Flex Queries pro daňový engine

Krok-za-krokem návod na vytvoření a spuštění Flex Queries v Interactive
Brokers Client Portalu tak, aby vygenerovaná CSV přesně odpovídala vstupům
enginu (viz [input_data_spec.md](../input_data_spec.md)). Sepsáno podle
skutečného nastavení 2026-07; názvy polí v IBKR UI se občas mění — když
některé pole nenajdete, hledejte synonymum a ověřte proti spec.

## Kdy to potřebujete

Jednou ročně pro nový daňový rok, nebo když IBKR query smaže/změní.
Queries zůstávají uložené v účtu — příště je stačí jen **spustit** s novým
obdobím (sekce [Spouštění](#spouštění)).

## Kde se to nastavuje

Client Portal → **Performance & Reports → Statements → záložka Flex
Queries** → panel **Activity Flex Query** → ikona **➕**.

> Nepoužívejte Default Statements ani Trade Confirmation Flex Query.

Vytvoříte **čtyři samostatné query** (každá právě jedna sekce!) — engine
čte každý dataset jako samostatné CSV. Jedna query s více sekcemi by
vyrobila slepený soubor, který parser nepřečte.

## Společná konfigurace (všechny čtyři query)

**Delivery Configuration:**

| Volba | Hodnota |
|---|---|
| Models | Optional |
| **Format** | **CSV** (ne XML!) |
| Include header and trailer records? | No |
| **Include column headers?** | **Yes** ← bez toho parser CSV nepřečte |
| Display single column header row? | No |
| Include section code and line descriptor? | No |
| Period | libovolné (při spuštění se přepíše na Custom Date Range) |

**General Configuration:**

| Volba | Hodnota |
|---|---|
| **Date Format** | **yyyy-MM-dd** |
| Time Format | HHmmss |
| Date/Time Separator | ; (semi-colon) |
| Profit and Loss | Default |
| Include Offsetting Trade/Cancel Pairs? | No |
| Include Currency Rates? | No |
| Include Audit Trail Fields? | No |
| Display Account Alias in Place of Account ID? | No |
| Breakout by Day? | No |
| Include Canceled Trades? | No (pokud je volba k dispozici) |

Zaškrtnutí polí navíc nevadí (parser neznámé sloupce ignoruje) — vadí
jen chybějící pole.

## Query 1: `TaxEngine-Trades`

- Sekce: **Trades**, Options: **Execution** (ne Closed Lots / Wash Sales / Order / Symbol Summary / Asset Class)
- Pole (23):

  Account ID · Currency · Asset Class · Sub Category · Symbol ·
  Description · Conid · ISIN · Underlying Conid · Underlying Symbol ·
  Multiplier · Strike · Expiry · Put/Call · Trade Date · Quantity ·
  Trade Price · IB Commission · IB Commission Currency ·
  **Open/Close Indicator** · Notes/Codes · Buy/Sell · Transaction ID

> **Open/Close Indicator je kritický** — bez něj engine neumí spolehlivě
> klasifikovat obchody (tvrdý požadavek, viz input_data_spec.md).
> Notes/Codes nese příznaky exercise/assignment/expirace opcí (Ex/A/Ep).

## Query 2: `TaxEngine-Cash`

- Sekce: **Cash Transactions**, Options: **Detail** (typy transakcí klidně
  všechny; minimálně Dividends, Payment in Lieu, Withholding Tax, Broker
  Interest)
- Pole (14):

  Account ID · Currency · Asset Class · Sub Category · Symbol ·
  Description · Conid · Underlying Conid · ISIN ·
  **Issuer Country Code** · Settle Date · Amount · Type · Transaction ID

> Issuer Country Code je nutný pro zápočet §38f per stát; ISIN zlepšuje
> párování WHT k dividendám.

## Query 3: `TaxEngine-Positions`

- Sekce: **Open Positions**, Options: **Summary i Lot** (obojí zaškrtnuté)
- Pole (19):

  Account ID · Currency · Asset Class · Sub Category · Symbol ·
  Description · Conid · ISIN · Issuer Country Code · Underlying Conid ·
  Underlying Symbol · Multiplier · Quantity · Mark Price · Position Value ·
  Cost Basis Money · Level Of Detail · Open Date Time ·
  Holding Period Date Time

> **Kde jsou jednotlivá pole:** v seznamu sekcí se „Open Positions" jen
> vybere. Na pole se klikne až **na název sekce**, což rozbalí panel se
> zaškrtávátky — `Issuer Country Code` tedy není sekce, ale položka uvnitř
> Open Positions.

> Kdyby seznam nabízel „Position" i „Quantity", zvolte **Quantity** —
> tak se jmenuje sloupec, který parser čeká.

### Proč Summary *i* Lot

Parser čte oba druhy řádků a každý k něčemu jinému:

- **SUMMARY** = jedna pozice na titul. Odtud jdou množství, EOY cena a hodnota.
  Řádky LOT se pro totály záměrně přeskakují, jinak by se pozice sečetla
  několikrát.
- **LOT** = jeden řádek na pořízení, s `OpenDateTime`. Odtud se plní
  `Asset.soy_lots`, tedy skutečná data nákupu jednotlivých lotů.

Bez LOT řádků nemá engine u pozic nesených z minulých let čím rekonstruovat
datum pořízení a spadne na náhradní 31. 12. předchozího roku. Ten lot se
označí `acquisition_date_estimated` a časový test §4 odst. 1 písm. u) se pak
**vůbec nevyhodnotí**: položka zůstane zdanitelná jako konzervativní default
a jde k ruční kontrole ([time_test.py:242-262](src/countries/cz/time_test.py:242)).
Summary sám o sobě tedy funguje, ale u starších nákupů vás připraví
o automatické osvobození.

### `Issuer Country Code` a zeměpis portfolia

`IssuerCountryCode` je autoritativní zdroj země emitenta. Pořadí zdrojů,
které engine používá, je: IBKR → země z výplaty příjmu → prefix ISIN →
„neznámé" (radši mezera než dohad).

Prefix ISIN je jen odhad a na reálné knize se od IBKR **lišil u 8 z 24**
titulů:

| Titul | IBKR | Prefix ISIN | Proč |
|---|---|---|---|
| BABA, BYDDY, DIDIY, JD, NICE | HK, CN, CN, CN, IL | US | ADR — ISIN označuje zemi depozitáře |
| NU | BR | KY | holdingová společnost na Kajmanech |
| GRAB | SG | KY | totéž |
| COPN | IE | NL | emitent jinde než registrace ISIN |

`Sub Category` rozlišuje ADR od běžné akcie; u ADR se proto z prefixu ISIN
země záměrně neodhaduje vůbec.

> Po zapnutí sloupce je nutné data **znovu stáhnout a rok přepočítat** —
> země se ukládá do `portfolio.json` při běhu, ne při zobrazení. Starší CSV
> IBKR zpětně nedoplní.

## Query 4: `TaxEngine-CorpActions`

- Sekce: **Corporate Actions**, Options: **Detail**
- Pole (16 + Asset Class nevadí):

  Account ID · Currency · Symbol · Description · Conid · ISIN ·
  Underlying Conid · Underlying Symbol · **Report Date** · Code ·
  **Type** · **Action ID** · Amount · **Proceeds** · Value · Quantity

> Type (FS/RS/TC…) rozlišuje split vs. merger; Proceeds nese hotovost
> z cash mergerů.

## Spouštění

U každé query ikona spuštění (šipka) → Period: **Custom Date Range**.
Flex umí max. **365 dní na jeden běh**, proto historie po letech:

| Query | Období | Proč |
|---|---|---|
| Trades | každý rok od založení účtu do 31. 12. daňového roku | historie je nutná pro rekonstrukci SOY pozic a časový test (§4/1/u — nákupy před >3 lety!) |
| CorpActions | stejné roky jako Trades | splity z dřívějších let ovlivňují SOY rekonstrukci |
| Cash | 1. 1. – 31. 12. daňového roku | starší dividendy nejsou potřeba |
| Positions | jednodenní rozsah 31. 12. roku PŘED daňovým rokem (SOY) a znovu 31. 12. daňového roku (EOY) | dva běhy téže query |

## Pojmenování a uložení

Do `data/real_<rok>/` (adresář `data/` je v .gitignore — data zůstávají
lokální):

```
data/real_2024/
├── trades_2021.csv … trades_2024.csv
├── corp_actions_2021.csv … corp_actions_2024.csv
├── cash_2024.csv
├── positions_soy_2023.csv
└── positions_eoy_2024.csv
```

Roční soubory trades/corp actions se před během spojí (stejné hlavičky —
stačí zřetězit bez opakování hlavičky, nebo to nechat na přípravném
skriptu).

## Navíc: reference pro rekonciliaci

Ze záložky **Statements** stáhněte i roční **Activity Statement** (PDF)
za daňový rok — realizované P/L, dividendy a WHT z něj slouží jako
nezávislá kontrola výstupů enginu.

## Spuštění enginu

```bash
uv run python -m src.main --country cz --no-interactive \
  --trades data/real_2024/trades_merged.csv \
  --cash data/real_2024/cash_2024.csv \
  --pos_start data/real_2024/positions_soy_2023.csv \
  --pos_end data/real_2024/positions_eoy_2024.csv \
  --corp_actions data/real_2024/corp_actions_merged.csv \
  --cz-fx-mode compare \
  --output-json vysledek.json --output-xlsx vysledek.xlsx
```

`--cz-fx-mode compare` spočítá denní i jednotný kurz a doporučí levnější
režim (exporty dostanou přípony `.daily`/`.uniform`).

## Časté chyby

| Symptom | Příčina |
|---|---|
| Parser nenačte žádné řádky | Format XML místo CSV, nebo chybí column headers |
| „Data inconsistency" u obchodů | chybí sloupec Open/Close Indicator |
| Zápočet WHT bez rozpadu per stát | chybí Issuer Country Code v Cash query |
| Nesedí SOY rekonstrukce | trades/corp actions nestažené za celou historii účtu |
| Prázdné pozice | Positions spuštěné přes rozsah místo jednodenního „as of" data |
