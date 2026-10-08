<!--
SPDX-FileCopyrightText: 2026 Deniz Özer

SPDX-License-Identifier: CC0-1.0
-->

# Utvecklarguide

## Förutsättningar

- Python 3.11 eller senare
- [uv](https://docs.astral.sh/uv/) (rekommenderas) eller pip

## Kom igång

```sh
uv venv
uv pip install -e ".[dev]"
```

Med pip:

```sh
python -m venv .venv
. .venv/bin/activate
pip install -e ".[dev]"
```

## Kvalitetskontroller

Samma kontroller körs i CI (`.github/workflows/ci.yml`):

```sh
ruff format --check src tests scripts   # formatering
ruff check src tests scripts            # lint
python -m pytest -q             # tester mot mockade API:er
reuse lint                      # licensinformation (kräver `pip install reuse`)
```

Alla tester körs utan nätverk: HTTP-trafiken till myndigheternas API:er ersätts av `httpx2.MockTransport`
med svar som speglar API:ernas dokumenterade format (se `tests/fixtures/`).

### Livetester mot de riktiga API:erna

`tests/test_live.py` anropar de riktiga API:erna med små förfrågningar.
De hoppas över som standard och körs med:

```sh
FUZZY_MCP_LIVE_TESTS=1 python -m pytest -q -m live
```

Kör dem när en myndighet har ändrat sitt API, före en utgåva och när du lagt till ett nytt verktyg.

### Källkontroll i GitHub Actions

`scripts/verify_sources.py` gör en fast lista med läsande GET-anrop och skriver svaren som rader i loggen. Det
används för att kontrollera API-kontrakt innan ett verktyg byggs eller ändras, och för att hämta SCB:s nyckelfiler
när den egna miljön saknar nätåtkomst till källorna. Skriptet använder bara standardbiblioteket, anropar bara värdarna
i `ALLOWED_HOSTS` och följer bara omdirigeringar inom dem.

| Grupp | Vad som kontrolleras |
| --- | --- |
| `scb-geodata` | WFS för DeSO/RegSO 2018–2025: lager, attribut, antal, kommunfilter, sidning, koordinatsystem, punktsökning och format |
| `scb-pxweb` | PxWebApi 2: tabeller med DeSO/RegSO, regionkoder (med och utan versionssuffix), kodlistor och ett litet datauttag |
| `scb-nycklar` | SCB:s DeSO- och RegSO-sidor som text, länkade filer och nyckelfilerna (kopplingar, historiska förändringar) |
| `socialstyrelsen` | Statistikdatabasens API v1: dokumentation, ämnen, variabler, värden, ett litet resultat och sidning |

Kör så här:

1. Actions → **Källkontroll** → **Run workflow** (från main eller en gren).
2. Varje grupp är ett eget jobb. Sammanfattningen visar status per anrop, och jobbloggen innehåller detaljerna.

Raderna i loggen börjar med `VERIFY` (ett anrop: status, storlek, sha256 och sammanfattning), `BODY` (hela små
JSON-svar), `TEXT` (en webbsida som text i delar) eller `B64` (en nedladdad fil i base64-delar). En fil återskapas
genom att delarna slås ihop i ordning och avkodas; kontrollera sha256 mot `VERIFY`-raden. Lokalt körs samma sak med:

```sh
python scripts/verify_sources.py --group scb-geodata
```

HTTP-fel och tomma svar är resultat, inte bevis för att data saknas. Skriptet avslutas med fel bara om inget anrop
fick något svar alls.

## Arkitektur

```text
src/fuzzy_mcp/
├── __main__.py          CLI: fuzzy-mcp [--transport stdio|streamable-http|sse]
├── server.py            build_server(): MCPServer, livscykel, registrering av källor
├── config.py            inställningar från miljövariabler (FUZZY_MCP_*)
├── core.py              Services, felöversättning (tool_errors), kompakta svar (structured)
├── http.py              delad HTTP-klient: cache, omförsök, anropsgränser, Deprecation/Sunset
├── meta.py              fuzzy_list_sources, fuzzy_api_notices, fuzzy://sources
├── prompts.py           promptar och autokomplettering
├── pxweb/               gemensamt för PxWeb: JSON-stat 2, urvalsuttryck, v1- och v2-klienter, Power Query,
│                        v1_tools.py / v2_tools.py = verktygsfabriker för PxWeb API v1 och PxWebApi 2
├── reference/           kodlistor och referensdata (kommuner/län m.m.), entitetskatalog
└── sources/
    ├── scb.py           SCB Statistikdatabasen (PxWebApi 2, beskrivs som PxWebV2Source)
    ├── scb_geodata.py   SCB:s öppna geodata (WFS)
    ├── fohm.py          Folkhälsomyndigheten Folkhälsodata (PxWeb API v1)
    ├── dataportal.py    Sveriges dataportal (EntryStore)
    └── skolverket/      Statistikdatabasen, Skolenhetsregistret, Syllabus, Planerad utbildning, Susa-navet
```

Varje källmodul exporterar `register(server, services)` och registreras i `server.SOURCE_MODULES`.

### Principer

- **Endast läsning.** Alla verktyg är `readOnlyHint=True` och anropar bara öppna API:er.
- **Validera före anrop.** PxWeb-urval expanderas lokalt mot metadata (`pxweb/selection.py`) så att okända koder och
  för stora urval upptäcks innan API:et anropas.
- **Kompakta svar.** Verktyg returnerar `Annotated[CallToolResult, Modell]` via `core.structured()`: kompakt JSON plus
  strukturerat innehåll. MCP-klienter som Claude Code begränsar verktygssvar till cirka 25 000 tokens.
- **Begripliga fel.** Fel från API:erna översätts till `ToolError` med HTTP-status och API:ets egen felbeskrivning
  (`core.tool_errors`). Andra undantag maskeras av SDK:n.
- **Versionshantering enligt DIGG:s REST API-profil.** Major-versionen ligger i sökvägen och bas-URL:erna kan bytas via
  miljövariabler. `Deprecation`- och `Sunset`-huvuden samlas in och visas med `fuzzy_api_notices`.
- **Tolerant tolkning.** Okända fält ignoreras och kända stavningsvarianter hanteras (t.ex. `codelists`/`codeLists`).
- **Inga personuppgifter som standard.** Namn på enskilda (rektor, c/o, kontaktpersoner) returneras bara via en
  uttrycklig parameter vars beskrivning innehåller "personuppgift", och tas annars bort även ur råsvar.
- **Säkra sökvägar.** Id:n som hamnar i URL-sökvägar valideras med vitlistor som kräver bokstav eller siffra först
  (httpx slår ihop `.`/`..`-segment), och användarvärden i frågespråk (Solr, CQL) escapas fullt ut.

## Lägga till en datakälla

1. Skaffa API-specifikationen (OpenAPI) eller verifierade exempel på svar.
2. Skapa `src/fuzzy_mcp/sources/<källa>.py` med `register(server, services)` enligt mönstret i `sources/scb.py`.
3. Lägg till bas-URL i `config.DEFAULT_BASE_URLS`, modulen i `server.SOURCE_MODULES` och källan i `meta.SOURCES`.
4. Skriv tester med `router`/`make_client` från `tests/conftest.py` och fixturer i `tests/fixtures/`.
5. Lägg till ett livetest i `tests/test_live.py`, kör `python scripts/generate_tool_docs.py` och uppdatera CHANGELOG.
   En ny PxWeb-installation behöver bara en `PxWebV1Source` (se `sources/fohm.py`) eller en `PxWebV2Source`
   (se `sources/scb.py`). FoHM och Skolverkets statistikdatabas har båda beskrivningarna och väljer med
   `FUZZY_MCP_<KÄLLA>_API_VERSION`.

## Leveranspaket och container

```sh
python scripts/build_release.py --platform windows   # Python 3.14; --platform linux ger Python 3.12
docker build -t fuzzy-mcp:dev .
python scripts/smoke_test.py --stdio -- --sources reference      # eller --url http://127.0.0.1:8000/mcp
```

Låsningen görs för målplattformen: på Windows kräver MCP-SDK:n även `pywin32`. Se [docs/DRIFT.md](docs/DRIFT.md).

## Utgåvor

1. Uppdatera `version` i `pyproject.toml` och `src/fuzzy_mcp/__init__.py`, `softwareVersion`/`releaseDate` i
   `publiccode.yml`, versionen i exemplen i `Dockerfile`, `docs/DRIFT.md`, `docs/ENEO.md` och
   `docs/eneo/docker-compose.fuzzy-mcp.yml`, och flytta posterna under `[Unreleased]` i CHANGELOG.md.
2. Kör kontrollerna ovan, inklusive livetesterna, och merga till main.
3. Kör arbetsflödet **Release** från main (Actions → Release → Run workflow, *Use workflow from* `main`). Det bygger den
   commit som är senaste på main när flödet startar (`GITHUB_SHA`) och ingen annan, även om main flyttas under tiden; en
   äldre commit släpps med den manuella vägen nedan. Flödet kör lint och tester och bygger sedan Windows- och
   Linux-paketen (med container-image). Paketen kontrolleras: kontrollsummor, installation utan nät (Linux på riktigt,
   Windows med `pip --dry-run` för win_amd64), att `fuzzy-mcp --version` stämmer, röktest och containern med och utan
   token. Sedan skapar det ett release-**utkast** `vX.Y.Z` med paketen, deras `.sha256`-filer och en releasetext från
   CHANGELOG-avsnittet med kontrollsummor och spårbarhet (`scripts/release_notes.py`). Flödet stoppar om det inte
   startas från main, om taggen eller releasen redan finns, om CHANGELOG saknar versionen eller om ett paket inte är
   byggt från rätt commit. Ett ofullständigt utkast tas bort automatiskt; finns ett kvar ändå, ta bort det (`gh release
   delete vX.Y.Z`) innan du kör igen.
4. Granska utkastet under Releases. Kontrollera precis före publicering att taggen `vX.Y.Z` inte finns (Tags-listan,
   eller `gh api repos/deno-li/fuzzy-MCP/git/ref/tags/vX.Y.Z` ger 404 Not Found) och publicera sedan. Taggen skapas då
   av GitHub på den byggda commiten, som en lättviktig tagg utan signatur; spårbarheten bygger på raden `Commit` och
   kontrollsummorna i releasetexten. Kontrollera efteråt att `gh api repos/deno-li/fuzzy-MCP/commits/vX.Y.Z --jq .sha`
   ger samma commit. DRIFT.md hänvisar till releasebeskrivningen som den separat publicerade kontrollsumman.

Utan GitHub Actions: bygg med `build_release.py` (se ovan), skapa taggen och releasen för hand, bifoga paketen och
`.sha256`-filerna och använd `python scripts/release_notes.py --version X.Y.Z --changelog CHANGELOG.md --release-dir
release --commit <sha>` för releasetexten.
