from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin

from geoml_platform.config import SamplePipelineConfig
from geoml_platform.pipeline import run_sample


def write_raster(path: Path, data: np.ndarray) -> None:
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        height=data.shape[1],
        width=data.shape[2],
        count=data.shape[0],
        dtype=data.dtype,
        crs="EPSG:32629",
        transform=from_origin(635520, 4725750, 10, 10),
    ) as dataset:
        dataset.write(data)


def test_pipeline_creates_deterministic_chips_and_prediction(tmp_path: Path) -> None:
    before = np.full((6, 4, 4), 100, dtype="uint16")
    after = before.copy()
    after[3] = 20
    after[5] = 180
    label = np.zeros((1, 4, 4), dtype="uint8")
    write_raster(tmp_path / "before.tif", before)
    write_raster(tmp_path / "after.tif", after)
    write_raster(tmp_path / "label.tif", label)

    config = SamplePipelineConfig(
        before=tmp_path / "before.tif",
        after=tmp_path / "after.tif",
        label=tmp_path / "label.tif",
        output_dir=tmp_path / "output",
        chip_size=2,
    )
    first = json.loads(run_sample(config).read_text(encoding="utf-8"))
    second = json.loads(run_sample(config).read_text(encoding="utf-8"))

    assert first == second
    assert first["chip_count"] == 4
    assert len(list((tmp_path / "output" / "chips").glob("*.npz"))) == 4
    with rasterio.open(tmp_path / "output" / "dummy_prediction.tif") as prediction:
        assert prediction.read(1).tolist() == [[1] * 4 for _ in range(4)]


def test_pipeline_rejects_misaligned_label(tmp_path: Path) -> None:
    image = np.full((6, 4, 4), 100, dtype="uint16")
    write_raster(tmp_path / "before.tif", image)
    write_raster(tmp_path / "after.tif", image)
    write_raster(tmp_path / "label.tif", np.zeros((1, 3, 4), dtype="uint8"))

    with pytest.raises(ValueError, match="same grid"):
        run_sample(
            SamplePipelineConfig(
                before=tmp_path / "before.tif",
                after=tmp_path / "after.tif",
                label=tmp_path / "label.tif",
                output_dir=tmp_path / "output",
                chip_size=2,
            )
        )
