# Audit výpočtů — checkpoint / report (2026-07)

> Netrackovaný pracovní soubor. Průběžně aktualizován během auditu; na konci obsahuje finální report.
> Stav repa: HEAD 7fccf79, čistý working tree.

## Stav oprav (větev fix/audit-2026-07-batch1, 2026-07-02)

**Opraveno (15 nálezů, 6 commitů, 508 testů zelených):**
- Dávka 1: H1 (short FX swap + časový test shortů), H2 (zapojení ČNB provideru v CLI), H3 (WHT refundy netting + druhý průchod linkeru)
- Dávka 2: M1 (časový test kalendářními roky), M2 (práh 23 % per rok), L2 (zaokrouhlení DAP), M21 (EUR-mode guard sazeb)
- Dávka 3: M3 (FX-fail blokuje 100k exempci), M14 (PENDING ztráta mimo základ)
- Dávka 4: M4 (rozdělení stejnodenních WHT), M20 (reversal dividendy drží znaménko), L1 (FTC strop max(0, gross))
- Dávka 5: M16 (Stückzinsen snižují §8 úroky), L7 (řaditelné kategorie), L8 (měnová konzistence mutace)

**Opraveno v dávkách 6–8 (dalších 9 nálezů, 519 testů zelených):**
- Dávka 6: M5 (opční prémie přežijí částečné fily — numerický klíč, pro-rata alokace, ERROR pro zbytky), M10 (0DTE: nákup opce se řadí před její expiraci/exercise)
- Dávka 7: M7 (SOY přebytek nechává NEJNOVĚJŠÍ loty + warning), M8 (příznak odhadnutého data nabytí z SOY fallbacku → PENDING v časovém testu), M13 (FX-fail vypuštěné obchody: ERROR per obchod + souhrnný ERROR za běh)
- Dávka 8: L5 (záporné čisté proceeds drží znaménko), L6 (cash merger konzumuje jen quantity_disposed; short pozice → warning), L10 (prefetch plní cache), L12 (EUR-mode wht_paid jen EUR záznamy)

**Opraveno v dávce 9 (další 3 nálezy, 523 testů zelených):** L9 (get_rate_info → skutečné datum kurzu ve fx_date_used + conversion_note při fallbacku), L11 (odmítnutí budoucího data — ČNB by tiše vrátila dnešní listek), L13 (standalone úroková WHT do sekce úroků; sekce úroků má vlastní wht_paid řádek).

