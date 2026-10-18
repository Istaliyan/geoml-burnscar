# Dependency Decisions

## Status and scope

### Milestone 3 update (2026-10-18)

The Windows ingestion path continues the Milestone 2 CPython 3.12 wheel-based
environment. `pyproject.toml` defines supported dependencies and
`requirements-dev.lock` records the exact development/test snapshot; CI installs
that snapshot before the editable package. The earlier Conda locks are retained
as historical inputs for future Linux services and GPU training, not as the
current ingestion environment.

Milestone 3 adds PySTAC Client, HTTPX, SQLAlchemy, Psycopg's binary wheel, Shapely,
and development-only Shapely type stubs. Shapely validates perimeters and checks
scene coverage; its wheel avoids adding another GDAL binding. Psycopg supplies
the PostgreSQL driver without a local libpq build. PostgreSQL/PostGIS runs in the
selected `postgis/postgis:17-3.5` container. GeoAlchemy2 is deferred until native
spatial table mappings are required; the ingestion schema exposes its stored
GeoJSON through a PostGIS geometry view. Pixel processing remains in Rasterio.

No new GDAL/Conda stack is introduced on Windows. The original GDAL command-line
tools are needed only to export a new EFFIS feature from the SpatiaLite source;
the package imports its WGS84 GeoJSON. See [the ingestion runbook](milestone3-ingestion.md)
for schema setup, storage/recovery behavior, provider evidence, and limitations.

Clean Linux container verification found that `python:3.12-slim` does not include
`libexpat.so.1`, which Rasterio's wheel requires. Debian-based application/test
containers must install `libexpat1` before importing Rasterio. CI installs this
system dependency explicitly; Python package pins alone do not supply it.

**Status:** accepted for Milestone 0; revise only when a milestone produces evidence that changes a decision.

This document selects the smallest dependency set required by the plan. It does not install packages or create application scaffolding. Dependencies are introduced only in their named milestone, then resolved to exact versions in Conda lock files.

MinIO is intentionally not a project dependency. The local MVP stores geospatial artifacts in bind-mounted filesystem zones and keeps PostgreSQL/PostGIS for metadata and prediction polygons.

## Toolchain and reproducibility policy

| Decision | Choice | Rationale |
| --- | --- | --- |
| Python | CPython 3.12 only (`>=3.12,<3.13`) | Rasterio 1.5 requires Python 3.12+; a single supported interpreter keeps the first geospatial environment understandable. |
| Python distribution and environment manager | Miniconda with Conda (`conda>=25,<27`) | Conda manages the Python interpreter and native GIS libraries as one compatible environment. |
| Channels | `conda-forge` only, with strict channel priority | A single channel avoids mixed binary stacks; strict priority reduces incompatible solves. |
| Lock policy | Commit `conda-lock.yml` and rendered `conda-{platform}.lock` files; CI creates environments from locks | The environment specifications express intent while locks record exact packages, builds, and channels. |
| Dependency groups | `environment.yml` plus `environment-{dev,data,training,api,orchestration}.yml` | Each milestone adds a small Conda environment specification rather than installing the whole stack. |
| Upgrade policy | Monthly review; upgrade one package or a related set on a branch, regenerate the lock, run the relevant tests, then record noteworthy changes | Avoids both unbounded drift and risky bulk upgrades. |
| Vulnerability/SBOM policy | Run `pip-audit` against the locked environment and generate a CycloneDX SBOM in CI once CI exists | Gives a lightweight, reviewable supply-chain check without adding a security platform. |

`conda-lock.yml` and its rendered platform locks are the source of exact package versions. Docker images use explicit major/minor tags at first and are changed only through reviewed updates; the exact image digest may be captured in CI or release evidence later.

The source specifications are `environment.yml` plus the purpose-specific `environment-*.yml` files. Lock the base, development, data, training, and API specifications for Windows and Linux. Lock orchestration separately for Linux because Airflow belongs in its Linux container rather than the Windows development environment.

`virtual-packages.yml` records the system capabilities used when solving GPU locks. It declares `__cuda=13.1` from the local NVIDIA driver and `__glibc=2.28`, the minimum Linux baseline required by the selected PyTorch GPU 2.13 packages. This file is solver input, not an installable package list; the eventual Linux container base must provide glibc 2.28 or newer.

### Lock and installation procedure

