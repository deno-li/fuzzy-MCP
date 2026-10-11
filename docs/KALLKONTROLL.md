<!--
SPDX-FileCopyrightText: 2026 Deniz Özer

SPDX-License-Identifier: CC0-1.0
-->

# Källkontroll – resultat

**Datum:** 2026-10-10. **Körning:** <https://github.com/deno-li/fuzzy-MCP/actions/runs/38048135516> (arbetsflödet
**Källkontroll**, fyra jobb – `scb-geodata`, `scb-pxweb`, `scb-nycklar`, `socialstyrelsen` – alla gröna).
**Skript:** `scripts/verify_sources.py` i commit `943a582` på `main` (körningens `head_sha`
`943a58231c2df8d47f2e69a3e9feae8b5d222aed`).

Det här dokumentet är en läsning av körningens logg (raderna `VERIFY`, `BODY`, `TEXT` och `B64`). Uppgifterna nedan
är det som faktiskt stod i svaren; tal, fältnamn och koder är källornas egna. Det som inte kontrollerades står under
[Inte verifierat](#inte-verifierat). Ett tomt svar eller ett HTTP-fel hade varit ett resultat att redovisa, inte ett
bevis för att något saknas – i den här körningen gav alla anrop 200 utom det avsiktliga felanropet (404).

## SCB:s öppna geodata (WFS) – `scb-geodata`

- Tjänst: <https://geodata.scb.se/geoserver/stat/wfs> (GeoServer, WFS 1.1.0). `GetCapabilities` listar **49 lager**,
  bland dem `DeSO_2018`, `DeSO_2025`, `RegSO_2020` och `RegSO_2025` med `DefaultSRS urn:x-ogc:def:crs:EPSG:3006`
  (SWEREF 99 TM). Rumsliga operatorer: BBOX, Beyond, Contains, Crosses, DWithin, Disjoint, Equals, Intersects,
  Overlaps, Touches, Within.
- `DescribeFeatureType` (`outputFormat=application/json`) ger för DeSO-lagren attributen `objectid` (int),
  `objektidentitet`, `objekttyp` (`'deso'`), `desokod`, `regsokod`, `lanskod`, `kommunkod`, `version` (t.ex.
  `'2025_v2'`, `'2018_v3'`), `ansvarig_organisation`, `referensdatum` (`'20250101'` respektive `'20180101'`) och
  geometrin `sp_geometry` (`gml:Polygon`). RegSO-lagren har `regsokod` och `regsonamn` (ingen `desokod`), `objekttyp`
  `'regso'`, `version` `'2025_v2'` respektive `'2020_v3'`, och i övrigt samma attribut.
- `resultType=hits`: `DeSO_2018` **5 984**, `DeSO_2025` **6 160**, `RegSO_2020` **3 363**, `RegSO_2025` **3 363**.
  WFS 2.0.0 med `resultType=hits` fungerar också (`numberMatched="6160"` för `DeSO_2025`).
- `GetFeature` med `outputFormat=application/json` ger `{type, features, totalFeatures, numberMatched,
  numberReturned, timeStamp, crs}`. Med `propertyName` utan geometrin är `crs` `null` och varje objekts `geometry`
  `null`.
- `CQL_FILTER=kommunkod='0180'`: **544** objekt i `DeSO_2018`, **569** i `DeSO_2025`, **127** i `RegSO_2020` och i
  `RegSO_2025`.
- Sidning: `maxFeatures` + `startIndex` + `sortBy=desokod` fungerar. `startIndex=0&maxFeatures=4` gav 0114A0010,
  0114C1010, 0114C1020, 0114C1030; `startIndex=2&maxFeatures=2` gav element 3–4 (0114C1020, 0114C1030).
- `srsName=EPSG:4326` (liksom `urn:ogc:def:crs:EPSG::4326`) fungerar för JSON: koordinaterna skrivs `[lon, lat]`
  (13.09577306, 55.5417832) med `crs` `urn:ogc:def:crs:EPSG::4326`. Med `srsName=EPSG:3006` (det
  `scb_geodata_get_features` skickar som standard när geometri ingår) skrivs `[E, N]` (379844.1709, 6156729.8055)
  med `crs` `urn:ogc:def:crs:EPSG::3006`. JSON med geometri utan `srsName` prövades inte; lagrens `DefaultSRS` är
  EPSG:3006. Det är samma hörnpunkt i DeSO 1280C1070 – ett verifierat omräkningspar SWEREF 99 TM ↔ WGS84.
- Axelordning i CQL för EPSG:3006 (WFS 1.1.0): `INTERSECTS(sp_geometry,POINT(674032 6580822))` (E N) gav **0**
  objekt; `INTERSECTS(sp_geometry,POINT(6580822 674032))` (N E) gav DeSO **0180C4040** (RegSO 0180R047). En punkt
  anges alltså `POINT(N E)`. CSV-formatets WKT skriver också N E (`POLYGON ((6156729.8055 379844.1709, …))`).
- `outputFormat=csv` (`text/csv`), `shape-zip` (`application/zip`) och `geopackage`
  (`application/geopackage+sqlite3`) fungerar. `propertyName` fungerar för csv: rubriken blir
  `FID,objectid,objektidentitet,objekttyp,desokod,regsokod,lanskod,kommunkod,version,ansvarig_organisation,referensdatum`
  (utan `sp_geometry`).

## SCB:s statistikdatabas (PxWebApi 2) – `scb-pxweb`

- `/api/v2/config`: `apiVersion` **2.3.2**, `maxDataCells` **150000**, `maxCallsPerTimeWindow` **30** per
  `timeWindow` **10** sekunder, licens CC0 (`sourceReferences`: "Källa: SCB"), `defaultDataFormat` `json-stat2`,
  `dataFormats` json-stat2, csv, px, xlsx, html, json-px.
- Sökning: `query=DeSO` gav **29** tabeller, `query=RegSO` **48**. Exempel: TAB6574 Folkmängden per region efter
  ålder och kön, 2010–2025; TAB6638 Antal lägenheter efter region (DeSO/RegSO 2025) och upplåtelseform, 2024–2025;
  TAB6680 Arbetsmarknadsstatus efter bostadens belägenhet, region (DeSO/RegSO), kön och ålder, 2020–2024; TAB6258
  Antal lägenheter efter region (DeSO 2018/RegSO 2020) och upplåtelseform (uppdateras ej), 2015–2023.
- Kodlistor för variabeln Region (`/codelists/<id>`):
  - `vs_DeSO2018` "DeSO 2018 t.o.m. år 2023": **5 984** koder, rena koder (`code` = `label` = `valueMap`, t.ex.
    `0114A0010`).
  - `vs_DeSO2025` "DeSO 2025 fr.o.m. år 2024": **6 160** koder, `code` **med suffix `_DeSO2025`**
    (`0114A0010_DeSO2025`), `label` = ren kod (`0114A0010`), `valueMap` = den suffixade koden.
  - `vs_RegSO2020` "RegSO 2020 t.o.m. år 2023": **3 363** koder, rena koder, `label` t.ex.
    `Upplands Väsby (Bollstanäs)`.
  - `vs_RegSO2025` "RegSO 2025 fr.o.m. år 2024": **3 363** koder, `code` **med suffix `_RegSO2025`**
    (`0114R001_RegSO2025`), `label` `0114R001 Upplands Väsby (Bollstanäs)`.
  - Äldre tabeller märkta "uppdateras ej" (TAB6258, TAB5551, TAB5199) har i stället `vs_DeSoHE` "DeSO t.o.m. år
    2023" (5 984, rena koder) och `vs_RegSo1` "RegSO t.o.m. år 2023" (3 363, rena koder).
- En tabell med båda versionerna (TAB6680, liksom TAB6682) har alla fyra kodlistorna och **18 870** regionkoder:
  5 984 rena DeSO 2018 + 6 160 suffixade DeSO 2025 + 3 363 rena RegSO 2020 + 3 363 suffixade RegSO 2025. TAB6638
  har bara 2025-versionerna (9 523 koder, alla med suffix). Vilka kodlistor en tabell har framgår av
  `scb_get_table_metadata`.
- SCB:s tabellnoter. TAB6680: "Från och med referensåret 2024 publiceras en ny uppdaterad version av DeSO och
  RegSO (DeSO 2025 och RegSO 2025, tidigare DeSO 2018 och RegSO 2020). Flera revideringar har gjorts i de nya
  versionerna. Vissa ändringar är mer omfattande, vilket gör att alla områden inte är jämförbara över tid."
  TAB6638 och TAB6258: "I mars 2025 publicerades nya versioner av DeSO och RegSO (DeSO2025 och RegSO2025). Tidigare
  årgångar uppdateras inte med de nya indelningarna." Referensåren är alltså: DeSO 2018/RegSO 2020 t.o.m. 2023,
  DeSO 2025/RegSO 2025 fr.o.m. 2024; äldre år räknas inte om.
- Jokertecken: `valueCodes[Region]=0180C*` i TAB6638 matchade **569** regioner (suffixade koder). Datauttag med
  `outputFormat=json-stat2` fungerar (TAB6638 med Region `0114A0010_DeSO2025`, Tid 2024 och 2025).

## SCB:s sidor och nyckelfiler – `scb-nycklar`

- Fem sidor på scb.se lästes som text: DeSO och RegSO under regionala indelningar, deras informationssidor om
  tabellerna i Statistikdatabasen, och DeSO under öppna geodata. Citat ur texterna:
  - "DeSO har inga namn utan endast en kod. Det är den koden du behöver känna till för att kunna använda
    statistiken."
  - "Statistik som publiceras under 2025 är på den nya versionen DeSO 2025 och består av 6 160 områden."
  - "Årgångar bakåt i tiden uppdateras inte på den nya indelningen. Ingen ny statistik kommer heller att publiceras
    som öppen data på den gamla versionen."
  - "RegSO delar in Sverige i 3 363 områden med en befolkning mellan cirka 650 och 23 000 invånare. Antalet RegSO i
    kommunerna varierar från två till 147 områden."
- Nyckelfilerna är länkade från sidan om DeSO under öppna geodata, i katalogen
  `https://www.scb.se/contentassets/923c3627a8a042a5b9215e8ff3bde0a3/`:

  | Fil | Byte | sha256 | Last-Modified |
  | --- | --- | --- | --- |
  | `deso-historiska-forandringar-2025-09-19.xlsx` | 64 148 | `c57825792c4a0754515b62847fbcd6aa94805579212cedb44ef1c71bd3face91` | 2025-09-23 |
  | `koppling-deso2018-regso2020_2026-03-25.xlsx` | 250 587 | `1117b35503e166944a0147174b9cd3152d769846ed587c464d232524d7b6395e` | 2026-03-31 |
  | `koppling-deso2025-regso2025_2026-03-25.xlsx` | 240 574 | `560a0a673f45bb1556cc2a4a72234fb44d497f4be0e54fe3e032e8ab71ed74a2` | 2026-03-31 |

  Alla tre sha256 är **identiska** med `fil.sha256` i kodlistorna `deso_forandringar`, `deso_regso_2018` och
  `deso_regso_2025`: den medföljande referensdatan är byggd ur exakt de filer SCB publicerade vid körningen.
  Fil-URL:erna kan ändras när SCB publicerar nya versioner; en ny körning visar de aktuella.
- Samma sida länkar WFS-guiden
  `https://www.scb.se/contentassets/302de7b6076847e08e260d112475dfc5/scbs-oppna-geodata-via-wms-och-wfs-tjanster_20250303.pdf`
  (länken registrerad, filen inte hämtad).
- I den här körningen fick två sidor samma loggnamn (`scb.sida.demografiska-statistikomraden-deso`, båda sökvägarna
  slutar så). Skriptet namnger nu sidorna med de två sista sökvägssegmenten
  (`regionala-indelningar.demografiska-statistikomraden-deso` respektive
  `oppna-geodata.demografiska-statistikomraden-deso`).

## Socialstyrelsens statistikdatabas (API v1) – `socialstyrelsen`

- `https://sdb.socialstyrelsen.se/api` → `[{"kod": "v1", "text": "version 1"}]`; `/api/v1` → språken `sv` och `en`;
  `/api/v1/sv` → **15 ämnen** med `namn` och `text`: `amning`, `diagnoserislutenoppenvard`,
  `diagnoserislutenvard`, `diagnoserioppenvard`, `drgstatistikislutenvard`, `dodsorsaker_manad`, `dodsorsaker`,
  `graviditeterforlossningarochnyfodda`, `lakemedel`, `operationerislutenvard`, `operationerioppenvard`,
  `skadorochskadehandelserisverigeskommunerochlan`, `tandhalsa`, `yttreorsakertillskadorochforgiftningarbarn`,
  `yttreorsakertillskadorochforgiftningar` (amning, diagnoser i sluten/öppen/sluten+öppen vård, DRG, dödsorsaker per
  år och månad, graviditeter/förlossningar/nyfödda, läkemedel, operationer sluten/öppen, skador i kommuner och län,
  tandhälsa, yttre orsaker barn/alla).
- `/api/v1/sv/<ämne>` ger variablerna som `[{namn, text, info}]` (dödsorsaker: `region`, `alder`, `kon`, `matt`,
  `ar`, `diagnos`; `info` kan innehålla HTML). `/api/v1/sv/<ämne>/<variabel>` ger värdena med `id`, `kod` (där det
  finns) och `text`, ibland fler fält (`grupp`, `info`, `vardform`).
- Resultat: `/api/v1/sv/<ämne>/resultat/<variabel>/<värde>/…?per_sida=N` ger
  `{data: [{…Id, ar, varde}], amne, sida, per_sida, sidor, nasta_sida, foregaende_sida}`. Raderna har fälten
  `<variabel>Id` (`diagnosId`, `regionId`, `alderId`, `konId`, `mattId`, `vardformId`, `typId`), `ar` och `varde`.
  **`varde` är en sträng** (`"123"`, `"639,3"` med decimalkomma). `nasta_sida` är en absolut URL (med `http://`).
  Dödsorsaker `matt/1/ar/2025` med `per_sida=3` gav 36 306 sidor.
- **Region-id varierar per ämne.** Skador i kommuner och län: `id` = `kod` = kommun- eller länskod som sträng
  (`"00"`, `"01"`, `"0114"`; 321 värden). Dödsorsaker, läkemedel, amning och yttre orsaker barn: numeriskt `id` med
  `kod` (`{id: 0, kod: "00", text: "Riket"}`, `{id: 1, kod: "01", text: "Stockholms län"}`; 22 värden). Tandhälsa:
  312 värden. Graviditeter, förlossningar och nyfödda: femsiffriga koder (`{id: 0, kod: "00000", text: "Riket"}`,
  `{id: 110, kod: "00110", text: "Stockholm"}`).
- `ar`: dödsorsaker 1997–2025 (29 värden, numeriska `id`); skador har treårsperioder som `id` (`"1987-1989"` …
  `"2023-2025"`, 37 värden, med fältet `vardform`).
- Okänt ämne (`/api/v1/sv/finnsinte`) ger **404 med `text/html`**, ingen JSON-felkropp. `Accept: application/xml`
  ignoreras – svaret är fortfarande `application/json`.

## Inte verifierat

Påstå inte något av det här förrän en körning visat det:

- `srsName` för andra format än JSON (csv, shape-zip, geopackage).
- Att GeoServer tar emot EWKT (`SRID=4326;POINT(...)`) i CQL. Kontrollerna `wfs.intersects.srid4326`
  (`POINT(lon lat)`) och `wfs.intersects.srid4326_latlon` (`POINT(lat lon)`) finns med från och med nästa körning:
  en träff i exakt en av dem visar vilken ordning som läses, en felstatus att syntaxen inte godtas.
- Enheterna för `DWithin`.
- WFS 2.0.0 för annat än `resultType=hits`.
- Punktsökning (`INTERSECTS`) mot andra lager än `DeSO_2025`. `scb_geodata_locate` söker som standard även
  `RegSO_2025` och tillåter `DeSO_2018`, `RegSO_2020` och andra polygonlager, men körningen (och
  `test_scb_geodata_locate_live`) prövade bara `DeSO_2025`. Kontrollerna `wfs.intersects.y_x.<lager>` för de tre
  andra DeSO-/RegSO-lagren finns med från och med nästa körning, med samma punkt `POINT(6580822 674032)`; DeSO-träffen
  anger `regsokod` 0180R047, så det är det väntade svaret från `RegSO_2025`.
- Parameterkombinationer som `scb_geodata_get_features` skickar men körningen inte prövade: `startIndex` + `sortBy`
  tillsammans med `CQL_FILTER` (sidningskontrollen hade inget filter), `sortBy` på andra attribut än `desokod`
  (servern sorterar RegSO-lager på `regsokod` vid `offset`) och `propertyName` med geometrin tillsammans med
  `srsName` (`wfs.crs.*` hade inget `propertyName`). Kontrollerna `wfs.sida.filter`, `wfs.sida.regso` och
  `wfs.crs.propertyName` finns med från och med nästa körning.
- Innehållet i nyckelfilerna är kontrollerat via sha256 mot kodlistorna, inte läst på nytt; WFS-guiden (pdf) är
  inte läst maskinellt.

## Köra om och läsa loggen

1. Actions → **Källkontroll** → **Run workflow** (från `main` eller en gren). Varje grupp är ett eget jobb, och
   körningens sammanfattning visar status per kontroll.
2. Jobbloggen innehåller detaljerna, en post per rad:
   - `VERIFY {json}` – ett anrop: `check`, `url`, `status`, `content_type`, `bytes`, `sha256`, eventuella
     `headers` (Deprecation, Sunset, Last-Modified, ETag …) och `summary`; vid nätverksfel `error` i stället.
   - `BODY <check> {json}` – hela svaret för små JSON-svar.
   - `TEXT <check> <i>/<n> "json"` – en webbsida som text i delar, varje del en JSON-sträng.
   - `B64 <namn> <i>/<n> <data>` – en nedladdad fil i base64-delar. Slå ihop delarna i ordning, avkoda och
     kontrollera sha256 mot `VERIFY`-raden.
3. Kontrollnamnen: `wfs.*` (geodata), `pxweb.*` (Statistikdatabasen), `scb.sida.<två sista sökvägssegment>` och
   `scb.nyckelfil<N>` (sidor och filer), `sdb.*` (Socialstyrelsen). `start` och `slut` ramar in varje grupp.
4. Lokalt, från en miljö med nätåtkomst: `python -I scripts/verify_sources.py --group scb-geodata`. Skriptet
   avslutas med fel bara om inget anrop fick något svar alls.
