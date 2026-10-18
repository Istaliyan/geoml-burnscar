from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from typer.testing import CliRunner

from geoml_platform.catalog import COLLECTION, discover, select_scene
from geoml_platform.cli import app
from geoml_platform.download import download, expected_sha256
from geoml_platform.ingestion import freeze_plan, ingest, json_bytes
from geoml_platform.ingestion_config import STAC_URL, EventConfig
from geoml_platform.provenance import (
    assets,
    connect_database,
    events,
    ingestions,
    initialize_database,
    scenes,
)
from geoml_platform.storage import LocalFsBackend

POLYGON = {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]]}
PAYLOAD = b"synthetic-source-asset"


@pytest.fixture
def event() -> EventConfig:
    return EventConfig(
        event_id="test-fire",
        source_feature_id="123",
        source_downloaded_at="2026-09-11T00:00:00Z",
        fire_start="2022-07-14T00:00:00Z",
        fire_end="2022-07-23T00:00:00Z",
        bbox=(0, 0, 1, 1),
        geometry=POLYGON,
        mgrs_tile="29TPH",
        assets=("B02", "SCL"),
    )


def item(name: str, acquired: str) -> dict[str, Any]:
    return {
        "type": "Feature",
        "stac_version": "1.0.0",
        "id": name,
        "collection": COLLECTION,
        "geometry": POLYGON,
        "bbox": [0, 0, 1, 1],
        "links": [],
        "properties": {"datetime": acquired, "s2:mgrs_tile": "29TPH", "eo:cloud_cover": 5},
        "assets": {
            name: {
                "href": f"https://assets.example/{acquired[:10]}/{name}.tif",
                "file:size": len(PAYLOAD),
                "file:checksum": "1220" + hashlib.sha256(PAYLOAD).hexdigest(),
            }
            for name in ("B02", "SCL")
        },
    }


@pytest.fixture
def store(tmp_path: Path, event: EventConfig) -> LocalFsBackend:
    result = LocalFsBackend(tmp_path / "data")
    result.write_bytes(
        f"raw/ingestion/{event.event_id}/plan.json",
        json_bytes(
            {
                "schema_version": 1,
                "provider": STAC_URL,
                "collection": COLLECTION,
                "event": event.model_dump(mode="json"),
                "queries": {},
                "scenes": {
                    "before": item("before", "2022-07-08T00:00:00Z"),
                    "after": item("after", "2022-08-02T00:00:00Z"),
                },
            }
        ),
    )
    return result


@pytest.fixture
def engine() -> Any:
    result = connect_database("sqlite://")
    initialize_database(result)
    yield result
    result.dispose()


def roundtrip(event: EventConfig, store: LocalFsBackend, engine: Engine) -> None:
    requests: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(200, content=PAYLOAD)

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        first = ingest(event, store, engine, client)
        second = ingest(event, store, engine, client)
    assert first == second
    assert len(requests) == 4  # Second run performs neither discovery nor downloads.
    manifest = json.loads(store.path(first.key).read_bytes())
    for obj in manifest["objects"]:
        assert hashlib.sha256(store.path(obj["key"]).read_bytes()).hexdigest() == obj["sha256"]
    assert manifest["roles"] == {"before": f"{COLLECTION}/before", "after": f"{COLLECTION}/after"}
    with engine.connect() as connection:
        assert [
            connection.scalar(select(func.count()).select_from(table))
            for table in (events, scenes, assets, ingestions)
        ] == [1, 2, 4, 1]


def test_ingestion_is_idempotent(event: EventConfig, store: LocalFsBackend, engine: Engine) -> None:
    roundtrip(event, store, engine)


def test_postgis_roundtrip(event: EventConfig, store: LocalFsBackend) -> None:
    url = os.environ.get("GEOML_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set GEOML_TEST_DATABASE_URL to a disposable empty PostGIS database")
    engine = connect_database(url)
    try:
        initialize_database(engine)
        initialize_database(engine)  # Schema setup is also idempotent.
        roundtrip(event, store, engine)
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT ST_SRID(geometry) FROM event_geometries")) == 4326
            assert connection.scalar(text("SELECT ST_Area(geometry) FROM event_geometries")) == 1
        with pytest.raises(IntegrityError), engine.begin() as connection:
            connection.execute(
                assets.insert().values(
                    key="raw/invalid",
                    scene_key="missing-scene",
                    sha256="0" * 64,
                    size_bytes=1,
                    record={},
                )
            )
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    "key", ["", "/raw/x", "../x", "raw/../x", "raw/./x", "a//b", "C:/x", "a\\b", "//server/share"]
)
def test_storage_rejects_unsafe_keys(tmp_path: Path, key: str) -> None:
    with pytest.raises(ValueError):
        LocalFsBackend(tmp_path).path(key)