After installing Miniconda, run these commands from the repository root in a new Miniconda Prompt. The first command creates an isolated lock-generator environment; it does not pollute the project environment.

```powershell
conda create --name geoml-locker --channel conda-forge --override-channels --strict-channel-priority conda-lock=4

conda run --name geoml-locker conda-lock lock `
  --file environment.yml `
  --virtual-package-spec virtual-packages.yml `
  --platform win-64 `
  --platform linux-64 `
  --lockfile conda-lock.yml

conda run --name geoml-locker conda-lock lock `
  --file environment.yml `
  --file environment-orchestration.yml `
  --platform linux-64 `
  --lockfile conda-orchestration-lock.yml

conda run --name geoml-locker conda-lock render conda-lock.yml --kind explicit --platform win-64
conda run --name geoml-locker conda-lock render conda-lock.yml --kind explicit --platform linux-64
```

The base Conda lock is retained for later Linux/service and training evaluation. The target Windows development environment uses standard CPython because every tested conda-forge Rasterio stack failed native loading on that machine:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
```

Do not install Rasterio into the Windows development environment through Conda: Rasterio 1.4.4, 1.5.0, and 1.5.1 conda-forge builds all failed to load `_base`. Installing its wheel into Conda subsequently caused collisions in Python's XML, hash, and SSL extensions. The standard CPython virtual environment keeps the working wheel self-contained.

The main lock intentionally excludes Airflow. Install the orchestration lock only in the Linux environment or container used for Milestone 8:

```bash
conda-lock install --name geoml-airflow conda-orchestration-lock.yml
```

Do not add training or API specifications to the main lock until their milestones begin. Revisit the native-package strategy before Milestone 3 adds more geospatial dependencies and before Milestone 5 introduces CUDA.

## Direct dependency decision table

The constraints below are compatibility boundaries, not a request to install every package now. Licenses must be rechecked from the resolved release metadata when each group is introduced.

