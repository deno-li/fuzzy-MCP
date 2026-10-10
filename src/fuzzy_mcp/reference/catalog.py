# SPDX-FileCopyrightText: 2026 Deniz Özer
#
# SPDX-License-Identifier: MIT

"""Entity catalogue: every data entity exposed by the server, its identifiers
and how entities from different sources join (kommunkod, skolenhetskod,
organisationsnummer, skolform, kurs-/ämnes-/programkoder)."""

from typing import Any

ENTITIES: list[dict[str, Any]] = [
    # --- SCB -----------------------------------------------------------------------------------------
    {
        "kalla": "scb",
        "entitet": "Tabell",
        "nyckel": "table_id (TAB + siffror, t.ex. TAB638)",
        "verktyg": ["scb_search_tables", "scb_browse_subjects", "scb_get_table"],
        "beskrivning": "Statistiktabell i Statistikdatabasen med titel, period, tidsenhet och ämnessökväg.",
    },
    {
        "kalla": "scb",
        "entitet": "Variabel och värde",
        "nyckel": "variabelkod (Region, Kon, Alder, ContentsCode, Tid ...) + värdekod",
        "verktyg": ["scb_get_table_metadata"],
        "beskrivning": "Dimensioner i en tabell. ContentsCode = mått, Tid = tid, Region = geografi (kommun/län/riket).",
    },
    {
        "kalla": "scb",
        "entitet": "Kodlista",
        "nyckel": "codelist_id (agg_* aggregering, vs_* värdemängd; skiftlägeskänsligt)",
        "verktyg": ["scb_get_codelist"],
        "beskrivning": "Alternativa indelningar, t.ex. vs_RegionLän07, agg_RegionNUTS2_2008, agg_Ålder5år.",
    },
    {
        "kalla": "scb",
        "entitet": "Datacell",
        "nyckel": "tabell + en värdekod per variabel",
        "verktyg": ["scb_get_table_data", "scb_build_query"],
        "beskrivning": "Statistikvärde med ev. statuskod ('..' = uppgift saknas).",
    },
    # --- SCB: öppna geodata -------------------------------------------------------------------------
    {
        "kalla": "scb",
        "entitet": "Geografiskt område (DeSO, RegSO, tätort m.m.)",
        "nyckel": "DeSO-/RegSO-kod och kommunkod; attributnamn och lagernamn (årsversion) läses ur "
        "scb_geodata_layers/scb_geodata_describe_layer",
        "verktyg": [
            "scb_geodata_layers",
            "scb_geodata_describe_layer",
            "scb_geodata_get_features",
            "scb_geodata_download_url",
        ],
        "beskrivning": "Statistikområden och andra indelningar som WFS-lager; koderna kopplar till SCB-tabeller "
        "på DeSO/RegSO-nivå. Geometri via nedladdningslänk (GeoPackage, GeoJSON, Shape, CSV).",
    },
    # --- Folkhälsomyndigheten ------------------------------------------------------------------------
    {
        "kalla": "fohm",
        "entitet": "Tabell",
        "nyckel": "sökväg A_Folkhalsodata/<mappar>/<tabell>.px",
        "verktyg": ["fohm_browse", "fohm_search_tables"],
        "beskrivning": "Tabell i Folkhälsodata, t.ex. Nationella folkhälsoenkäten (B_HLV) och HBSC (C_HBSC).",
    },
    {
        "kalla": "fohm",
        "entitet": "Variabel och värde",
        "nyckel": "variabelkod (svenska ord, t.ex. 'Region', 'Kön', 'År', 'Andel och konfidensintervall') + värdekod",
        "verktyg": ["fohm_get_table_metadata"],
        "beskrivning": "Koder måste skickas exakt som i metadata. Region använder SCB:s läns-/kommunkoder.",
    },
    {
        "kalla": "fohm",
        "entitet": "Datacell",
        "nyckel": "tabell + en värdekod per variabel",
        "verktyg": ["fohm_get_table_data", "fohm_build_query"],
        "beskrivning": "Ofta andelar med konfidensintervall; flerårsperioder som '2021-2024' i regionala enkäter.",
    },
    # --- Skolverket: statistikdatabasen ---------------------------------------------------------------
    {
        "kalla": "skolverket",
        "entitet": "Tabell och mått (Skolverkets statistikdatabas)",
        "nyckel": "sökväg Skolverkets_statistikdatabas/<område>/<skolform>/.../<tabell>.px; mått = 'variable'",
        "verktyg": ["skolverket_stat_browse", "skolverket_stat_search_tables", "skolverket_stat_get_table_metadata"],
        "beskrivning": "Kommunala jämförelsetal (KJT) och Underlag för analys (UFA) per skolform: elever, betyg, "
        "behörighet, personal, kostnader, nationella delmål.",
    },
    {
        "kalla": "skolverket",
        "entitet": "Jämförelsevärde per kommun, huvudman eller skolenhet",
        "nyckel": "level: kommun-/länskod eller '00' (KJT); orgnr eller 'orgnr-skolenhetskod' (UFA)",
        "verktyg": ["skolverket_stat_get_table_data", "skolverket_stat_build_query"],
        "beskrivning": "Värden per läsår (2024 = 2024/25) eller kalenderår. Ej officiell statistik; '..' = sekretess.",
    },
    # --- Skolverket: Skolenhetsregistret --------------------------------------------------------------
    {
        "kalla": "skolverket",
        "entitet": "Skolenhet",
        "nyckel": "skolenhetskod / schoolUnitCode (8 siffror)",
        "verktyg": ["skolverket_search_school_units", "skolverket_get_school_unit"],
        "beskrivning": "Registrerad skolenhet med status, skolformer, kommunkod, adress, koordinater och huvudman.",
    },
    {
        "kalla": "skolverket",
        "entitet": "Huvudman",
        "nyckel": "organisationsnummer (10 siffror)",
        "verktyg": ["skolverket_search_organizers", "skolverket_get_organizer"],
        "beskrivning": "Kommun, region, stat eller enskild huvudman med sina skolenheter.",
    },
    {
        "kalla": "skolverket",
        "entitet": "Utbildningsanordnare och entreprenad (komvux)",
        "nyckel": "educationProviderCode; (organisationsnummer, educationProviderCode)",
        "verktyg": [
            "skolverket_search_education_providers",
            "skolverket_get_education_provider",
            "skolverket_search_contracts",
            "skolverket_get_contract",
        ],
        "beskrivning": "Anordnare av vuxenutbildning och avtal om utbildning på entreprenad.",
    },
    # --- Skolverket: Syllabus ------------------------------------------------------------------------
    {
        "kalla": "skolverket",
        "entitet": "Ämne",
        "nyckel": "ämneskod (t.ex. MAT, SVE; Gy25: MATE, SVEN)",
        "verktyg": ["skolverket_list_subjects", "skolverket_get_subject"],
        "beskrivning": "Ämnesplan/kursplan med syfte, centralt innehåll och betygskriterier (kunskapskrav).",
    },
    {
        "kalla": "skolverket",
        "entitet": "Kurs / nivå (Gy25)",
        "nyckel": "kurskod (t.ex. MATMAT01a) eller nivåkod (Gy25, t.ex. MATE1A00X)",
        "verktyg": ["skolverket_list_courses", "skolverket_get_course", "skolverket_get_subject"],
        "beskrivning": "Kurser (GY11, komvux) och ämnesnivåer (Gy25, betyg sätts på ämnesnivå).",
    },
    {
        "kalla": "skolverket",
        "entitet": "Program",
        "nyckel": "programkod / studievägskod (t.ex. NA, NA25, IMV)",
        "verktyg": ["skolverket_list_programs", "skolverket_get_program", "skolverket_syllabus_valuestore"],
        "beskrivning": "Gymnasieprogram med inriktningar och programgemensamma ämnen.",
    },
    {
        "kalla": "skolverket",
        "entitet": "Läroplan",
        "nyckel": "läroplanskod (t.ex. LGR22)",
        "verktyg": ["skolverket_list_curriculums", "skolverket_get_curriculum"],
        "beskrivning": "Läroplaner för skolformerna.",
    },
    # --- Skolverket: Planerad utbildning -------------------------------------------------------------
    {
        "kalla": "skolverket",
        "entitet": "Skolenhet (utbud och statistik)",
        "nyckel": "schoolUnitCode (= skolenhetskod)",
        "verktyg": [
            "skolverket_pe_search_school_units",
            "skolverket_pe_get_school_unit",
            "skolverket_pe_school_unit_statistics",
            "skolverket_pe_school_unit_surveys",
        ],
        "beskrivning": "Nyckeltal per skolform (behöriga lärare, meritvärde, behörighet ...) och Skolenkäten.",
    },
    {
        "kalla": "skolverket",
        "entitet": "Utbildningstillfälle (gymnasie- och vuxenutbildning)",
        "nyckel": "skolenhetskod + studievägskod; vuxenutbildningstillfällets id",
        "verktyg": [
            "skolverket_pe_school_unit_education_events",
            "skolverket_pe_search_education_events",
            "skolverket_pe_search_adult_education_events",
        ],
        "beskrivning": "Planerade gymnasieprogram per skola och kurser inom komvux/sfi.",
    },
    {
        "kalla": "skolverket",
        "entitet": "Nationella värden och SALSA",
        "nyckel": "skolform (+ programkod för gy); skolenhetskod för SALSA",
        "verktyg": ["skolverket_pe_national_statistics", "skolverket_pe_salsa"],
        "beskrivning": "Rikets värden för jämförelse och SALSA (förväntade resultat givet elevsammansättning).",
    },
    # --- Skolverket: Susa-navet -----------------------------------------------------------------------
    {
        "kalla": "skolverket",
        "entitet": "Utbildning / utbildningstillfälle / anordnare (Susa-navet)",
        "nyckel": "id (t.ex. 'e.uoh.kth.dd1420...', 'i.' respektive 'p.'-prefix)",
        "verktyg": [
            "skolverket_susa_search_education_events",
            "skolverket_susa_get_education_info",
            "skolverket_susa_get_education_provider",
        ],
        "beskrivning": "Utbildningsinformation från högskola, yrkeshögskola, folkhögskola och komvux (EMIL 3).",
    },
    # --- Dataportal ----------------------------------------------------------------------------------
    {
        "kalla": "dataportal",
        "entitet": "Dataset, distribution och datatjänst (DCAT-AP-SE)",
        "nyckel": "context_id + entry_id; dataset-URI",
        "verktyg": ["dataportal_search_datasets", "dataportal_get_dataset"],
        "beskrivning": "Metadata om myndigheters dataset med licens, utgivare, tema och nedladdningslänkar.",
    },
    # --- Referensdata --------------------------------------------------------------------------------
    {
        "kalla": "reference",
        "entitet": "Kommun och län",
        "nyckel": "kommunkod (4 siffror), länskod (2 siffror), länsbokstav",
        "verktyg": ["ref_lookup_region", "ref_list_municipalities"],
        "beskrivning": "SCB:s regionala indelning – nyckeln som kopplar ihop alla källor geografiskt.",
    },
    {
        "kalla": "reference",
        "entitet": "DeSO och RegSO (kopplingstabeller och förändringslogg)",
        "nyckel": "DeSO-kod (9 tecken), RegSO-kod (kommunkod + R + 3 siffror)",
        "verktyg": ["ref_lookup_deso", "ref_list_deso"],
        "beskrivning": "SCB:s koppling DeSO↔RegSO för båda versionsparen (DeSO 2018/RegSO 2020, DeSO 2025/RegSO 2025) "
        "och SCB:s förändringslogg för DeSO, med kontroll av summerbarhet över förändringar.",
    },
]

