# SPDX-FileCopyrightText: 2026 Deniz Özer
#
# SPDX-License-Identifier: MIT

import pytest
from mcp.types import PromptReference, ResourceTemplateReference

pytestmark = pytest.mark.anyio


async def test_prompts_listed_and_rendered(make_client):
    async with make_client("reference") as client:
        names = {p.name for p in (await client.list_prompts()).prompts}
        assert {"analysera_kommun", "jamfor_kommuner", "hitta_statistik", "power_bi_fraga", "skolenhet_profil"} <= names
        prompt = await client.get_prompt("analysera_kommun", {"kommun": "Gävle"})
        text = prompt.messages[0].content.text
        assert "Gävle" in text and "ref_lookup_region" in text and "scb_get_table_data" in text


async def test_completion_for_region_and_code_lists(make_client):
    async with make_client("reference") as client:
        result = await client.complete(
            PromptReference(name="analysera_kommun"), argument={"name": "kommun", "value": "göt"}
        )
        assert "Göteborg" in result.completion.values
        result = await client.complete(
            PromptReference(name="analysera_kommun"), argument={"name": "kommun", "value": ""}
        )
        assert len(result.completion.values) == 100 and result.completion.has_more
        result = await client.complete(
            PromptReference(name="jamfor_kommuner"), argument={"name": "kommuner", "value": "Gävle, Sand"}
        )
        assert "Gävle, Sandviken" in result.completion.values
        result = await client.complete(
            ResourceTemplateReference(uri="fuzzy://codes/{name}"), argument={"name": "name", "value": "reg"}
        )
        assert result.completion.values == ["regioner"]
