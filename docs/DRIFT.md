<!--
SPDX-FileCopyrightText: 2026 Deniz Özer

SPDX-License-Identifier: CC0-1.0
-->

# Driftinstruktion – fuzzy-mcp

För IT-drift som installerar och kör servern. Utvecklare läser [DEVELOPMENT.md](../DEVELOPMENT.md).
**Ska servern kopplas till Eneo?** Läs även [ENEO.md](ENEO.md).

## 1. Sammanfattning

| | |
| --- | --- |
| Vad | MCP-server (Model Context Protocol) som ger AI-assistenter läsåtkomst till öppna data från SCB, Skolverket, Folkhälsomyndigheten och Sveriges dataportal |
| Körning | En Python-process. Transport `stdio` (startas av klienten) eller `streamable-http` (tjänst på en port) |
| Data | Bara läsning från öppna API:er. Ingen databas, inga filer skrivs. Cache i minnet (högst 64 MB) |
| Hemligheter | En token som HTTP-klienterna skickar (fil, `FUZZY_MCP_AUTH_TOKEN_FILE`). Inga nycklar eller konton mot myndigheternas API:er |
| Autentisering | Statisk token för HTTP (`Authorization: Bearer` eller API-nyckelheader) – sätt `FUZZY_MCP_AUTH_TOKEN_FILE` |
| Personuppgifter | Returneras inte som standard. Namn på enskilda (t.ex. rektor) bara när en klient uttryckligen ber om det |
| Resurser | Cirka 85 MB minne i vila med alla källor; försumbar CPU; under 100 MB disk |
| Licens | MIT (koden), CC0 1.0 (kodlistor, referensdata och dokumentation; se `REUSE.toml`). Beroendena har tillåtande licenser; `pywin32` (bara Windows) innehåller `adodbapi` under LGPL-2.1 |

## 2. Leveransen

Paketet byggs med `python scripts/build_release.py --platform windows|linux` (Python 3.14 för Windows och 3.12 för
Linux som standard; annan version med `--python`) och innehåller:

| Fil | Syfte |
| --- | --- |
| `dist/fuzzy_mcp-<version>-py3-none-any.whl` | servern |
| `dist/fuzzy_mcp-<version>.tar.gz` | källkoden, för granskning |
| `requirements.lock` | alla beroenden med exakt version och SHA-256, låsta för målplattformen |
| `wheelhouse/` | beroendena för installation utan internet |
| `sbom.cdx.json` | komponentförteckning (CycloneDX) över Python-beroendena i `requirements.lock` |
| `audit.txt` | resultat från `pip-audit` för `requirements.lock` vid byggtillfället |
| `smoke_test.py` | röktest efter installation (`--token-file` när token krävs) |
| `eneo_check.py` | kontroll med Eneos MCP-klient, körs i Eneos backend (se `docs/ENEO.md`) |
| `docs/DRIFT.md` | den här instruktionen |
| `docs/ENEO.md`, `docs/eneo/` | Eneo-guide, assistentinstruktion, compose-fil och verktygsprofiler |
| `fuzzy-mcp-<version>-image.tar.gz` | färdig container-image (bara om paketet byggts med `--with-image`); `docker load -i …` |
| `README.md`, `CHANGELOG.md`, `SECURITY.md`, `DEVELOPMENT.md` m.fl. | dokumentation, samma sökvägar som i repot |
| `LICENSE`, `LICENSES/`, `REUSE.toml` | licenstexter och vilka filer som har vilken licens (MIT för koden, CC0 1.0 för kodlistor och dokument) |
| `BUILDINFO.txt` | vilken commit, plattform och vilka verktyg paketet byggts med |
| `SHA256SUMS` | kontrollsummor för filerna ovan |