def test_storage_integrity_and_immutable_write(tmp_path: Path) -> None:
    store = LocalFsBackend(tmp_path / "data")
    original = store.write_bytes("raw/x", b"original")
    assert store.write_bytes("raw/x", b"original") == original
    with pytest.raises(ValueError, match="different content"):
        store.write_bytes("raw/x", b"changed")
    source = tmp_path / "source"
    source.write_bytes(b"wrong")
    with pytest.raises(ValueError, match="Checksum mismatch"):
        store.write_from_path_atomic("raw/new", source, "0" * 64)
    assert not store.exists("raw/new")
    assert store.path("raw/x").read_bytes() == b"original"
    assert not list(store.root.rglob("*.part"))


def test_scene_selection_filters_and_orders(event: EventConfig) -> None:
    old = item("old", "2022-07-01T00:00:00Z")
    nearest = item("nearest", "2022-07-08T00:00:00Z")
    cloudy = item("cloudy", "2022-07-13T00:00:00Z")
    cloudy["properties"]["eo:cloud_cover"] = 99
    wrong_tile = copy.deepcopy(cloudy)
    wrong_tile["properties"].update({"s2:mgrs_tile": "30TXK", "eo:cloud_cover": 0})
    assert select_scene([cloudy, old, wrong_tile, nearest], event, "before")["id"] == "nearest"
    assert select_scene([nearest, old], event, "before")["id"] == "nearest"
    with pytest.raises(ValueError, match="No compatible"):
        select_scene([cloudy, wrong_tile], event, "before")
    assert (
        select_scene(
            [item("a", "2022-07-24T00:00:00Z"), item("b", "2022-08-02T00:00:00Z")], event, "after"
        )["id"]
        == "a"
    )
    clipped = copy.deepcopy(nearest)
    clipped["geometry"] = {
        "type": "Polygon",
        "coordinates": [[[0, 0], [0.5, 0], [0.5, 0.5], [0, 0.5], [0, 0]]],
    }
    with pytest.raises(ValueError, match="No compatible"):
        select_scene([clipped], event, "before")


def test_discovery_records_queries_and_selected_items(event: EventConfig, monkeypatch: Any) -> None:
    calls: list[dict[str, Any]] = []

    class Catalog:
        def search(self, **kwargs: Any) -> Any:
            calls.append(kwargs)
            return self

        def items_as_dicts(self) -> list[dict[str, Any]]:
            return [item("before", "2022-07-08T00:00:00Z"), item("after", "2022-08-02T00:00:00Z")]

    monkeypatch.setattr("geoml_platform.catalog.Client.open", lambda *a, **k: Catalog())
    plan = discover(event)
    assert len(calls) == 2
    assert calls[0]["query"]["s2:mgrs_tile"] == {"eq": "29TPH"}
    assert plan["scenes"]["after"]["id"] == "after"
    assert plan["queries"]["before"]["datetime"].endswith("2022-07-14T00:00:00+00:00")


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(503),
        httpx.Response(200, content=b"truncated"),
        httpx.Response(200, content=b"x" * len(PAYLOAD)),
    ],
)
def test_download_retries_and_cleans_partial_files(
    tmp_path: Path, response: httpx.Response
) -> None:
    calls = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return copy.copy(response)

    path = tmp_path / "download.part"
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(ValueError, match="3 attempt"):
            download(
                client,
                "https://assets.example/source",
                path,
                expected_size=len(PAYLOAD),
                expected_hash=hashlib.sha256(PAYLOAD).hexdigest(),
                retry_delay=0,
            )
    assert calls == 3
    assert not path.exists()