| Dependency | Group / milestone | Purpose | Considered alternative | Decision and rationale | Constraint | License |
| --- | --- | --- | --- | --- | --- | --- |
| Miniconda / Conda | developer tool / 2 | Create and manage the project’s Python and native-library environment | Micromamba, Poetry, pip-tools | Use Miniconda because it is familiar, supports the required native GIS stack, and keeps the learning environment explicit. | `conda>=25,<27` | BSD-3-Clause |
| `conda-lock` | developer tool / 2 | Resolve the environment specifications to exact multi-platform locks | Exporting an environment manually | Generates reproducible lock files from `environment*.yml` inputs. | `>=4,<5` | Apache-2.0 AND BSD-2-Clause AND BSD-3-Clause AND ISC AND MIT |
| `pydantic` and `pydantic-settings` | base / 2 | Typed configuration and environment-variable loading | Dataclasses plus manual parsing | Pydantic validation makes configuration errors visible at the boundary. | `pydantic>=2.12,<3`; `pydantic-settings>=2.8,<3` | MIT |
| `typer` | base / 2 | Small, typed CLI surface | argparse | Keeps first CLIs readable while preserving type-driven help. | `>=0.15,<1` | MIT |
| `structlog` | base / 2 | Structured application logs | stdlib logging only | Use structured records for pipeline IDs and later API timings; retain stdlib-compatible output. | `>=25,<26` | Apache-2.0 OR MIT |
| `pystac-client` | data / 1 then 3 | STAC discovery and saved item/query handling | Hand-written HTTP client | Use the STAC-specific client; no provider-specific SDK is needed before provider selection. | `>=0.9,<0.10` | Apache-2.0 |
| `httpx` | data / 3 | Stream asset downloads with explicit timeouts and retries | requests | Supports synchronous and asynchronous clients; use only the synchronous path unless evidence requires async. | `>=0.28,<1` | BSD-3-Clause |
| `numpy` | data / 1 | Array representation for raster and model data | None | Required common numerical array layer. | `>=2.0,<3` | BSD-3-Clause |
| `rasterio` | data / 1 | Read/write georeferenced rasters, grids, transforms, and windows | GDAL Python bindings directly | Use the official 1.5.1 wheel on the target Windows machine; three conda-forge versions failed their import smoke test there. | `=1.5.1` | BSD-3-Clause |
| `click` | data / 2 | CLI runtime used by Typer and Rasterio | Unbounded transitive dependency | Keep the API expected by Rasterio 1.5.1; Click 8.5 removed the top-level `FileError` import. | `>=8,<8.5` | BSD-3-Clause |
| `geopandas` | data / 1 | Read and inspect perimeter vectors during feasibility | Fiona/Shapely-only code | Appropriate high-level vector tooling for exploratory and tabular geospatial work. | `>=1.1,<1.2` | BSD-3-Clause |
| `shapely` | data / 1 | Geometry validation and operations used directly by preprocessing | Geometry methods only through GeoPandas | Keep direct geometry behavior explicit where code needs it. | `>=2.1,<3` | BSD-3-Clause |
| `pyproj` | data / 1 | CRS inspection and coordinate transforms | Call PROJ/GDAL command-line tools | Explicit CRS transformations and testable coordinate behavior. | `>=3.7,<4` | MIT |
| `sqlalchemy`, `psycopg`, and `geoalchemy2` | data / 3 | PostgreSQL/PostGIS metadata and vector persistence | Raw SQL or an ORM-only abstraction | SQLAlchemy gives explicit transactions and migrations later; Psycopg is the PostgreSQL driver; GeoAlchemy2 maps spatial columns. | `sqlalchemy>=2,<3`; `psycopg>=3,<4`; `geoalchemy2>=0.17,<1` | MIT; LGPL-3.0-or-later; MIT |
| `pytorch-gpu` and `cuda-version` | training / 5 | U-Net baseline, data loading, and inference on the local NVIDIA RTX 3080 | TensorFlow, JAX, CPU-only PyTorch | Conda Forge provides PyTorch GPU 2.13 for Windows and Linux. Pin the CUDA 13.0 runtime; the installed 591.86 driver reports CUDA 13.1 and supports the older runtime. | `pytorch-gpu>=2.13,<2.14`; `cuda-version=13.0` | BSD-3-Clause; NVIDIA EULA |
| `mlflow` | training / 5 | Run metadata, artifacts, and model registry | Weights & Biases, custom database | Local-first tracking and registry without an external account. Conda Forge currently provides the 3.14 release line. | `>=3.14,<3.15` | Apache-2.0 |
| `fastapi` and `uvicorn[standard]` | api / 7 | Bounded inference API and ASGI server | Flask | Typed request validation and generated OpenAPI fit the API contract. | `fastapi>=0.115,<1`; `uvicorn[standard]>=0.30,<1` | MIT |
| `apache-airflow` | orchestration / 8 | Schedule stable CLI workflows | Prefect, cron | Use only after stable CLIs exist; use Airflow’s published constraints when it is introduced. | `>=3,<4` plus matching constraints file | Apache-2.0 |
| `ruff`, `mypy`, `pytest`, `pre-commit`, `pip-audit` | dev / 2 | Linting, typing, tests, hooks, vulnerability audit | Separate formatters/linters, manual checks | Small standard development toolchain with automated feedback. | `ruff>=0.12,<1`; `mypy>=1.15,<2`; `pytest>=8,<9`; `pre-commit>=4,<5`; `pip-audit>=2,<3` | MIT; MIT; MIT; MIT; Apache-2.0 |

## Native libraries and container policy

- Local development uses Miniconda with Conda packages from `conda-forge`, strict channel priority, and Python 3.12. Rasterio 1.5 requires GDAL 3.8+ and NumPy 2+; resolve Rasterio, GDAL, PROJ, GEOS, and their transitive libraries together in the Conda environment.
- The first container image uses the same rendered Conda lock as local development. If a future dependency requires OS packages, list them explicitly in the Dockerfile and document why the Conda package is insufficient. Do not mix Conda channels or add pip packages to this environment unless a documented exception is accepted.
- PostGIS is isolated in `postgis/postgis:17-3.5`, a Debian-based image with the legacy PostgreSQL data-volume path. It is the initial relational/spatial database, not a raster store.
- Python-side GDAL/PROJ and database-side PostGIS libraries are allowed to differ. Exchange geometry using well-defined formats and CRS identifiers; do not attempt to share native binaries between containers.
- Before Milestone 2, record the chosen Miniconda-based Python image and its digest in the dependency update record. Before Milestone 5, verify GPU availability with `torch.cuda.is_available()`, record the resolved CUDA build, and measure VRAM use on the RTX 3080.