JOIN_KEYS: list[dict[str, Any]] = [
    {
        "nyckel": "DeSO-/RegSO-kod",
        "format": "DeSO: 9 tecken = kommunkod (1–4) + kategori A/B/C (5) + löpnummer (6–8) + reservsiffra som används "
        "vid delningar (9), t.ex. 2180C1010; DeSO har inga namn. RegSO: kommunkod + R + 3 siffror, t.ex. 2180R001; "
        "RegSO har namn, men namnet är unikt bara inom kommunen och kan ändras.",
        "forekomst": {
            "scb": "variabeln Region i tabeller på DeSO/RegSO-nivå; tabellens kodlistor läses ur "
            "scb_get_table_metadata",
            "scb_geodata": "attribut i SCB:s DeSO-/RegSO-lager; lager- och attributnamn läses ur scb_geodata_layers "
            "respektive scb_geodata_describe_layer",
            "referens": "ref_lookup_deso och ref_list_deso – kopplingen DeSO↔RegSO för båda versionsparen och SCB:s "
            "förändringslogg (kodlistorna deso_regso_2018, deso_regso_2025, deso_forandringar)",
        },
        "not": "Två versionspar: DeSO 2018 med RegSO 2020 och DeSO 2025 med RegSO 2025. 5 835 koder finns i båda, men "
        "enligt SCB:s förändringslogg (förändringar daterade 2025-01-01; filen daterad 2025-09-19) har 603 av dem en "
        "egen rad (samma kod före och efter) och ytterligare 84 förekommer bara som mottagare på andra koders rader; "
        "ref_lookup_deso ger 'ändrad gräns' för alla 687: samma kod är inte alltid samma yta. Kontrollera i tabellens "
        "metadata (scb_get_table_metadata: noter och kodlistan för Region) vilken DeSO/RegSO-version som gäller för "
        "den valda perioden, och summerbarheten i ref_lookup_deso innan statistik summeras över en förändring.",
    },
    {
        "nyckel": "kommunkod",
        "format": "4 siffror (länskod + 2), t.ex. 0180 Stockholm, 1480 Göteborg",
        "forekomst": {
            "scb": "variabeln Region (4-siffriga koder; vs_RegionKommun07)",
            "fohm": "variabeln Region i kommuntabeller (valueTexts har ofta koden först, t.ex. '2180 Gävle')",
            "skolenhetsregistret": "municipalityCode på skolenhet",
            "skolverkets_statistikdatabas": "dimensionen 'level' i KJT-tabeller",
            "planerad_utbildning": "geographicalAreaCode",
            "susa_navet": "locations[].areaCode",
            "referens": "ref_lookup_region",
        },
    },
    {
        "nyckel": "länskod",
        "format": "2 siffror (01–25, saknar 02, 11, 15, 16); riket = 00",
        "forekomst": {
            "scb": "variabeln Region (2-siffriga koder; vs_RegionLän07)",
            "fohm": "variabeln Region (t.ex. '21 Gävleborgs län')",
            "referens": "ref_lookup_region (kind='lan'); kommunkodens två första siffror",
        },
    },
    {
        "nyckel": "skolenhetskod",
        "format": "8 siffror",
        "forekomst": {
            "skolenhetsregistret": "schoolUnitCode",
            "planerad_utbildning": "schoolUnitCode",
            "skolverkets_statistikdatabas": "suffix i 'level' i UFA-tabeller: '<organisationsnummer>-<skolenhetskod>'",
            "ss12000": "Organisation.schoolUnitCode (organisationType 'Skolenhet')",
        },
    },
    {
        "nyckel": "organisationsnummer",
        "format": "10 siffror (NNNNNN-NNNN)",
        "forekomst": {
            "skolenhetsregistret": "huvudmannens organizationNumber",
            "skolverkets_statistikdatabas": "dimensionen 'level' i UFA-tabeller (huvudman)",
            "dataportal": "utgivare http://dataportal.se/organisation/SE<orgnr>",
            "ss12000": "Organisation.organisationNumber (huvudman)",
        },
    },
    {
        "nyckel": "skolform",
        "format": "kod, t.ex. GR, GY, GRAN/GRS, GYAN/GYS, FKLASS, FTH, VUX, SFI",
        "forekomst": {
            "skolenhetsregistret": "schoolTypes (versaler)",
            "planerad_utbildning": "typeOfSchooling (gemener: gr, gy, gran, gyan, fsk ...)",
            "syllabus": "schooltype (versaler)",
            "susa_navet": "schoolType",
            "ss12000": "schoolTypes",
        },
        "not": "Koderna skiljer sig mellan standarder och API:er (t.ex. anpassad grundskola: GRS i SS 12000 v2.1, "
        "GRAN i Skolenhetsregistret, gran i Planerad utbildning). Översätt med kodlistan 'skolformer'.",
    },
    {
        "nyckel": "ämnes-, kurs- och programkoder",
        "format": "t.ex. ämne MAT/MATE, kurs MATMAT01a, nivå MATE1A00X, program NA/NA25",
        "forekomst": {
            "syllabus": "subjects/courses/programs code",
            "planerad_utbildning": "studyPathCode / programkod",
            "ss12000": "Syllabus.subjectCode / courseCode, Programme.code",
        },
        "not": "Programkoder: kodlistan 'gymnasieprogram' (Gy11 t.ex. NA, Gy25 t.ex. NA25), 'introduktionsprogram'; "
        "kodmönster i 'kodstrukturer'.",
    },
    {
        "nyckel": "betyg",
        "format": "A–F, streck, Godkänt/Icke godkänt; från 2028-07-02 även 1–10 (och 1–4 för gymnasiearbete)",
        "forekomst": {
            "referens": "kodlistan 'betygsskalor' (skalor, markeringar och regler med lagrum)",
            "scb": "kodlistan 'scb_betygskoder' (2, 3, 9, Y, Z vid betygsinsamling)",
            "ss12000": "Grade.gradeValue (fri text), Grade.trial (prövning), Grade.semester/year",
        },
    },
]

