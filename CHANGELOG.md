# Changelog

Alla betydande ändringar i projektet dokumenteras i den här filen.

Formatet följer [Keep a Changelog](https://keepachangelog.com/sv/1.1.0/)
och projektet använder [Semantic Versioning](https://semver.org/lang/sv/).

## [Unreleased]

### Tillagt

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
- Verktygen `ref_lookup_deso` (för DeSO: version, RegSO, förändringar och summerbarhet; för RegSO: namn, DeSO-koder
  per version och om namnet ändrats) och `ref_list_deso` (områdena i en kommun per RegSO).
- Prompten `omradesprofil` (områdesprofil för DeSO/RegSO med jämförelse mot kommun och rike) och profilen
  Områdesstatistik (DeSO och RegSO) (`scb.statistik,scb.geodata,reference`) för Eneo.
- `scripts/build_deso_reference.py` som bygger kodlistorna ur SCB:s xlsx-filer med bara standardbiblioteket och
  registrerar sha256 och datum per fil.

### Ändrat

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

- Entitetskatalogen påstod att SCB:s tabeller på DeSO/RegSO-nivå använder kodlistorna `vs_DeSO*`/`vs_RegSO*` (inte
  belagt; tabellens kodlistor läses ur `scb_get_table_metadata`), och `scb_geodata_get_features` beskrev DeSO-områden
  "med koder och namn"; DeSO har bara koder, RegSO har namn.

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
