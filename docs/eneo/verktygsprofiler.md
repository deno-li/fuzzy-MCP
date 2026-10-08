<!--
SPDX-FileCopyrightText: 2026 Deniz Özer

SPDX-License-Identifier: CC0-1.0
-->

# Verktygsprofiler för Eneo

Genereras från servern med `python scripts/generate_tool_docs.py` – ändra inte för hand.

Eneo skickar alla aktiverade verktygsdefinitioner till modellen i varje tur. Aktivera därför en profil
per assistent: antingen genom att bara slå på profilens verktyg i assistenten, eller med en egen
fuzzy-mcp-instans per profil (`FUZZY_MCP_SOURCES`). Katalogen är namn, beskrivning och indataschema
per verktyg (det Eneo skickar till modellen). Token är en grov uppskattning (byte / 4); den faktiska
kostnaden beror på modellen.

| Profil | `FUZZY_MCP_SOURCES` | Verktyg | Katalog | Cirka token per tur |
| --- | --- | --- | --- | --- |
| Statistik | `scb.statistik,skolverket.statistik,fohm,reference` | 28 | 23 655 byte | 6 000 |
| Skolor och skolenheter | `skolverket.skolenhetsregistret,skolverket.planerad,reference` | 31 | 44 730 byte | 11 000 |
| Läroplaner, ämnen och kurser | `skolverket.syllabus,reference` | 21 | 23 020 byte | 6 000 |
| Vuxen- och högre utbildning | `skolverket.susa,skolverket.planerad,reference` | 28 | 42 694 byte | 10 500 |
| Geodata och dataset | `scb.geodata,dataportal,reference` | 15 | 13 473 byte | 3 500 |
| Allt | alla | 81 | 108 992 byte | 27 000 |

## Statistik

`FUZZY_MCP_SOURCES=scb.statistik,skolverket.statistik,fohm,reference`

- `fohm_browse`
- `fohm_build_query`
- `fohm_get_table_data`
- `fohm_get_table_metadata`
- `fohm_list_databases`
- `fohm_search_tables`
- `fuzzy_api_notices`
- `fuzzy_list_sources`
- `ref_entity_catalog`
- `ref_get_code_list`
- `ref_list_code_lists`
- `ref_list_municipalities`
- `ref_lookup_region`
- `scb_browse_subjects`
- `scb_build_query`
- `scb_get_codelist`
- `scb_get_config`
- `scb_get_default_selection`
- `scb_get_table`
- `scb_get_table_data`
- `scb_get_table_metadata`
- `scb_search_tables`
- `skolverket_stat_browse`
- `skolverket_stat_build_query`
- `skolverket_stat_get_table_data`
- `skolverket_stat_get_table_metadata`
- `skolverket_stat_list_databases`
- `skolverket_stat_search_tables`

## Skolor och skolenheter

`FUZZY_MCP_SOURCES=skolverket.skolenhetsregistret,skolverket.planerad,reference`

- `fuzzy_api_notices`
- `fuzzy_list_sources`
- `ref_entity_catalog`
- `ref_get_code_list`
- `ref_list_code_lists`
- `ref_list_municipalities`
- `ref_lookup_region`
- `skolverket_get_contract`
- `skolverket_get_education_provider`
- `skolverket_get_organizer`
- `skolverket_get_school_unit`
- `skolverket_pe_adult_education_areas`
- `skolverket_pe_compare_secondary`
- `skolverket_pe_get_adult_education_event`
- `skolverket_pe_get_school_unit`
- `skolverket_pe_national_statistics`
- `skolverket_pe_salsa`
- `skolverket_pe_school_unit_documents`
- `skolverket_pe_school_unit_education_events`
- `skolverket_pe_school_unit_statistics`
- `skolverket_pe_school_unit_surveys`
- `skolverket_pe_search_adult_education_events`
- `skolverket_pe_search_education_events`
- `skolverket_pe_search_school_units`
- `skolverket_pe_support_list`
- `skolverket_school_unit_api_info`
- `skolverket_school_unit_changes`
- `skolverket_search_contracts`
- `skolverket_search_education_providers`
- `skolverket_search_organizers`
- `skolverket_search_school_units`

## Läroplaner, ämnen och kurser

`FUZZY_MCP_SOURCES=skolverket.syllabus,reference`

- `fuzzy_api_notices`
- `fuzzy_list_sources`
- `ref_entity_catalog`
- `ref_get_code_list`
- `ref_list_code_lists`
- `ref_list_municipalities`
- `ref_lookup_region`
- `skolverket_get_course`
- `skolverket_get_curriculum`
- `skolverket_get_program`
- `skolverket_get_subject`
- `skolverket_list_course_versions`
- `skolverket_list_courses`
- `skolverket_list_curriculum_versions`
- `skolverket_list_curriculums`
- `skolverket_list_program_versions`
- `skolverket_list_programs`
- `skolverket_list_subject_versions`
- `skolverket_list_subjects`
- `skolverket_syllabus_api_info`
- `skolverket_syllabus_valuestore`

## Vuxen- och högre utbildning

`FUZZY_MCP_SOURCES=skolverket.susa,skolverket.planerad,reference`

- `fuzzy_api_notices`
- `fuzzy_list_sources`
- `ref_entity_catalog`
- `ref_get_code_list`
- `ref_list_code_lists`
- `ref_list_municipalities`
- `ref_lookup_region`
- `skolverket_pe_adult_education_areas`
- `skolverket_pe_compare_secondary`
- `skolverket_pe_get_adult_education_event`
- `skolverket_pe_get_school_unit`
- `skolverket_pe_national_statistics`
- `skolverket_pe_salsa`
- `skolverket_pe_school_unit_documents`
- `skolverket_pe_school_unit_education_events`
- `skolverket_pe_school_unit_statistics`
- `skolverket_pe_school_unit_surveys`
- `skolverket_pe_search_adult_education_events`
- `skolverket_pe_search_education_events`
- `skolverket_pe_search_school_units`
- `skolverket_pe_support_list`
- `skolverket_susa_api_info`
- `skolverket_susa_get_education_event`
- `skolverket_susa_get_education_info`
- `skolverket_susa_get_education_provider`
- `skolverket_susa_search_education_events`
- `skolverket_susa_search_education_infos`
- `skolverket_susa_search_education_providers`

## Geodata och dataset

`FUZZY_MCP_SOURCES=scb.geodata,dataportal,reference`

- `dataportal_get_dataset`
- `dataportal_list_publisher_datasets`
- `dataportal_search_datasets`
- `dataportal_theme_codes`
- `fuzzy_api_notices`
- `fuzzy_list_sources`
- `ref_entity_catalog`
- `ref_get_code_list`
- `ref_list_code_lists`
- `ref_list_municipalities`
- `ref_lookup_region`
- `scb_geodata_describe_layer`
- `scb_geodata_download_url`
- `scb_geodata_get_features`
- `scb_geodata_layers`