# Conceptual bridge from open, aggregated indicators to the BBIC triangle (Barns behov i centrum,
# Socialstyrelsen) and ICF (WHO). It frames population-level context for analysis; it is not a validated
# code mapping and never involves individual data.
SEMANTIC_BRIDGE: dict[str, Any] = {
    "syfte": "Analysstöd: vilka öppna indikatorer belyser vilka BBIC-domäner och ICF-komponenter på befolkningsnivå. "
    "Ingen validerad kodmappning och inga individdata.",
    "ramverk": {
        "bbic_barnets_behov": [
            "Hälsa",
            "Utbildning",
            "Känslo- och beteendemässig utveckling",
            "Identitet",
            "Familj och sociala relationer",
            "Socialt uppträdande",
            "Förmåga att klara sig själv",
        ],
        "icf_komponenter": {
            "b": "Kroppsfunktioner (t.ex. b1 Psykiska funktioner)",
            "s": "Kroppsstrukturer",
            "d": "Aktivitet och delaktighet (t.ex. d7 Mellanmänskliga interaktioner, d8 Viktiga livsområden)",
            "e": "Omgivningsfaktorer (t.ex. e3 Stöd och relationer, e5 Service, tjänster, system och policy)",
        },
    },
    "kopplingar": [
        {
            "indikatorer": "Behörighet till gymnasiet, meritvärde, andel med examen (Skolverket Planerad utbildning)",
            "bbic": ["Utbildning"],
            "icf": ["d810–d839 Utbildning", "e585 Utbildnings- och träningstjänster, -system och -policy"],
            "verifiering": "härlett",
            "ss12000": "Grade, Enrolment, Programme (individnivå i skolans system)",
        },
        {
            "indikatorer": "Skolenkäten: trygghet, studiero, stöd (Skolverket)",
            "bbic": ["Utbildning", "Socialt uppträdande"],
            "icf": ["d820 Skolutbildning", "e3 Stöd och relationer"],
            "verifiering": "härlett",
        },
        {
            "indikatorer": "Psykosomatiska besvär, livstillfredsställelse, psykiskt välbefinnande (FoHM, HBSC/HLV)",
            "bbic": ["Hälsa", "Känslo- och beteendemässig utveckling"],
            "icf": ["b1 Psykiska funktioner", "b152 Emotionella funktioner"],
            "verifiering": "härlett",
        },
        {
            "indikatorer": "Mobbning i skolan (FoHM, HBSC)",
            "bbic": ["Familj och sociala relationer", "Socialt uppträdande"],
            "icf": ["d7 Mellanmänskliga interaktioner och relationer"],
            "verifiering": "härlett",
        },
        {
            "indikatorer": "Levnadsvanor: alkohol, tobak, fysisk aktivitet (FoHM)",
            "bbic": ["Hälsa", "Förmåga att klara sig själv"],
            "icf": ["d570 Att sköta sin egen hälsa"],
            "verifiering": "härlett",
        },
        {
            "indikatorer": "Hushållens ekonomi och boende, t.ex. disponibel inkomst, ekonomisk utsatthet (SCB, FoHM)",
            "bbic": ["Familj och miljö/nätverk (ekonomi, boende, arbete)"],
            "icf": ["e165 Tillgångar", "e5 Service, tjänster, system och policy"],
            "verifiering": "härlett",
        },
    ],
    "forbehall": [
        "Öppna data är aggregerade (kommun/län/riket, skolenhet) och kan inte kopplas till enskilda barn.",
        "Koppling till individdata (SS 12000-system, BBIC-dokumentation) kräver rättslig grund och sekretessprövning "
        "(OSL) och ligger utanför den här servern.",
        "Använd kopplingarna som hypoteser för analys och behovsbeskrivning, inte som klassificering.",
        "Varje koppling har verifiering 'härlett' (projektets egen analys); se kodlistan 'verifiering'.",
    ],
}

