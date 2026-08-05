# Dotaz pro daňového poradce — výměna akcií při přeměně (fúzi/akvizici)

> **Odpovězeno 5. 8. 2026.** Zachováno jako záznam toho, co bylo dotázáno.
> Odpověď mimo jiné opravila i citaci použitou níže: časový test je
> §4 odst. 1 **písm. u)**, limit 100 000 Kč **písm. t)** — ne písm. w).
> Shrnutí dopadů na engine viz `docs/cz-tax-policy.md`.

## Kontext

Zpracovávám podklady pro daňové přiznání z výpisů zahraničního brokera (Interactive Brokers) pomocí vlastního nástroje, který dopočítává příjmy podle **§10 ZDP** (prodej cenných papírů), uplatňuje **časový test podle §4 odst. 1 písm. w ZDP** (3 kalendářní roky, u nabytí před 1. 1. 2014 přechodný 6měsíční test podle čl. II bodu 5 zák. opatření č. 344/2013 Sb.) a roční limit osvobozených příjmů 100 000 Kč.

Nástroj zatím **neumí zpracovat situaci, kdy jsou držené akcie vyměněny za akcie jiné společnosti** při přeměně nebo akvizici. Než to naprogramuji, potřebuji potvrdit daňový režim — na odpovědích přímo závisí, jaká čísla nástroj vykáže.

## Výchozí situace

- **Poplatník:** fyzická osoba, český daňový rezident
- **Majetek:** nepodnikatelský, cenné papíry drženy na účtu u zahraničního brokera
- **Emitenti:** převážně **americké** společnosti, v menší míře evropské
- **Podíl na emitentovi:** řádově setiny procenta, tedy výrazně pod 5 %
- **Situace:** během držby dojde k přeměně/akvizici a staré akcie jsou nahrazeny akciemi jiné společnosti — buď zcela bezhotovostně (výměna akcie za akcii v určitém poměru), nebo s peněžním doplatkem

## Otázky

### 1) Je samotná výměna zdanitelným příjmem?

Vzniká už v momentě výměny příjem podle §10 ZDP (tj. posuzuje se to jako zpeněžení starých akcií v tržní hodnotě nově získaných), nebo je výměna **daňově neutrální** a zdanění se odkládá až na skutečný prodej nových akcií?

### 2) Nabývací cena

Pokud je výměna neutrální — přenáší se nabývací cena starých akcií na nové akcie v plné výši? Jak se má rozpočítat, když výměnný poměr není 1:1 (např. za 1 starou akcii jsou 2,5 nové, nebo naopak za 3 staré 1 nová)?

### 3) Časový test — pro mě nejdůležitější otázka

**Běží 3letá lhůta podle §4 odst. 1 písm. w ZDP dál od nabytí *starých* akcií, nebo začíná znovu dnem výměny?**

Konkrétní příklad:

| Datum | Událost |
|---|---|
| 15. 3. 2023 | nákup akcií společnosti A |
| 10. 6. 2025 | fúze — akcie A vyměněny za akcie společnosti B |
| 20. 9. 2026 | prodej akcií B |

- **Varianta (a)** — lhůta běží od 15. 3. 2023: k datu prodeje je splněna (více než 3 roky) → **příjem osvobozen**
- **Varianta (b)** — lhůta běží od 10. 6. 2025: není splněna → **příjem zdanitelný**

Rozdíl je pro výslednou daň zásadní. Která varianta je správná, případně za jakých podmínek?

### 4) Zachovává se pre-2014 režim?

Pokud byly staré akcie nabyty **před 1. 1. 2014** (a podléhaly by tedy příznivějšímu 6měsíčnímu testu podle přechodného ustanovení), zůstává tento režim zachován i pro akcie získané výměnou, nebo se na nové akcie použije standardní 3letý test?

### 5) Kombinovaná transakce (akcie + peněžní doplatek)

Když je součástí plnění vedle nových akcií i peněžní doplatek — je zdanitelný **pouze ten doplatek** (a akciová část zůstává neutrální), nebo se celá transakce posuzuje jako prodej starých akcií?

Pokud je zdanitelný jen doplatek: jak se proti němu uplatní nabývací cena — poměrnou částí, nebo jinak?

### 6) Záleží na sídle emitenta?

Liší se odpověď podle toho, jde-li o přeměnu mezi společnostmi **z EU** (kde se, předpokládám, uplatní česká implementace směrnice o fúzích) versus o přeměnu **mimo EU** — typicky mezi americkými společnostmi, což je v mém případě většina?

Pokud ano, jaké ustanovení se použije v každém z těch případů?

### 7) Roční limit 100 000 Kč

Pokud je výměna daňově neutrální, nevstupuje hodnota vyměněných akcií do ročního limitu osvobozených příjmů z prodeje cenných papírů (100 000 Kč) — testuje se limit teprve při skutečném prodeji nových akcií? Chápu to správně?

### 8) Doklady

Broker mi u takové akce reportuje pouze: typ korporátní akce, datum, symbol a ISIN staré i nové akcie, počet kusů a výměnný poměr. **Ocenění (tržní hodnotu ke dni výměny) nedostávám.**

Je tento rozsah pro doložení dostačující? Pokud odpověď na otázku 1 nebo 5 znamená, že je potřeba tržní hodnota ke dni výměny, jak ji mám doložit?

## Proč se ptám tak podrobně

Odpovědi se přímo překlopí do chování nástroje:

| Odpověď | Důsledek v nástroji |
|---|---|
| Otázka 1 — neutrální | při výměně se nevykáže žádný příjem; loty se pouze převedou na nový cenný papír |
| Otázka 1 — zdanitelné | při výměně se vykáže příjem podle §10 a potřebuji ocenění |
| Otázka 3 — varianta (a) | u nových akcií se zachová **původní datum nabytí** → časový test může být splněn |
| Otázka 3 — varianta (b) | datem nabytí je den výměny → časový test se restartuje |
| Otázka 5 | rozhoduje, zda doplatek generuje samostatný zdanitelný příjem |

Pokud si u některé otázky nejste jistý bez znalosti konkrétní transakce, dejte prosím vědět — mohu doložit výpisy ke konkrétní přeměně.

---

*Poznámka: nástroj je pomůcka pro sestavení přiznání, ne daňové poradenství. Výsledná čísla před podáním kontroluji.*