`sbom.cdx.json` och `audit.txt` gäller paketets Python-beroenden. Container-imagen löser beroendena själv när den
byggs; `build_release.py --with-image` kontrollerar att den fick exakt samma versioner som `requirements.lock` och
skriver det i `BUILDINFO.txt`. En image som byggs direkt från repot med `docker build` låser sina beroenden själv
och omfattas bara av image-skanningen. Imagens bas (Debian-paket och Python från `python:3.12-slim`) omfattas inte – skanna
imagen med er vanliga verktygskedja (t.ex. Trivy eller Grype) före drift; samma verktyg (eller `syft`) kan ta fram
en SBOM för hela imagen. Basimagens Debian-paket har sina egna licenser (bl.a. GPL), som etiketten
`org.opencontainers.image.licenses` och `sbom.cdx.json` inte beskriver; etiketten gäller bara fuzzy-mcp. Källkoden till
Debian-paketen finns i Debians arkiv (<https://sources.debian.org>, äldre versioner på <https://snapshot.debian.org>)
och källkoden till `pywin32` (med `adodbapi`, LGPL-2.1) på <https://github.com/mhammond/pywin32>. pip är borttaget ur imagens filsystem men
finns kvar i basimagens lager, så en skanner som läser alla lager kan ändå rapportera det.

Paketet gäller **en plattform och en Python-version** (t.ex. Windows x64 + Python 3.14), eftersom några beroenden
är kompilerade.

**Windows eller container?** Windows-paketet installerar fuzzy-mcp direkt på en Windows-server (avsnitt 3–5).
Ska servern köras som container bredvid Eneo (ENEO.md avsnitt 2) behövs Linux-paketet byggt med `--with-image`;
Windows-paketets hjul fungerar inte i en Linux-container.

Sökvägar som `scripts/…`, `Dockerfile` och `config.py` i README finns bara i repot (eller i wheel-filen), inte
i paketet. `smoke_test.py` och `eneo_check.py` ligger i paketets rot.

**Kontrollera paketet före installation.** Jämför också kontrollsumman i `.sha256`-filen med den som publicerats
separat – i releasebeskrivningen på GitHub (`https://github.com/deno-li/fuzzy-MCP/releases`) eller i meddelandet från
den som levererade paketet. Filerna i samma leverans skyddar bara mot skadade filer, inte mot ett utbytt paket.

```powershell
# Windows (som administratör) – i mappen där zip-filen ligger; lägg bara ett paket där. Klistra in hela blocket
# (visar konsolen prompten >> efteråt: tryck Enter en gång till). Det körs som en enhet och avbryts vid första fel,
# så inget packas upp om kontrollsumman inte stämmer.
& {
$ErrorActionPreference = "Stop"
$zip = @(Get-ChildItem fuzzy-mcp-*-windows-*.zip)
if ($zip.Count -ne 1) { throw "Hittade $($zip.Count) paket – lägg exakt en zip-fil i mappen" }
$zip = $zip[0].Name
$want = (Get-Content -TotalCount 1 -LiteralPath "$zip.sha256").Split(" ")[0]
if ((Get-FileHash -Algorithm SHA256 -LiteralPath $zip).Hash -ne $want) { throw "Kontrollsumman för $zip stämmer inte – installera inte paketet" }
# Packa upp i en NY mapp som bara administratörer och SYSTEM kan ändra. En befintlig mapp kan ha behörigheter
# för andra konton som icacls nedan inte tar bort – ta bort den först (vid uppgradering: den gamla C:\install).
if (Test-Path C:\install) { throw "C:\install finns redan – ta bort den (Remove-Item -Recurse -Force C:\install) och börja om" }
New-Item -ItemType Directory C:\install | Out-Null
icacls C:\install /inheritance:r /grant:r "*S-1-5-32-544:(OI)(CI)F" "*S-1-5-18:(OI)(CI)F"
if ($LASTEXITCODE) { throw "icacls misslyckades" }
Expand-Archive -LiteralPath $zip -DestinationPath C:\install
$start = Get-Location
Set-Location (Join-Path C:\install ([IO.Path]::GetFileNameWithoutExtension($zip)))
try {   # vid fel: tillbaka till startmappen, så att C:\install går att ta bort
  # Varje fil i paketet stämmer, och inga okända filer finns (även dolda)
  $sums = Get-Content SHA256SUMS | ForEach-Object { $h, $p = $_ -split ' \*', 2; [pscustomobject]@{ Hash = $h; Path = $p } }
  $fel = @($sums | Where-Object { -not (Test-Path -LiteralPath $_.Path -PathType Leaf) -or
    (Get-FileHash -Algorithm SHA256 -LiteralPath $_.Path).Hash -ne $_.Hash } | ForEach-Object { "FEL: $($_.Path)" })
  $fel += @(Get-ChildItem -Recurse -File -Force | ForEach-Object { $_.FullName.Substring($PWD.Path.Length + 1).Replace('\', '/') } |
    Where-Object { $_ -ne 'SHA256SUMS' -and $_ -notin $sums.Path } | ForEach-Object { "OKÄND FIL: $_" })
  if ($fel) { $fel; throw "Paketet stämmer inte med SHA256SUMS – installera inte, ta bort C:\install" }
} catch { Set-Location $start; throw }
"Paketet är kontrollerat och uppackat i $PWD"
}
```

```bash
# Linux
sha256sum -c fuzzy-mcp-*.tar.gz.sha256 && tar xzf fuzzy-mcp-*.tar.gz
cd fuzzy-mcp-*/ && sha256sum -c SHA256SUMS
```

## 3. Installation

Kräver Python 64 bit i **samma version som paketet** (se paketnamnet, t.ex. `py3.14`). Ingen internetåtkomst
behövs vid installationen.

**Windows (PowerShell, som administratör, i paketmappen från avsnitt 2):**

Python ska vara installerat **för alla användare** (t.ex. `python-3.14.x-amd64.exe /quiet InstallAllUsers=1
Include_launcher=1`, hamnar under `C:\Program Files`). En installation per användare ligger under `C:\Users\…`,
och dit når inte tjänstekontot i avsnitt 5. Välj en Python-version som fortfarande får Windows-installationer med
säkerhetsrättningar (3.14 i oktober 2026; 3.12 får bara källkodsutgåvor).

```powershell
& {
$ErrorActionPreference = "Stop"
$py = "3.14"                  # samma som paketnamnet (py3.14)
$venv = "C:\fuzzy-mcp\venv"
# C:\fuzzy-mcp får bara ändras av administratörer och SYSTEM (koden körs av tjänsten och av administratörer).
# Första installationen: mappen ska vara ny (en befintlig mapp kan ha behörigheter för andra konton).
# Uppgradering: se avsnitt 10 – mappen finns då redan och är låst.
if (Test-Path C:\fuzzy-mcp) { throw "C:\fuzzy-mcp finns redan – vid uppgradering: följ avsnitt 10. Efter en avbruten första installation: Remove-Item -Recurse -Force C:\fuzzy-mcp och kör blocket igen" }
New-Item -ItemType Directory C:\fuzzy-mcp | Out-Null
icacls C:\fuzzy-mcp /inheritance:r /grant:r "*S-1-5-32-544:(OI)(CI)F" "*S-1-5-18:(OI)(CI)F"
if ($LASTEXITCODE) { throw "icacls misslyckades" }
py -$py -m venv $venv
if ($LASTEXITCODE) { throw "Kunde inte skapa $venv med Python $py (py -0p visar installerade versioner)" }
if ((Get-Content -LiteralPath "$venv\pyvenv.cfg") -match '^home\s*=\s*C:\\Users\\') { throw "$venv bygger på en Python under C:\Users – installera Python för alla användare" }
& "$venv\Scripts\python" -m pip install --no-index --find-links wheelhouse --require-hashes -r requirements.lock
if ($LASTEXITCODE) { throw "Beroendena kunde inte installeras – se felet ovan" }
& "$venv\Scripts\python" -m pip install --no-index --no-deps (Get-ChildItem dist\*.whl).FullName
if ($LASTEXITCODE) { throw "fuzzy-mcp kunde inte installeras – se felet ovan" }
& "$venv\Scripts\fuzzy-mcp" --version
& "$venv\Scripts\python" smoke_test.py --stdio -- --sources reference
}
```

**Linux** (x86_64, glibc 2.28 eller senare). Python 3.12 finns i t.ex. Ubuntu 24.04 (`python3.12-venv`) och
RHEL 9.4+ (`dnf install python3.12`). Debian 12 och Ubuntu 22.04 har Python 3.11 – bygg då paketet med
`--python 3.11` och installera `python3.11-venv`.

```bash
PY=3.12        # samma som paketnamnet (py3.12)
umask 022      # venv ska kunna läsas av tjänstekontot (DynamicUser i avsnitt 5)
python$PY -m venv /opt/fuzzy-mcp/venv
/opt/fuzzy-mcp/venv/bin/python -m pip install --no-index --find-links wheelhouse --require-hashes -r requirements.lock
/opt/fuzzy-mcp/venv/bin/python -m pip install --no-index --no-deps dist/*.whl
/opt/fuzzy-mcp/venv/bin/fuzzy-mcp --version
/opt/fuzzy-mcp/venv/bin/python smoke_test.py --stdio -- --sources reference
```

`--require-hashes` stoppar installationen om något paket inte stämmer med låsfilen.

## 4. Konfiguration

Allt styrs med miljövariabler (eller motsvarande flaggor till `fuzzy-mcp`). Inga konfigurationsfiler.

| Variabel | Standard | Beskrivning |
| --- | --- | --- |
| `FUZZY_MCP_TRANSPORT` | `stdio` | `streamable-http` för drift som tjänst (`sse` finns för äldre klienter) |
| `FUZZY_MCP_HOST` / `FUZZY_MCP_PORT` | `127.0.0.1` / `8000` | adress och port för HTTP. Låt den lyssna på 127.0.0.1 bakom en proxy på samma värd |
| `FUZZY_MCP_ALLOWED_HOSTS` | – | publika värdnamn klienterna använder, t.ex. `mcp.kommun.se`. **Krävs** om servern lyssnar på annat än localhost eller står bakom en omvänd proxy |
| `FUZZY_MCP_ALLOWED_ORIGINS` | – | `Origin` för webbläsarklienter, t.ex. `https://ai.kommun.se` |
| `FUZZY_MCP_AUTH_TOKEN_FILE` | – | fil med token (en per rad, minst 32 tecken) som HTTP-klienter måste skicka. **Krävs** för HTTP (avsnitt 7). `FUZZY_MCP_AUTH_TOKEN` finns som alternativ |
| `FUZZY_MCP_ALLOW_UNAUTHENTICATED` | `false` | `true` = HTTP utan token; bara lokal utveckling eller bakom en proxy som själv autentiserar |
| `FUZZY_MCP_AUTH_HEADER` | `X-API-Key` | header för API-nyckel som alternativ till `Authorization: Bearer` |
| `FUZZY_MCP_STATELESS` | `false` | `true` = Streamable HTTP utan sessioner (flera instanser utan sticky sessions; rekommenderas för Eneo) |
| `FUZZY_MCP_PERSONAL_DATA` | `opt-in` | `off` = inga namn på enskilda personer, även om en klient ber om det |
| `FUZZY_MCP_MAX_OUTPUT_CHARS` | `0` (av) | tak för ett verktygssvar i tecken; servern kortar listor och behåller giltig JSON (Eneo: `30000`) |
| `FUZZY_MCP_SOURCES` | alla | begränsa källorna, t.ex. `scb,skolverket,reference` (delkällor: `scb.statistik`, `scb.geodata`, `skolverket.statistik`, `skolverket.skolenhetsregistret`, `skolverket.syllabus`, `skolverket.planerad`, `skolverket.susa`) |
| `FUZZY_MCP_LANGUAGE` | `sv` | `sv` eller `en` |
| `FUZZY_MCP_HTTP_TIMEOUT` | `30` | sekunder per anrop mot myndigheterna |
| `FUZZY_MCP_MAX_RETRIES` | `3` | omförsök vid 429/502/503/504 och nätverksfel |
| `FUZZY_MCP_CACHE_TTL` | `3600` | sekunder i minnescachen, 0 = av |
| `FUZZY_MCP_CACHE_MAX_ENTRIES` / `FUZZY_MCP_CACHE_MAX_MB` | `512` / `64` | tak för cachen |
| `FUZZY_MCP_MAX_ROWS` | `1000` | tak för rader per datasvar |
| `FUZZY_MCP_<KÄLLA>_BASE_URL` | se `config.py` | byt adress till en myndighets API (t.ex. ny version) |
| `FUZZY_MCP_FOHM_API_VERSION`, `FUZZY_MCP_SKOLVERKET_STATISTIK_API_VERSION` | `v1` | `v2` när myndigheten publicerat PxWebApi 2 (kräver ny `_BASE_URL`) |
| `HTTPS_PROXY`, `NO_PROXY` | – | utgående proxy, t.ex. `http://proxy.kommun.se:8080`. Myndigheterna nås över HTTPS, så `HTTP_PROXY` ensam räcker inte. Sätt den uttryckligen för tjänsten: PAC/WPAD läses inte, och tjänstekontot har inte användarens proxyinställningar. Proxyinloggning fungerar bara som Basic i URL:en, inte NTLM/Kerberos – be hellre om ett undantag för värdarna i avsnitt 6 |
| `SSL_CERT_FILE` | – | behövs normalt inte: servern litar på operativsystemets rotcertifikat (på Windows även dem som delats ut via GPO/Intune). Sätt den bara om proxyn gör TLS-inspektion med en CA som inte finns där, och då till en **komplett** CA-bundle (publika rotcertifikat + organisationens CA) – filen ersätter systemets rotcertifikat. `SSL_CERT_DIR` (en eller flera kataloger) gör detsamma men används bara när `SSL_CERT_FILE` inte är satt. Saknas filen, eller är den oläsbar eller inte i PEM-format, startar servern inte (`Konfigurationsfel`). Båda ska vara tomma för hela maskinen om de inte behövs: `foreach ($n in 'SSL_CERT_FILE','SSL_CERT_DIR') { "$n=" + [Environment]::GetEnvironmentVariable($n, 'Machine') }` |

Ett ogiltigt värde i en `FUZZY_MCP_*`-variabel, en saknad token eller en CA-fil som saknas, inte kan läsas eller inte
är i PEM-format ger
`Konfigurationsfel: …` på stderr och avslutskod 2, och tjänsten startas inte om. En adress som inte kan användas
(porten upptagen eller fel `FUZZY_MCP_HOST`) ger avslutskod 3 och ett bindningsfel i loggen – `address already in use`
(Windows: `10048`, `only one usage of each socket address`), `could not bind on any address` eller
`Name or service not known` (Windows: `getaddrinfo failed`). systemd försöker då igen var 5:e sekund. NSSM väntar minst
5 sekunder (`AppRestartDelay`), men avslutas processen inom 1,5 sekunder ökar NSSM väntan stegvis upp till 4 minuter
och visar tjänsten som Pausad – rätta felet och kör `& "C:\Program Files\nssm\nssm.exe" restart fuzzy-mcp`.

## 5. Köra som tjänst

**Windows** – med till exempel [NSSM](https://nssm.cc/) eller WinSW (tredjepartsverktyg, välj enligt er standard).
Lägg `nssm.exe` (64 bit) i en mapp som tjänstekontot kan läsa och bara administratörer kan ändra, t.ex.
`C:\Program Files\nssm\nssm.exe`, och kör den därifrån med full sökväg (`$nssm` nedan) – tjänsten startar via
den sökvägen, och en kopia under `C:\Users\…` kan tjänstekontot inte läsa.

Kör tjänsten som ett konto utan administratörsrättigheter (ett virtuellt tjänstekonto som nedan). Starta den
först när tokenfilen finns – annars avslutas den direkt med `Konfigurationsfel` (avslutskod 2, och NSSM startar
då inte om den). Servern skriver all logg, även åtkomstloggen, till stderr.

```powershell
# 1. Tjänsten (startas i steg 3); kontot sätts med Windows inbyggda sc.exe
$nssm = "C:\Program Files\nssm\nssm.exe"
& $nssm install fuzzy-mcp C:\fuzzy-mcp\venv\Scripts\fuzzy-mcp.exe --transport streamable-http --host 127.0.0.1 --port 8000
& $nssm set fuzzy-mcp AppDirectory C:\fuzzy-mcp     # fast arbetskatalog, oberoende av venv-versionen
& $nssm set fuzzy-mcp AppEnvironmentExtra FUZZY_MCP_ALLOWED_HOSTS=mcp.kommun.se FUZZY_MCP_SOURCES=scb,skolverket,fohm,reference FUZZY_MCP_AUTH_TOKEN_FILE=C:\fuzzy-mcp\secrets\token.txt
& $nssm set fuzzy-mcp AppStderr C:\fuzzy-mcp\logs\fuzzy-mcp.log
& $nssm set fuzzy-mcp AppRotateFiles 1
& $nssm set fuzzy-mcp AppRotateOnline 1             # rotera även medan tjänsten kör, inte bara vid start
& $nssm set fuzzy-mcp AppRotateBytes 10485760       # ny loggfil vid 10 MB; rensa gamla filer enligt er gallringsrutin
& $nssm set fuzzy-mcp AppExit 2 Exit                # konfigurationsfel: stanna i stället för att starta om i en slinga
& $nssm set fuzzy-mcp AppRestartDelay 5000          # vänta 5 s före omstart efter andra fel (som RestartSec i systemd)
sc.exe config fuzzy-mcp obj= "NT SERVICE\fuzzy-mcp"

# 2. Rättigheter, token och loggmapp. C:\fuzzy-mcp är låst till administratörer och SYSTEM (avsnitt 3);
#    tjänstekontot får läsa (även token) och skriva i loggmappen – inga andra konton får något.
icacls C:\fuzzy-mcp /grant "NT SERVICE\fuzzy-mcp:(OI)(CI)RX"
New-Item -ItemType Directory -Force C:\fuzzy-mcp\secrets, C:\fuzzy-mcp\logs | Out-Null
icacls C:\fuzzy-mcp\logs /grant "NT SERVICE\fuzzy-mcp:(OI)(CI)M"
C:\fuzzy-mcp\venv\Scripts\python -c "import secrets; print(secrets.token_urlsafe(48))" | Set-Content -Encoding ascii C:\fuzzy-mcp\secrets\token.txt
icacls C:\fuzzy-mcp\secrets\token.txt     # Administratörer F, SYSTEM F, NT SERVICE\fuzzy-mcp RX – inget annat

# 3. Starta och vänta tills servern svarar (den behöver några sekunder)
& $nssm start fuzzy-mcp
$ok = $false
$np = @{}; if ($PSVersionTable.PSVersion.Major -ge 6) { $np.NoProxy = $true }   # PowerShell 7: aldrig via HTTP_PROXY
foreach ($i in 1..30) { try { Invoke-RestMethod @np -TimeoutSec 2 http://127.0.0.1:8000/healthz; $ok = $true; break } catch { Start-Sleep 1 } }
if (-not $ok) { throw "fuzzy-mcp svarar inte på /healthz – se C:\fuzzy-mcp\logs\fuzzy-mcp.log" }   # annars: status ok
```

**Linux (systemd)** – `/etc/systemd/system/fuzzy-mcp.service`:

```ini
[Unit]
Description=fuzzy-mcp (MCP-server för svensk öppen data)
After=network-online.target
Wants=network-online.target

[Service]
# Token: /etc/fuzzy-mcp/token (root, 0600) – systemd lämnar en kopia som bara tjänsten kan läsa.
# Kräver systemd 247 eller senare (LoadCredential). Saknas stödet blir sökvägen /token och servern startar inte.
LoadCredential=token:/etc/fuzzy-mcp/token
ExecStart=/opt/fuzzy-mcp/venv/bin/fuzzy-mcp --transport streamable-http --host 127.0.0.1 --port 8000 --auth-token-file ${CREDENTIALS_DIRECTORY}/token
Environment=FUZZY_MCP_ALLOWED_HOSTS=mcp.kommun.se
Environment=FUZZY_MCP_SOURCES=scb,skolverket,fohm,reference
DynamicUser=yes
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
PrivateDevices=yes
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
MemoryMax=512M
Restart=on-failure
# Konfigurationsfel (2) och saknad tokenfil (243 = CREDENTIALS) startas inte om i en evig slinga
RestartPreventExitStatus=2 243
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo install -d -m 700 /etc/fuzzy-mcp
sudo sh -c 'umask 077; /opt/fuzzy-mcp/venv/bin/python -c "import secrets; print(secrets.token_urlsafe(48))" > /etc/fuzzy-mcp/token'
systemctl daemon-reload && systemctl enable --now fuzzy-mcp && journalctl -u fuzzy-mcp -f
```

**Container** – `Dockerfile` i repot. Imagen kör som icke-root (uid 10001), har `HEALTHCHECK` mot `/healthz`,
installerar beroendena med hashkontroll och innehåller inte pip. Bara byggsteget behöver nätet (Docker Hub och PyPI):

```bash
docker build --build-arg VERSION=0.1.2 --build-arg VCS_REF=$(git rev-parse HEAD) -t fuzzy-mcp:0.1.2 .   # etiketterna: version och commit
# bakom proxy med TLS-inspektion – en komplett CA-bundle (publika rotcertifikat + organisationens CA), som bara
# används under bygget och inte hamnar i imagen:
docker build --build-arg HTTPS_PROXY=http://proxy.kommun.se:8080 --secret id=ca,src=ca-bundle.pem -t fuzzy-mcp:0.1.2 .

# token: läsbar bara för containerns tjänstekonto (uid 10001)
sudo mkdir -m 755 -p /etc/fuzzy-mcp
sudo sh -c 'umask 077; python3 -c "import secrets; print(secrets.token_urlsafe(48))" > /etc/fuzzy-mcp/token'
sudo chown 10001:10001 /etc/fuzzy-mcp/token && sudo chmod 0400 /etc/fuzzy-mcp/token

docker run -d --name fuzzy-mcp -p 127.0.0.1:8000:8000 \
  -e FUZZY_MCP_ALLOWED_HOSTS=mcp.kommun.se \
  -v /etc/fuzzy-mcp/token:/run/secrets/fuzzy_mcp_token:ro -e FUZZY_MCP_AUTH_TOKEN_FILE=/run/secrets/fuzzy_mcp_token \
  --read-only --cap-drop ALL --security-opt no-new-privileges fuzzy-mcp:0.1.2
docker inspect --format '{{.State.Health.Status}}' fuzzy-mcp     # healthy
```

Containern lyssnar på `0.0.0.0` inne i containern, så den startar inte utan token (avsnitt 7), och
`FUZZY_MCP_ALLOWED_HOSTS` ska alltid sättas. Behöver
servern en utgående proxy i drift: lägg till `-e HTTPS_PROXY=…`. Gör proxyn TLS-inspektion: montera en komplett
CA-bundle skrivskyddad och peka ut den, t.ex. värdens bundle om organisationens CA redan är installerad där
(`-v /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem:/certs/ca-bundle.pem:ro -e SSL_CERT_FILE=/certs/ca-bundle.pem`
på RHEL, `/etc/ssl/certs/ca-certificates.crt` på Debian/Ubuntu). En fil med bara organisationens CA räcker inte.

## 6. Nätverk

**Utgående (HTTPS 443)** – tillåt bara:

| Värd | Källa |
| --- | --- |
| `statistikdatabasen.scb.se` | SCB Statistikdatabasen |
| `geodata.scb.se` | SCB öppna geodata |
| `fohm-app.folkhalsomyndigheten.se` | Folkhälsomyndigheten Folkhälsodata |
| `api.skolverket.se` | Skolverkets API:er (Skolenhetsregistret, Syllabus, Planerad utbildning, Susa-navet) |
| `statistikdatabasen.skolverket.se` | Skolverkets statistikdatabas |
| `admin.dataportal.se` | Sveriges dataportal |

Källor som stängts av med `FUZZY_MCP_SOURCES` anropas inte.

**Inkommande** – bara HTTP-transporten lyssnar. Endpoints:

| Sökväg | Syfte |
| --- | --- |
| `/mcp` | MCP (POST/GET/DELETE), kräver token. Ett `GET /mcp` utan session ger 400; med `FUZZY_MCP_STATELESS=true` öppnar det i stället en SSE-ström som hålls öppen. Båda är normala – använd inte `/mcp` som hälsokontroll |
| `/healthz` | hälsokontroll utan token, `GET` → `{"status": "ok"}`. Visar ingen version eller konfiguration och anropar aldrig myndigheterna |

## 7. Säkerhet

- **Token:** krävs alltid för HTTP, även på 127.0.0.1 – en omvänd proxy på samma värd (som kan skriva om
  `Host` till 127.0.0.1) gör annars servern nåbar utan att den märker det. Utan `FUZZY_MCP_AUTH_TOKEN_FILE` (eller
  `FUZZY_MCP_AUTH_TOKEN`) startar den inte. Undantag: lokal utveckling, eller en proxy som själv autentiserar –
  `FUZZY_MCP_ALLOW_UNAUTHENTICATED=true`. Låt en omvänd proxy skicka vidare det ursprungliga värdnamnet (nginx
  `proxy_set_header Host $host;`, Apache `ProxyPreserveHost On`) och ange det i `FUZZY_MCP_ALLOWED_HOSTS`.
  Token jämförs i konstant tid och kan roteras (en per rad). Den loggas aldrig – inte heller om en felkonfigurerad
  klient lägger den i URL:en, eftersom åtkomstloggen inte tar med frågesträngen. `/healthz` kräver ingen token.
  Exponera ändå inte porten mot internet – använd internt nät eller en omvänd proxy med TLS.
- **Skydd mot DNS-rebinding** är på när servern lyssnar på localhost, och när `FUZZY_MCP_ALLOWED_HOSTS` är satt.
  Okända `Host` ger 421 och okända `Origin` 403 på `/mcp`. `/healthz` svarar på alla värdnamn, men visar bara
  `{"status": "ok"}`. Lyssnar servern på `0.0.0.0` utan värdlista skriver den en varning och skyddet är av –
  undvik det.
- **Inga skrivoperationer, inga godtyckliga URL:er.** Verktygen anropar bara de fasta adresserna ovan, och id:n
  valideras innan de hamnar i en URL.
- **Personuppgifter** (namn på rektor, c/o-namn, kontaktpersoner) visas bara om klienten anger en parameter märkt
  "personuppgift", och aldrig med `FUZZY_MCP_PERSONAL_DATA=off`. Servern sparar inga svar; åtkomstloggen
  innehåller däremot klienternas IP-adresser (avsnitt 8).
- **Beroenden:** granska `sbom.cdx.json` och `audit.txt`; kör `pip-audit` igen vid behov.

## 8. Övervakning och loggar

- **Hälsa:** `GET /healthz` (HTTP 200). Container-imagen har detta som `HEALTHCHECK`.
- **Funktion:** `python smoke_test.py --url https://mcp.kommun.se/mcp` gör ett MCP-anrop utan att röra
  myndigheterna; lägg till `--live` för ett litet anrop per myndighets-API (gör det sällan, t.ex. dagligen).
- **Loggar:** allt skrivs till stderr (åtkomstlogg och fel). Inga loggfiler skrivs av servern själv.
  Åtkomstloggen innehåller klientens IP-adress (bakom en proxy på samma värd: den ursprungliga klientens, från
  `X-Forwarded-For`) och anropad sökväg utan frågesträng (visas som `?…`), men aldrig token eller andra headers.
  MCP-biblioteket skriver `Terminating session: …` när en session avslutas (med `FUZZY_MCP_STATELESS=true`:
  `None` efter varje anrop) – det är normalt, och sessions-id:t räcker inte för att anropa servern utan token. Ett
  försök att öppna en WebSocket ger `Unsupported upgrade request` och ett tips om `uvicorn[standard]`; servern har
  inga WebSocket-endpoints, så installera det inte. Bestäm rotation och gallring (Windows: `AppRotateFiles` i
  avsnitt 5; systemd: journald).
- **Proxy:** `smoke_test.py` och containerns `HEALTHCHECK` går aldrig via `HTTP(S)_PROXY` mot 127.0.0.1/localhost.
- **API-förändringar:** verktyget `fuzzy_api_notices` visar `Deprecation`/`Sunset`-huvuden från myndigheterna.

## 9. Kapacitet

- Sessioner hålls i minnet (högst 10 000, stängs efter 30 minuters inaktivitet). Kör **en** instans, eller flera
  bakom en lastbalanserare med sticky sessions.
- Alternativ: `FUZZY_MCP_STATELESS=true` – inga sessioner på servern, så flera instanser kan stå bakom en vanlig
  lastbalanserare utan sticky sessions (rekommenderas för Eneo). Server-initierade meddelanden mellan anrop
  (t.ex. förloppsnotiser) försvinner då, vilket inga av verktygen behöver.
- Anropen mot myndigheterna begränsas per värd (t.ex. SCB 30 anrop/10 s) så att servern inte blir spärrad.

## 10. Uppdatering och återställning

1. Kontrollera och packa upp det nya paketet (avsnitt 2; ta först bort den gamla `C:\install`).
2. Installera i en **ny** virtuell miljö bredvid den gamla. `C:\fuzzy-mcp` finns redan och är låst; den nya
   miljön ärver rättigheterna. I den nya paketmappen:

   ```powershell
   & {
   $ErrorActionPreference = "Stop"
   $py = "3.14"                         # samma som paketnamnet
   $venv = "C:\fuzzy-mcp\venv-0.1.2"     # den nya versionen
   if (Test-Path $venv) { throw "$venv finns redan – efter ett avbrutet försök: Remove-Item -Recurse -Force $venv och kör blocket igen (inte den miljö som tjänsten använder nu)" }
   py -$py -m venv $venv
   if ($LASTEXITCODE) { throw "Kunde inte skapa $venv med Python $py" }
   if ((Get-Content -LiteralPath "$venv\pyvenv.cfg") -match '^home\s*=\s*C:\\Users\\') { throw "$venv bygger på en Python under C:\Users – installera Python för alla användare" }
   & "$venv\Scripts\python" -m pip install --no-index --find-links wheelhouse --require-hashes -r requirements.lock
   if ($LASTEXITCODE) { throw "Beroendena kunde inte installeras – se felet ovan" }
   & "$venv\Scripts\python" -m pip install --no-index --no-deps (Get-ChildItem dist\*.whl).FullName
   if ($LASTEXITCODE) { throw "fuzzy-mcp kunde inte installeras – se felet ovan" }
   & "$venv\Scripts\python" smoke_test.py --stdio -- --sources reference
   }
   ```

3. Peka om tjänsten till den nya miljön, starta om och kör röktestet mot `/mcp`:

   ```powershell
   & {
   $ErrorActionPreference = "Stop"
   $nssm = "C:\Program Files\nssm\nssm.exe"
   $venv = "C:\fuzzy-mcp\venv-0.1.2"
   & $nssm set fuzzy-mcp Application "$venv\Scripts\fuzzy-mcp.exe"
   if ($LASTEXITCODE) { throw "nssm set misslyckades" }
   & $nssm restart fuzzy-mcp
   if ($LASTEXITCODE) { throw "nssm restart misslyckades – återställ enligt steg 4" }
   $ok = $false
   $np = @{}; if ($PSVersionTable.PSVersion.Major -ge 6) { $np.NoProxy = $true }
   foreach ($i in 1..30) { try { Invoke-RestMethod @np -TimeoutSec 2 http://127.0.0.1:8000/healthz | Out-Null; $ok = $true; break } catch { Start-Sleep 1 } }
   if (-not $ok) { throw "fuzzy-mcp svarar inte på /healthz – se C:\fuzzy-mcp\logs\fuzzy-mcp.log och återställ enligt steg 4" }
   & "$venv\Scripts\python" smoke_test.py --token-file C:\fuzzy-mcp\secrets\token.txt
   }
   ```

   Linux: samma sak med `/opt/fuzzy-mcp/venv-<version>` och ny sökväg i `ExecStart`, sedan
   `systemctl daemon-reload && systemctl restart fuzzy-mcp`. Vänta på `/healthz` innan röktestet, t.ex.
   `timeout 30 sh -c 'until curl --noproxy "*" -fsS http://127.0.0.1:8000/healthz; do sleep 1; done'`.
4. Återställning: peka tillbaka till den gamla miljön (`Application` respektive `ExecStart`) och starta om. Ingen
   data behöver migreras. Ta bort den gamla miljön när den nya har fungerat en tid.

## 11. Felsökning

| Symptom | Orsak och åtgärd |
| --- | --- |
| `401 Unauthorized` | token saknas eller är fel (`FUZZY_MCP_AUTH_TOKEN_FILE`) |
| `421 Misdirected Request` | `Host` saknas i `FUZZY_MCP_ALLOWED_HOSTS` |
| `403` med `Origin` | webbklientens origin saknas i `FUZZY_MCP_ALLOWED_ORIGINS` |
| `400 Missing session ID` på `/mcp` | normalt för `GET` utan MCP-session; använd `/healthz` för hälsokontroll |
| Verktyg svarar "Kunde inte nå API:t (ProxyError/ConnectError)" | utgående trafik blockeras – kontrollera brandvägg, `HTTPS_PROXY` och listan i avsnitt 6. Kräver proxyn NTLM/Kerberos-inloggning: be om undantag för värdarna |
| "HTTP 429" från en källa | myndighetens anropsgräns; servern försöker igen själv, minska samtidiga klienter |
| Certifikatfel mot myndigheterna | proxy med TLS-inspektion och en CA som saknas i operativsystemet: installera organisationens rotcertifikat där, eller sätt `SSL_CERT_FILE` till en komplett CA-bundle. Pekar `SSL_CERT_FILE` på en fil med bara organisationens CA blir det certifikatfel mot alla värdar som inte inspekteras |
| `Konfigurationsfel: …` och avslutskod 2 | fel värde i en miljövariabel, saknad token eller en CA-fil som saknas eller inte kan läsas; meddelandet säger vilken |
| Avslutskod 3 och `address already in use` (Windows: `10048`), `could not bind on any address` eller `Name or service not known` (Windows: `getaddrinfo failed`) i loggen | porten används redan, eller `FUZZY_MCP_HOST` är fel (adressen finns inte på datorn eller namnet kan inte slås upp) |

## 12. Ansvar

Förvaltare: Deniz Özer ([@deno-li](https://github.com/deno-li)). Säkerhetsproblem rapporteras enligt
[SECURITY.md](../SECURITY.md). Ändringar per version finns i [CHANGELOG.md](../CHANGELOG.md).