def test_download_refreshes_signing_and_recovers(tmp_path: Path) -> None:
    calls: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.host == "planetarycomputer.microsoft.com":
            return httpx.Response(
                200, json={"href": "https://test.blob.core.windows.net/a?sig=secret"}
            )
        return httpx.Response(403) if len(calls) == 2 else httpx.Response(200, content=PAYLOAD)

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        digest, size = download(
            client, "https://test.blob.core.windows.net/a", tmp_path / "asset", retry_delay=0
        )
    assert size == len(PAYLOAD)
    assert digest == hashlib.sha256(PAYLOAD).hexdigest()
    assert len(calls) == 4


def test_no_completed_ingestion_after_failure_and_retry_reuses_assets(
    event: EventConfig,
    store: LocalFsBackend,
    engine: Engine,
) -> None:
    calls = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, content=PAYLOAD) if calls == 1 else httpx.Response(404)

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(ValueError, match="download failed"):
            ingest(event, store, engine, client)
    assert not store.exists(f"raw/ingestion/{event.event_id}/manifest.json")
    with engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(ingestions)) == 0
    resumed: list[str] = []

    def succeed(request: httpx.Request) -> httpx.Response:
        resumed.append(str(request.url))
        return httpx.Response(200, content=PAYLOAD)

    with httpx.Client(transport=httpx.MockTransport(succeed)) as client:
        ingest(event, store, engine, client)
    assert len(resumed) == 3


def test_corruption_is_rejected(event: EventConfig, store: LocalFsBackend, engine: Engine) -> None:
    with httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, content=PAYLOAD))
    ) as c:
        result = ingest(event, store, engine, c)
        manifest = json.loads(store.path(result.key).read_bytes())
        store.path(manifest["assets"][0]["key"]).write_bytes(b"x" * len(PAYLOAD))
        with pytest.raises(ValueError, match="Checksum mismatch"):
            ingest(event, store, engine, c)


def test_database_failure_can_be_recovered_without_downloads(
    event: EventConfig,
    store: LocalFsBackend,
    engine: Engine,
    monkeypatch: Any,
) -> None:
    from geoml_platform.provenance import register

    def fail(*args: Any) -> None:
        raise RuntimeError("database unavailable")

    monkeypatch.setattr("geoml_platform.ingestion.register", fail)
    with httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, content=PAYLOAD))
    ) as c:
        with pytest.raises(RuntimeError, match="unavailable"):
            ingest(event, store, engine, c)
    monkeypatch.setattr("geoml_platform.ingestion.register", register)

    def unexpected(request: httpx.Request) -> httpx.Response:
        pytest.fail("Recovery should reuse all local assets")

    with httpx.Client(transport=httpx.MockTransport(unexpected)) as client:
        ingest(event, store, engine, client)


def test_configuration_changes_require_new_event(event: EventConfig, store: LocalFsBackend) -> None:
    changed = event.model_copy(update={"max_cloud_cover": 10})
    with pytest.raises(ValueError, match="configuration changed"):
        freeze_plan(changed, store)


def test_database_conflicts_roll_back_transaction(
    event: EventConfig,
    store: LocalFsBackend,
    engine: Engine,
) -> None:
    from geoml_platform.provenance import register

    with httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, content=PAYLOAD))
    ) as c:
        result = ingest(event, store, engine, c)
    manifest = json.loads(store.path(result.key).read_bytes())
    manifest["plan"]["event"]["event_id"] = "another-event"
    manifest["assets"][0]["sha256"] = "0" * 64
    with pytest.raises(ValueError, match="Conflicting provenance"):
        register(engine, manifest, "raw/new-manifest.json", "0" * 64)
    with engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(events)) == 1


