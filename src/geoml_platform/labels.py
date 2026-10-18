"""Import one exported EFFIS feature without changing its perimeter or attributes."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from shapely.geometry import shape

from geoml_platform.ingestion_config import EventConfig
from geoml_platform.storage import sha256_file


def import_effis(perimeter: Path, mgrs_tile: str, downloaded_at: str) -> EventConfig:
    document = json.loads(perimeter.read_text(encoding="utf-8"))
    if document.get("type") == "FeatureCollection":
        features = document["features"]
        if len(features) != 1:
            raise ValueError("Export exactly one EFFIS feature per event")
        feature = features[0]
    elif document.get("type") == "Feature":
        feature = document
    else:
        raise ValueError("Expected an EFFIS GeoJSON Feature or FeatureCollection")
    if "crs" in document and document["crs"].get("properties", {}).get("name") not in {
        "urn:ogc:def:crs:OGC:1.3:CRS84",
        "EPSG:4326",
        "urn:ogc:def:crs:EPSG::4326",
    }:
        raise ValueError("Export the perimeter in WGS84 (EPSG:4326)")
    properties = feature["properties"]
    fields = {key.lower(): value for key, value in properties.items()}

    def effis_date(name: str) -> datetime:
        value = datetime.fromisoformat(fields[name])
        # EFFIS exports these fields as UTC strings without an offset.
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value

    start, end = effis_date("firedate"), effis_date("finaldate")
    feature_id = str(fields["id"])
    return EventConfig.model_validate(
        {
            "event_id": f"effis-{start.year}-{feature_id}",
            "source_feature_id": feature_id,
            "source_downloaded_at": downloaded_at,
            "source_properties": properties,
            "source_export_sha256": sha256_file(perimeter),
            "fire_start": start,
            "fire_end": end,
            "bbox": shape(feature["geometry"]).bounds,
            "geometry": feature["geometry"],
            "mgrs_tile": mgrs_tile,
        }
    )