# Generic conceptual model of what an analysis of open school, health and welfare statistics needs, mapped to the
# open sources. Open data is aggregated and serves as comparison values (benchmarks) and context; data about
# individuals is only mentioned to say that open data cannot provide it.
INFORMATION_MODEL: dict[str, Any] = {
    "syfte": "Visar vilka begrepp en analys av öppna data behöver (mått, dimensioner och spårbarhet), vilka verktyg "
    "som ger dem och vilka nycklar som binder ihop dem.",
    "entiteter": [
        {
            "entitet": "Indikatorvärde",
            "roll": "fakta",
            "oppna_kallor": "Datacell i SCB-, FoHM- och Skolverket-tabeller (värde + status) per geografi och period",
            "verktyg": ["scb_get_table_data", "fohm_get_table_data", "skolverket_stat_get_table_data"],
            "falt": "värde, status ('..' = prickad/saknas → suppression), period, geografi, nedbrytning (kön, ålder "
            "m.m.), källa (citation), request_url",
            "not": "Lagra tabell-id och urval (scb_build_query/fohm_build_query) som regelversion för spårbarhet.",
        },
        {
            "entitet": "Indikator och definition",
            "roll": "dimension (metadata)",
            "oppna_kallor": "Tabellmetadata: innehåll (ContentsCode/mått), enhet, decimaler, referensperiod, måttyp, "
            "fotnoter och länkar till definitioner (META-ID)",
            "verktyg": ["scb_get_table_metadata", "fohm_get_table_metadata", "skolverket_stat_get_table_metadata"],
            "falt": "contents_info (base, decimals, refperiod, measuring_type), notes, links",
        },
        {
            "entitet": "Period",
            "roll": "dimension",
            "oppna_kallor": "Tidsvariabler: SCB 'Tid' (2024, 2024M01, 2024K1), FoHM 'År' (även flerårsperioder "
            "som 2021-2024), Skolverket 'time' (läsår kodat med hösttermin: 2024 = 2024/25)",
            "verktyg": ["scb_get_table_metadata", "fohm_get_table_metadata", "skolverket_stat_get_table_metadata"],
            "not": "Normalisera till kalenderår, läsår och termin; markera flerårsperioder separat.",
        },
        {
            "entitet": "Geografi",
            "roll": "dimension",
            "oppna_kallor": "Riket (00), län (2 siffror), kommun (4 siffror), kommungrupper (A1–C9), DeSO och RegSO",
            "verktyg": [
                "ref_lookup_region",
                "ref_list_municipalities",
                "ref_lookup_deso",
                "ref_list_deso",
                "scb_geodata_get_features",
                "scb_geodata_download_url",
            ],
            "not": "Stadsdelar, NYKO och postnummer finns inte i källorna; koppla dem lokalt till DeSO/RegSO.",
        },
        {
            "entitet": "Organisation",
            "roll": "dimension",
            "oppna_kallor": "Huvudman (organisationsnummer), skolenhet (skolenhetskod), utbildningsanordnare",
            "verktyg": [
                "skolverket_search_school_units",
                "skolverket_get_school_unit",
                "skolverket_search_organizers",
                "skolverket_pe_get_school_unit",
            ],
            "not": "Lokala nivåer (nämnd, verksamhetsområde, rektorsområde) mappas till skolenhetskoderna lokalt.",
        },
        {
            "entitet": "Skolform",
            "roll": "dimension",
            "oppna_kallor": "Kodlistan 'skolformer' med koder i SS 12000, Skolenhetsregistret, Syllabus och "
            "Planerad utbildning",
            "verktyg": ["ref_get_code_list"],
        },
        {
            "entitet": "Population",
            "roll": "dimension/nämnare",
            "oppna_kallor": "Folkmängd per region, ålder och kön (SCB) som nämnare; elevantal per skolform "
            "(Skolverket)",
            "verktyg": ["scb_search_tables", "scb_get_table_data", "skolverket_stat_get_table_data"],
        },
        {
            "entitet": "Styrdokument och mål",
            "roll": "dimension (styrning)",
            "oppna_kallor": "Läroplaner, ämnen, kurser och program (Syllabus); nationella delmål i Skolverkets "
            "underlag för analys; BBIC/ICF-bryggan",
            "verktyg": ["skolverket_list_curriculums", "skolverket_get_subject", "skolverket_stat_search_tables"],
            "not": "Resursen fuzzy://crosswalk/bbic-icf ger en konceptuell brygga, ingen validerad kodmappning.",
        },
        {
            "entitet": "Kodöversättning",
            "roll": "spårbarhet",
            "oppna_kallor": "Skolformskoder mellan standarder, SCB:s kodlistor (agg_/vs_ med valueMap), SCB:s "
            "betygskoder, kodstrukturer för ämnen/kurser/program",
            "verktyg": ["ref_get_code_list", "scb_get_codelist"],
            "not": "Varje post i de medföljande kodlistorna har fältet verifiering (författning, officiell_spec, "
            "verifierat_uttag, myndighetswebb, referenskod, härlett eller okänd). Ta med det som kolumn i en "
            "översättningstabell så att otestade översättningar kan filtreras fram.",
        },
        {
            "entitet": "Datakälla",
            "roll": "spårbarhet",
            "oppna_kallor": "Källförteckning med licens och API-version; dataset i Sveriges dataportal (DCAT-AP-SE)",
            "verktyg": ["fuzzy_list_sources", "dataportal_search_datasets", "dataportal_get_dataset"],
        },
        {
            "entitet": "Uttag och uppdatering",
            "roll": "spårbarhet",
            "oppna_kallor": "Återanvändbara frågor (GET-URL, POST, Power Query) och tabellens 'updated' som "
            "vattenstämpel; API-livscykelnotiser (Deprecation/Sunset)",
            "verktyg": ["scb_build_query", "fohm_build_query", "skolverket_stat_build_query", "fuzzy_api_notices"],
        },
        {
            "entitet": "Datakvalitet",
            "roll": "spårbarhet",
            "oppna_kallor": "Statuskoder och teckenförklaring, fotnoter, avslutade tabeller (discontinued), "
            "values_withheld, trunkeringsflaggor i svaren",
            "verktyg": ["scb_get_table_metadata", "scb_get_table_data"],
        },
        {
            "entitet": "Studie- och provresultat (aggregat)",
            "roll": "fakta (aggregat)",
            "oppna_kallor": "Betyg, meritvärde, behörighet och nationella prov per kommun, huvudman och skolenhet",
            "verktyg": ["skolverket_stat_get_table_data", "skolverket_pe_school_unit_statistics"],
            "not": "Endast aggregat. Individnivå (betyg per elev) finns bara i verksamhetens egna system.",
        },
        {
            "entitet": "Enkätresultat (aggregat)",
            "roll": "fakta (aggregat)",
            "oppna_kallor": "Andelar med konfidensintervall från Nationella folkhälsoenkäten och Skolbarns "
            "hälsovanor (FoHM) samt Skolenkäten (Skolverket)",
            "verktyg": ["fohm_get_table_data", "skolverket_pe_school_unit_surveys"],
            "not": "Inga enskilda svar; använd som referensvärden för lokala enkäter.",
        },
        {
            "entitet": "Personal och ekonomi (aggregat)",
            "roll": "fakta (aggregat)",
            "oppna_kallor": "Lärare med legitimation, elever per lärare och kostnad per elev (Skolverkets kommunala "
            "jämförelsetal); sysselsättning och inkomster (SCB)",
            "verktyg": ["skolverket_stat_get_table_data", "scb_get_table_data"],
        },
    ],
    "ej_i_oppna_data": [
        "Uppgifter om enskilda personer finns inte i öppna data. De hanteras i verksamhetens egna system med "
        "rättslig grund; servern levererar bara aggregerade jämförelsevärden.",
    ],
}


CAVEATS: list[str] = [
    "Alla källor är öppna data. Som standard returneras inga personuppgifter; namn på enskilda (rektor, c/o-namn, "
    "kontaktpersoner) visas bara med uttryckliga parametrar märkta 'personuppgift'.",
    "Jämför bara värden med samma definition, period och population (kontrollera metadata och fotnoter).",
    "Kodlistornas poster anger med fältet verifiering hur de är kontrollerade; 'okänd' betyder odokumenterat ursprung.",
    "Små tal kan vara prickade/undertryckta ('..', '*', 'OMITTED_DUE_TO_BASED_ON_FEW_PUPILS').",
    "Folkhälsomyndighetens regionala enkätdata avser ofta flerårsperioder (t.ex. 2021-2024).",
]


def catalogue(source: str | None = None) -> dict[str, Any]:
    entities = [e for e in ENTITIES if source is None or e["kalla"] == source]
    return {"entiteter": entities, "kopplingsnycklar": JOIN_KEYS, "forbehall": CAVEATS}


def semantic_bridge() -> dict[str, Any]:
    return SEMANTIC_BRIDGE


def information_model() -> dict[str, Any]:
    return INFORMATION_MODEL
