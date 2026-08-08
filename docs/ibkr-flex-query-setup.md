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

Vytvoříte **pět samostatných query** (každá právě jedna sekce!) — engine
čte každý dataset jako samostatné CSV. Jedna query s více sekcemi by
vyrobila slepený soubor, který parser nepřečte.

Query 1–4 jsou povinné. Query 5 (Statement of Funds) je **volitelná a zatím
do daně nic nepřináší**: engine bez ní funguje úplně stejně a konverze měn
tak či tak jen označí k ruční kontrole (§10 kurzový zisk se ještě nepočítá,
chybí FIFO na hotovosti). Pořizuje se dopředu, protože je to jediný výpis,
který nese hotovostní zůstatky — a nabývací kurz pozbyté měny může být
z dřívějšího roku, takže historii je potřeba mít stáhnutou, než se výpočet
doplní.

## Společná konfigurace (všechny query)

**Delivery Configuration:**

| Volba | Hodnota |
|---|---|
| Models | Optional |
| **Format** | **CSV** (ne XML!) |
| Include header and trailer records? | No |
| **Include column headers?** | **Yes** ← bez toho parser CSV nepřečte |
| Display single column header row? | No |
| Include section code and line descriptor? | No |
| **Period** | **Year to Date** |

> **Period není libovolný**, i když se při ručním spuštění přepisuje na
> Custom Date Range. Když appka stahuje **běžící rok**, žádný rozsah
> nepředává a spoléhá právě na uložený Period ([services.py](../src/webapp/services.py)) —
> s „Last Business Day" by dostala jediný den. Pro historii appka posílá
> `fd`/`td` po letech sama.

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

## Query 5: `TaxEngine-Statement-of-Funds`

Volitelná — jen pro kurzové rozdíly z konverzí měn (§10). Bez ní engine
konverze pouze označí k ruční kontrole.

- Sekce: **Statement of Funds**
- Options — na těchhle třech to stojí:

  | Volba | Hodnota | Proč |
  |---|---|---|
  | **Include Starting and Ending Balances** | **zapnuto** | Bez počátečního zůstatku není znám nabývací kurz držené měny a FIFO nemá z čeho vycházet. Tohle je důvod, proč query existuje. |
  | **Currency Breakout** | **zapnuto** | Řádky v původní měně. Měna pohybu je pro kurzový rozdíl to hlavní. |
  | **Base Currency Summary** | **vypnuto** | Přepočet kurzy IBKR — pro §10 nepoužitelné (nutný denní kurz ČNB) a zdvojilo by každý pohyb. |
  | Summarize Trades by Symbol…, Order Summary | vypnuto | potřebujeme jeden řádek na pohyb, ne agregát |

- Pole (24):

  Account ID · Currency · Asset Class · Symbol · Description · Conid ·
  ISIN · Underlying Symbol · **Report Date** · **Date** · **Settle Date** ·
  **Activity Code** · **Activity Description** · Trade ID · Trade Quantity ·
  Trade Gross · Trade Commission · **Trade Tax** · **Debit** · **Credit** ·
  **Amount** · **Balance** · Level Of Detail · **Transaction ID**

> `FX Rate To Base` **nezapínejte** — je to kurz IBKR, pro §10 se musí použít
> denní kurz ČNB.

### Jak vypadá výstup

Ověřeno na skutečném běhu za červenec 2026 (24 sloupců, jedna hlavička):

- Soubor je **seskupený po měnách**. Každý blok začíná řádkem
  `Starting Balance` a končí `Ending Balance`.
- Zůstatkové řádky se poznají takto: `ActivityCode` je **prázdný**,
  `TransactionID` **prázdný**, `Amount` je **0** a hodnota zůstatku je
  ve sloupci **`Balance`**. `LevelOfDetail` je na všech řádcích `Currency`,
  takže k rozlišení neslouží.
- `Amount == Debit + Credit` (ověřeno na všech řádcích); `Debit` je záporný,
  `Credit` kladný.
- **Konverze měn = dvě NEBO TŘI řádky se stejným `TradeID`** — jedna za každou
  měnovou nohu a většinou ještě komise, účtovaná v té měně, ve které ji IBKR
  fakturuje. `ActivityCode` je `FOREX` na všech.
  `TransactionID` **spojovací klíč není**: komise má na některých obchodech
  vlastní a na jiných stejné jako nohy (reálné obchody 1466896593 a
  1295097860), takže seskupení podle něj udělalo z 17 konverzí 26. Popis taky
  ne — nohy se mezi sebou samy liší („Trad**ed** Currency Leg" vs
  „Trad**ing** Currency Leg"). Nohy pozná jen **symbol páru**, který jedna
  z nich vždycky nese: `USD.CZK` jmenuje obě měny.
- `ActivityCode` viděné v praxi: `BUY`, `SELL`, `FOREX`, `DINT` (debetní
  úrok), `DIV`, `PIL` (payment in lieu), `OFEE`, `FRTAX` (srážková daň
  a její opravy), prázdný u zůstatků.
- **`Date` je ekonomické datum, `ReportDate` datum výpisu** — u opravy
  srážkové daně se lišily o měsíc (report 2026-07-30, date 2026-06-25).
  Pro kurz ČNB se používá `Date`, pro pohyb hotovosti `SettleDate`.
- `TradeCommission` může být **kladná** (rebate od IBKR) — potvrzeno i tady,
  stejný jev jako v Trades.

> **Záporné zůstatky jsou normální, ne chyba.** Na marginovém účtu je debetní
> zůstatek *vypůjčená* měna. Na reálném běhu byly na začátku měsíce záporné
> USD, CZK, SEK i HKD. Kurzový rozdíl z vypůjčené měny je zrcadlový oproti
> držené (zisk vzniká při oslabení), takže to FIFO musí umět.

## Spouštění

U každé query ikona spuštění (šipka) → Period: **Custom Date Range**.
Flex umí max. **365 dní na jeden běh**, proto historie po letech:

| Query | Období | Proč |
|---|---|---|
| Trades | každý rok od založení účtu do 31. 12. daňového roku | historie je nutná pro rekonstrukci SOY pozic a časový test (§4/1/u — nákupy před >3 lety!) |
| CorpActions | stejné roky jako Trades | splity z dřívějších let ovlivňují SOY rekonstrukci |
| Cash | 1. 1. – 31. 12. daňového roku | starší dividendy nejsou potřeba |
| Positions | jednodenní rozsah 31. 12. roku PŘED daňovým rokem (SOY) a znovu 31. 12. daňového roku (EOY) | dva běhy téže query |
| Statement of Funds | stejné roky jako Trades | nabývací kurz pozbyté měny může být z dřívějšího roku — bez historie je FIFO na hotovosti k ničemu |

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
