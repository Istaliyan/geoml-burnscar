"""Command-line entry point for the local vertical slice."""

import os
from pathlib import Path
from typing import Annotated

import typer

from geoml_platform.config import SamplePipelineConfig
from geoml_platform.logging import configure_logging
from geoml_platform.pipeline import run_sample

app = typer.Typer(
    no_args_is_help=True,
    pretty_exceptions_show_locals=False,
    help="Ingest source data and run local GeoML pipelines.",
)


@app.callback()
def main() -> None:
    """Run reproducible GeoML pipeline commands."""


@app.command()
def sample(
    before: Annotated[Path, typer.Option(help="Aligned six-band pre-event GeoTIFF.")],
    after: Annotated[Path, typer.Option(help="Aligned six-band post-event GeoTIFF.")],
    label: Annotated[Path, typer.Option(help="Aligned single-band label GeoTIFF.")],
    output_dir: Annotated[
        Path, typer.Option(help="Directory for chips, prediction, and manifest.")
    ],
    chip_size: Annotated[int, typer.Option(help="Square chip size in pixels.")] = 256,
) -> None:
    """Run ingestion, contract validation, chip creation, and dummy inference."""
    configure_logging()
    manifest = run_sample(
        SamplePipelineConfig(
            before=before, after=after, label=label, output_dir=output_dir, chip_size=chip_size
        )
    )
    typer.echo(manifest)


@app.command("db-init")
def db_init() -> None:
    """Create the versioned metadata schema using GEOML_DATABASE_URL."""
    from geoml_platform.provenance import connect_database, initialize_database

    engine = connect_database(_database_url())
    try:
        initialize_database(engine)
        typer.echo("Metadata schema version 1 ready")
    finally:
        engine.dispose()


def _database_url() -> str:
    value = os.environ.get("GEOML_DATABASE_URL")
    if not value:
        raise typer.BadParameter("Set GEOML_DATABASE_URL (postgresql+psycopg://...)")
    return value


@app.command("import-effis")
def import_effis_command(
    perimeter: Annotated[Path, typer.Option(exists=True, dir_okay=False)],
    mgrs_tile: Annotated[str, typer.Option(help="Single covering Sentinel tile, e.g. 29TPH.")],
    source_downloaded_at: Annotated[str, typer.Option(help="EFFIS download timestamp in UTC.")],
    output: Annotated[Path, typer.Option(help="Output event configuration JSON.")],
) -> None:
    """Prepare an event configuration from a single WGS84 EFFIS GeoJSON feature."""
    from geoml_platform.ingestion import json_bytes
    from geoml_platform.labels import import_effis

    event = import_effis(perimeter, mgrs_tile, source_downloaded_at)
    content = json_bytes(event.model_dump(mode="json"))
    if output.exists() and output.read_bytes() != content:
        raise typer.BadParameter("Output already contains a different event configuration")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(content)
    typer.echo(output)


@app.command("discover")
def discover_command(
    event: Annotated[
        Path, typer.Option(exists=True, dir_okay=False, help="Event JSON configuration.")
    ],
    data_root: Annotated[Path, typer.Option(envvar="GEOML_DATA_ROOT")] = Path("data"),
) -> None:
    """Search STAC and freeze scene selection without downloading imagery."""
    from geoml_platform.ingestion import freeze_plan
    from geoml_platform.ingestion_config import EventConfig
    from geoml_platform.storage import LocalFsBackend

    config = EventConfig.model_validate_json(event.read_text(encoding="utf-8"))
    key, _ = freeze_plan(config, LocalFsBackend(data_root))
    typer.echo(key)


@app.command("ingest")
def ingest_command(
    event: Annotated[
        Path, typer.Option(exists=True, dir_okay=False, help="Event JSON configuration.")
    ],
    data_root: Annotated[Path, typer.Option(envvar="GEOML_DATA_ROOT")] = Path("data"),
) -> None:
    """Download selected assets, verify checksums, and register provenance."""
    import httpx

    from geoml_platform.ingestion import ingest
    from geoml_platform.ingestion_config import EventConfig
    from geoml_platform.provenance import connect_database
    from geoml_platform.storage import LocalFsBackend

    configure_logging()
    config = EventConfig.model_validate_json(event.read_text(encoding="utf-8"))
    engine = connect_database(_database_url())
    try:
        with httpx.Client(timeout=60, follow_redirects=True) as client:
            obj = ingest(config, LocalFsBackend(data_root), engine, client)
        typer.echo(obj.key)
    finally:
        engine.dispose()