**Opraveno v dávkách 10–11 (dalších 6 nálezů dle rozhodnutí uživatele, 537 testů zelených):** M6 (stock dividenda = §8 příjem v FMV + zero-cost lot místo ztráty kvantity), M12 (pro-rata repatriace po akciích), M15 (REVIEW REQUIRED poznámka při konverzích měn), M22 (PRIVATE_SALE/neznámé kategorie → PENDING místo tichých exempcí), L4 (§4/3 40M cap 2025+ flaguje osvobozené položky), M19 („C;O" flip zavře pozici a otevře protipozici místo pádu).

**Zbývá — mechanické, vyžaduje rozhodnutí/data od uživatele (zapsáno i v docs/future-work.md):**
- **M9** (opční prémie v historické SOY simulaci): korektní oprava = přehrát historii globálně chronologicky přes procesory (dnes se přehrává per-asset bez lifecycle událostí, prémie kříží assety) — refactor historické větve engine, dopad i na DE. Alternativa: dokumentovat jako známou mezeru.
- **M11** (cash-in-lieu u frakcí po reverse splitu): potřeba vidět, jak CIL vypadá v reálných IBKR datech uživatele (cash transaction řádek? corporate action detail?), aby šel navázat na disposal frakce.
- **L14** (benevolentní tolerance WHT párování): zpřísnění hrozí false negatives — ladit proti reálným výpisům uživatele.
**Zbývá — čeká na rozhodnutí uživatele (⚖):** M6 (stock dividendy v §8), M12 (pro-rata repatriace), M15 (kurzové zisky konverzí), M22 (PRIVATE_SALE fallback), M17/M18 (oddělené datum prémie — návrh datového modelu), M19 (C;O flip — sémantika FIFO), L4 (40M cap 2025+).

## Stav auditu

- [x] Krok 0: checkpoint soubor založen
- [x] Krok 1: baseline `uv run pytest` (473 passed)
- [x] Krok 2: paralelní revize (A: FIFO/CA, B: FX, C: CZ rutiny, D: linkery) — 46 syrových kandidátů
- [x] Krok 3: adverzariální ověření nálezů — žádný vyvrácen, 39 unikátních po sloučení duplicit
- [x] Krok 4: finální report — viz sekce „Krok 4: Finální report" níže (3 H / 22 M / 14 L)

## Krok 1: Baseline

`uv run pytest -q` na HEAD 7fccf79: **473 passed, 0 failed** (1.39 s). Očekávání z plánu (356/357 s 1 známým failem) bylo zastaralé — suite je zelená a větší.

## Krok 2: Syrové nálezy agentů

### Agent A — FIFO jádro a corporate actions (HOTOVO, 15 kandidátů + R1)

- **A-1 (H, vysoká, reprodukováno)** — `fifo_manager.py:530-543` × `item_builder.py:402-404`: short pozice mají prohozené CZK kurzy nohou (TŘETÍ nezávislé potvrzení B-B/D-3). Sub-nález: stock short dostane SECURITY_DISPOSAL → časový test měří open→cover → short >3 roky chybně osvobozen (u shortu se papír nedrží).
- **A-2 (M, vysoká mechanika, reprodukováno)** — `fifo_manager.py:213-229`: SOY rekonstrukce s přebytkem (chybí historický prodej) ponechá NEJSTARŠÍ loty místo nejnovějších, bez warningu → špatný cost basis i chybně splněný časový test (zisk 5 000 vs. 1 000 EUR, osvobozeno vs. zdanitelné).
- **A-3 (M, vysoká, reprodukováno)** — `fifo_manager.py:303,337` × `time_test.py:142-162`: SOY fallback lot má syntetické acq. datum 31.12. a RGL nenese příznak odhadu → CZ časový test ho bere jako reálné → zdaněno místo osvobození, RESOLVED bez manual review; fallback basis konvertován kurzem 1.1. místo skutečného data nákupu.
- **A-4 (M, vysoká)** — `calculation_engine.py:102-104` × `fifo_manager.py:169-171`: historická SOY rekonstrukce neaplikuje opční premium adjustment na stock trady z prior-year assignmentu → cost basis bez prémie (zisk 200 místo 500 EUR).
- **A-5 (M, vysoká mechanika, reprodukováno)** — `sorting_utils.py:23-27,100-113`: same-day nákup opce + expirace (0DTE) → lifecycle se řadí PŘED nákup → expirace na prázdném ledgeru → ztráta −150 EUR se nevytvoří, lot visí; same-day exercise → ValueError → celý běh spadne.
- **A-6 (M, vysoká absence, reprodukováno)** — `fifo_manager.py:564-602`: splity s frakcí bez cash-in-lieu logiky → frakce visí v ledgeru, CIL hotovost nezdaněna, cost frakce propadne.
- **A-7 (M, střední právně, reprodukováno)** — `fifo_manager.py:812-831`: kapitálová repatriace snižuje basis sekvenčně od nejstaršího lotu místo pro-rata na akcii → špatná distribuce mezi zdaněné/osvobozené loty (±500 EUR v příkladu).
- **A-8 (M, vysoká mechanika)** — `fifo_manager.py:352,378-380,407,486` × `enrichment.py:132-134`: selhání FX u trade → tiché vypuštění obchodu z FIFO (žádný RGL, jen warning v logu) → zdanitelný prodej úplně chybí (překryv tématu s C-N3).
- **A-9 (M, nízká–střední)** — `option_processor.py:66,120` × `trade_processor.py:149-163`: cash-settled exercise bez stock tradu → premium adjustment se nikdy nespotřebuje → prémie zmizí (jen INFO log počtu) (příbuzné D-5).
- **A-10 (M, střední výskyt)** — `domain_event_factory.py:69-86` × `fifo_manager.py:470-480`: Open/CloseIndicator „C;O" (flip jedním obchodem) → SELL_LONG celé kvantity → ValueError → celý běh spadne.
- **A-11 (L, vysoká, reprodukováno)** — `fifo_manager.py:410,384`: `copy_abs` překlopí záporné čisté proceeds (komise > hrubá částka; zavírání skoro bezcenných opcí) → chyba ~2× komise.
- **A-12 (L, vysoká mechanika, reprodukováno)** — `fifo_manager.py:604-661`: cash merger ignoruje `quantity_disposed` (realizuje vždy celou long pozici) i short loty.
- **A-13 (M, střední interpretace)** — `fifo_manager.py:667-678` × `corporate_action_processor.py:82-84` × `item_builder.py:171-184`: stock dividend — FIFO dá lotu cost=FMV, ale CZ FMV nezdaní (nezdaněný step-up; = D-4); komentář deklaruje zero-basis, kód dělá FMV; při FMV=None se lot vůbec nevytvoří → ztráta kvantity.
- **A-14 (L, vysoká mechanika, trigger vzácný)** — `sorting_utils.py:66,76,87`: AssetCategory v sort klíči není orderable → latentní TypeError při shodě tx id a různých kategoriích.
- **A-15 (L, nízká)** — `calculation_engine.py:267-268`: mutace CAPITAL_REPAYMENT eventu míchá jednotky (= D-13).

**A — R1 vyřešeno:** FIFO už DE klasifikaci neobsahuje — jediná kopie v `de/plugin.py`, injektováno callbackem; žádná divergence čísel. Jen zastaralý docstring v de/plugin.py:52-57.

**A — prověřeno OK:** párování lotů (FIFO pořadí, částečné konzumace, tolerance), znaménka (mimo A-11), splity invarianty (H2 drží), cash merger multiplier (L1 drží), opce znaménka všech 4 kombinací adjustmentů + expirace, řazení (CA→lifecycle→trade→cash, numerické tx id, M3/L3 drží), SOY přesná shoda, EOY validace (jen loguje — design). Repro: `scratchpad/repro_fifo_audit.py`.

### Agent B — FX pipeline (HOTOVO, 8 kandidátů)

- **B-A (H, vysoká)** — `src/main.py:170-177`: produkční CLI nikdy nepředá `fx_provider` do `get_tax_plugin("cz")` → `has_fx=False` → celá ČNB pipeline je v ostrém běhu mrtvý kód; výstupy v EUR, 100k limit se přeskočí, FTC v EUR režimu. `create_fx_provider("cnb")` se mimo testy nikde nevolá, `cnb_cache_file_path` nikdo nečte. Oprava: v main.py postavit CNB provider a předat pluginu.
- **B-B (H, vysoká — mechanismus reprodukován)** — `item_builder.py:402-404` + `fifo_manager.py:530-543`: u shortů (short cover, short opce, expirace short) je v RGL `acquisition_date`=otevření, ale cost_eur je cash flow z POKRYTÍ a proceeds_eur z OTEVŘENÍ → plošný převod „cost @ acquisition_date, proceeds @ realization_date" dá každé noze ČNB kurz té druhé nohy. Repro s reálnými kurzy: short 100×100 USD (5.1.2024) pokryt @90 USD (5.6.2024): engine zisk 22 206,90 CZK vs. správně 21 110,00 CZK (+5,2 %); při pohybu CZK 24→26 chyba +34 810 CZK. Oprava: pro short realizace prohodit data (cost @ realization_date, proceeds @ acquisition_date), dlouhodobě nést datum cash flow per noha.
- **B-C (M, střední)** — `trade_processor.py:155-157` + `item_builder.py:403`: opční prémie vmíchaná do stock basis při exercise/assignment nese ECB kurz dne otevření opce, ale EUR→CZK se dělá kurzem dne akciového obchodu → prémie oceněna kombinací dvou dat. Chyba ~úměrná prémie × drift (příklad: ~300 CZK, při 5% driftu ~5 600 CZK na 5 000 USD prémii). Oprava: nést prémii odděleně s vlastním datem.
- **B-D (M, střední)** — `fifo_manager.py:826-828` + `item_builder.py:403`: kapitálová splátka snižuje EUR basis kurzem dne splátky, CZ pak snížený basis převádí kurzem dne nabytí → smíšené kurzy. Oprava: jako B-C; minimálně flag manual review.
- **B-E (L, vysoká)** — `fx_policy.py:160-177`: `fx_date_used` uvádí požadované datum, i když provider použil fallback (víkend); `conversion_note` se nikdy nenastavuje. Čísla OK, auditní stopa lže. Oprava: provider má vracet skutečné datum kurzu.
- **B-F (L, vysoká)** — `cnb_exchange_rate_provider.py:319-331`: `prefetch_rates` stažené kurzy zahodí (neukládá do cache). Jen výkonnost. 
- **B-G (L, střední)** — `cnb_exchange_rate_provider.py:193-215`: parser neověřuje datum v hlavičce listku; pro budoucí datum (vadná data) ČNB vrátí dnešní listek a provider mlčky vrátí dnešní kurz místo None. Oprava: porovnat hlavičku s query_date.
- **B-H (M, vysoká)** — `tax_liability.py:160-169`: v EUR režimu se EUR základ porovnává s CZK prahem (duplikát C-N6, zesílený B-A: je to produkční default).

**B — mapa toků:** dividendy/úroky/WHT: přímo originál→CZK (1 konverze, ČNB @ událost); proceeds/cost long: originál→EUR→CZK se shodnými daty obou kroků (odchylka dvojí konverze kvantifikována: průměr |0,012 %|, max 0,027 % ≈ ±627 CZK na 100 000 USD — zanedbatelné); rizikem nejsou křížové kurzy, ale míchání DAT (B-B, B-C, B-D).

**B — prověřeno OK:** ČNB parsing (čárka, sloupec množství JPY/100), směr kurzu (foreign-per-CZK, žádná inverze), víkend/svátek fallback (hlasité selhání → PENDING, H1 drží), long+corporate actions data konzistentní (N1 fix drží), žádné mezizaokrouhlení (28 míst, quantize jen na výstupu), ECB provider OK, M1/M2 guardy fungují.

### Agent C — České daňové rutiny (HOTOVO, 10 kandidátů)

- **C-N1 (M, důvěra vysoká, reprodukováno)** — `src/countries/cz/config.py:47-50` + `time_test.py:143-144`: časový test jako pevných `>1095 dní` místo 3 kalendářních let. Okno obsahující 29.2. má 1096 dní → prodej přesně na 3. výročí (např. 1.6.2021→1.6.2024) engine chybně osvobodí (`1096>1095`), správně dle §4/1/x + §33 DŘ zdanitelné. Podhodnocení daně; test `test_boundary_1096_days_is_exempt` chybu kodifikuje. Oprava: porovnávat data (nabytí + 3 kalendářní roky), ne dny.
- **C-N2 (M, vysoká)** — `config.py:31-35`: práh 23 % = 1 935 552 Kč je hodnota 2023 (48×40 324), pro TAX_YEAR=2024 má být 36×43 967 = 1 582 812 Kč (2025: 1 676 052). Základ 1 935 552 za 2024 → daň podhodnocena o 28 219,20 Kč. Oprava: tabulka prahů per rok.
- **C-N3 (M, vysoká, reprodukováno)** — `annual_limit.py:142-156` + `:106-118`: prodej s `fx_conversion_failed` (proceeds_czk=None) vypadne z úhrnu pro 100k limit → zbylé položky se chybně osvobodí a označí RESOLVED. Scénář: 60k OK + 80k FX-fail → úhrn 60k ≤ 100k → 60k osvobozeno, správně vše zdanitelné (úhrn 140k). Oprava: při jakémkoli FX-fail disposalu exempci nepřiznávat, položky PENDING.
- **C-N4 (M, střední)** — `loss_offsetting.py:112-121`: PENDING ztráta (chybí acquisition_date) je zahrnuta do nettingu — u ztrát je „konzervativní zahrnutí" obrácené: pokud pozice prošla časovým testem, ztráta je neuplatnitelná. Zisk 100k + PENDING ztráta −50k → základ 50k, správně možná 100k. Oprava: PENDING zisky zahrnout, PENDING ztráty vyřadit (jen pending_total).
- **C-N5 (L, vysoká, reprodukováno)** — `foreign_tax_credit.py:209`: `copy_abs()` u záporného hrubého příjmu (storno dividendy −500, WHT +75) → kredit 75, správně 0. Test `test_cz_ftc_boundaries.py:130-158` má docstring „clamps to zero", asserty ale fixují 75. Oprava: `max(0, gross) * cap_rate`.
- **C-N6 (L, střední, reprodukováno)** — `tax_liability.py:160-169`: v EUR režimu (has_fx=False) se CZK práh 23 % porovnává s EUR základem → 100 000 EUR celé v 15 %. Stejná třída jako opravená M1. Oprava: v EUR režimu elevated-rate přeskočit/warning.
- **C-N7 (L, vysoká, reprodukováno)** — `tax_liability.py:162-179`: chybí zaokrouhlení pro DAP — základ na celá sta Kč dolů (§16/2 ZDP), daň na celé Kč (§146/1 DŘ). 123 456,78 → engine 18 518,52; správně 18 510. Oprava: quantize ROUND_FLOOR na stovky před sazbami.
- **C-N8 (L, střední)** — `tax_liability.py:187-205`: prostý zápočet §38f/8 agregátně přes státy místo za každý stát zvlášť. Diverguje jen při country_credit_caps > 15 % (např. DE 0,26): DE 100k/WHT 26k + US 100k/WHT 0 → agregát kredit 26k, per-country 15k. Oprava: strop per stát, pak součet.
- **C-N9 (M podmíněně, nízká–střední)** — `enums.py:48-50`: PRIVATE_SALE_ASSET (Gold-ETC, krypto-ETP) i fallback neznámých kategorií → CZ_10_SECURITIES → dostávají CP osvobození (časový test + 100k), i když nemusí být cennými papíry. Oprava: mapovat mimo exempce nebo flag PENDING_MANUAL_REVIEW.
- **C-N10 (L, vysoká právně)** — `annual_limit.py:2-7`, `config.py:41-45`: popisek „2025+ amendment" u 100k limitu mate (platí od 2014); od 2025 chybí 40mil. strop na osvobozené příjmy (§4/3 ZDP od 2025) a není v known gaps. Oprava: doplnit cap pro tax_year≥2025 aspoň do Known Limitations.

**C — prověřeno OK:** 100k limit (hrubé příjmy, all-or-nothing, exkluze time-test-exempt příjmů z úhrnu dle D-59, hranice 100 000/100 001), oddělené nettování CP vs. deriváty s floorem 0 (§10/4), FTC per-item min(WHT, cap×gross) vč. US 30 %→15 %, finalizace FTC s ratio cap, marginální 23 % jen nad prahem, ostrá nerovnost časového testu, §8 bez časového testu, opce bez časového testu, H1/M1/M2/N1-FX fixy drží. Reprodukce: `scratchpad/repro_cz_audit.py`.

### Agent D — Linkery a stavba položek (HOTOVO, 13 kandidátů)

- **D-1 (H, vysoká, reprodukováno)** — `domain_event_factory.py:514`: parser dělá `copy_abs()` pro KAŽDÝ řádek WHT → kladný řádek (refund/oprava, běžné u IBKR true-upů) se uloží jako další zaplacená daň. Dividenda 100, WHT −10, refund +10 → `foreign_tax_paid=20`, creditable 15, správně 0. Oprava: ukládat se znaménkem / netovat reversal páry.
- **D-2 (M, vysoká, reprodukováno)** — `withholding_tax_linker.py:135-144` + `item_builder.py:244-247`: dvě stejnodenní dividendy téhož assetu → obě WHT skončí na jedné (tie-break max(str(event_id)); fallback dict přepis). FTC per-item cap pak ukousne zápočet: WHT 15+30 na div 100 → creditable 15 místo 45. Oprava: exkluzivní přiřazení, dict → list.
- **D-3 (M, vysoká, reprodukováno)** — `item_builder.py:402-404`: shorty mají prohozené FX kurzy nohou (DUPLIKÁT B-B, nezávisle potvrzeno). Repro: short open 15.1. (25 CZK/EUR) proceeds 1000 EUR, cover 20.11. (20 CZK/EUR) cost 800 EUR → správně +9 000 CZK, engine 0 CZK.
- **D-4 (M, vysoká kód / střední interpretace)** — `item_builder.py:171-184`: zdanitelná stock dividend (`CORP_STOCK_DIVIDEND`, FMV) není CashFlowEvent → tiché continue → v CZ zmizí ze základu (DE ji daní; lot přitom dostane FMV jako cost → nezdaní se nikdy). Scrip dividend 349 EUR → §8 +0. Oprava: větev pro CORP_STOCK_DIVIDEND → §8 položka.
- **D-5 (M, vysoká mechanismus, reprodukováno)** — `option_trade_linker.py:44-48,87-96` + `calculation_engine.py:284-285` + `trade_processor.py:167-177`: exercise/assignment spotřebuje opční loty bez RGL a prémie visí v `pending_option_adjustments`; při nenalinkování (částečné fily 50+50 vs. klíč qty "100"; Decimal string klíč "100.0"≠"100") se prémie ZTRATÍ úplně (jen INFO log). Long call prémie 200 EUR → zisk +100 místo −100. Oprava: numerické porovnání, alokace na částečné fily, leftover → RGL nebo hard fail.
- **D-6 (M, vysoká kód / interpretace k potvrzení)** — `calculation_engine.py:273-281` + item_builder: kurzové zisky z konverzí měn (CurrencyConversionEvent) se nikde nedaní ani neflагují (v ČR §10 zdanitelné; není v dokumentovaných mezerách). Oprava: min. PENDING flag za rok s konverzemi.
- **D-7 (M, vysoká)** — `item_builder.py:180-184`: `INTEREST_PAID_STUECKZINSEN` (zaplacený naběhlý úrok při nákupu dluhopisu) v CZ tiše zmizí → úrokový příjem §8 nadhodnocen (kupón 500, zaplacený AÚV 400 → engine 500, správně ~100). Oprava: záporná položka CZ_8_INTEREST nebo PENDING.
- **D-8 (M, střední frekvence, mechanismus reprodukován)** — `domain_event_factory.py:439-442`: reversal dividendy s Code=DI/IN/PO se `copy_abs()` překlopí na +100 → sekvence 100, −100(kód), 80 dá §8=280 místo 80. Oprava: abs podmínit kladným raw_amount.
- **D-9 (L, vysoká)** — `plugin.py:162-171`: `wht_paid_eur` sčítá WHT napříč měnami bez konverze (USD 15 + EUR 10 = „25"). Jen reporting.
- **D-10 (L, vysoká)** — `item_builder.py:350-363` + `plugin.py:159-165`: standalone WHT natvrdo do sekce dividend (i daň z úroku/capital repayment) → řádek `wht_paid` neodpovídá zápočtu. Konzervativní, jen výkaz.
- **D-11 (L, střední)** — `withholding_tax_linker.py:300,346,396-401`: benevolentní tolerance (interest-pattern bez kontroly částky; proximity do 100 % příjmu; min_rate záporné) → false positive párování. Dopad omezený.
- **D-12 (L, střední)** — `enums.py:42-50` + `item_builder.py:384-395`: UNKNOWN/CASH_BALANCE/PRIVATE_SALE_ASSET tiše do CZ_10_SECURITIES vč. časového testu (překryv s C-N9).
- **D-13 (L, vysoká mutace/nízký dopad)** — `calculation_engine.py:266-268`: mutovaný CAPITAL_REPAYMENT event má EUR hodnotu v `gross_amount_foreign_currency`, ale původní `local_currency` (USD) → nekonzistence pro PDF/diagnostiku (třída jako opravená M6).

**D — prověřeno OK:** žádné dvojité započtení §8/§10 (CashFlow vs. RGL oddělené; excess jednou; WHT nikdy linked+standalone), znaménka opčních adjustmentů (4 kombinace správně), exercise negeneruje RGL opce (žádné zdvojení), multiplier default 100, řazení lifecycle před stock trade, kandidáti jen A/EX, hrubé dividendy + kladná WHT, M4 fronta, H1/M1/M2/N1 fixy, unlinked WHT se neztrácí. Repro: `scratchpad/repro_audit.py`.

## Krok 3: Verdikty ověření

Metoda: (1) nezávislé přečtení všech citovaných míst v kódu v hlavním sezení, (2) opětovné spuštění reprodukčních skriptů agentů proti živému enginu (`repro_fifo_audit.py`, `repro_cz_audit.py`, `repro_audit.py` — všechny výstupy potvrzují tvrzené chování), (3) cílené protikontroly míst, která mohla nálezy vyvrátit (registry/plugin fallback pro FX provider, interní cache `_fetch_rates_for_date`, existující testy).

**Výsledek: žádný kandidát nebyl vyvrácen.** 46 syrových nálezů po sloučení duplicit (short FX swap hlášen 3×, EUR práh 2×, PRIVATE_SALE 2×, stock dividend 2×, mutace CAPITAL_REPAYMENT 2×, ztráta prémií 2×) = **39 unikátních potvrzených nálezů**. U 5 z nich je mechanismus potvrzen, ale konečná závažnost závisí na daňové interpretaci (označeno ⚖ ve finálním reportu). Nejblíže vyvrácení byly: docstring pluginu slibující auto-vytvoření CNB provideru (kód to nedělá — nález H2 platí) a riziko R1 z ARCHITECTURE_AUDIT.md (už vyřešeno — není chyba).

## Krok 4: Finální report

### Shrnutí

Audit potvrdil **39 nálezů: 3 vysoké (H), 22 středních (M), 14 nízkých (L)**. Klíčový vzorec: většina chyb je **tichá a ve směru podhodnocení daně** (riziko doměrku), menšina nadhodnocuje (Stückzinsen, SOY fallback). Pozitivní: jádro FIFO párování, znaménkové konvence, ČNB parsing, ECB provider, oddělené nettování §10 a všech 12 dříve opravených chyb drží.

### Vysoká závažnost (H)

**H1 — Short pozice: CZK kurzy obou nohou jsou prohozené** — [item_builder.py:402-404](src/countries/cz/item_builder.py:402) × [fifo_manager.py:530-543](src/engine/fifo_manager.py:530)
RGL u shortu ukládá `acquisition_date`=otevření (kdy byly PŘIJATY proceeds) a `total_cost_basis_eur`=výdaj z POKRYTÍ; item_builder ale plošně převádí cost @ acquisition_date a proceeds @ realization_date → každá noha dostane ČNB kurz dne té druhé. Týká se short coveru, zavření i expirace vypsaných opcí (velmi běžné). Repro: open 15.1. (25 CZK/EUR) proceeds 1 000 EUR, cover 20.11. (20 CZK/EUR) cost 800 EUR → engine 0 CZK, správně +9 000 CZK. Potvrzeno 3 agenty nezávisle + živá reprodukce. *Sub-nález:* stock short dostane SECURITY_DISPOSAL → časový test měří open→cover → short >3 roky by byl chybně „osvobozen".
**Oprava:** pro short realization types prohodit data konverze (cost @ realization_date, proceeds @ acquisition_date); dlouhodobě nést v RGL explicitní datum cash flow per noha; shorty vyjmout z časového testu.

**H2 — Produkční CLI nikdy nezapne CZK konverzi — celá ČNB pipeline je mrtvý kód** — [main.py:170](src/main.py:170), [pipeline_runner.py:124](src/pipeline_runner.py:124), [plugin.py:349-359](src/countries/cz/plugin.py:349)
`get_tax_plugin("cz")` se volá bez `fx_provider`; plugin nemá fallback (docstring ho slibuje, kód nedělá); `create_fx_provider("cnb")` se mimo testy nikde nevolá. Důsledek: výstupy v EUR, 100k limit přeskočen, práh 23 % nefunkční, FTC v EUR režimu. Aktivuje i M21.
**Oprava:** v main.py/pipeline_runneru při `--country cz` postavit `create_fx_provider("cnb", cache_file_path=cz_config.cnb_cache_file_path)` a předat pluginu.

**H3 — Vratky/opravy WHT se počítají jako další zaplacená daň** — [domain_event_factory.py:514](src/parsers/domain_event_factory.py:514)
Bezpodmínečné `copy_abs()` pro každý WHT řádek → kladný refund (běžný u IBKR true-upů) se přičte k zaplacené dani. Repro: dividenda 100, WHT −10, refund +10 → `foreign_tax_paid=20`, kredit 15, správně 0.
**Oprava:** ukládat WHT se znaménkem a sčítat (refund záporně), příp. netovat reversal páry před linkingem.

### Střední závažnost (M)

**M1 — Časový test: pevných 1095 dní místo 3 kalendářních let** — [config.py:47-50](src/countries/cz/config.py:47), [time_test.py:143-144](src/countries/cz/time_test.py:143). Okno s 29.2. má 1096 dní → prodej přesně na 3. výročí chybně osvobozen (≈75 % tříletých oken). Test `test_boundary_1096_days_is_exempt` chybu kodifikuje. **Oprava:** porovnávat data (nabytí + 3 kalendářní roky dle §33 DŘ), ne dny.

**M2 — Práh 23 % sazby je hodnota 2023** — [config.py:31-35](src/countries/cz/config.py:31). 1 935 552 = 48×40 324 (2023); pro 2024 platí 36×43 967 = 1 582 812, pro 2025 1 676 052. U základu 1,9 mil. za 2024 daň podhodnocena o ~28 tis. Kč. **Oprava:** tabulka prahů klíčovaná `tax_year`.

**M3 — 100k limit: FX-failed prodej vypadne z úhrnu → ostatní se chybně osvobodí** — [annual_limit.py:142-156](src/countries/cz/annual_limit.py:142). Repro: 60k OK + 80k FX-fail → úhrn 60k ≤ 100k → 60k osvobozeno+RESOLVED, správně vše zdanitelné. **Oprava:** při jakémkoli FX-fail disposalu exempci nepřiznat, položky PENDING.

**M4 — Dvě stejnodenní dividendy téhož assetu: obě WHT na jedné** — [withholding_tax_linker.py:135-144](src/processing/withholding_tax_linker.py:135) + [item_builder.py:244-247](src/countries/cz/item_builder.py:244). Tie-break i dict-overwrite pošlou obě WHT na tutéž dividendu → per-item FTC cap ukousne kredit (15 místo 45). **Oprava:** exkluzivní přiřazení; `by_asset_date` jako dict→list.

**M5 — Ztracené opční prémie při nenalinkování exercise/assignment** — [option_trade_linker.py:44-48,87-96](src/processing/option_trade_linker.py:44), [calculation_engine.py:285](src/engine/calculation_engine.py:285). Klíč porovnává Decimal stringy (`"100.0"≠"100"`) a nezvládá částečné fily dodávky (50+50 vs. 100); nespárovaný adjustment = prémie zmizí (jen INFO log). Totéž u cash-settled exercise (žádný stock trade neexistuje). **Oprava:** numerická normalizace klíče, FIFO alokace na částečné fily, leftover adjustmenty → RGL nebo hard fail.

**M6 — Stock dividend: v CZ nezdaněný příjem + FMV step-up; FMV=None ztratí kvantitu** ⚖ — [item_builder.py:171-184](src/countries/cz/item_builder.py:171), [fifo_manager.py:663-691](src/engine/fifo_manager.py:663). Není CashFlowEvent → tichý skip v §8, lot ale dostane cost=FMV → příjem se nezdaní nikdy. Při FMV=None se lot vůbec nevytvoří (pozdější prodej může spadnout). **Oprava:** větev pro CORP_STOCK_DIVIDEND → §8 položka (dle zvolené interpretace FMV/zero-basis); kvantitu nikdy neztrácet.

**M7 — SOY rekonstrukce s přebytkem nechá NEJSTARŠÍ loty** — [fifo_manager.py:213-229](src/engine/fifo_manager.py:213). Chybí-li historický prodej, měl dle FIFO spotřebovat nejstarší loty → zůstat měly nejnovější; engine nechá nejstarší (bez warningu) → špatný basis i chybně splněný časový test. **Oprava:** přiřazovat od konce + warning/manual review.

**M8 — SOY fallback lot: syntetické datum 31.12. bez příznaku** — [fifo_manager.py:303,337](src/engine/fifo_manager.py:303). RGL nenese, že jde o odhad → časový test počítá od 31.12. → reálně >3 roky držené pozice zdaněny, RESOLVED bez review; basis konvertován kurzem 1.1. **Oprava:** propagovat `is_estimated_acquisition` do RGL → PENDING_MANUAL_REVIEW.

**M9 — Historická SOY rekonstrukce neaplikuje opční prémie** — [calculation_engine.py:102-104](src/engine/calculation_engine.py:102). Filtr nezahrnuje OptionLifecycleEvent → stock lot z prior-year assignmentu má basis bez prémie. **Oprava:** replay lifecycle událostí v historické simulaci, nebo adjustment už při enrichmentu.

**M10 — 0DTE: same-day expirace/exercise se řadí PŘED nákup opce** — [sorting_utils.py:23-27,100-113](src/utils/sorting_utils.py:23). Expirace na prázdném ledgeru → ztráta prémie se nevytvoří + orphan lot; same-day exercise → ValueError → pád běhu. **Oprava:** intra-day prioritu lifecycle omezit na události JINÉHO assetu; jinak řadit dle tx id.

**M11 — Reverse split s frakcí: chybí cash-in-lieu** — [fifo_manager.py:564-602](src/engine/fifo_manager.py:564). Frakce visí v ledgeru navždy (EOY mismatch), CIL hotovost nezdaněna, náklad frakce propadne. **Oprava:** zpracovat CIL jako disposal frakce.

**M12 — Kapitálová repatriace: sekvenčně od nejstaršího lotu místo pro-rata** ⚖ — [fifo_manager.py:812-831](src/engine/fifo_manager.py:812). Výplata je na akcii → snížení basis má být pro-rata; engine dá 0/1000 místo 500/500 → špatná distribuce mezi zdaněné/osvobozené loty. **Oprava:** rozpočítat dle kvantity lotů.

**M13 — Selhání FX u trade → tiché vypuštění z FIFO** — [fifo_manager.py:352,407,486](src/engine/fifo_manager.py:352). `return`/`return []` bez výjimky → zdanitelný prodej úplně chybí (jen warning v enrichmentu). **Oprava:** hard fail nebo agregovaný seznam vynechaných obchodů do reportu.

**M14 — PENDING ztráta (bez acquisition_date) snižuje základ** — [loss_offsetting.py:112-121](src/countries/cz/loss_offsetting.py:112). „Konzervativní zahrnutí" je u ztrát obrácené (pokud pozice prošla časovým testem, ztráta je neuplatnitelná). **Oprava:** PENDING zisky zahrnout, PENDING ztráty jen do pending_total.

**M15 — Kurzové zisky z konverzí měn se nikde nedaní ani neflagují** ⚖ — [calculation_engine.py:273-281](src/engine/calculation_engine.py:273). V ČR zdanitelný §10 příjem; není v dokumentovaných mezerách. **Oprava:** minimálně PENDING flag za rok s konverzemi; plnohodnotně FIFO na cash.

**M16 — Zaplacené Stückzinsen v CZ tiše zmizí → úroky §8 nadhodnoceny** — [item_builder.py:180-184](src/countries/cz/item_builder.py:180). INTEREST_PAID_STUECKZINSEN spadne do `continue`. **Oprava:** záporná položka CZ_8_INTEREST nebo PENDING.

**M17 — Prémie vmíchaná do stock basis nese smíšená FX data** — [trade_processor.py:155-157](src/engine/event_processors/trade_processor.py:155). Prémie v EUR (ECB @ otevření opce) se převádí ČNB kurzem dne akciového obchodu → kombinace dvou dat. **Oprava:** nést prémii odděleně s vlastním datem.

**M18 — Kapitálová splátka: EUR snížení kurzem dne splátky, CZK konverze kurzem dne nabytí** — [fifo_manager.py:826-828](src/engine/fifo_manager.py:826). Stejná třída jako M17. **Oprava:** dtto / flag manual review.

**M19 — Open/CloseIndicator „C;O" (flip jedním obchodem) shodí celý běh** — [domain_event_factory.py:69-86](src/parsers/domain_event_factory.py:69). Default SELL_LONG celé kvantity → nedostatek lotů → ValueError → abort. **Oprava:** rozdělit na close (dostupné) + open (zbytek).

**M20 — Reversal dividendy s Code=DI/IN/PO se překlopí na kladný příjem** — [domain_event_factory.py:439-442](src/parsers/domain_event_factory.py:439). Repro: −100 s Code="Di" → +100 → §8 = 280 místo 80. **Oprava:** abs() podmínit kladným raw_amount.

**M21 — EUR režim: CZK práh 23 % porovnán s EUR základem** — [tax_liability.py:160-169](src/countries/cz/tax_liability.py:160). Annual limit a FTC mají EUR guardy (M1/M2 fixy), sazby ne; do opravy H2 je to produkční default. **Oprava:** guard/N-A v no-FX režimu (oprava H2 to prakticky eliminuje).

**M22 — PRIVATE_SALE_ASSET a neznámé kategorie dostávají CP osvobození** ⚖ — [enums.py:42-50](src/countries/cz/enums.py:42). Tichý fallback do CZ_10_SECURITIES → časový test + 100k limit i pro Gold-ETC/krypto-ETP/UNKNOWN, které nemusí být cennými papíry. **Oprava:** fallback bez exempcí + PENDING_MANUAL_REVIEW.

### Nízká závažnost (L)

**L1** — FTC kredit z |záporného příjmu| (storno dividendy): [foreign_tax_credit.py:209](src/countries/cz/foreign_tax_credit.py:209) `copy_abs()` → kredit 75 z příjmu −500, správně 0; test s docstringem „clamps to zero" fixuje 75. Oprava: `max(0, gross)`.
**L2** — Chybí zaokrouhlení pro DAP (základ na celá sta dolů §16/2 ZDP, daň na celé Kč §146/1 DŘ): [tax_liability.py:162-179](src/countries/cz/tax_liability.py:162). Repro: 18 518,52 vs. správně 18 510.
**L3** — Prostý zápočet §38f/8 agregátně místo za každý stát: [tax_liability.py:187-205](src/countries/cz/tax_liability.py:187). Diverguje jen při country cap > 15 %.
**L4** — Chybí 40mil. strop osvobození od 2025 (§4/3 ZDP) a matoucí popisek „2025+ amendment" u 100k limitu (platí od 2014): [config.py:41-45](src/countries/cz/config.py:41).
**L5** — `copy_abs` překlopí záporné čisté proceeds (komise > hrubá částka): [fifo_manager.py:384,410](src/engine/fifo_manager.py:384). Chyba ~2× komise.
**L6** — Cash merger ignoruje `quantity_disposed` (realizuje vždy vše) i short loty: [fifo_manager.py:604-661](src/engine/fifo_manager.py:604).
**L7** — AssetCategory v sort klíči není orderable → latentní TypeError: [sorting_utils.py:66,76,87](src/utils/sorting_utils.py:66). Oprava: `.name`.
**L8** — Mutace CAPITAL_REPAYMENT eventu: EUR hodnota v `gross_amount_foreign_currency` při původní `local_currency`: [calculation_engine.py:266-268](src/engine/calculation_engine.py:266).
**L9** — `fx_date_used` uvádí požadované datum i při víkendovém fallbacku; `conversion_note` se nikdy nenastavuje: [fx_policy.py:160-177](src/countries/cz/fx_policy.py:160). Auditní stopa, čísla OK.
**L10** — `prefetch_rates` stažené kurzy zahodí (cache plní jen `get_rate`): [cnb_exchange_rate_provider.py:319-331](src/utils/cnb_exchange_rate_provider.py:319). Jen výkonnost.
**L11** — Parser ČNB neověřuje datum hlavičky listku → pro budoucí datum mlčky vrátí dnešní kurz: [cnb_exchange_rate_provider.py:193-215](src/utils/cnb_exchange_rate_provider.py:193).
**L12** — `wht_paid` v EUR režimu sčítá WHT napříč měnami bez konverze: [plugin.py:162-171](src/countries/cz/plugin.py:162). Jen reporting.
**L13** — Standalone WHT natvrdo do sekce dividend (i daň z úroku/capital repayment): [item_builder.py:350-363](src/countries/cz/item_builder.py:350). Výkazový řádek nesedí se zápočtem.
**L14** — Benevolentní tolerance WHT párování (pattern match bez kontroly částky; proximity do 100 % příjmu; min_rate záporné): [withholding_tax_linker.py:300,346,396-401](src/processing/withholding_tax_linker.py:300).

⚖ = mechanismus potvrzen, konečné daňové posouzení (interpretace ZDP) k potvrzení uživatelem/poradcem.

### Zamítnutí kandidáti / neproblémy

- **R1 (3× duplikovaná DE klasifikace ve FIFO)** — už vyřešeno: jediná kopie v `de/plugin.py`, injektováno callbackem; jen zastaralý docstring (de/plugin.py:52-57).
- **Dvojí konverze měna→EUR→CZK jako taková** — kvantifikováno: při shodných datech obou kroků je odchylka proti přímé konverzi průměrně |0,012 %|, max 0,027 % (±627 CZK na 100 000 USD) — zanedbatelné. Skutečné riziko je míchání DAT (H1, M17, M18), ne křížový kurz.
- **Plugin docstring „let the plugin create one"** — prověřeno: kód fallback nemá (proto H2 platí).

### Doporučené pořadí oprav

1. **H2** (zapojit CNB provider) — bez něj je vše CZK-specifické neaktivní; poté **H1** (short FX swap) a **H3** (WHT refundy).
2. **M1, M2** (časový test, práh 23 %) — triviální opravy s plošným dopadem; **M3, M4, M5** (limit/WHT/prémie).
3. SOY blok **M7–M9**, řazení **M10**, parser **M19, M20**.
4. Interpretační ⚖ nálezy (M6, M12, M15, M22) — nejdřív rozhodnout daňové pojetí, pak implementace.
5. L nálezy průběžně; L2 (zaokrouhlení DAP) spolu s M2.

### Ověření po auditu

- `uv run pytest`: **473 passed** (beze změny proti baseline).
- Repo nezměněno (jediný nový soubor je tento netrackovaný report; reprodukční skripty jen ve scratchpadu).
