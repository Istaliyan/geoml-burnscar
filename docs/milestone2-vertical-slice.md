# Milestone 2 — Thin vertical slice

The `geoml sample` command is the project’s first end-to-end local data path:

```text
aligned before/after/label GeoTIFFs
  -> grid and band contract validation
  -> paired 12-band chips + label chips
  -> deterministic NBR-change dummy prediction
  -> manifest with output checksum
```

On Windows, use a standard CPython virtual environment and install all Python dependencies together from the package metadata. This keeps Rasterio's bundled native libraries isolated from Conda's native DLLs:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"

python -c "import hashlib, ssl, pyexpat, rasterio; print(rasterio.__version__)"
ruff check .
mypy src
pytest

geoml sample --before data/interim/aligned/effis-2022-206702/before.tif --after data/interim/aligned/effis-2022-206702/after.tif --label data/interim/aligned/effis-2022-206702/label.tif --output-dir data/processed/milestone2
```

The dummy model is deliberately not a learned model: it marks pixels when the fixed normalized-burn-ratio change is greater than `0.1`. Its role is to prove the data and output contracts deterministically; Milestone 5 replaces it with trained baselines.

Quality checks are `ruff check .`, `mypy src`, and `pytest`. The GitHub Actions workflow runs all three on Linux.
