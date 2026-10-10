# fuzzy-MCP – svensk öppen data för AI-assistenter

[![CI](https://github.com/deno-li/fuzzy-MCP/actions/workflows/ci.yml/badge.svg)](https://github.com/deno-li/fuzzy-MCP/actions/workflows/ci.yml)
[![Licens: MIT och CC0 1.0](https://img.shields.io/badge/licens-MIT%20%2B%20CC0--1.0-blue.svg)](REUSE.toml)
[![publiccode.yml](https://img.shields.io/badge/publiccode.yml-0.4-green.svg)](publiccode.yml)
[![MCP](https://img.shields.io/badge/MCP-Python%20SDK%202.x-purple.svg)](https://py.sdk.modelcontextprotocol.io/)

**fuzzy-mcp** är en [MCP-server](https://modelcontextprotocol.io/) som ger AI-assistenter (Claude, Copilot,
Eneo m.fl.) läsåtkomst till svenska myndigheters öppna data. Den kopplar dessutom ihop källorna via
kommunkod, skolenhetskod, organisationsnummer och skolformskoder.

| Myndighet | Källa | API | Licens |
| --- | --- | --- | --- |
| SCB | Statistikdatabasen | PxWebApi 2.0 | CC0 1.0 |
| SCB | Öppna geodata (lagren `DeSO_2018`, `DeSO_2025`, `RegSO_2020`, `RegSO_2025`, tätorter m.m.) | OGC WFS 1.1.0 (GeoServer) | CC0 1.0 |
| Skolverket | Statistikdatabasen (kommunala jämförelsetal, underlag för analys) | PxWeb API v1 | CC0 1.0 |
| Skolverket | Skolenhetsregistret | REST v2 | CC0 1.0 |
| Skolverket | Läroplan/Syllabus (ämnen, kurser, Gy25-nivåer, program, läroplaner) | REST v1 | CC0 1.0 |
| Skolverket | Planerad utbildning (skolenheter, statistik, Skolenkäten, SALSA, utbildningar) | REST v4 (HAL) | CC0 1.0 |
| Skolverket | Susa-navet (utbildningsinformation från högskola, YH, folkhögskola, komvux) | REST EMIL 3 | CC0 1.0 |
| Folkhälsomyndigheten | Folkhälsodata (HLV, HBSC, SmiNet, vaccinationer m.m.) | PxWeb API v1 | Anges inte i API:et |
| Digg | Sveriges dataportal (dataset från alla myndigheter, DCAT-AP-SE) | EntryStore | Per dataset |

Allt är **skrivskyddat**. Servern hämtar bara öppna data och returnerar som standard inga personuppgifter. Namn på
enskilda personer som finns i källorna (rektor, c/o-namn, kontaktpersoner för statistik) visas bara när verktyget
anropas med en uttrycklig parameter vars beskrivning säger *personuppgift*, och tas då även bort ur råsvar annars.

## Innehåll

- [Snabbstart](#snabbstart)
- [Anslut till en AI-klient](#anslut-till-en-ai-klient)
- [Exempel](#exempel)
- [Verktyg, resurser och promptar](#verktyg-resurser-och-promptar)
- [Koppla ihop källorna](#koppla-ihop-källorna)
- [Konfiguration](#konfiguration)
- [Kända begränsningar](#kända-begränsningar)
- [Utveckling och bidrag](#utveckling-och-bidrag)
- [English summary](#english-summary)

## Snabbstart

Kräver Python 3.11 eller senare.

```sh
# med uv (rekommenderas)
uvx --from git+https://github.com/deno-li/fuzzy-MCP fuzzy-mcp --help

# eller installera i en virtuell miljö
pip install "git+https://github.com/deno-li/fuzzy-MCP"
fuzzy-mcp                                   # stdio (för lokala klienter)
fuzzy-mcp --transport streamable-http --port 8000 --allow-unauthenticated   # lokal HTTP utan token, bara för utveckling
```

## Anslut till en AI-klient

**Claude Code**

```sh
claude mcp add fuzzy-mcp -- uvx --from git+https://github.com/deno-li/fuzzy-MCP fuzzy-mcp
```

**Claude Desktop** (`claude_desktop_config.json`) eller **VS Code** (`.mcp.json` i projektroten; i den äldre
`.vscode/mcp.json` heter nyckeln `servers` i stället för `mcpServers`):

```json
{
  "mcpServers": {
    "fuzzy-mcp": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/deno-li/fuzzy-MCP", "fuzzy-mcp"],
      "env": { "FUZZY_MCP_SOURCES": "scb,skolverket,fohm,reference" }
    }
  }
}
```

Bakom en företagsproxy: sätt `HTTPS_PROXY` i `env`. Gör proxyn TLS-inspektion behöver `uvx` dessutom
`"UV_SYSTEM_CERTS": "true"` i `env` (uv 0.11 eller senare; äldre uv: `"UV_NATIVE_TLS": "true"`; Claude Code:
`-e UV_SYSTEM_CERTS=true`), eftersom uv annars inte använder
operativsystemets rotcertifikat. Servern själv använder dem; `SSL_CERT_FILE` behövs bara om organisationens CA
saknas där, och då som en komplett CA-bundle (se `docs/DRIFT.md` avsnitt 4).

**Som HTTP-tjänst (t.ex. i en container eller bakom en proxy)**

```sh
python -c "import secrets; print(secrets.token_urlsafe(48))" > token.txt   # läsbar bara för tjänstekontot
fuzzy-mcp --transport streamable-http --host 0.0.0.0 --port 8000 --allowed-hosts mcp.exempel.se \
  --auth-token-file token.txt
```

Klienterna skickar `Authorization: Bearer <token>` (eller `X-API-Key`). Utan token startar HTTP-transporten inte
(utom med `--allow-unauthenticated` för lokal utveckling). `--allowed-hosts` behåller skyddet mot DNS-rebinding när servern nås via ett riktigt värdnamn. För Eneo:
följ [docs/ENEO.md](docs/ENEO.md) (nätverk, rekommenderade inställningar och verktygsprofiler).

> **Tips:** Servern har många verktyg. Begränsa med `FUZZY_MCP_SOURCES` (eller `--sources`) till det du behöver,
> t.ex. `scb,skolverket.statistik,reference`, så blir det lättare för modellen att välja rätt verktyg.

## Exempel

Frågor du kan ställa till assistenten:

- *"Hur har folkmängden i Gävle utvecklats de senaste fem åren jämfört med riket?"*
  SCB: `scb_search_tables` → `scb_get_table_metadata` → `scb_get_table_data` med `{"Region": ["2180", "00"], "Tid": ["TOP(5)"]}`.
- *"Visa elevantal och betyg i grundskolan för Gävle och riket enligt kommunala jämförelsetal."*
  Skolverkets statistikdatabas: `skolverket_stat_browse` → `skolverket_stat_get_table_data` med `{"variable": ["2"], "level": ["2180", "00"], "time": ["TOP(5)"]}`.
- *"Lista aktiva grundskolor i Sundsvall och visa behöriga lärare och meritvärde för en av dem."*
  `ref_lookup_region` → `skolverket_search_school_units` → `skolverket_pe_school_unit_statistics`.
- *"Vad säger ämnesplanen för Matematik nivå 1a i Gy25 om betygskriterier?"*
  `skolverket_get_subject` (MATE) eller `skolverket_get_course` (MATE1A00X).
- *"Andel 13-åringar som mobbats i skolan i Gävleborgs län?"*
  `fohm_search_tables` ("mobbning") → `fohm_get_table_data`.
- *"Bygg en Power Query som hämtar folkmängd per kommun i Gävleborg till Power BI."*
  `scb_build_query` ger GET-URL, POST-kropp och färdig M-kod.
- *"Vilket RegSO hör DeSO 2180C1010 till, och har området ändrats mellan DeSO 2018 och DeSO 2025?"*
  `ref_lookup_deso` (lokalt, utan nätverk) → `scb_search_tables` ("DeSO") → `scb_get_table_data`.
- *"Vilket DeSO ligger skolenheten 43038662 i?"*
  `scb_geodata_locate` (`school_unit_code`: besöksadressens koordinater ur Skolenhetsregistret mot lagren
  `DeSO_2025`/`RegSO_2025`) → `ref_lookup_deso` → `scb_get_table_data` med koden under `ssd_koder`.

Promptarna `analysera_kommun`, `jamfor_kommuner`, `hitta_statistik`, `power_bi_fraga`, `skolenhet_profil` och
`omradesprofil` paketerar sådana arbetsflöden.

## Verktyg, resurser och promptar

Tabellen genereras från servern med `python scripts/generate_tool_docs.py`.

<!-- verktyg:start -->
Servern har **84 verktyg**, 11 resurser och 6 promptar.

#### Översikt

| Verktyg | Beskrivning |
| --- | --- |
| `fuzzy_list_sources` | Lista alla datakällor (myndighet, API, bas-URL, licens, verktygsprefix) och om de är aktiverade. |
| `fuzzy_api_notices` | Visa Deprecation/Sunset-varningar som API:erna skickat under sessionen (DIGG:s REST API-profil). |

#### Referensdata (lokalt, utan nätverk)

| Verktyg | Beskrivning |
| --- | --- |
| `ref_lookup_region` | Fuzzy-sök kommun- och länskoder (SCB:s regionala indelning) på namn eller kod. |
| `ref_list_municipalities` | Lista kommuner (kod, namn, länskod), valfritt filtrerat på län. |
| `ref_lookup_deso` | Slå upp en DeSO- eller RegSO-kod i SCB:s nyckelfiler: kommun, kategori, RegSO per version och jämförbarhet mellan DeSO 2018 och DeSO 2025. |
| `ref_list_deso` | Lista alla DeSO-koder i en kommun grupperade per RegSO (kod, namn, antal) för DeSO 2025 eller DeSO 2018, med antal per kategori A/B/C. |
| `ref_list_code_lists` | Lista medföljande kodlistor och referensdata med beskrivning, källa och verifiering. |
| `ref_get_code_list` | Hämta en hel kodlista (koder, namn, beskrivningar och källhänvisning). |
| `ref_entity_catalog` | Katalog över alla dataentiteter som servern exponerar (tabeller, skolenheter, huvudmän, ämnen, kurser, program, utbildningstillfällen, dataset ...) med identifierare, verktyg och hur källorna kopplas ihop (kommunkod, länskod, skolenhetskod, organisationsnummer, skolform, kurs-/ämnes-/programkoder). |

#### SCB – Statistikdatabasen (PxWebApi 2)

| Verktyg | Beskrivning |
| --- | --- |
| `scb_get_config` | Visa PxWebApi 2-konfigurationen för Statistikdatabasen (SCB). |
| `scb_search_tables` | Sök tabeller i SCB:s Statistikdatabasen (befolkning, utbildningsnivå, inkomster, arbetsmarknad, priser, val m.m.). |
| `scb_browse_subjects` | Bläddra i ämnesträdet för Statistikdatabasen (ämnesområde → statistikprodukt → tabellgrupp). |
| `scb_get_table` | Katalogposten för en tabell i Statistikdatabasen. |
| `scb_get_table_metadata` | Variabler (Region, Kon, Alder, ContentsCode, Tid ...) med värdekoder och texter, om variabeln kan elimineras, vilka kodlistor (agg_/vs_) som finns, enhet/referensperiod/måttyp per innehåll och länkar till definitioner och statistikens webbsida (META-ID). |
| `scb_get_table_data` | Hämta data ur en tabell i Statistikdatabasen som rader (en rad per cell, JSON-stat 2). |
| `scb_get_default_selection` | Tabellens standardurval i Statistikdatabasen (det urval webbgränssnittet visar först) med konkreta värdekoder. |
| `scb_get_codelist` | Hämta en kodlista i Statistikdatabasen (aggregering agg_* eller värdemängd vs_*). |
| `scb_build_query` | Bygg en delbar GET-URL och en POST-fråga för ett urval i Statistikdatabasen, med Power Query (M) för Power BI/Excel och curl. |
| `scb_geodata_layers` | Lista SCB:s öppna geodatalager (WFS). |
| `scb_geodata_describe_layer` | Visa ett geodatalagers attribut (fältnamn och typer) som desokod, regsokod, kommunkod, lanskod och version. |
| `scb_geodata_get_features` | Hämta objekt ur ett geodatalager, som alla DeSO-områden i en kommun med koder (DeSO har bara koder; RegSO har namn). |
| `scb_geodata_download_url` | Bygg en nedladdningslänk (WFS GetFeature) för ett helt lager eller ett filtrerat urval i GeoPackage, Shape (zip), CSV eller GeoJSON – för QGIS, ArcGIS eller Power BI. |
| `scb_geodata_locate` | Slå upp vilket DeSO- och RegSO-område (eller annat polygonlager) en punkt ligger i. |

#### Folkhälsomyndigheten – Folkhälsodata (PxWeb)

| Verktyg | Beskrivning |
| --- | --- |
| `fohm_list_databases` | Lista databaserna i Folkhälsodata (Folkhälsomyndigheten, PxWeb API v1) och de kända ämnesmapparna (A_Mo8: Folkhälsan i Sverige – indikatorer för de åtta målområdena och hälsoutfall; ANDTS: ANDTS-indikatorer (alkohol, narkotika, dopning, tobak, spel); B_HLV: Nationella folkhälsoenkäten – Hälsa på lika villkor; C_HBSC: Skolbarns hälsovanor (HBSC); D_Antibiotika: Antibiotikastatistik – försäljning och resistens; E_HALT: Vårdrelaterade infektioner och antibiotikaanvändning inom särskilt boende (Svenska HALT); H_Sminet: Smittsamma sjukdomar (SmiNet), bl.a. |
| `fohm_browse` | Lista mappar (type 'l') och tabeller (type 't') på en nivå i Folkhälsodata. |
| `fohm_search_tables` | Sök tabeller i Folkhälsodata på titel, variabler och värden (Lucene-syntax). |
| `fohm_get_table_metadata` | Visa en tabells variabler i Folkhälsodata med värdekoder och värdetexter och om variabeln kan elimineras. |
| `fohm_get_table_data` | Hämta data ur en tabell i Folkhälsodata som rader (en rad per cell). |
| `fohm_build_query` | Bygg en återanvändbar fråga (URL + JSON-kropp) för urvalet, med färdig Power Query (M) för Power BI/Excel och ett curl-kommando. |

#### Skolverket – Statistikdatabasen (PxWeb)

| Verktyg | Beskrivning |
| --- | --- |
| `skolverket_stat_list_databases` | Lista databaserna i Skolverkets statistikdatabas (Skolverket, PxWeb API v1) och de kända ämnesmapparna (Kommunala_jamforelsetal. |
| `skolverket_stat_browse` | Lista mappar (type 'l') och tabeller (type 't') på en nivå i Skolverkets statistikdatabas. |
| `skolverket_stat_search_tables` | Sök tabeller i Skolverkets statistikdatabas på titel, variabler och värden (Lucene-syntax). |
| `skolverket_stat_get_table_metadata` | Visa en tabells variabler i Skolverkets statistikdatabas med värdekoder och värdetexter och om variabeln kan elimineras. |
| `skolverket_stat_get_table_data` | Hämta data ur en tabell i Skolverkets statistikdatabas som rader (en rad per cell). |
| `skolverket_stat_build_query` | Bygg en återanvändbar fråga (URL + JSON-kropp) för urvalet, med färdig Power Query (M) för Power BI/Excel och ett curl-kommando. |

#### Skolverket – Planerad utbildning (v4)

| Verktyg | Beskrivning |
| --- | --- |
| `skolverket_pe_search_school_units` | Sök skolenheter i Skolverkets Planerad utbildning (v4) med serverfilter: namn, skolform, huvudmannatyp, inriktning, resursskola, kommun-/länskod, årskurs och närhet (lat/long + radie i km). |
| `skolverket_pe_get_school_unit` | Hämta en skolenhet ur Planerad utbildning: namn, huvudman (organisationsnummer, bolagsnamn, bolagsform), huvudmannatyp, inriktning, resursskola, startdatum, kommunkod, webb, telefon och adresser, koordinater (WGS84 och SWEREF 99) samt skolformer med årskurser. |
| `skolverket_pe_school_unit_statistics` | Statistik för en skolenhet per skolform (fsk\|gr\|gran\|gy\|gyan): elever per lärare, andel legitimerade lärare, elevantal, betyg och meritvärde, nationella prov, behörighet, och för gymnasiet per program (antagningspoäng, GBP, examen inom 3 år). |
| `skolverket_pe_national_statistics` | Nationella jämförelsevärden (riket) för en skolform, i samma format som skolenhetsstatistiken: senaste värde per indikator (text + tolkat tal) och valfri tidsserie. |
| `skolverket_pe_salsa` | SALSA (Skolverkets värdeadderingsmodell för grundskolan, åk 9): faktiskt och modellberäknat meritvärde och andel som nått kunskapskraven, avvikelsen (positivt = bättre än förväntat utifrån elevsammansättningen) samt bakgrundsvariabler (andel nyinvandrade, andel pojkar, föräldrarnas utbildning). |
| `skolverket_pe_school_unit_surveys` | Skolenkätens resultat (Skolinspektionen) för en skolenhet: elevers (åk 5, åk 8, gymnasiet år 2) och vårdnadshavares svar om nöjdhet, trygghet, studiero, stöd och stimulans – medelvärde (0–10, text + tal) och svarsandelar ('44%', '-' = dolt pga få svar), antal svar och svarsfrekvens. |
| `skolverket_pe_school_unit_education_events` | Gymnasieutbildningar (program/inriktningar, 'education events') som en skolenhet erbjuder enligt Planerad utbildning, valfritt en enskild studieväg. |
| `skolverket_pe_search_education_events` | Sök gymnasieutbildningar (program/inriktningar per skola) i hela landet med filter för skolnamn, studievägskod (t.ex. |
| `skolverket_pe_search_adult_education_events` | Sök vuxenutbildningar (komvux, sfi, yrkeshögskola, folkhögskola, högskolekurser m.m.) med fritext, ort, kommun/län, kommunkod, utbildningsform, inriktning, språk, studietakt, terminsstart, kursstart och distans. |
| `skolverket_pe_get_adult_education_event` | Detaljer för ett vuxenutbildningstillfälle. |
| `skolverket_pe_adult_education_areas` | Utbildningsområden och inriktningar för vuxenutbildning (/adult-education-events/areas) med areaId och directionId. |
| `skolverket_pe_support_list` | Hämta Planerad utbildnings stöd-/kodlistor (giltiga filtervärden): skolformer, geografiska områden (code, name, areaType MUNICIPALITY/TOWN), kommuner och skolenheter, huvudmannatyper, gymnasieprogram med inriktningar, undervisningsspråk, distansformer, vuxenutbildningsformer (id + gammal kod) och programvarianter. |
| `skolverket_pe_compare_secondary` | Hämta inriktningar, antagningspoäng (min/medel) och antal elever på program och skolenhet för flera (skolenhet, studieväg)-par i ett anrop via POST /v4/school-unit-secondary – underlaget till Utbildningsguidens jämförelsevy. |
| `skolverket_pe_school_unit_documents` | Dokument kopplade till en skolenhet (t.ex. |

#### Skolverket – Susa-navet (EMIL 3)

| Verktyg | Beskrivning |
| --- | --- |
| `skolverket_susa_search_education_events` | Sök utbildningstillfällen (education events, kurs-/programomgångar) i Skolverkets Susa-navet: högskola, yrkeshögskola, folkhögskola, arbetsmarknadsutbildning, konst- och kulturutbildning och komvux. |
| `skolverket_susa_search_education_infos` | Sök utbildningar (education infos: kurser, program, kurspaket) i Susa-navet – titel, kurskod, skolform, poäng (hp/yh-poäng), upplägg och nivå. |
| `skolverket_susa_search_education_providers` | Sök utbildningsanordnare (education providers: lärosäten, folkhögskolor, YH-anordnare, kommuner m.fl.) i Susa-navet. |
| `skolverket_susa_get_education_event` | Hämta ett utbildningstillfälle (education event) i Susa-navet: titel, beskrivning, start/slut, studietakt, distans, orter med kommunkod, undervisningsspråk, platser, avgifter, ansökningsperiod och UH-/komvux-tillägg (t.ex. |
| `skolverket_susa_get_education_info` | Hämta en utbildning (education info: kurs, program eller kurspaket) i Susa-navet: titel, kurskod, beskrivning, skolform, nivå (ISCED), poäng, omfattning, ämnen, examen, behörighet, förkunskaper, studiemedelsrätt (CSN) och kvalifikationsnivå. |
| `skolverket_susa_get_education_provider` | Hämta en utbildningsanordnare (education provider) i Susa-navet: namn, organisationsnummer, huvudman (kommunal/region/statlig/enskild/annan), kontakt- och besöksadresser med kommunkod, e-post, telefon och webbadress. |
| `skolverket_susa_api_info` | Visa information om Susa-navets API (utgivare, API- och EMIL-schemaversion, releasedatum, status och dokumentationslänk) från /emil3/api-info. |

#### Skolverket – Skolenhetsregistret (v2) och Läroplan/Syllabus (v1)

| Verktyg | Beskrivning |
| --- | --- |
| `skolverket_search_school_units` | Sök skolenheter i Skolverkets skolenhetsregister (Skolenhetsregistret, API v2). |
| `skolverket_get_school_unit` | Hämta en skolenhet ur Skolenhetsregistret: namn, status, skolformer med årskurser/program/komvux-delar, kommunkod, huvudman (organisationsnummer, namn, typ), skolenhetens kontaktuppgifter (e-post, telefon, webb), besöksadress med WGS84-koordinater (latitude/longitude) och SWEREF 99 TM, inriktning, komvux i egen regi, start-/slutdatum och ändringsdatum. |
| `skolverket_search_organizers` | Sök huvudmän (organizers: kommuner, regioner, enskilda huvudmän m.fl.) i Skolenhetsregistret. |
| `skolverket_get_organizer` | Hämta en huvudman (organizer) ur Skolenhetsregistret: namn, huvudmannatyp, bolagsform, status hos Skatteverket/Bolagsverket, säteskommun, region, skolformer, kontaktuppgifter, adress samt skolenhetskoderna för huvudmannens skolenheter och eventuella komvux-entreprenader. |
| `skolverket_search_education_providers` | Sök utbildningsanordnare (education providers) inom kommunal vuxenutbildning i Skolenhetsregistret: anordnarkod (8 siffror), organisationsnummer, namn, folkhögskolenamn och betygsrätt. |
| `skolverket_get_education_provider` | Hämta en utbildningsanordnare inom komvux: namn, adress, kontaktuppgifter, bolagsform, betygsrätt med giltighetsperiod, skolformsdelar (VUXGR, VUXGY, VUXSFI ...) och entreprenadavtal (huvudmannens organisationsnummer + anordnarkod, används i skolverket_get_contract). |
| `skolverket_search_contracts` | Sök avtal om kommunal vuxenutbildning på entreprenad (contracts) mellan en huvudman och en utbildningsanordnare. |
| `skolverket_get_contract` | Hämta ett entreprenadavtal inom komvux. |
| `skolverket_school_unit_changes` | Lista skolenheter och/eller huvudmän (samt valfritt utbildningsanordnare och entreprenader) som ändrats sedan ett datum, via meta_modified_after. |
| `skolverket_school_unit_api_info` | Metadata om Skolenhetsregistrets API (utgivare, version, release, status, dokumentation). |
| `skolverket_list_subjects` | Lista ämnen/ämnesplaner/kursplaner (subjects) i Skolverkets Syllabus-API med kod, namn, skoltyper, ämnestyp, reform (GY11/GY25 när API:t anger det), version och giltighetsdatum. |
| `skolverket_get_subject` | Hämta ett ämne (ämnesplan/kursplan) med ämnets beskrivning och syfte (HTML omgjort till text), kurser eller Gy25-nivåer med kod/namn/poäng, centralt innehåll per kurs/nivå (och per stadium för grundskolans kursplaner) samt betygskriterier med betygssteg (E–A). |
| `skolverket_list_subject_versions` | Lista alla versioner av ett ämne (nyast först) med giltighetsdatum, ändringsdatum och SKOLFS-nummer (skolfsGrund/skolfsAndring visas bara här). |
| `skolverket_list_courses` | Lista kurser (courses) i Syllabus-API:t med kod, namn, poäng och giltighet. |
| `skolverket_get_course` | Hämta en kurs (GY11) eller nivå (Gy25) med namn, poäng, centralt innehåll och betygskriterier samt ämnet den hör till. |
| `skolverket_list_course_versions` | Lista versioner av en kurs (nyast först). |
| `skolverket_list_programs` | Lista gymnasieprogram (programs) med kod, namn, studievägstyp (PROGRAM = GY11, PROGRAM25 = Gy25), kategori (högskoleförberedande/yrkesprogram/riksrekryterande), inriktningar (orientations) och canceledDate för upphävda GY11-program. |
| `skolverket_get_program` | Hämta ett gymnasieprogram med metadata, ämnesgrupper (foundationSubjects = gymnasiegemensamma ämnen, programmeSpecificSubjects = programgemensamma ämnen och övriga grupper som API:t returnerar) med ämneskoder, inriktningar/profiler och övriga fält (HTML omgjort till text). |
| `skolverket_list_program_versions` | Lista versioner av ett program (nyast först). |
| `skolverket_list_curriculums` | Lista läroplaner (curriculums), t.ex. |
| `skolverket_get_curriculum` | Hämta en läroplan (curriculum) med metadata och innehåll (HTML omgjort till text, långa texter förkortade). |
| `skolverket_list_curriculum_versions` | Lista versioner av en läroplan (nyast först). |
| `skolverket_syllabus_valuestore` | Hämta Syllabus-API:ts värdeförråd/kodlistor. |
| `skolverket_syllabus_api_info` | Visa information om Skolverkets Syllabus-API (version, releasedatum, status, dokumentation och ev. |

#### Sveriges dataportal

| Verktyg | Beskrivning |
| --- | --- |
| `dataportal_search_datasets` | Sök dataset (DCAT-AP-SE) i Sveriges dataportal – katalogen över öppna data från alla svenska myndigheter, regioner och kommuner. |
| `dataportal_list_publisher_datasets` | Lista en utgivares (myndighets/kommuns) dataset i Sveriges dataportal, senast ändrade först (metadatans dcterms:modified, som på dataportal.se), med paginering (total, next_offset). |
| `dataportal_get_dataset` | Hämta ett dataset (eller en datatjänst/dataserie) från Sveriges dataportal med alla distributioner: titel, beskrivning, utgivare, kontaktpunkt, teman, nyckelord, uppdateringsfrekvens, åtkomsträttigheter, licens, tidsperiod och geografisk täckning, samt per distribution accessURL/downloadURL, format/mediatyp, licens och conformsTo, och datatjänster (API) med endpointURL/endpointDescription. |
| `dataportal_theme_codes` | Lista EU:s datateman (dcat:theme) som används i DCAT-AP-SE och dataportalen: kod (t.ex. |

#### Resurser

| URI | Innehåll |
| --- | --- |
| `fuzzy://sources` | Översikt över alla datakällor, bas-URL:er och licenser. |
| `fuzzy://entities` | Alla dataentiteter, deras nycklar och kopplingar mellan SCB, Skolverket, FoHM och dataportalen. |
| `fuzzy://crosswalk/bbic-icf` | Öppna indikatorer som belyser BBIC-domäner och ICF-komponenter (analysstöd, ej validerad). |
| `fuzzy://crosswalk/informationsmodell` | Vilka begrepp en analys av öppna data behöver (indikatorvärde, period, geografi, organisation, skolform, kodöversättning, datakälla m.m.) och vilka verktyg och nycklar som ger dem. |
| `fuzzy://codes` | Index över medföljande kodlistor och referensdata. |
| `fuzzy://skolverket/skolenhetsregistret/codes` | Kodlistor i Skolenhetsregistret v2 (status, skolformer inkl. |
| `fuzzy://skolverket/syllabus/codes` | Kodlistor för Skolverkets Syllabus-API. |
| `fuzzy://skolverket/planned-educations/codes` | Kodlistor för Skolverkets Planerad utbildning (v4). |
| `fuzzy://skolverket/susa-navet/codes` | Kodlistor för Susa-navet (EMIL 3). |
| `fuzzy://dataportal/codes` | EU-teman, åtkomsträttigheter, uppdateringsfrekvenser, licens-URI:er, HVD-kategorier, utgivartyper och vanliga format som används i Sveriges dataportal. |
| `fuzzy://codes/{name}` | En kodlista i JSON, t.ex. |

#### Promptar

| Prompt | Beskrivning |
| --- | --- |
| `analysera_kommun` | Ta fram en faktabaserad lägesbild för en kommun genom att kombinera SCB, Skolverket och Folkhälsomyndigheten. |
| `jamfor_kommuner` | Jämför en indikator mellan flera kommuner med samma källa och period. |
| `hitta_statistik` | Hitta rätt källa och tabell för en statistikfråga. |
| `power_bi_fraga` | Bygg återanvändbara frågor (Power Query M) mot SCB eller Folkhälsomyndigheten. |
| `skolenhet_profil` | Sammanställ register-, utbuds- och statistikuppgifter för en skolenhet. |
| `omradesprofil` | Ta fram statistik för ett DeSO eller RegSO med rätt version och jämförelse med kommunen och riket. |
<!-- verktyg:end -->

### Urval i PxWeb (SCB, Skolverkets statistikdatabas, Folkhälsomyndigheten)

Urval anges som `{variabelkod: [värdekoder eller uttryck]}`:

| Uttryck | Betydelse |
| --- | --- |
| `"*"`, `"01*"`, `"????"` | alla, jokertecken, exakt antal tecken (t.ex. alla kommunkoder) |
| `"TOP(5)"`, `"TOP(5,1)"` | de 5 senaste perioderna (tid) eller de 5 första värdena |
| `"BOTTOM(2)"` | de 2 äldsta perioderna eller de 2 sista värdena |
| `"RANGE(2020,2024)"`, `"FROM(2020)"`, `"TO(2015)"` | intervall i metadatans ordning |

Urvalet expanderas och kontrolleras mot metadata innan API:et anropas, så okända koder och för stora urval
upptäcks direkt. Utelämnade variabler elimineras om tabellen tillåter det. Annars väljs senaste perioden, och
övriga variabler med högst 100 värden tas med i sin helhet. Svaret visar exakt vilka koder som användes.

### PxWebApi 2 och PxWeb API v1

PxWebApi 2 är PxTools efterföljare till PxWeb API v1 och används av fler än SCB (t.ex. SSB i Norge). v1 är i
praktiken legacy, men det är myndigheten som väljer vad den publicerar:

| Installation | API i dag | Kontroll |
| --- | --- | --- |
| SCB Statistikdatabasen | PxWebApi 2 (`/api/v2`) | i drift sedan oktober 2025; SCB:s v1 stängs runt årsskiftet 2026/2027 |
| Folkhälsomyndigheten Folkhälsodata | PxWeb API v1 | `/api/v2/config` gav 404 vid kontroll 2026-09-25 |
| Skolverkets statistikdatabas | PxWeb API v1 | `/api/v2/config` gav 404 vid kontroll 2026-09-25 |

Verktygen för v2 byggs från en gemensam fabrik (`pxweb/v2_tools.py`), så när Folkhälsomyndigheten eller Skolverket
publicerar PxWebApi 2 räcker det med konfiguration – samma verktygsprefix (`fohm_*`, `skolverket_stat_*`) får då
v2-verktygen (sök, ämnesträd, metadata med enheter och META-ID-länkar, data, kodlistor och delbara frågor):

```bash
FUZZY_MCP_FOHM_API_VERSION=v2 FUZZY_MCP_FOHM_BASE_URL=https://<värd>/api/v2 fuzzy-mcp
```

`python scripts/check_pxwebapi2.py` kontrollerar vilka installationer som svarar på `/api/v2/config`.

Metadata från PxWebApi 2 innehåller även `links` (META-ID-länkar till statistikens webbsida och definitioner) och
`contents_info` (enhet, decimaler, referensperiod och måttyp per innehåll) – användbart när mått ska definieras i
t.ex. Power BI.

## Koppla ihop källorna

| Nyckel | Format | Finns i |
| --- | --- | --- |
| Kommunkod | 4 siffror (`2180` = Gävle) | SCB `Region`, Skolverkets statistikdatabas `level`, Skolenhetsregistret, Planerad utbildning, Susa-navet, FoHM `Region` |
| Länskod | 2 siffror (`21`), riket `00` | SCB, Skolverkets statistikdatabas, FoHM |
| Skolenhetskod | 8 siffror | Skolenhetsregistret, Planerad utbildning, Skolverkets statistikdatabas (`orgnr-skolenhetskod`), SS 12000 |
| Organisationsnummer | 10 siffror | Skolenhetsregistret (huvudman), Skolverkets statistikdatabas (UFA), dataportalen, SS 12000 |
| Skolform | t.ex. GR, GRAN/GRS, GY | Alla Skolverket-API:er och SS 12000 – översätt med kodlistan `skolformer` |
| DeSO-/RegSO-kod | DeSO 9 tecken (t.ex. `2180C1010`), RegSO 8 tecken (t.ex. `2180R001`) | SCB:s tabeller: variabeln `Region` med kodlistorna `vs_DeSO2018`/`vs_RegSO2020` (t.o.m. referensår 2023, rena koder) och `vs_DeSO2025`/`vs_RegSO2025` (fr.o.m. 2024, koder med suffix `_DeSO2025`/`_RegSO2025`; `ref_lookup_deso` ger dem under `ssd_koder`); geodatalagren `DeSO_2018`, `DeSO_2025`, `RegSO_2020`, `RegSO_2025` (attributen `desokod`, `regsokod`, `regsonamn`, `kommunkod`, `lanskod`, `version`, `referensdatum`); kopplingstabellerna i `ref_lookup_deso`/`ref_list_deso` |
| Koordinat | SWEREF 99 TM (E, N i meter) eller WGS84 (lat, lon) | Skolenhetsregistret (besöksadressens `geoCoordinates`: `latitude`/`longitude`, `coordinateSweRefE`/`N`), `scb_geodata_locate` (punkt → DeSO/RegSO via INTERSECTS; i CQL skrivs punkten `POINT(N E)` för EPSG:3006) |

Mer finns i `ref_entity_catalog` och resursen `fuzzy://entities`. Medföljande kodlistor (`ref_list_code_lists`):
regioner, betygsskalor (inklusive Gy25 och kommande skala 1–10), SCB:s betygskoder, gymnasieprogram, anpassad
gymnasieskola, introduktionsprogram, komvux-koder, betygsdokument, kodstrukturer, SS 12000, skolformer samt
DeSO/RegSO-kopplingar för båda versionsparen (DeSO 2018–RegSO 2020, DeSO 2025–RegSO 2025) och SCB:s förändringslogg
för DeSO. DeSO-kopplingarna är en daterad ögonblicksbild av SCB:s nyckelfiler (filnamn, datum och sha256 står i
varje kodlista); nya filer läses in med `scripts/build_deso_reference.py`.

Relaterade källor som inte är anslutna som verktyg: SCB:s sida om DeSO under öppna geodata
(<https://www.scb.se/vara-tjanster/oppna-data/oppna-geodata/demografiska-statistikomraden-deso/>) med nyckelfilerna,
och SCB:s Regina (<https://regina.scb.se/indelningar>) för historiska regionala indelningar sedan 1952 (webbplats
utan dokumenterat API). Nyckelfilernas adresser kan ändras när SCB publicerar nya versioner; arbetsflödet
Källkontroll visar de aktuella fil-URL:erna och deras sha256, och [docs/KALLKONTROLL.md](docs/KALLKONTROLL.md)
redovisar körningen 2026-10-10 (sha256 för de tre filerna var då identiska med kodlistornas).

Varje post i kodlistorna har fältet `verifiering` som anger hur posten är kontrollerad:

| Värde | Betyder |
| --- | --- |
| `författning` | kontrollerat mot lagrum (SL, SF, GyF, VuxF) |
| `officiell_spec` | myndighetens eller standardägarens OpenAPI eller specifikation |
| `verifierat_uttag` | sett i ett verkligt API-svar (med `verifierad_datum`) |
| `myndighetswebb` | myndighetens webbplats |
| `referenskod` | community- eller referensimplementation |
| `härlett` | egen slutsats eller sammanställning |
| `okänd` | ursprunget är inte dokumenterat – kontrollera mot källan |

Regler:

- `okänd` ersätts aldrig med ett antaget värde. Kodlistans `kalla` anger varifrån listan som helhet kommer.
- Bygger en post på flera uppgifter med olika underlag avgör det **svagaste**, och `verifiering_not` förklarar
  delarna (t.ex. lagrummet kontrollerat mot författning men en användning bara belagd via webbsökning).
- Objekt inuti en post (t.ex. inriktningar i ett program) ärver postens värde. Uppslagstabeller som inte är poster
  (t.ex. SS 12000:s uppräkningar) får ett värde i `verifiering_samlingar`.
- Valfria följefält: `verifiering_not`, `verifierad_datum` (krävs för `verifierat_uttag`) och `kalla_url`.

`ref_list_code_lists` visar fördelningen per samling i varje kodlista, och vokabulären finns som kodlistan
`verifiering`. Kopplingarna i BBIC/ICF-bryggan är märkta `härlett`.
Resursen `fuzzy://crosswalk/bbic-icf` beskriver vilka öppna indikatorer som belyser BBIC-domäner och
ICF-komponenter. Den är ett analysstöd på befolkningsnivå, inte en validerad mappning.
Resursen `fuzzy://crosswalk/informationsmodell` visar vilka begrepp en analys av öppna data behöver (indikatorvärde,
indikator och definition, period, geografi, organisation, skolform, population, kodöversättning, datakälla, uttag,
datakvalitet och aggregerade jämförelsevärden) och vilka verktyg och nycklar som ger dem.

## Konfiguration

| Miljövariabel | Standard | Beskrivning |
| --- | --- | --- |
| `FUZZY_MCP_SOURCES` | alla | `scb`, `fohm`, `skolverket`, `dataportal`, `reference` eller enskilda delkällor: `scb.statistik`, `scb.geodata`, `skolverket.statistik`, `skolverket.skolenhetsregistret`, `skolverket.syllabus`, `skolverket.planerad`, `skolverket.susa` |
| `FUZZY_MCP_LANGUAGE` | `sv` | `sv` eller `en` där API:et stöder det |
| `FUZZY_MCP_<KÄLLA>_BASE_URL` | se `config.py` | t.ex. `FUZZY_MCP_SCB_BASE_URL` – byt API-version eller peka mot en spegel |
| `FUZZY_MCP_HTTP_TIMEOUT` | `30` | sekunder |
| `FUZZY_MCP_MAX_RETRIES` | `3` | omförsök vid 429/502/503/504 och nätverksfel |
| `FUZZY_MCP_FOHM_API_VERSION`, `FUZZY_MCP_SKOLVERKET_STATISTIK_API_VERSION` | `v1` | `v2` när installationen fått PxWebApi 2 (kräver motsvarande `_BASE_URL`) |
| `FUZZY_MCP_CACHE_TTL` | `3600` | sekunder i minnescachen (0 = av) |
| `FUZZY_MCP_CACHE_MAX_ENTRIES`, `FUZZY_MCP_CACHE_MAX_MB` | `512`, `64` | tak för cachen (antal svar och megabyte; svar större än en fjärdedel cachas inte) |
| `FUZZY_MCP_MAX_ROWS` | `1000` | tak för rader per datasvar |
| `FUZZY_MCP_TRANSPORT`, `FUZZY_MCP_HOST`, `FUZZY_MCP_PORT` | `stdio`, `127.0.0.1`, `8000` | transportinställningar |
| `FUZZY_MCP_ALLOWED_HOSTS`, `FUZZY_MCP_ALLOWED_ORIGINS` | – | skydd mot DNS-rebinding för HTTP-transporterna: tillåtna `Host`- och `Origin`-värden (localhost tillåts alltid). Ange dem när servern binds till annat än localhost. |
| `FUZZY_MCP_AUTH_TOKEN_FILE` (`FUZZY_MCP_AUTH_TOKEN`), `FUZZY_MCP_AUTH_HEADER` | –, `X-API-Key` | token som HTTP-klienter måste skicka (Bearer eller API-nyckelheader); krävs för HTTP |
| `FUZZY_MCP_ALLOW_UNAUTHENTICATED` | `false` | `true` = HTTP utan token (bara lokal utveckling eller bakom en proxy som själv autentiserar) |
| `FUZZY_MCP_STATELESS` | `false` | Streamable HTTP utan sessioner |
| `FUZZY_MCP_PERSONAL_DATA` | `opt-in` | `off` stänger av alla parametrar för personuppgifter |
| `FUZZY_MCP_MAX_OUTPUT_CHARS` | `0` | tak för ett verktygssvar i tecken (Eneo: `30000`) |

Anropsgränser per värd följer det myndigheterna publicerar: SCB 30 och Skolverkets statistikdatabas 10 anrop per
10 sekunder. `fuzzy_api_notices` visar `Deprecation`- och `Sunset`-huvuden som API:erna skickat, enligt
[DIGG:s REST API-profil](https://www.dataportal.se/rest-api-profil/versionhantering).

## Kända begränsningar

- **Kontrollerat mot officiella specifikationer, delvis mot live-uttag.** API-kontrakten bygger på myndigheternas
  egna OpenAPI-specifikationer och PxWeb-källkoden, kompletterade med verifierade uttag från 25–28 september 2026.
  SCB:s geodata (WFS), Statistikdatabasens DeSO-/RegSO-kodlistor, SCB:s nyckelfiler och Socialstyrelsens
  statistikdatabas kontrollerades 2026-10-10 med arbetsflödet Källkontroll; resultatet och det som fortfarande är
  overifierat står i [docs/KALLKONTROLL.md](docs/KALLKONTROLL.md).
  Kör `FUZZY_MCP_LIVE_TESTS=1 pytest -m live` från en miljö med internetåtkomst innan du tar servern i drift.
- **SCB:s v1-API** (`api.scb.se/OV0104/v1`) stängs runt årsskiftet 2026/2027 och används inte.
- **Stora svar** kortas för att hålla sig under ungefär 25 000 token (gränsen i t.ex. Claude Code): svaren
  markerar då `truncated` och säger hur man bläddrar vidare. Klienter med hårdare gräns (t.ex. Eneo) sätter
  `FUZZY_MCP_MAX_OUTPUT_CHARS`; servern kortar då listor och text och anger det i fältet `_kortat`. Geometrier från geodata begränsas till tio objekt;
  hämta hela lager via `scb_geodata_download_url`.
- **Folkhälsomyndigheten** anger ingen licens i API:et. Ange "Källa: Folkhälsomyndigheten" och tabell.
- **Skolverkets statistikdatabas** är inte officiell statistik. Värden med `..` är sekretessmarkerade, och sex
  UFA-tabeller saknar kodlista för `level`, så där skickas koderna vidare okontrollerade.
- **Planerad utbildning** kan ge ungefärliga värden (`cirka`, `~`). De markeras med `approximate: true`.
- **Susa-navet** saknar fritextsökning i API:et. Sökningen bläddrar därför igenom ett begränsat antal sidor och
  redovisar hur mycket som täckts.

## Drift

[docs/DRIFT.md](docs/DRIFT.md) är driftinstruktionen för IT-drift: leveranspaket, installation utan internet,
konfiguration, tjänst (Windows/systemd), container, nätverk, säkerhet, övervakning och felsökning.

- `python scripts/build_release.py --platform windows|linux` bygger leveranspaketet (Windows: Python 3.14, Linux: 3.12; wheel,
  låsta beroenden med SHA-256, hjul för offlineinstallation, SBOM, sårbarhetskontroll och kontrollsummor).
  `scripts/build_release.ps1` och `scripts/build_release.sh` är genvägar.
- `Dockerfile` bygger en container som kör som icke-root med hälsokontroll.
- `GET /healthz` svarar på HTTP-transporterna utan att anropa myndigheterna.
- `python scripts/smoke_test.py [--url …|--stdio] [--live]` kontrollerar en installation.

### Eneo

[docs/ENEO.md](docs/ENEO.md) beskriver hur servern kopplas till [Eneo](https://github.com/eneo-ai/eneo): token,
nätverk bredvid Eneos backend, rekommenderade inställningar, verktygsprofiler per assistent, acceptanstest och
felsökning. `scripts/eneo_check.py` kontrollerar servern med Eneos eget MCP-klientbibliotek (SDK 1.x) och kan köras
direkt i Eneos backend-container. Färdig assistentinstruktion: [docs/eneo/assistentinstruktion.md](docs/eneo/assistentinstruktion.md).

## Utveckling och bidrag

- [DEVELOPMENT.md](DEVELOPMENT.md): installation, tester, arkitektur och hur man lägger till en källa
- [CONTRIBUTING.md](CONTRIBUTING.md): bidrag, Conventional Commits och DCO
- [SECURITY.md](SECURITY.md): rapportera säkerhetsproblem
- [CHANGELOG.md](CHANGELOG.md), [GOVERNANCE.md](GOVERNANCE.md), [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md)

Projektet följer [DIGG:s projektmall](https://diggsweden.github.io/opensource-docs/sv/projektmall/) och
[REUSE](https://reuse.software/).

**Underhållare:** Deniz Özer ([@deno-li](https://github.com/deno-li)).

**Licens:** koden är MIT-licensierad ([LICENSE](LICENSE)). Medföljande kodlistor, referensdata, testdata och
dokumentation är CC0 1.0 ([REUSE.toml](REUSE.toml) visar vilka filer och källor); kodlistorna och testdata bygger på
myndigheternas öppna data, och kodlistan för SS 12000 återger kodvärden ur SIS standard som referens. Data som hämtas
via API:erna omfattas av respektive myndighets villkor.

**Referenser:** [PxWebApi 2](https://github.com/PxTools/PxApiSpecs), [Skolverkets öppna data](https://www.skolverket.se/om-skolverket/oppna-data),
[SCB öppna data](https://www.scb.se/vara-tjanster/oppna-data/), [Folkhälsodata](https://fohm-app.folkhalsomyndigheten.se/Folkhalsodata/),
[Sveriges dataportal](https://www.dataportal.se/), [MCP Python SDK](https://py.sdk.modelcontextprotocol.io/).

## English summary

fuzzy-mcp is a read-only Model Context Protocol server for Swedish open government data. It covers Statistics
Sweden (PxWebApi 2 and open geodata via WFS), the Swedish National Agency for Education (statistics database, school unit register,
syllabus API, planned educations and Susa-navet), the Public Health Agency (Folkhälsodata) and the Swedish data
portal. It validates PxWeb selections against metadata before calling the APIs and returns compact tables with
citations. It also ships reference code lists (municipality/county codes, grading scales, programme codes,
SS 12000) that join the sources together. Install it with `pip install git+https://github.com/deno-li/fuzzy-MCP`
and run `fuzzy-mcp` (stdio) or `fuzzy-mcp --transport streamable-http`.
