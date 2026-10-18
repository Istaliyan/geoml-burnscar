"""Transactional ingestion metadata; PostgreSQL in production, SQLite in unit tests."""

from __future__ import annotations

from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Column,
    ForeignKey,
    MetaData,
    String,
    Table,
    create_engine,
    select,
    text,
)
from sqlalchemy.engine import Connection, Engine

metadata = MetaData()
events = Table(
    "events",
    metadata,
    Column("event_id", String(200), primary_key=True),
    Column("record", JSON, nullable=False),
)
scenes = Table(
    "scenes",
    metadata,
    Column("scene_key", String(500), primary_key=True),
    Column("record", JSON, nullable=False),
)
assets = Table(
    "assets",
    metadata,
    Column("key", String(1000), primary_key=True),
    Column("scene_key", ForeignKey("scenes.scene_key"), nullable=False),
    Column("sha256", String(64), nullable=False),
    Column("size_bytes", BigInteger, nullable=False),
    Column("record", JSON, nullable=False),
)
ingestions = Table(
    "ingestions",
    metadata,
    Column("event_id", ForeignKey("events.event_id"), primary_key=True),
    Column("manifest_key", String(1000), nullable=False),
    Column("manifest_sha256", String(64), nullable=False),
    Column("record", JSON, nullable=False),
)
SCHEMA_VERSION = 1


def connect_database(url: str) -> Engine:
    if not url.startswith(("postgresql+psycopg://", "sqlite://")):
        raise ValueError("Use a postgresql+psycopg database URL")
    return create_engine(url)


def initialize_database(engine: Engine) -> None:
    with engine.begin() as connection:
        if engine.dialect.name == "postgresql":
            connection.execute(text("SELECT pg_advisory_xact_lock(735001)"))
            connection.execute(text("CREATE EXTENSION IF NOT EXISTS postgis"))
        connection.execute(
            text("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY)")
        )
        version = connection.execute(text("SELECT version FROM schema_version")).scalar()
        if version not in (None, SCHEMA_VERSION):
            raise ValueError(f"Unsupported database schema version: {version}")
        metadata.create_all(connection)
        if engine.dialect.name == "postgresql":
            connection.execute(
                text("""
                CREATE OR REPLACE VIEW event_geometries AS
                SELECT event_id,
                  ST_SetSRID(ST_GeomFromGeoJSON(record->'geometry'), 4326) AS geometry
                FROM events
            """)
            )
        if version is None:
            connection.execute(text("INSERT INTO schema_version (version) VALUES (1)"))


def check_schema(engine: Engine) -> None:
    """Fail before downloading assets when the database has not been initialized."""
    with engine.connect() as connection:
        version = connection.execute(text("SELECT version FROM schema_version")).scalar()
        if version != SCHEMA_VERSION:
            raise ValueError("Metadata schema is not ready; run geoml db-init")


def _insert_immutable(connection: Connection, table: Table, values: dict[str, Any]) -> None:
    primary = next(iter(table.primary_key.columns))
    existing = (
        connection.execute(select(table).where(primary == values[primary.name])).mappings().first()
    )
    if existing is None:
        connection.execute(table.insert().values(**values))
    elif dict(existing) != values:
        raise ValueError(f"Conflicting provenance in {table.name}; use a new event ID or data root")


def register(engine: Engine, manifest: dict[str, Any], key: str, digest: str) -> None:
    """Commit all rows together, only after artifact verification has succeeded."""
    with engine.begin() as connection:
        if engine.dialect.name == "postgresql":
            # The MVP is a single-writer pipeline; serialize registrations including shared scenes.
            connection.execute(text("SELECT pg_advisory_xact_lock(735002)"))
        event = manifest["plan"]["event"]
        _insert_immutable(connection, events, {"event_id": event["event_id"], "record": event})
        for scene in manifest["scenes"]:
            _insert_immutable(
                connection,
                scenes,
                {
                    "scene_key": scene["scene_key"],
                    "record": scene["record"],
                },
            )
        for asset in manifest["assets"]:
            _insert_immutable(
                connection,
                assets,
                {
                    "key": asset["key"],
                    "scene_key": asset["scene_key"],
                    "sha256": asset["sha256"],
                    "size_bytes": asset["size_bytes"],
                    "record": asset["record"],
                },
            )
        _insert_immutable(
            connection,
            ingestions,
            {
                "event_id": event["event_id"],
                "manifest_key": key,
                "manifest_sha256": digest,
                "record": manifest,
            },
        )
