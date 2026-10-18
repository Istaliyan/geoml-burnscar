"""Recoverable ingestion from a frozen scene selection to files and database rows."""

from __future__ import annotations

import hashlib
import json
import tempfile
from dataclasses import asdict
from pathlib import Path
from typing import Any

import httpx
import structlog
from sqlalchemy.engine import Engine

from geoml_platform.catalog import COLLECTION, discover, select_scene
from geoml_platform.download import download, expected_sha256
from geoml_platform.ingestion_config import STAC_URL, EventConfig
from geoml_platform.provenance import check_schema, register
from geoml_platform.storage import LocalFsBackend, StoredObject

logger = structlog.get_logger()


def json_bytes(value: Any) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n").encode()


def read_json(store: LocalFsBackend, key: str) -> dict[str, Any]:
    with store.open_read(key) as stream:
        result: dict[str, Any] = json.load(stream)
        return result


def freeze_plan(event: EventConfig, store: LocalFsBackend) -> tuple[str, dict[str, Any]]:
    key = f"raw/ingestion/{event.event_id}/plan.json"
    if store.exists(key):
        plan = read_json(store, key)
        if plan["event"] != event.model_dump(mode="json"):
            raise ValueError("Event configuration changed; use a new event ID or data root")
    else:
        plan = discover(event)
        store.write_bytes(key, json_bytes(plan))
    if (
        plan["provider"] != STAC_URL
        or plan["collection"] != COLLECTION
        or plan["schema_version"] != 1
    ):
        raise ValueError("Unsupported ingestion plan")
    for role in ("before", "after"):
        select_scene([plan["scenes"][role]], event, role)
    return key, plan


def ingest(
    event: EventConfig, store: LocalFsBackend, engine: Engine, client: httpx.Client
) -> StoredObject:
    check_schema(engine)
    plan_key, plan = freeze_plan(event, store)
    objects: list[dict[str, Any]] = []
    plan_object = store.write_bytes(plan_key, json_bytes(plan))
    objects.append(asdict(plan_object))
    label = {
        "type": "Feature",
        "id": event.source_feature_id,
        "geometry": event.geometry,
        "properties": event.model_dump(mode="json", exclude={"geometry"}),
    }
    objects.append(
        asdict(
            store.write_bytes(f"raw/labels/{event.event_id}/perimeter.geojson", json_bytes(label))
        )
    )
    scenes: list[dict[str, Any]] = []
    assets: list[dict[str, Any]] = []
    roles: dict[str, str] = {}
    for role in ("before", "after"):
        item = plan["scenes"][role]
        scene_key = f"{COLLECTION}/{item['id']}"
        roles[role] = scene_key
        item_key = f"raw/stac-items/{scene_key}.json"
        item_object = store.write_bytes(item_key, json_bytes(item))
        objects.append(asdict(item_object))
        scenes.append(
            {
                "scene_key": scene_key,
                "record": {
                    "provider": STAC_URL,
                    "collection": COLLECTION,
                    "item_id": item["id"],
                    "datetime": item["properties"]["datetime"],
                    "cloud_cover": item["properties"]["eo:cloud_cover"],
                    "stac_key": item_key,
                    "stac_sha256": item_object.sha256,
                    "attribution": "Contains Copernicus Sentinel data "
                    + item["properties"]["datetime"][:4],
                },
            }
        )
        for name in event.assets:
            source = item["assets"][name]
            key = f"raw/imagery/{scene_key}/{name}.tif"
            receipt_key = f"raw/receipts/{scene_key}/{name}.json"
            source_record = {
                "asset": name,
                "href": source["href"],
                "file:size": source.get("file:size"),
                "file:checksum": source.get("file:checksum"),
            }
            expected_hash = expected_sha256(source.get("file:checksum"))
            receipt = read_json(store, receipt_key) if store.exists(receipt_key) else None
            if receipt is not None and receipt["source"] != source_record:
                raise ValueError(f"Asset provenance changed: {key}")
            if receipt is not None:
                # A missing file must be restored to its originally recorded bytes even
                # when the provider does not publish file:checksum or file:size.
                expected_hash = receipt["object"]["sha256"]
                expected_size = receipt["object"]["size_bytes"]
            else:
                expected_size = source.get("file:size")
            if receipt is not None and store.exists(key):
                obj = StoredObject(**receipt["object"])
                if obj.key != key:
                    raise ValueError("Receipt key does not match asset")
                store.verify(obj)
                logger.info("asset_reused", key=key, sha256=obj.sha256)
            else:
                with tempfile.TemporaryDirectory() as directory:
                    temporary = Path(directory) / "download"
                    digest, _ = download(
                        client,
                        source["href"],
                        temporary,
                        expected_size=expected_size,
                        expected_hash=expected_hash,
                    )
                    obj = store.write_from_path_atomic(key, temporary, digest)
                receipt = {"object": asdict(obj), "source": source_record}
                logger.info(
                    "asset_downloaded", key=key, size_bytes=obj.size_bytes, sha256=obj.sha256
                )
            receipt_object = store.write_bytes(receipt_key, json_bytes(receipt))
            objects.extend([asdict(obj), asdict(receipt_object)])
            assets.append({**asdict(obj), "scene_key": scene_key, "record": source_record})
    manifest = {
        "schema_version": 1,
        "plan": plan,
        "scenes": scenes,
        "roles": roles,
        "assets": assets,
        "objects": objects,
    }
    key = f"raw/ingestion/{event.event_id}/manifest.json"
    content = json_bytes(manifest)
    # Artifact commit precedes the DB transaction; a retry repairs a failed DB commit.
    obj = store.write_bytes(key, content)
    register(engine, manifest, key, hashlib.sha256(content).hexdigest())
    logger.info(
        "ingestion_complete",
        event_id=event.event_id,
        manifest_key=key,
        asset_count=len(assets),
        manifest_sha256=obj.sha256,
    )
    return obj
