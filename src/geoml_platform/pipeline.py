"""A deterministic, local one-sample geospatial ML vertical slice."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import rasterio
import structlog
from rasterio.windows import Window

from geoml_platform.config import SamplePipelineConfig

LOGGER = structlog.get_logger()


@dataclass(frozen=True)
class RasterContract:
    """The shared grid expected from the feasibility alignment process."""

    width: int
    height: int
    count: int
    crs: str | None
    transform: tuple[float, ...]


def _contract(dataset: rasterio.DatasetReader) -> RasterContract:
    return RasterContract(
        width=dataset.width,
        height=dataset.height,
        count=dataset.count,
        crs=str(dataset.crs) if dataset.crs else None,
        transform=tuple(dataset.transform),
    )


def _validate_contract(
    before: rasterio.DatasetReader,
    after: rasterio.DatasetReader,
    label: rasterio.DatasetReader,
) -> None:
    reference = _contract(before)
    if before.count != 6:
        raise ValueError(f"before raster must have six bands; found {before.count}")
    if _contract(after) != reference:
        raise ValueError("after raster must have the same grid and six bands as before raster")
    if label.count != 1:
        raise ValueError(f"label raster must have one band; found {label.count}")
    label_contract = _contract(label)
    label_grid = (
        label_contract.width,
        label_contract.height,
        label_contract.crs,
        label_contract.transform,
    )
    reference_grid = (
        reference.width,
        reference.height,
        reference.crs,
        reference.transform,
    )
    if label_grid != reference_grid:
        raise ValueError("label raster must have the same grid as before raster")


def _windows(width: int, height: int, chip_size: int) -> list[Window]:
    return [
        Window(column, row, chip_size, chip_size)
        for row in range(0, height - chip_size + 1, chip_size)
        for column in range(0, width - chip_size + 1, chip_size)
    ]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for block in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def run_sample(config: SamplePipelineConfig) -> Path:
    """Validate, chip, and run fixed dummy inference on one aligned sample."""
    for source in (config.before, config.after, config.label):
        if not source.is_file():
            raise FileNotFoundError(source)

    config.output_dir.mkdir(parents=True, exist_ok=True)
    chip_directory = config.output_dir / "chips"
    chip_directory.mkdir(exist_ok=True)
    prediction_path = config.output_dir / "dummy_prediction.tif"

    with (
        rasterio.open(config.before) as before,
        rasterio.open(config.after) as after,
        rasterio.open(config.label) as label,
    ):
        _validate_contract(before, after, label)
        windows = _windows(before.width, before.height, config.chip_size)
        if not windows:
            raise ValueError("chip_size exceeds raster dimensions")

        for index, window in enumerate(windows):
            features = np.concatenate((before.read(window=window), after.read(window=window)))
            np.savez_compressed(
                chip_directory / f"chip-{index:04d}.npz",
                features=features,
                label=label.read(1, window=window),
            )

        before_nir = before.read(4).astype("float32")
        before_swir = before.read(6).astype("float32")
        after_nir = after.read(4).astype("float32")
        after_swir = after.read(6).astype("float32")
        before_nbr = (before_nir - before_swir) / (before_nir + before_swir + 1.0)
        after_nbr = (after_nir - after_swir) / (after_nir + after_swir + 1.0)
        prediction = ((before_nbr - after_nbr) > 0.1).astype("uint8")
        profile = before.profile.copy()
        profile.pop("blockxsize", None)
        profile.pop("blockysize", None)
        profile.update(count=1, dtype="uint8", compress="deflate")
        with rasterio.open(prediction_path, "w", **profile) as output:
            output.write(prediction, 1)

    manifest = config.output_dir / "manifest.json"
    payload = {
        "chip_count": len(windows),
        "chip_size": config.chip_size,
        "inputs": {
            "before": str(config.before),
            "after": str(config.after),
            "label": str(config.label),
        },
        "prediction_sha256": _sha256(prediction_path),
    }
    manifest.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    LOGGER.info("sample_pipeline_completed", chip_count=len(windows), manifest=str(manifest))
    return manifest
