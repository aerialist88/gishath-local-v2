"""
engine_client.py — async httpx wrapper for the local gishath-engine HTTP server.

Engine contract (api/cmd/serve/main.go):
    GET /api/search?s=<urlencoded card name>[&lgs=<CSV store names>]
    200 → {"data": [Card, ...], "errors": [StoreError, ...]}
    400 → s missing or < 3 chars

Card fields:  name, url, img, price, inStock, isFoil, src, quality, extraInfo
StoreError:   store, error
"""
from __future__ import annotations

import asyncio
import os

import httpx

ENGINE_PORT: str = os.environ.get("GISHATH_ENGINE_PORT", "8080")
ENGINE_BASE: str = f"http://127.0.0.1:{ENGINE_PORT}"

# Engine per-site cap: 20 s. Controller floor: 1 s. We add a generous buffer.
SEARCH_TIMEOUT: float = 35.0


async def search_one(
    client: httpx.AsyncClient,
    card_name: str,
    stores: list[str] | None = None,
) -> tuple[list[dict], list[dict]]:
    """Search for a single card name.

    Returns:
        (cards, store_errors) — both lists of dicts matching the engine's JSON shapes.
        On any error, returns ([], [{"store": "engine", "error": "<msg>"}]).
    """
    params: dict[str, str] = {"s": card_name}
    if stores:
        params["lgs"] = ",".join(stores)

    try:
        resp = await client.get(
            f"{ENGINE_BASE}/api/search",
            params=params,
            timeout=SEARCH_TIMEOUT,
        )
        resp.raise_for_status()
        body = resp.json()
        return body.get("data", []), body.get("errors", [])
    except httpx.HTTPStatusError as exc:
        snippet = exc.response.text[:120]
        return [], [{"store": "engine", "error": f"HTTP {exc.response.status_code}: {snippet}"}]
    except Exception as exc:  # network error, timeout, JSON parse failure
        return [], [{"store": "engine", "error": str(exc)}]


async def search_many(
    card_names: list[str],
    stores: list[str] | None = None,
) -> dict[str, dict]:
    """Search for multiple card names concurrently via asyncio.gather.

    Returns:
        {card_name: {"cards": [...], "errors": [...]}}
        Preserves the order of card_names.
    """
    names = [n for n in card_names if n.strip()]
    if not names:
        return {}

    async with httpx.AsyncClient() as client:
        tasks = [search_one(client, name, stores) for name in names]
        gathered: list[tuple[list, list]] = await asyncio.gather(*tasks)

    return {
        name: {"cards": cards, "errors": errors}
        for name, (cards, errors) in zip(names, gathered)
    }