@pytest.mark.parametrize(
    "update",
    [
        {"event_id": "../unsafe"},
        {"fire_end": "2022-07-01T00:00:00Z"},
        {"bbox": (1, 0, 0, 1)},
        {"geometry": {"type": "Point", "coordinates": [0, 0]}},
        {"assets": []},
        {"mgrs_tile": "bad"},
        {"fire_start": "2022-07-14T00:00:00"},
    ],
)
def test_invalid_event_is_rejected(event: EventConfig, update: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        EventConfig.model_validate(event.model_dump(mode="json") | update)


def test_checksum_metadata_is_not_silently_ignored() -> None:
    with pytest.raises(ValueError, match="Only SHA-256"):
        expected_sha256("unsupported")


def test_cli_discover_reuses_frozen_plan(
    event: EventConfig,
    store: LocalFsBackend,
    tmp_path: Path,
) -> None:
    config = tmp_path / "event.json"
    config.write_text(event.model_dump_json(), encoding="utf-8")
    result = CliRunner().invoke(
        app, ["discover", "--event", str(config), "--data-root", str(store.root)]
    )
    assert result.exit_code == 0, result.output
    assert "plan.json" in result.output


def test_effis_import_preserves_source_and_dates(tmp_path: Path) -> None:
    from geoml_platform.labels import import_effis

    properties = {
        "id": "206702",
        "firedate": "2022-07-14 22:29:00",
        "finaldate": "2022-07-23 12:46:00",
        "country": "ES",
    }
    source = tmp_path / "perimeter.geojson"
    source.write_text(
        json.dumps(
            {
                "type": "FeatureCollection",
                "features": [{"type": "Feature", "properties": properties, "geometry": POLYGON}],
            }
        ),
        encoding="utf-8",
    )
    event = import_effis(source, "29TPH", "2026-09-11T00:00:00Z")
    assert event.event_id == "effis-2022-206702"
    assert event.source_properties == properties
    assert event.fire_start.isoformat() == "2022-07-14T22:29:00+00:00"
    assert event.source_export_sha256 == hashlib.sha256(source.read_bytes()).hexdigest()
    output = tmp_path / "event.json"
    result = CliRunner().invoke(
        app,
        [
            "import-effis",
            "--perimeter",
            str(source),
            "--mgrs-tile",
            "29TPH",
            "--source-downloaded-at",
            "2026-09-11T00:00:00Z",
            "--output",
            str(output),
        ],
    )
    assert result.exit_code == 0, result.output
    assert EventConfig.model_validate_json(output.read_bytes()) == event


def test_missing_asset_is_restored_from_recorded_receipt(
    event: EventConfig,
    store: LocalFsBackend,
    engine: Engine,
) -> None:
    requests: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(200, content=PAYLOAD)

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        result = ingest(event, store, engine, client)
        manifest = json.loads(store.path(result.key).read_bytes())
        store.path(manifest["assets"][0]["key"]).unlink()
        assert ingest(event, store, engine, client) == result
    assert len(requests) == 5


def test_cli_ingest_registers_provenance(
    event: EventConfig,
    store: LocalFsBackend,
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    database_path = tmp_path / "metadata.sqlite"
    database_url = f"sqlite:///{database_path.as_posix()}"
    monkeypatch.setenv("GEOML_DATABASE_URL", database_url)
    monkeypatch.setenv("GEOML_DATA_ROOT", str(store.root))
    config = tmp_path / "event.json"
    config.write_text(event.model_dump_json(), encoding="utf-8")
    engine = connect_database(database_url)
    try:
        runner = CliRunner()
        assert runner.invoke(app, ["db-init"]).exit_code == 0
        real_client = httpx.Client
        monkeypatch.setattr(
            "httpx.Client",
            lambda **kwargs: real_client(
                transport=httpx.MockTransport(lambda r: httpx.Response(200, content=PAYLOAD))
            ),
        )
        for _ in range(2):
            result = runner.invoke(app, ["ingest", "--event", str(config)])
            assert result.exit_code == 0, result.exception
            assert "manifest.json" in result.output
        with engine.connect() as connection:
            assert connection.scalar(select(func.count()).select_from(ingestions)) == 1
    finally:
        engine.dispose()


def test_data_root_can_be_relocated(
    event: EventConfig,
    store: LocalFsBackend,
    engine: Engine,
    tmp_path: Path,
) -> None:
    import shutil

    with httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, content=PAYLOAD))
    ) as c:
        original = ingest(event, store, engine, c)
    relocated = LocalFsBackend(tmp_path / "relocated")
    shutil.copytree(store.root, relocated.root)

    def unexpected(request: httpx.Request) -> httpx.Response:
        pytest.fail("Relocation should not require network access")

    with httpx.Client(transport=httpx.MockTransport(unexpected)) as client:
        assert ingest(event, relocated, engine, client) == original
