"""Command-line entry point for the local vertical slice."""

from pathlib import Path
from typing import Annotated

import typer

from geoml_platform.config import SamplePipelineConfig
from geoml_platform.logging import configure_logging
from geoml_platform.pipeline import run_sample

app = typer.Typer(no_args_is_help=True, help="Run the GeoML one-sample vertical slice.")


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
