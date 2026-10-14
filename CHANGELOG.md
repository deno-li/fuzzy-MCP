# Changelog

Alla betydande ändringar i projektet dokumenteras i den här filen.

Formatet följer [Keep a Changelog](https://keepachangelog.com/sv/1.1.0/)
och projektet använder [Semantic Versioning](https://semver.org/lang/sv/).

## [Unreleased]

### Ändrat

- Eneo-assistentinstruktionen: har flera tabeller samma mått, samma tidsindelning som frågan (time_unit hos SCB,
  annars tidsvariabelns värden) och de regioner och variabler frågan kräver, väljs den som har den senaste perioden
  (last_period hos SCB; hos FoHM och Skolverket sista värdet i tidsvariabeln i metadata). "De senaste fem åren"
  räknas från den senaste perioden bland dessa tabeller, så att en månadstabell (t.ex. till 2026M08) inte flyttar
  fönstret för årsvärden. Täcker ingen tabell hela perioden används den som täcker mest och assistenten säger vilken
  period som saknas. I ett prov valde modellen SCB:s TAB638, som slutar 2024, fast 2025 fanns i en annan tabell, så
  "de senaste fem åren" blev 2020–2024. Servern tipsar redan om last_period i sökresultatet, men det räckte inte.
- Eneo-assistentinstruktionen: egna beräkningar (förändringar mellan perioder, summor av svarsalternativ) får bara
  göras ur hämtade värden när tabellen saknar dem och alla ingående värden är siffror (inte `..`, `.` eller tomma).
  De redovisas som beräknade, med uträkning och en anmärkning om avrundning. En andel räknas aldrig fram som 100
  minus en annan andel. I ett prov summerade modellen avrundade svarsalternativ (Gävleborg: 24 % och 27 % mot 23 %
  och 26 % som komplement) utan att säga det. Skolverkets statistikdatabas redovisas som inte officiell statistik
  (Skolverket klassar bara riksnivån som officiell), en gång vid källan. "Samma period" gäller jämförelser mellan
  regioner, inte förändringar över tid.
- Eneo-assistentinstruktionen: steg 2 kräver att koderna finns i tabellen, inte i den del av metadata som visas
  (normalt 60 värden per variabel), och att kommun- och länskoder kommer från `ref_lookup_region`. I tabeller med
  alla 312 regioner syns inte alltid kommunkoden, och modellen kunde då tro att kommunen saknades. Steg 3 säger hur
  variablerna väljs (det frågan avgränsar, tid med `TOP(1)` om ingen period anges men flera perioder vid en
  utveckling, annars totalvärdet, elimination eller alla värden) och att de jämförda kommunerna, deras län och riket
  hämtas i samma anrop. "Tolka rätt" säger att det inte finns någon lista över liknande kommuner, att en kommuns
  kommungrupp inte tas ur minnet och att länets värde redovisas som länets när ingen tabell med samma mått har
  kommunnivå. Uppdatera instruktionen i befintliga assistenter.

### Rättat

- ENEO.md avsnitt 6: acceptanstestets rad 5 ber om hela kodlistan regioner (`ref_get_code_list`), som kortas med
  `_kortat` vid `FUZZY_MCP_MAX_OUTPUT_CHARS=30000`. Sedan 0.3.0 ryms alla 290 kommuner i `ref_list_municipalities`
  (cirka 26 600 tecken som Eneo räknar dem), så "Visa alla kommuner i Sverige." gav inget kortat svar. Rad 2 godtar
  andra tabeller med folkmängd än TAB638, och rad 3 och 4 anger, som rad 8, vilken källa de kräver.
- `eneo_check.py`: kontrollen "Stort svar" hämtar hela kodlistan regioner i stället för `ref_list_municipalities`,
  som sedan 0.3.0 ryms även utan `FUZZY_MCP_MAX_OUTPUT_CHARS`. Saknas gränsen ger kontrollen fel igen, som ENEO.md
  avsnitt 4 beskriver.

## [0.3.0] - 2026-10-13

### Säkerhet

- Omdirigeringar följs bara till myndigheternas egna ursprung: samma värd, port och protokoll som i en konfigurerad
  bas-URL eller den adress som anropades ([#28](https://github.com/deno-li/fuzzy-MCP/issues/28)). Tidigare jämfördes
  bara värdnamnet, så `https://api.skolverket.se:22/x` följdes, och pekade en bas-URL på en intern spegel blev alla
  portar på den värden nåbara via omdirigering. `https://värd:443` räknas som `https://värd`, och en bas-URL med egen
  port (t.ex. `https://spegel:8443`) tillåter bara den porten. En annan port ger felet "Omdirigeringen till …
  stoppades – servern följer bara omdirigeringar till samma port och protokoll som i myndighetens adress", och felen
  namnger nu även målets port. En spegel med en http-adress följs inte längre till https på samma värd: ange spegelns
  https-adress i `FUZZY_MCP_<KÄLLA>_BASE_URL`.
- Releasearkiven får en signerad byggproveniens (`actions/attest-build-provenance`) som visar att arkivet byggts av
  repots Release-flöde från en viss commit, även när filen hämtats från en spegel. Kontrollera den med
  `gh attestation verify` enligt DRIFT.md avsnitt 2. Bara attesteringsjobbet får signera, och det kör ingen kod från
  PyPI eller paketen ([#24](https://github.com/deno-li/fuzzy-MCP/issues/24)).
- Basimagen i `Dockerfile` är låst till en digest (`python:3.12-slim@sha256:…`) som Dependabot uppdaterar, och
  `BUILDINFO.txt` och releasetexten anger den (`Imagens bas`). Byggverktygen (uv, hatchling, pip, cyclonedx-bom och
  pip-audit med beroenden) installeras med SHA-256-kontroll från `scripts/build_requirements.txt` i stället för att
  lösas på nytt vid varje bygge, även i imagens byggsteg ([#25](https://github.com/deno-li/fuzzy-MCP/issues/25)).
  Bygget är ännu inte reproducerbart byte för byte.
- `skolverket_get_contract` tar bort e-post ur `raw` när personuppgifter är avstängda (`FUZZY_MCP_PERSONAL_DATA=off`),
  som de andra detaljverktygen i Skolenhetsregistret ([#28](https://github.com/deno-li/fuzzy-MCP/issues/28)).

### Rättat

- HTTP-transporterna avslutas med kod 0 när tjänsten stoppas med Ctrl+C (även `nssm stop`) eller SIGTERM
  (`systemctl stop`, `docker stop`), och stänger då HTTP-klienten mot myndigheterna
  ([#22](https://github.com/deno-li/fuzzy-MCP/issues/22)). Tidigare gav Ctrl+C `RuntimeError: Event loop is closed`
  och avslutskod 1, och SIGTERM avslutade processen med 143 utan att stänga klienten. Loggen slutar nu med
  `HTTP-klienten mot myndigheterna är stängd` när servern har anropat någon myndighet. En port som redan används
  ger avslutskod 3 även med äldre uvicorn (0.31–0.44), som tidigare gav 1.
- Med `FUZZY_MCP_MAX_OUTPUT_CHARS` kortas stora svar med många listor på en bråkdel av en sekund i stället för upp
  till en halv minut, och kortningen stoppar inte längre andra sessioner
  ([#23](https://github.com/deno-li/fuzzy-MCP/issues/23)). Servern mäter bara om det som ändrats i varje steg och
  kortar i en egen tråd: 200 listor med 500 värden tar 0,3 s i stället för 33 s. `FUZZY_MCP_TOOL_TIMEOUT` avbryter nu
  även kortningen. Svaren kortas exakt som förut.
- `ref_list_deso` och `ref_lookup_region` tar kommunens officiella namn i genitiv: "Lunds kommun" ger Lund (1281),
  "Göteborgs stad" Göteborg (1480), "Stockholms stad" Stockholm (0180) och "Falu kommun" Falun (2080). Namn som redan
  slutar på s ("Borås stad", "Höganäs kommun") ger ingen felaktig träff
  ([#26](https://github.com/deno-li/fuzzy-MCP/issues/26)).
- Susa-navets kommunfilter (`municipality_code` med kommunnamn) tolkar namn som `ref_list_deso`: ett namn måste peka ut
  en enda kommun. Tidigare valdes bästa träffen, så "Habo" gav Håbo (0305) och "Göte" gav Götene. Nu ger tvetydiga
  namn och stavfel ett fel med förslag ([#26](https://github.com/deno-li/fuzzy-MCP/issues/26)).
- `fohm_get_table_data` och `skolverket_stat_get_table_data` (PxWeb v1) avvisar `*x*` för variabler utan värdelista
  i stället för att skicka det som `filter: all`. Bara `*`, `prefix*` och `*suffix` går, och felet säger det
  ([#28](https://github.com/deno-li/fuzzy-MCP/issues/28)).
- `skolverket_school_unit_changes` sätter `next_offset` till första posten som inte visades när svaret kortas för
  storleken. Poster utan id kunde annars ge dubbletter på nästa sida
  ([#28](https://github.com/deno-li/fuzzy-MCP/issues/28)).
- Katalognoten om DeSO-förändringar anger rätt antal: 85 koder förekommer bara som mottagare i SCB:s förändringslogg,
  och 84 av dem får "ändrad gräns" ([#27](https://github.com/deno-li/fuzzy-MCP/issues/27)).
- Dokumentationen hänvisar till Eneo-kontrollen i releasekörningen för 0.2.2, versionsrubrikerna i CHANGELOG.md har
  länkar, och några inaktuella rader i DRIFT.md, DEVELOPMENT.md och `scripts/verify_release.py` är rättade
  ([#27](https://github.com/deno-li/fuzzy-MCP/issues/27)).
- Med `FUZZY_MCP_MAX_OUTPUT_CHARS` säger ett kortat svar inte längre att det är komplett, och bläddring hoppar inte
  över poster ([#20](https://github.com/deno-li/fuzzy-MCP/issues/20)). Efter en kortning är `truncated`,
  `…_truncated` och `returned` rätt i objektet som håller listan och objekten ovanför; tabelldata (`*_get_table_data`)
  anger till exempel `data.truncated=true` med 367 av 870 rader i stället för `false`. Verktyg som bläddrar deklarerar
  hur de fortsätter:
  - offset (Dataportalens sökningar, Skolenhetsregistrets sökningar och ändringslista, kursplanslistorna, geodata):
    `next_offset = offset + antal visade`, även på en kortad sista sida;
  - sidnummer (`scb_search_tables`, Planerad utbildnings sökningar): `next_page` tas bort och `_kortat.fortsatt` ger
    sida och sidstorlek som börjar vid eller före första posten som inte visades;
  - Susa-navets sökningar kortar själva vid hela träffar och ger `next_page`/`next_skip_matches` för den första
    träffen som inte visades;
  - versionslistorna och `skolverket_get_subject` namnger det som utelämnades (`_kortat.utelamnade` respektive
    `fortsatt` med `course_codes`), och `skolverket_pe_salsa` ger skolenhetskoderna som saknas.
  Utan deklarerad bläddring tas `next_*` bort vid en kortning (ett stopp, aldrig en lucka). Notes, filter- och
  urvalsekon, dimensioner och liknande index kortas aldrig, `raw` lämnas ut helt innan något annat kortas, URL:er och
  frågor halveras inte, och verktygets egen `_kortat`-notis behålls.
- `ref_get_code_list` följer `FUZZY_MCP_MAX_OUTPUT_CHARS` och ger inte längre fel vid 30000 tecken.
- `scb_search_tables` begränsar `page_size` till 1–100 även i svarets eko när källan inte anger sidstorleken.
- `scb_geodata_get_features` anger `next_offset` när fler objekt finns, och notisen säger vilket `offset` nästa
  anrop ska använda. Tidigare hoppade bläddring med `offset += limit` över objekt när svaret kortats till
  teckenbudgeten (447 av Stockholms 569 DeSO). Med `FUZZY_MCP_MAX_OUTPUT_CHARS` kortar verktyget själv vid hela
  objekt inom taket, så att serverns generella kortning inte behöver korta en sida som inte går att fortsätta från
  ([#21](https://github.com/deno-li/fuzzy-MCP/issues/21)). Utdataschemat har ett nytt fält: synkronisera verktygen
  i Eneo efter uppgraderingen.

### Ändrat

- `scb_search_tables` (och `fohm_`/`skolverket_stat_search_tables` med v2-API) har fälten `truncated` och
  `next_page`. `_kortat` kan innehålla `fortsatt`, `utelamnade` och `utelamnade_falt`, och `kortade` anger antal listor
  (`listor`) och tecken (`enhet`). `*_get_default_selection` och `scb_geodata_download_url` lämnas ut hela eller
  avvisas, som frågerecepten. Synkronisera verktygen i Eneo och uppdatera assistentinstruktionen.
- Release-flödet kör hela CI (`ci.yml`) för commiten innan paketen byggs: Python 3.11–3.14, kontrollen av
  verktygstabellen och REUSE ingår nu ([#28](https://github.com/deno-li/fuzzy-MCP/issues/28)).
- `build_release.py --with-image` kräver basimagens Python-version (`--python 3.12`)
  ([#25](https://github.com/deno-li/fuzzy-MCP/issues/25)).

## [0.2.2] - 2026-10-12

### Rättat

- Serverns gemensamma API-notishistorik innehåller bara källans identifierare och säker livscykelinformation.
  Anropsadresser och råa headervärden delas inte mellan klienter; detaljer i ett enskilt anrops svar behålls.
- Frågerecept från `scb_build_query`, `fohm_build_query` och `skolverket_stat_build_query` kortas inte längre
  när `FUZZY_MCP_MAX_OUTPUT_CHARS` är satt. Ett för stort recept ger ett tydligt fel så att urvalet kan minskas;
  URL, frågekropp, Power Query och curl förblir överens om hela urvalet när receptet ryms.
- `scb_geodata_get_features` använder samma standardsortering från första sidan, så att bläddring inte byter
  ordning mellan `offset=0` och senare sidor. Dataportalens utgivarfilter använder samma URI-upplösning på alla
  sidor, även när anropet börjar med `offset>0`.
- Personuppgiftsfiltreringen hanterar även personliga `mailto:`-adresser i strukturerade sökresultat och i
  Dataportalens RDF-subjekt och referenser. Sökresultat, detaljsvar och rådata följer samma val för personuppgifter.
- Dataportalen visar en `mailto:`-identifierare för en utgivare, upphovsperson eller kontakt (även en datatjänsts)
  bara när metadata visar att den inte tillhör en privatperson. Klassificeringen görs även med
  `resolve_publisher_names=false`, och när uppslaget misslyckas eller inte hittar något utelämnas adressen (även i
  `raw` och `attribution`) med en notis. En privatpersons eller kontaktpersons kända adress och namn tas också bort
  ur andra fält: en kontaktpunkt av organisationstyp med samma adress eller namn, distributionernas och
  datatjänsternas adresser, `dataset_uri` för en agentpost och `raw`. Ett fel vid sökning med en privatpersons
  utgivarfilter återger inte längre anropsadressen.
- Med `FUZZY_MCP_PERSONAL_DATA=off` ersätter Susa-navet även e-postadresser och `mailto:`-länkar i fritext och HTML
  (beskrivningar, ansökningsinstruktioner) med `[e-post utelämnad]`, också i `raw`. Det gäller även adresser som
  döljs med teckenreferenser (`&#64;`), osynliga tecken eller inbäddad markup (`anna@<wbr>kth.se`), medan `@` i en
  URL-sökväg (`logo@2x.png`) eller en version (`react@18.2.0`) lämnas orört.
- `fuzzy_api_notices` visar varje notis en gång, så att upprepade anrop till ett API inte trycker undan andra
  källors notiser.
- En omdirigering med en `Location` som inte går att tolka ger ett vanligt källfel utan omförsök, och
  `Retry-After: nan` ger standardväntetid i stället för ett oväntat undantag.
- Containerbygget skickar proxyvariablernas namn till Docker, som hämtar värdena ur miljön, så att byggkommandot
  och dess återgivning vid fel inte innehåller proxyinloggning.
- Giltiga `TOP`-uttryck med blanksteg normaliseras före PxWeb v1-anrop även när metadata saknar värdelista.
- Eneos separata testklient använder AnyIO 4.14.2 i stället för 4.14.1, som omfattas av
  GHSA-82r6-8w77-94w6, GHSA-3w57-8xmc-8v26 och GHSA-5p39-cfhj-2xmp. Dokumentationen skiljer denna
  säkerhetsuppdaterade klientprofil (även PyJWT 2.15.0) från Eneo 2.2.2:s ursprungliga låsfil.
- Eneo-guiden redovisar verifieringen av den publicerade 0.2.1-imagen med Eneo 2.2.2:s hash-låsta klientberoenden
  och Python 3.11, med länk till releasekörningen. Acceptanstest med modell och myndighetsåtkomst i driftmiljön
  skiljs från de godkända klientkontrollerna.

## [0.2.1] - 2026-10-12

### Tillagt

- `docs/KALLKONTROLL.md` ingår nu i både Windows- och Linux-leveranspaketet, så länkarna från README och
  DEVELOPMENT fungerar även i de uppackade paketen.
- Release-kontrollerna verifierar de färdiga arkivens kontrollsummor och innehåll och testar de uppackade paketen.
  Den exporterade container-imagen läses tillbaka med `docker load` och provas med och utan token. Eneo-kontrollen
  körs med klientberoenden låsta till Eneo 2.2.2, och Windows-paketet installeras utan nät och röktestas på en
  Windows-runner innan release-utkastet skapas.

### Ändrat

- Version 0.2.1; driftens image- och venv-exempel använder den nya versionen.
- Eneo-guiden är granskad mot v2.2.2, commit `af647d28b1902af76503c1711ff51217deee7141`, 2026-10-12.
  MCP-klienten och dess låsta beroenden är oförändrade från 2.2.1. Kontrollen med klientberoendena passerade mot den
  levererade 0.2.0-imagen, men ersätter inte acceptanstest i en full Eneo-installation. Uppgraderingsnoteringen
  skiljer Eneos inkommande API-trafik och CORS från backendens utgående MCP-anrop; transport och token behålls.

### Rättat

- 0.2.0 publicerades 2026-10-12; `publiccode.yml` och rubriken nedan angav 2026-10-11.
- DRIFT.md och ENEO.md: med Docker som lagrar images i containerd visar `docker image inspect` manifestets digest i
  stället för image-id på releasesidan. Dokumenten visar hur digesten läses ur arkivets `index.json`.
- ENEO.md och kommentarerna i compose-filerna: egress-proxyn byggs med `docker compose … build` innan den gamla
  versionen stoppas, och `--build-arg` hör till den raden (`up` tar inte flaggan). Nya verktyg är avslagna i
  befintliga assistenter i Eneo 2.2 även efter godkännandet och måste slås på där. Kontrollen efter start visar också
  att en myndighet nås genom proxyn. Knappen heter **Synkronisera verktyg** i Eneos svenska gränssnitt. `chmod` på
  `egress/` så att Squid kan läsa `squid.conf` på servrar med snäv umask. Med organisationens proxy: en `https_proxy`
  med gemener går före tilläggets `HTTPS_PROXY`, så anropen når inte Squid, och vid TLS-inspektion ska
  `SSL_CERT_FILE` vara kvar.

## [0.2.0] - 2026-10-12

### Säkerhet

- Omdirigeringar följs bara mellan myndigheternas värdar (värdarna i de konfigurerade bas-URL:erna och den värd som
  anropades), aldrig från https till http (okrypterat bara så länge varje steg varit okrypterat, t.ex. mot en spegel
  med en http-adress) och högst fem i rad. Tidigare följde HTTP-klienten upp till 20
  omdirigeringar till vilken adress som helst, så en komprometterad myndighetsvärd eller en öppen omdirigering kunde
  få servern att hämta en intern adress (t.ex. en tjänst på Eneos nät) och lämna svaret till modellen. Ett stoppat
  försök ger ett fel som bara namnger målets schema och värd och säger om det var en annan värd eller en övergång
  till http, och inget hämtas därifrån.
- Anropen till myndigheterna loggas inte längre. MCP-biblioteket sätter loggnivån INFO, och då loggade httpx2 varje
  anrop med hela URL:en (sökord, namn och koordinater i frågesträngen). Nu loggas bara httpx2:s varningar, och alla
  loggrader – även bibliotekets `Tool … failed: …` med myndighetens adress – skrivs med URL:er utan frågesträng
  (`?…`). Ett felmeddelande kan fortfarande återge ett ogiltigt argument (t.ex. ett kommunnamn).
- Utgående trafik i Eneo-uppsättningen: tillägget `docs/eneo/docker-compose.egress.yml` gör `fuzzy_egress` till ett
  internt nät och skickar trafiken via en Squid-proxy (`docs/eneo/egress/`, Ubuntu 24.04, körs som användaren
  `proxy`, skrivskyddad, högst 4 096 öppna filer så att den ryms i 128 MB, `squid.conf` monterad så att en ändring bara
  kräver omstart) som bara släpper fram HTTPS till de sex värdarna i DRIFT.md avsnitt 6. Provad med Squid
  6.14: myndigheternas värdar släpps igenom (även via en överordnad proxy med `cache_peer`); andra värdar,
  IP-adresser, andra portar och okrypterad HTTP får 403. Leveranspaketet innehåller filerna.

### Tillagt

- `FUZZY_MCP_TOOL_TIMEOUT` (sekunder, 0 = av): ett verktyg som inte är klart i tid svarar med ett tydligt fel och
  avbryter sina återstående anrop till myndigheterna, och loggen får en rad om det. Eneo-compose: `50`. Eneo ger upp
  efter 60 s, och i Eneo 2.2 räknas både timeouts och felsvar mot kretsbrytaren som pausar servern i 60 s efter fem
  fel i rad.
- docs/ENEO.md avsnitt 5.1 om lokal modell (t.ex. vLLM): mät verktygens verkliga tokenkostnad, välj profil efter
  kontextfönstret och anpassa `FUZZY_MCP_MAX_OUTPUT_CHARS`; acceptanstest 8 (skolenhet → DeSO/RegSO);
  återställning till en tidigare version med godkännande i Eneo.
- `scb_geodata_locate`: DeSO- och RegSO-kod för en punkt (SWEREF 99 TM eller WGS84) eller för en skolenhet
  (skolenhetskod → besöksadressens koordinater ur Skolenhetsregistret), genom punktsökning (INTERSECTS) i SCB:s
  WFS-lager. Tillhörigheten räknas bara ur koordinater, aldrig ur namn eller adresstext. Svaret anger vilken adress
  koordinaterna kommer från (`skolenhet.adresstyp`): en annan adress än besöksadressen används bara när besöksadressen
  saknar koordinater, och det sägs då i `notes`; orimliga registerkoordinater (t.ex. `0`) hoppas över till förmån för
  adressens WGS84-par. När en träff är DeSO 2025/RegSO 2025 påminner `notes` om suffixet `_DeSO2025`/`_RegSO2025` i
  Statistikdatabasen. Modulen `geo` räknar om SWEREF 99 TM ↔ WGS84 (Gauss-Krüger) och kontrollerar att punkten
  ligger i Sverige; axelordningen `POINT(N E)` för EPSG:3006 i CQL ligger i `scb_geodata`. När
  Skolenhetsregistrets SWEREF 99 TM- och WGS84-koordinater för samma adress ligger mer än 50 m isär används
  SWEREF 99 TM, och `notes` säger det.
- `scb_geodata_get_features`: parametrarna `attributes`, `offset`, `sort_by` och `srs` samt svarsfälten `offset` och
  `srs`; `scb_geodata_download_url`: parametern och svarsfältet `srs`. Felen för geometriattributet (`sp_geometry`)
  i `filters`, `attributes` eller `sort_by` säger att det är lagrets geometri och hur den fås.
- `ref_lookup_deso`: fältet `ssd_koder` med koden så som Statistikdatabasens variabel Region skriver den per version
  (`vs_DeSO2018`/`vs_RegSO2020` rena koder t.o.m. referensår 2023, `vs_DeSO2025`/`vs_RegSO2025` med suffix
  `_DeSO2025`/`_RegSO2025` fr.o.m. referensår 2024), och en kopplingsnyckel `koordinat (SWEREF 99 TM / WGS84)` i
  entitetskatalogen.
- `docs/KALLKONTROLL.md`: resultatet av Källkontroll-körningarna 2026-10-10 och 2026-10-11 (geodata,
  Statistikdatabasen, SCB:s sidor och nyckelfiler, Socialstyrelsens statistikdatabas) och det som fortfarande är
  overifierat.
- Källkontroll: kontroller av EWKT (`SRID=4326;POINT(...)`) i CQL i båda axelordningarna, punktsökning mot
  `DeSO_2018`, `RegSO_2020` och `RegSO_2025` samt de parameterkombinationer servern skickar: `startIndex` + `sortBy`
  med `CQL_FILTER`, `sortBy` på `regsokod` och `propertyName` med geometri tillsammans med `srsName`
  (`wfs.intersects.srid4326*`, `wfs.intersects.y_x.<lager>`, `wfs.sida.filter`, `wfs.sida.regso`,
  `wfs.crs.propertyName`). Körningen 2026-10-11 gav status 200 och väntat innehåll för alla åtta nya kontroller
  (var och en mot ett lager) och visade att GeoServer läser `SRID=4326` som lon lat (lon lat träffade DeSO 0180C4040,
  lat lon gav 0 objekt); punktsökningen är därmed verifierad för alla fyra DeSO-/RegSO-lagren. SCB-sidorna namnges
  med de två sista sökvägssegmenten så att två sidor som slutar lika inte får samma namn.
- Arbetsflödet Källkontroll (`.github/workflows/verify-sources.yml`) och `scripts/verify_sources.py`: en fast lista
  med läsande anrop som kontrollerar API-kontrakten för SCB:s geodata (DeSO/RegSO), SCB:s statistikdatabas, SCB:s
  DeSO-sidor och nyckelfiler samt Socialstyrelsens statistikdatabas. Svaren skrivs i jobbloggen. Startas för hand,
  har bara läsrätt och används inte av servern.
- Kodlistorna `deso_regso_2018`, `deso_regso_2025` och `deso_forandringar`: SCB:s nyckelfiler "Koppling DeSO och RegSO"
  (DeSO 2018–RegSO 2020 och DeSO 2025–RegSO 2025, daterade 2026-03-25) och "Historiska förändringar i DeSO" (daterad
  2025-09-19), med filnamn, datum och sha256 i varje kodlistas `fil`. DeSO 2018 har 5 984 koder och DeSO 2025 6 160;
  5 835 koder finns i båda, 149 upphör och 325 är nya. RegSO 2020 och RegSO 2025 har 3 363 koder vardera (en kod
  bytt: 2584R001 bort, 2523R011 ny; 10 RegSO har nytt namn). Förändringsloggen har 1 234 rader (1 232 daterade
  2025-01-01 och 2 daterade 2018-02-21) och täcker alla upphörda och nya koder. Kategorierna A/B/C beskrivs i
  kodlistornas `kategorier`.
- Verktygen `ref_lookup_deso` (för DeSO: version, RegSO, förändringar och om antal kan summeras över en förändring;
  för RegSO: namn, DeSO-koder per version och om namnet ändrats) och `ref_list_deso` (områdena i en kommun per
  RegSO).
- Prompten `omradesprofil` (områdesprofil för DeSO/RegSO med jämförelse mot kommun och rike) och profilen
  Områdesstatistik (DeSO och RegSO) (`scb.statistik,scb.geodata,reference`) för Eneo.
- `scripts/build_deso_reference.py` som bygger kodlistorna ur SCB:s xlsx-filer med bara standardbiblioteket och
  registrerar sha256 och datum per fil.

### Ändrat

- Version 0.2.0; User-Agent anger paketets version (`fuzzy-mcp/0.2.0`).
- `skolverket_search_school_units` med `with_details=true` hämtar detaljer för högst 19 rader (tidigare 25). Då ryms
  listanropet och detaljanropen i api.skolverket.se:s 20 anrop per 10 s; tidigare tog ett sådant anrop minst 10 s,
  och fem samtidiga omkring 60 s.
- Eneo-compose: `FUZZY_MCP_TOOL_TIMEOUT=50`, `FUZZY_MCP_CACHE_MAX_MB=24` (cachen räknar svarens storlek, men tolkade
  svar tar 3–5 gånger mer minne, och containern har 512 MB), loggrotation (json-file, 5 × 10 MB) och imagen
  `fuzzy-mcp:0.2.0`.
- docs/ENEO.md är kontrollerad mot Eneos källkod och dokumentation (docs.eneo.ai/v2.2) för taggen v2.2.1; där de
  skiljer sig gäller koden. Kretsbrytaren räknar även felsvar (`isError`) och timeouts, per backend-process;
  verktygssvaren i en tur får ta högst hälften av modellens Max indatatokens och skickas med i senare turer; högst 10
  verktygsrundor per svar; verktygets titel står först i beskrivningen; servernamnet skrivs med gemener och `_` i
  stället för t.ex. å, ä, ö; Eneos egna inställningar (`MCP_CLIENT_*`, `MCP_TOOL_OUTPUT_MAX_CHARS`,
  `MCP_CIRCUIT_BREAKER_*`). Nytt: modellen måste ha **Verktygsstöd** i Eneo och Max indata- + utdatatokens får inte
  överstiga vLLM:s `--max-model-len`; `ENCRYPTION_KEY` krävs inte av Eneo, så token kan ligga i klartext – med en
  kontroll; Eneo sparar verktygsanropen med samtalet; nätverken `object_content_net` och `proxy_tier`; eget internt
  nät via `docker-compose.override.yml`; säkerhetsklassning på servern; rotation av token i Eneo; felsökningsrader
  för "External tool service temporarily unavailable", saknade verktyg och okrypterbar token.
- DRIFT.md och ENEO.md: imagen hämtas från leveranspaketet och image-id jämförs med releasesidan; en image som byggs
  från repot taggas `fuzzy-mcp:dev` och används bara för test. DRIFT.md beskriver omdirigeringar, att `ufw` inte
  stoppar containertrafik, loggarnas innehåll, cachens minne, vilka data som lämnar servern och vad
  `FUZZY_MCP_PERSONAL_DATA=off` inte täcker (fritext).
- `scripts/generate_tool_docs.py` fyller även profiltabellen i docs/ENEO.md och kontrollerar den med `--check`.
- Verifierade uppgifter från Källkontroll 2026-10-10 i entitetskatalogen, kopplingsnycklarna, informationsmodellen,
  README, prompten `omradesprofil`, serverinstruktionen och Eneo-assistentinstruktionen: lagernamnen `DeSO_2018`,
  `DeSO_2025`, `RegSO_2020`, `RegSO_2025` och attributen `desokod`, `regsokod`, `regsonamn`, `kommunkod`, `lanskod`,
  `version`, `referensdatum`, `sp_geometry`; kodlistorna `vs_DeSO2018`, `vs_DeSO2025`, `vs_RegSO2020`, `vs_RegSO2025`
  (äldre tabeller "uppdateras ej": `vs_DeSoHE`, `vs_RegSo1`) och referensåren (2018/2020 t.o.m. 2023, 2025 fr.o.m.
  2024; äldre år räknas inte om).
- Testfixturerna för geodata bygger på verkliga svar från WFS-tjänsten (Källkontroll 2026-10-10).
- Texterna om DeSO/RegSO i entitetskatalogen, kopplingsnycklarna, informationsmodellen, serverinstruktionen, README
  och `scb_geodata_get_features`: två versionspar (DeSO 2018 med RegSO 2020, DeSO 2025 med RegSO 2025; 687 koder
  behåller kod men har ändrad gräns enligt loggen: 603 med egen rad och 84 som bara tar emot från andra koder),
  DeSO-kodens kategori A/B/C (femte tecknet) och reservsiffra (nionde tecknet, används vid delningar i DeSO 2025),
  och att RegSO-namn bara är unika inom kommunen och kan ändras.
- Release-flödet bygger alltid den commit på main som flödet startades från (`GITHUB_SHA`); indata `ref` är borttaget.
  Ingen kod från en annan commit körs i flödet, vilket stänger CodeQL-varningarna om cache poisoning, och flödet blir
  enklare (en utcheckning, ingen jämförelse av arbetsflödesfiler). En äldre commit släpps med den manuella vägen i
  DEVELOPMENT.md.
- Testerna jämför värdnamn och mängder i stället för delsträngar i URL:er (CodeQL: incomplete URL substring
  sanitization).
- CI-flödet låser alla actions till commit-SHA, som release-flödet (även `fsfe/reuse-action`).

### Rättat

- docs/ENEO.md angav att underlaget var Eneos gren `main`, commit `d305507`. Commiten ligger på `develop` och ingår
  inte i Eneo 2.2.0 eller 2.2.1. Tokensiffran för alla verktyg (28 500) stämde inte med verktygsprofiler.md (29 000).
- DRIFT.md lovade att servern bara anropar de fasta adresserna och att loggen saknar frågesträngar; det stämde inte
  för omdirigeringar och för httpx2:s loggrader, och gör det nu.
- Kodlistorna för DeSO/RegSO i Statistikdatabasen: entitetskatalogen påstod utan belägg att tabellerna på
  DeSO/RegSO-nivå använder `vs_DeSO*`/`vs_RegSO*`. Uppgiften togs bort i #5 och återinförs nu med de verifierade
  namnen (2026-10-10, TAB6680 har alla fyra) och suffixregeln: koderna i `vs_DeSO2025`/`vs_RegSO2025` har suffix
  `_DeSO2025`/`_RegSO2025`, de i `vs_DeSO2018`/`vs_RegSO2020` är rena, och äldre tabeller märkta "uppdateras ej"
  har `vs_DeSoHE`/`vs_RegSo1` (rena koder). Vilka en tabell har framgår av `scb_get_table_metadata`.
- `scb_geodata_get_features` beskrev DeSO-områden "med koder och namn"; DeSO har bara koder, RegSO har namn.

## [0.1.2] - 2026-10-08

Första publika utgåvan. Projektet ligger nu i ett nytt publikt repo med ren historik. Det tidigare repot är privat,
och utgåvorna 0.1.0 och 0.1.1 finns bara där. Servern fungerar som i 0.1.1.

### Ändrat

- Uppförandekoden hänvisar till Contributor Covenant 2.1 och sammanfattar den med egna ord, i stället för att återge
  delar av texten (som är licensierad under CC BY 4.0).
- `REUSE.toml` anger källorna för testdata (myndigheternas API-svar) och för kodlistan SS 12000 (kodvärden ur SIS
  standard, Sambruks EGIL-profil och Skolverket). README beskriver samma sak.
- DRIFT.md: etiketten `org.opencontainers.image.licenses` gäller bara fuzzy-mcp, och källkoden till basimagens
  Debian-paket och till `pywin32` finns hos Debian respektive pywin32-projektet.
- Resursen `fuzzy://crosswalk/informationsmodell` använder neutrala, beskrivande begrepp (t.ex. "Indikatorvärde",
  "Kodöversättning") i stället för namn i tabellform, och uppgifter om enskilda beskrivs i allmänna ordalag.

### Rättat

- Planerad utbildning: skolenhetskod och kommun- eller länskod godtar bara ASCII-siffror. Helbreddssiffror och
  arabisk-indiska siffror skickades tidigare vidare och gav 404 från API:t i stället för ett tydligt fel.

## [0.1.1] - 2026-10-08

### Tillagt

- Arbetsflödet **Release** (`.github/workflows/release.yml`, startas för hand från main): tester i ett eget jobb,
  bygger och kontrollerar Windows- och Linux-paketen från exakt den testade commiten och skapar ett release-utkast med
  paket, kontrollsummor och releasetext (`scripts/release_notes.py`). Bara commits på main kan släppas, actions är
  låsta till commit-SHA, bara jobbet som skapar utkastet har skrivrätt, kontrollerna av befintlig tagg och release
  stoppar vid fel, och taggen skapas när utkastet publiceras.
- `build_release.py` kör pip, cyclonedx-py och pip-audit i fasta versioner.

### Ändrat

- `sbom.cdx.json` beskriver fuzzy-mcp som rotkomponent (med licensen `MIT AND CC0-1.0`), varje komponents licens
  enligt dess wheel och de direkta beroendena i beroendegrafen; övriga beroenden mellan komponenterna anges som
  okända (`compositions`). SBOM:en byggs reproducerbart (utan tidsstämpel).
- Container-imagen: gruppen `fuzzy` har gid 10001 (som användaren), och inga pip-variabler finns kvar i miljön.
- Felmeddelandet för okända källor nämner både `--sources` och `FUZZY_MCP_SOURCES`.
- CI kontrollerar REUSE även i den byggda sdist-filen.

### Rättat

- `reuse lint` på en uppackad sdist godkänner nu den genererade `PKG-INFO` (CC0 1.0 i `REUSE.toml`).
- DRIFT.md: NSSM väntar minst 5 sekunder före omstart men upp till 4 minuter (tjänsten visas som Pausad) om processen
  avslutas inom 1,5 sekunder; systemd försöker var 5:e sekund. Beskriver loggraderna `Terminating session: …` och
  `Unsupported upgrade request`, att Enter behövs en gång till vid prompten `>>` och att basimagens Debian-paket har
  egna licenser (SBOM för hela imagen med t.ex. Trivy, Grype eller syft).
- Testdata innehåller inget riktigt telefonnummer till en skola.

## [0.1.0] - 2026-10-07

### Tillagt

- MCP-server (MCP Python SDK 2.x, `MCPServer`) med transporterna stdio, streamable HTTP och SSE.
- SCB Statistikdatabasen via PxWebApi 2: sökning, ämnesträd, tabellinformation, metadata, data,
  kodlistor, standardurval och delbara frågor (GET-URL, Power Query M, curl).
- Folkhälsomyndigheten Folkhälsodata via PxWeb API v1: databaser, bläddring, sökning, metadata, data och
  återanvändbara frågor.
- Skolverket: Statistikdatabasen (PxWeb v1: kommunala jämförelsetal och underlag för analys), Skolenhetsregistret v2,
  Syllabus/Läroplan-API v1, Planerad utbildning v4 och Susa-navet (EMIL 3).
- Sveriges dataportal: sökning och detaljer för dataset (DCAT-AP-SE) via EntryStore.
- Referensdata: 21 län och 290 kommuner med fuzzy-uppslagning; kodlistor för betygsskalor (inkl. Gy25 och skala 1–10),
  gymnasieprogram, introduktionsprogram, komvux, betygsdokument, SS 12000 och skolformer; entitetskatalog med
  kopplingsnycklar och en konceptuell brygga till BBIC/ICF.
- Promptar för vanliga arbetsflöden och autokomplettering av kommuner och kodlistor.
- Projektfiler enligt DIGG:s projektmall, REUSE, CI och livetester mot de riktiga API:erna.
- Gemensamt HTTP-lager med cache (antal- och bytegräns), omförsök, anropsgränser och rapportering av
  `Deprecation`/`Sunset`.
- SCB:s öppna geodata (WFS): lager, attribut, objekt (DeSO, RegSO, tätorter m.m.) och nedladdningslänkar.
- Fabrik för PxWebApi 2-verktyg: Folkhälsomyndigheten och Skolverkets statistikdatabas kan byta till v2 via
  `FUZZY_MCP_<KÄLLA>_API_VERSION=v2` utan kodändring. Metadata visar META-ID-länkar och enhet, referensperiod
  och måttyp per innehåll.
- Resursen `fuzzy://crosswalk/informationsmodell` som kopplar en generisk analysmodell till verktyg och nycklar.
- Fältet `verifiering` på varje post i kodlistorna (författning, officiell_spec, verifierat_uttag, myndighetswebb,
  referenskod, härlett, okänd) med kodlistan `verifiering` som vokabulär; fördelningen visas i
  `ref_list_code_lists` per samling. Kopplingarna i BBIC/ICF-bryggan är märkta `härlett`. Vid blandade källor
  avgör det svagaste underlaget; nästlade objekt ärver postens värde och uppslagstabeller får ett värde i
  `verifiering_samlingar`.
- Verifieringsvärden för kodlistorna satta fil för fil ur researchanteckningarna, med citerat underlag och oberoende
  granskning: 590 av 634 poster har nu dokumenterat ursprung (okänd: 44). Bekräftade datafel rättade, bl.a.
  giltighetsdatum för skalorna A–E, E–F och E-only, lagrum för examensbevis i komvux, mönstret för Gy25-nivåkoder
  och missvisande metadatatexter.
- Kodlistornas text är helt på svenska; nycklarna i `ss12000.andra_versioner` heter nu `kalla`, `falt` och
  `avvikelser` (tidigare `source`, `fields`, `deviations`).

- Driftunderlag: `docs/DRIFT.md`, `scripts/build_release.py` (med `.ps1`/`.sh`) som bygger ett leveranspaket per
  plattform (låsta beroenden med SHA-256, hjul för offlineinstallation, SBOM, pip-audit, kontrollsummor),
  `Dockerfile` (icke-root, hälsokontroll, stöd för proxy med egen CA vid bygget), `scripts/smoke_test.py` och
  `GET /healthz` på HTTP-transporterna.

- Eneo-stöd (`docs/ENEO.md`, `docs/eneo/`): verifierade fakta om Eneos MCP-klient, compose-exempel bredvid Eneos
  backend, rekommenderade inställningar, verktygsprofiler per assistent, assistentinstruktion, acceptanstest och
  `scripts/eneo_check.py` som kontrollerar servern med Eneos klientbibliotek (MCP SDK 1.x). Provat med SDK 1.28.1
  (Eneos låsta version), 1.30.0 och 1.9.4.
- Token-autentisering för HTTP (`FUZZY_MCP_AUTH_TOKEN_FILE`, Bearer eller API-nyckelheader), `FUZZY_MCP_STATELESS`,
  `FUZZY_MCP_PERSONAL_DATA=off` och `FUZZY_MCP_MAX_OUTPUT_CHARS` (kortar svar och behåller giltig JSON).
- Alla verktyg svarar med kompakt JSON i ett textblock; verktygens indatascheman saknar `$ref`.
  Verktygsbeskrivningarna skickas utan docstring-indrag, så de är lika (och kortare) på Python 3.11–3.13.
- `build_release.py --with-image` lägger en färdig container-image i leveranspaketet. Paketet behåller repots
  sökvägar för dokumenten (`docs/`, `LICENSES/` m.fl.) så att länkarna fungerar; `smoke_test.py` och
  `eneo_check.py` ligger i roten.
- `docs/eneo/verktygsprofiler.md` med exakta verktyg och storlek per profil, genererad från servern
  (`scripts/generate_tool_docs.py`, kontrolleras i CI).

- `BUILDINFO.txt` i leveranspaketet med commit, plattform, byggtid och verktygsversioner.
- DRIFT.md: kontroll av `SHA256SUMS` i PowerShell, tjänsten på Windows startas först när tokenfil och loggmapp
  finns, administratörsgruppen anges med SID (fungerar på svenskspråkig Windows) och vilket paket som gäller för
  Windows-server respektive container bredvid Eneo.
- `anyio` och `uvicorn` deklareras som direkta beroenden, eftersom HTTP-transporten använder dem.
- Dependabot samlar uppdateringar av GitHub Actions i en PR (`groups` i `.github/dependabot.yml`).
- CI: `actions/checkout` v7, `actions/setup-python` v7 (Node 24, inga Node 20-varningar) och `fsfe/reuse-action` v6
  (reuse 6.2).
- Python 3.14 testas i CI. Windows-paketet rekommenderas för en Python-version som fortfarande får
  Windows-installationer (3.12 får bara källkodsutgåvor).
- Container-imagen får OCI-etiketter (version, commit – med `-dirty` vid ändringar som inte är incheckade –, källa,
  licens); image-id står i `BUILDINFO.txt`, och lokala sökvägar (CA-fil, absolut `--out`) visas inte där.
- `build_release.py` bygger som standard för Python 3.14 på Windows och 3.12 på Linux.
- Åtkomstloggen skrivs till stderr som all annan logg (en Windows-tjänst med bara `AppStderr` tappade den förut).
- `smoke_test.py` och containerns `HEALTHCHECK` går aldrig via `HTTP(S)_PROXY` mot 127.0.0.1/localhost.
- DRIFT.md: Python för alla användare på Windows, `C:\fuzzy-mcp` och `C:\install` skapas nya och låses till
  administratörer, kontroll av okända filer i paketet, `nssm.exe` under `C:\Program Files`, tjänstekontot via
  `sc.exe`, loggrotation även medan tjänsten kör och en
  väntan på `/healthz` som rapporterar fel; systemd 247+ med token som argument (startar inte utan),
  `RestartPreventExitStatus=2 243` och kommandon för att skapa token; Linux-distributioner per Python-version; vad
  åtkomstloggen innehåller.
- DRIFT.md: blocken för kontroll, installation och uppgradering (avsnitt 2, 3 och 10) körs som en enhet och
  avbryts vid första fel – kontrollsumman spärrar uppackningen, avvikelser mot `SHA256SUMS` (även dolda filer)
  stoppar installationen och en venv som bygger på en Python under `C:\Users` stoppas. NSSM anropas med full
  sökväg, stannar vid konfigurationsfel (`AppExit 2 Exit`), väntar 5 s före omstart och har en fast arbetskatalog.
  Uppgraderingen har egna kommandon för en ny venv, för att peka om tjänsten och för att vänta på `/healthz`.
- Releaserutinen i DEVELOPMENT.md: paketens kontrollsummor publiceras i GitHub-releasen, som DRIFT.md hänvisar till.
- `build_release.py --with-image` (bara med `--platform linux`) kontrollerar att imagens Python-beroenden har samma
  versioner som `requirements.lock` (så att SBOM och `audit.txt` gäller även imagen) och skriver det i
  `BUILDINFO.txt`.
- DRIFT.md: väntan på `/healthz` går aldrig via en proxy (PowerShell 7 och curl), kontrollblocket lämnar
  `C:\install` vid fel, en avbruten uppgradering går att köra om, och avslutskod 3 (adressen kan inte användas)
  är beskriven. Containerns `HEALTHCHECK` tolkar `FUZZY_MCP_PORT` som servern.
- DRIFT.md, Dockerfile och compose-exemplet: servern litar på operativsystemets rotcertifikat, och `SSL_CERT_FILE`
  (som ersätter dem) ska bara sättas till en komplett CA-bundle – inte en fil med bara organisationens CA. Samma
  sak för `--ca-file`/`--secret id=ca` vid container-bygget. Proxy: `HTTPS_PROXY` krävs för tjänsten (PAC/WPAD och
  NTLM stöds inte).
- `smoke_test.py --stdio` skickar hela miljön till servern, så att `HTTPS_PROXY`, `SSL_CERT_FILE` och
  `FUZZY_MCP_*` gäller även i röktestet (MCP-biblioteket skickar annars bara ett fåtal variabler), och startar den
  alltid med `--transport stdio` (även där miljön anger HTTP, t.ex. i containern). HTTP-inställningarna (port,
  `FUZZY_MCP_STATELESS`, `FUZZY_MCP_ALLOW_UNAUTHENTICATED`) kontrolleras bara för HTTP-transporterna.
- README: VS Code-exemplet gäller `.mcp.json` i projektroten (`.vscode/mcp.json` använder nyckeln `servers`) och
  beskriver proxyinställningarna, även `UV_SYSTEM_CERTS` för `uvx` vid TLS-inspektion.
- Licensuttrycket i paketmetadata är `MIT AND CC0-1.0` (kodlistor, testdata och dokumentation är CC0), och båda
  licenstexterna följer med wheel och sdist; `REUSE.toml` följer med sdist och leveranspaketet. Bygget kräver
  hatchling 1.27 eller senare.

### Säkerhet och integritet

- Testdata innehåller inga riktiga personers e-postadresser (fiktiv adress på den reserverade domänen `.example`)
  och ett uppenbart fiktivt rektorsnamn.
- HTTP-transporterna kräver alltid token, även på localhost (en omvänd proxy på samma värd kan annars göra servern
  nåbar utan att den märker det). Undantag med `--allow-unauthenticated` (`FUZZY_MCP_ALLOW_UNAUTHENTICATED=true`).
- Testdata innehåller inga riktiga kontaktpersoner (fiktiv kontakt i SCB-metadata).
- Namn på enskilda personer (rektor, c/o-namn, kontaktpersoner, privatpersoner som utgivare) returneras bara via
  uttryckliga parametrar märkta "personuppgift" och tas annars bort även ur råsvar.
- Strikt validering av id:n och koder (inga `.`/`..`-segment, inga Unicode-siffror, exakta värdnamn för
  dataportal-länkar) och fullständig escaping av Solr-frågor.
- Storleksgränser för alla verktygssvar så att de håller sig under värdklienternas tokengräns.
- HTTP-transporterna: `Origin`-lista (`FUZZY_MCP_ALLOWED_ORIGINS`), `[::1]` i värdlistan och varning när skyddet
  mot DNS-rebinding stängs av; den delade HTTP-klienten stängs först när sista sessionen avslutas.
- HTTP på annat än localhost kräver token (servern startar inte utan, om inte
  `FUZZY_MCP_ALLOW_UNAUTHENTICATED=true`). `/healthz` visar bara `{"status": "ok"}`.
- Åtkomstloggen tar inte med frågesträngen (`/mcp?…`, även för en URL i absolut form), så en token som en
  felkonfigurerad klient lägger i URL:en hamnar inte i loggen. WebSocket är avstängt i uvicorn.
- En `SSL_CERT_FILE` som saknas, inte kan läsas eller inte är i PEM-format (och en `SSL_CERT_DIR` som inte är en
  katalog, när `SSL_CERT_FILE` inte är satt) ger `Konfigurationsfel` vid start i stället för att varje verktyg
  misslyckas.
- `FUZZY_MCP_STATELESS` och `FUZZY_MCP_ALLOW_UNAUTHENTICATED` tolkas strikt: okända värden ger konfigurationsfel
  i stället för att tyst betyda "av". Ogiltig `FUZZY_MCP_PORT`/`--port` och `FUZZY_MCP_TRANSPORT` ger också
  `Konfigurationsfel: …` och avslutskod 2 (tidigare en traceback med kod 1 för porten), så att tjänsten inte startas
  om i en slinga.
- Container-imagen innehåller inte pip, setuptools, wheel eller packaging från basimagen (pip 25.0.1 i basimagen hade kända sårbarheter). DRIFT.md anger att SBOM
  och `audit.txt` bara täcker `requirements.lock`, inte imagens bas.
- Testdata innehåller inga riktiga efternamn som c/o-namn.
- Med `FUZZY_MCP_PERSONAL_DATA=off` utelämnas även e-postadresser från Skolenhetsregistret och Susa-navet (fält,
  listor, `mailto:`-länkar och råsvar), med en notering om att uppgifter har utelämnats.

[Unreleased]: https://github.com/deno-li/fuzzy-MCP/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/deno-li/fuzzy-MCP/compare/v0.2.2...v0.3.0
[0.2.2]: https://github.com/deno-li/fuzzy-MCP/compare/v0.2.1...v0.2.2
[0.2.1]: https://github.com/deno-li/fuzzy-MCP/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/deno-li/fuzzy-MCP/compare/v0.1.2...v0.2.0
[0.1.2]: https://github.com/deno-li/fuzzy-MCP/releases/tag/v0.1.2