## Services and data dependencies

| Dependency | Introduced | Decision | Open question / gate |
| --- | --- | --- | --- |
| `postgis/postgis:17-3.5` | 3 | Keep PostgreSQL/PostGIS for relational metadata and vector predictions. | Confirm migrations, local port policy, and backup/export procedure when Compose is added. |
| MLflow | 5 | Start with a local tracking server and local artifact directory/volume. | Confirm the model-serving hand-off to the API. |
| Apache Airflow | 8 | Add only after ingestion, dataset, training, and batch CLIs are stable. | Confirm that the reference workload benefits from DAG scheduling. |
| Public STAC API | 1 | Use a standards-compliant public API; select the provider from observed coverage and access behavior. | Validate exact endpoint, collection IDs, and asset access. |
| Sentinel-2 Level-2A | 1 | Initial before/after imagery source. | Validate bands, dates, clouds, spatial overlap, and license/terms for selected scenes. |
| EFFIS burned-area perimeter polygons | 1 | First label candidate. | Validate access, coverage, event/date matching, geometry quality, version, and license before use. |

## Local filesystem storage contract

The configured data root is `GEOML_DATA_ROOT`. On the host it is `./data`; inside every artifact-reading/writing Compose container it is `/app/data`.

```text
StorageBackend
  exists(key: str) -> bool
  open_read(key: str) -> BinaryIO
  write_from_path_atomic(key: str, source: Path, sha256: str) -> StoredObject

LocalFsBackend(root=Path(os.environ["GEOML_DATA_ROOT"]))
  "raw/imagery/{collection}/{item_id}/{asset}" -> /app/data/raw/imagery/...
```

Contract rules:

- Keys are non-empty, relative, POSIX-style paths. Reject absolute paths, `.`/`..` segments, backslashes, and keys outside the configured root.
- Callers store keys and checksums in manifests/database rows. They never store host-specific absolute paths.
- An atomic write copies to a unique temporary file in the target directory, verifies the expected SHA-256, then replaces the final path. Existing matching content is a successful idempotent result; a mismatched existing checksum is an error.
- `LocalFsBackend` may expose a resolved `Path` only to local libraries such as Rasterio. That capability remains local-backend-specific, not a promise that future remote backends behave identically.
- The MVP needs no generic listing, deletion, S3, or cloud credentials API. Add an S3-compatible backend only when a deployment requirement proves the need.

## Verification checklist

- [x] A direct dependency is assigned to a purpose and introduction milestone.
- [x] MinIO is excluded; the filesystem and PostGIS responsibilities are distinct.
- [x] Python version, Miniconda/Conda workflow, channels, groups, lock policy, update policy, and initial service image are chosen.
- [x] Native-library and accelerator decisions have explicit gates rather than hidden assumptions.
- [x] Storage keys, roots, atomic-write behavior, and portability limits are documented.
- [ ] Milestone 1 must validate the external data dependencies before project scaffolding begins.

## References

- [Conda channel management and strict priority](https://docs.conda.io/docs/user-guide/tasks/manage-channels.html)
- [Conda Python environment management](https://docs.conda.io/projects/conda/en/latest/user-guide/tasks/manage-python.html)
- [conda-lock environment specifications](https://conda.github.io/conda-lock/src_environment_yml/)
- [conda-lock lockfiles and installation](https://conda.github.io/conda-lock/basic_usage/)
- [conda-lock virtual package specifications](https://github.com/conda/conda-lock/blob/main/README.md)
- [Rasterio package requirements](https://pypi.org/project/rasterio/)
- [Conda Forge PyTorch GPU package](https://anaconda.org/conda-forge/pytorch-gpu)
- [Conda Forge CUDA version package](https://anaconda.org/conda-forge/cuda-version)
- [Conda Forge MLflow package](https://anaconda.org/conda-forge/mlflow)
- [PySTAC Client package](https://pypi.org/project/pystac-client/)
- [GeoPandas package](https://pypi.org/project/geopandas/)
- [PyTorch package](https://pypi.org/project/torch/)
- [MLflow package](https://pypi.org/project/mlflow/)
- [Apache Airflow package](https://pypi.org/project/apache-airflow/)
- [PostGIS Docker image](https://hub.docker.com/r/postgis/postgis/)
