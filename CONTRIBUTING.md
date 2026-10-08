<!--
SPDX-FileCopyrightText: 2026 Deniz Özer

SPDX-License-Identifier: CC0-1.0
-->

# Riktlinjer för bidrag

[![Conventional Commits](https://img.shields.io/badge/Conventional%20Commits-1.0.0-yellow.svg)](https://conventionalcommits.org)
[![Contributor Covenant](https://img.shields.io/badge/Contributor%20Covenant-2.1-4baaaa.svg)](CODE_OF_CONDUCT.md)
[![DCO](https://img.shields.io/badge/DCO-signoff-blue.svg)](https://developercertificate.org/)

Välkommen! Vi är glada över att du vill bidra till projektet.

## Sätt att bidra

Som ny bidragsgivare är du i en utmärkt position att ge värdefull återkoppling. Du kan hjälpa till genom att:

- Fixa eller rapportera ett programfel.
- Föreslå förbättringar av kod, tester och dokumentation.
- Rapportera eller fixa problem som upptäcks under installation eller i utvecklingsmiljöer.
- Föreslå nya datakällor, verktyg eller förbättringar.
- Rapportera när en myndighets API har ändrats (nya fält, ny version, `Deprecation`/`Sunset`-huvuden).

## Uppförandekod

Var trevlig och respektfull mot varandra.
Vi följer [Contributor Covenants uppförandekod](CODE_OF_CONDUCT.md).

## Skapa ett ärende

Innan du skapar ett nytt ärende, kontrollera om ett liknande redan finns.
Om det gör det, lägg till din information som en kommentar till det befintliga ärendet.

### Rapportera ett fel

1. Öppna ett ärende som sammanfattar felet.
2. Ange verktyg, argument, förväntat och faktiskt resultat. Klistra gärna in felmeddelandet från verktyget.
3. Sätt etiketten "bug".

### Föreslå en förbättring

1. Öppna ett ärende som sammanfattar den önskade funktionaliteten och dess användningsfall.
2. Sätt etiketten "enhancement".

## Bidra med kod, dokumentation och mer

1. Diskutera dina planer i förväg för att säkerställa att de stämmer överens med projektets mål.
2. Kontrollera listan över öppna ärenden. Tilldela dig själv ett befintligt ärende eller skapa ett nytt.
3. Följ projektets konventioner för tester, kodstil, dokumentation och commit-meddelanden (se [DEVELOPMENT.md](DEVELOPMENT.md)).
4. Bidrag kan avslås om de inte överensstämmer med projektets riktlinjer eller mål.
5. Bekanta dig med [Pull Request-livscykeln](#pull-request-livscykel).
6. Godkänn normen "inbound=outbound": dina bidrag licensieras under samma licens som projektet.
7. [Signera dina commits](#dco--signoff-och-signering-av-en-commit).

### Särskilt för nya datakällor och verktyg

- API-kontrakt (sökvägar, parametrar, fältnamn) ska komma från myndighetens egen specifikation eller verifierade svar – gissa aldrig.
- Varje verktyg ska vara skrivskyddat, ha svensk beskrivning och returnera kompakta, strukturerade svar.
- Tester körs mot mockade svar (`httpx2.MockTransport`) som speglar API:ets dokumenterade format exakt.

## Återkoppling på ärenden och Pull Requests

Projektansvariga strävar efter att granska och svara på ärenden inom fem arbetsdagar.
Kvaliteten på informationen i ditt ärende eller din pull request påverkar hur snabbt du får återkoppling.
För icke-triviala bidrag, diskutera med projektansvariga först.

**Om projektet inte är markerat som arkiverat underhålls det.**

## Pull Request-livscykel

Vi använder Fork-and-Pull-modellen:

1. Forka repositoriet.
2. Skapa en ämnesgren från din forks huvudgren.
3. Pusha dina ändringar till ämnesgrenen i din fork.
4. Öppna en ny pull request mot huvudprojektet.
5. Svara på eventuell återkoppling från projektansvariga.

## Commit-riktlinjer

### DCO – signoff och signering av en commit

#### Signoff (DCO-godkännande)

***En signoff försäkrar projektet att du har rätt att bidra med ditt innehåll.***

```sh
git commit --signoff -m "fix: hantera tomt urval i scb_get_table_data"
```

#### Signera

***En signatur försäkrar att commiten kom från dig.***

```sh
git commit --signoff --gpg-sign -m "fix: hantera tomt urval i scb_get_table_data"
```

### Commit-standard

- Använd [Conventional Commits](https://www.conventionalcommits.org/sv/v1.0.0/) (`feat:`, `fix:`, `docs:`, `test:`, `refactor:`, `chore:`, `ci:`).
- Gruppera relevanta ändringar i samma commit.
- Skriv tydliga, lättlästa commit-meddelanden.

## Rapportera säkerhetsproblem

För säkerhetssårbarheter, följ riktlinjerna i [SECURITY.md](SECURITY.md).

## Utvecklingsriktlinjer

Se [DEVELOPMENT.md](DEVELOPMENT.md).

## Skrivstil och språk

- Användardokumentation och verktygsbeskrivningar skrivs i första hand på svenska (språklagen 2009:600).
- Kod, kodkommentarer och tekniska diskussioner får vara på engelska.
- Håll dokumentationen lättläst, använd punktlistor och länka till externa resurser vid behov.

## FOSS-standarder

- [REUSE](https://reuse.software/) för licensinformation
- [Conventional Commits](https://www.conventionalcommits.org/)
- [Keep a Changelog](https://keepachangelog.com/sv/1.1.0/) och [Semantic Versioning](https://semver.org/lang/sv/)
- [Contributor Covenant](https://www.contributor-covenant.org/)
- [publiccode.yml](https://yml.publiccode.tools/) och [Standard for Public Code](https://standard.publiccode.net/)
