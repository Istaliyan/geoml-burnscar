"""Bounded STAC searches and deterministic, single-tile scene selection."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pystac_client import Client
from pystac_client.stac_api_io import StacApiIO
from shapely.geometry import shape

from geoml_platform.ingestion_config import STAC_URL, EventConfig, identifier

COLLECTION = "sentinel-2-l2a"


def select_scene(items: list[dict[str, Any]], event: EventConfig, role: str) -> dict[str, Any]:
    start, end = event.interval(role)
    candidates: list[tuple[datetime, float, str, dict[str, Any]]] = []
    for item in items:
        properties = item["properties"]
        acquired = datetime.fromisoformat(properties["datetime"].replace("Z", "+00:00"))
        cloud = float(properties.get("eo:cloud_cover", 101))
        tile = properties.get("s2:mgrs_tile")
        if tile is None:
            tile = "".join(
                str(properties.get(f"mgrs:{part}", ""))
                for part in ("utm_zone", "latitude_band", "grid_square")
            )
        if (
            item.get("collection") != COLLECTION
            or tile != event.mgrs_tile
            or not start <= acquired <= end
            or (role == "before" and acquired >= event.fire_start)
            or (role == "after" and acquired <= event.fire_end)
            or not 0 <= cloud <= event.max_cloud_cover
            or not all(asset in item.get("assets", {}) for asset in event.assets)
            or not shape(item["geometry"]).covers(shape(event.geometry))
        ):
            continue
        identifier(item["id"])
        candidates.append((acquired, cloud, item["id"], item))
    if not candidates:
        raise ValueError(f"No compatible {role} scene for tile {event.mgrs_tile}")
    # Nearest acquisition to the fire interval; cloud and ID break ties predictably.
    candidates.sort(
        key=lambda c: (-c[0].timestamp() if role == "before" else c[0].timestamp(), c[1], c[2])
    )
    return candidates[0][3]


def discover(event: EventConfig) -> dict[str, Any]:
    catalog = Client.open(STAC_URL, stac_io=StacApiIO(timeout=60, max_retries=3))
    scenes: dict[str, Any] = {}
    queries: dict[str, Any] = {}
    for role in ("before", "after"):
        start, end = event.interval(role)
        query = {
            "eo:cloud_cover": {"lte": event.max_cloud_cover},
            "s2:mgrs_tile": {"eq": event.mgrs_tile},
        }
        interval = f"{start.isoformat()}/{end.isoformat()}"
        queries[role] = {"bbox": list(event.bbox), "datetime": interval, "query": query}
        # Do not cap the complete result set: pagination must not change selection.
        items = list(
            catalog.search(
                collections=[COLLECTION], bbox=event.bbox, datetime=interval, query=query
            ).items_as_dicts()
        )
        scenes[role] = select_scene(items, event, role)
    return {
        "schema_version": 1,
        "provider": STAC_URL,
        "collection": COLLECTION,
        "event": event.model_dump(mode="json"),
        "queries": queries,
        "scenes": scenes,
    }
