<!--
SPDX-FileCopyrightText: 2026 Deniz Özer

SPDX-License-Identifier: CC0-1.0
-->

# Säkerhetsrapport

Om du har hittat en säkerhetssårbarhet uppskattar vi att du tar dig tid att rapportera den privat.
Följ dessa riktlinjer när du skickar in din rapport.

## Rapportering

Rapportera via GitHubs privata sårbarhetsrapportering:
[Security → Report a vulnerability](https://github.com/deno-li/fuzzy-MCP/security/advisories/new).
Skapa **inte** ett offentligt ärende för säkerhetsproblem.

Inkludera följande information:

1. **PROJEKT** – projektets URL och vilken version eller commit som berörs.
2. **OFFENTLIG** – om sårbarheten redan har diskuterats eller publicerats offentligt, med länkar.
3. **BESKRIVNING** – en detaljerad beskrivning, hur den kan återskapas och vilken påverkan den har.

## Omfattning

Servern läser enbart öppna data via HTTPS och har inga egna inloggningsuppgifter.
Särskilt intressant är till exempel:

- sätt att få servern att anropa andra värdar än de konfigurerade API:erna (SSRF),
- injektion via sökvägar eller parametrar som skickas vidare till myndigheternas API:er,
- brister i HTTP-transporten (värdkontroll/DNS-rebinding) när servern körs med `streamable-http`.

## Konfidentialitet

Vi ber dig att hålla rapporten konfidentiell tills vi gjort ett offentligt tillkännagivande.

## Hantering

- Vi bekräftar mottagandet inom fem arbetsdagar.
- Sårbarheter hanteras efter bästa förmåga och åtgärdas i en ny utgåva.
- Du meddelas samtidigt som det offentliga tillkännagivandet görs.

Tack för att du hjälper oss att förbättra projektets säkerhet!
