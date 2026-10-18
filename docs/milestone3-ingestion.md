# Milestone 3 — Recoverable ingestion and provenance

The ingestion path imports one EFFIS event, searches Planetary Computer's
`sentinel-2-l2a` collection, freezes one before/after selection, downloads original
assets, and commits event/scene/asset provenance to PostgreSQL/PostGIS.
Rasters stay in the filesystem object store. Dataset alignment, cloud masking,
mosaics, and splits belong to Milestone 4.

## Run locally

Use the standard CPython 3.12 environment from Milestone 2:

```powershell
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements-dev.lock
python -m pip install --no-deps -e .

$env:GEOML_POSTGRES_PASSWORD = 'choose-a-local-password'
docker compose up -d --wait postgis
$env:GEOML_DATA_ROOT = './data'
$env:GEOML_DATABASE_URL = 'postgresql+psycopg://geoml:choose-a-local-password@localhost:5433/geoml'
geoml db-init
```

URL-encode special characters when placing a password in a database URL. Compose
binds PostGIS to loopback port 5433 and persists it in the `postgis-data` volume.
Only the database is containerized; the host CLI reads/writes `GEOML_DATA_ROOT`.

For an existing workspace configured with the ignored `.env` file, load those
settings instead of choosing a new password for the existing database volume:

```powershell
Get-Content .env | ForEach-Object {
  if ($_ -match '^([A-Z_]+)=(.*)$') {
    [Environment]::SetEnvironmentVariable($Matches[1], $Matches[2], 'Process')
  }
}
docker compose up -d --wait postgis
```

The local configuration records the Compose project, database connection, and
data root. Keep it outside version control. Compose reads it automatically;
the host CLI uses the process environment loaded above.

Import the reference perimeter already exported during Milestone 1:

```powershell
geoml import-effis --perimeter data/interim/aligned/effis-2022-206702/effis_perimeter.geojson --mgrs-tile 29TPH --source-downloaded-at 2026-09-11T00:00:00Z --output data/config/effis-2022-206702.json
geoml discover --event data/config/effis-2022-206702.json
geoml ingest --event data/config/effis-2022-206702.json
geoml ingest --event data/config/effis-2022-206702.json
```

The download date is known to day precision; midnight in this example represents
that date, not a measured download time. The original EFFIS properties, feature
ID, fire interval, perimeter, attribution, and exported-file SHA-256 are retained.

For another event, export exactly one feature from the selected SpatiaLite source
with GDAL (needed for this extraction only), then run `import-effis` with its
covering tile and actual source download date:

```powershell
ogr2ogr -f GeoJSON -t_srs EPSG:4326 data/effis-event.geojson D:\Descargas\effis_layer.db -dialect SQLITE -sql "SELECT * FROM [modis.ba.poly] WHERE id = '206702'"
```

`import-effis` accepts a WGS84 GeoJSON Feature or one-feature FeatureCollection,
checks polygon validity, and interprets EFFIS's unzoned fire-date fields as UTC.
It preserves the source polygon without simplifying or repairing it. An invalid
polygon must be reviewed before import. The resulting JSON can be reviewed before
any search or download. It defaults to a 30-day window on each side of the fire,
at most 20% scene cloud, and assets B02/B03/B04/B08/B11/B12/SCL. Set these fields
before discovery; an `assets` list containing only `SCL` permits a small smoke run
in a separate data root.

`discover` downloads metadata only. It filters to the configured tile and scenes
covering the entire perimeter, then selects the nearest acquisition strictly
before the first fire date and strictly after the final fire date. Cloud cover
and item ID break ties. It consumes all search pages. A multi-tile event with no
single covering scene fails explicitly; it needs the later mosaic path. Scene
cloud percentage is a discovery filter, not pixel-level cloud masking.

## Artifacts and recovery

All paths in manifests and database rows are relative POSIX storage keys:

```text
raw/ingestion/{event_id}/plan.json
raw/ingestion/{event_id}/manifest.json
raw/labels/{event_id}/perimeter.geojson
raw/stac-items/sentinel-2-l2a/{item_id}.json
raw/imagery/sentinel-2-l2a/{item_id}/{asset}.tif
raw/receipts/sentinel-2-l2a/{item_id}/{asset}.json
```

The plan preserves the query, provider, event configuration, and selected original
STAC items. Subsequent runs use that selection without contacting STAC again.
Changing the event configuration requires a new event ID or separate data root;
there is no implicit refresh of completed inputs. Original selected assets are
full-tile files, so the default seven-asset pair may require gigabytes of disk.

Downloads use 60-second HTTP timeouts, three attempts with exponential backoff,
and fresh Planetary Computer signing on each retry. Tokens are never saved in
STAC metadata, receipts, manifests, or application logs. Downloads are streamed to
temporary files; empty responses, truncated Content-Length, and published STAC
`file:size`/SHA-256 `file:checksum` mismatches fail verification. Unsupported
published checksum algorithms fail explicitly. Where no upstream checksum exists,
the locally computed SHA-256 establishes the checksum for subsequent verification;
it is not an independent upstream authenticity check.

Storage writes copy into a unique temporary file beside the destination, verify
SHA-256, and atomically publish the final file. Existing matching files succeed;
conflicting content fails. Interrupted downloads never become final objects.
Receipts let a failed run reuse already verified downloads. A rerun rehashes every
asset; corruption fails explicitly, and a missing file is restored against the
receipt's original checksum. Application code uses a single writer per data root;
filesystem atomicity is not a distributed locking protocol. Complete orphaned
objects can remain after a failure and are reconciled by retrying the same command.

The manifest is written after all files verify, then all database rows commit in
one transaction. If the database commit fails, the command fails; retrying reuses
the files and registers the same manifest. A manifest on disk alone does not prove
database completion. The `ingestions` table records completed registration.
Database conflicts roll back the transaction instead of overwriting provenance.

## Database lifecycle

`geoml db-init` installs PostGIS and schema version 1. Repeating it is safe.
`schema_version` is the migration boundary; unknown versions are rejected rather
than silently modified. Future schema changes must add an explicit migration.

- `events`: event ID, original source properties, fire dates, geometry, and licence.
- `scenes`: unique collection/item IDs, acquisition/cloud metadata, attribution,
  original STAC key and checksum.
- `assets`: unique storage keys, scene foreign keys, source links, sizes, checksums.
- `ingestions`: event foreign key, before/after scene roles, manifest key/checksum,
  and the complete provenance record.
- `event_geometries`: a PostGIS view exposing event polygons in EPSG:4326.

PostgreSQL registrations use a transaction advisory lock, including across events
sharing scenes. SQLite is an isolated test adapter, not the operational database.
Shutdown with `docker compose stop postgis`; the named volume remains intact.

Back up the metadata to a host file (avoid PowerShell redirection of binary output):

```powershell
docker compose exec -T postgis pg_dump -U geoml -d geoml -Fc -f /tmp/geoml.dump
docker compose cp postgis:/tmp/geoml.dump ./geoml.dump
```

Back up `GEOML_DATA_ROOT` separately while ingestion is stopped. To restore into
a fresh instance, copy the dump into the container and use
`pg_restore -U geoml -d geoml /tmp/geoml.dump`. Preserve matching files and database
metadata together; a database dump does not contain the source rasters.

## Verification and evidence

```powershell
ruff check .
mypy src
pytest
```

Unit tests use synthetic STAC responses, assets, and SQLite. They cover deterministic
selection, coverage rejection, traversal rejection, atomic-write integrity,
transient/permanent HTTP failures, signing refresh, partial-run recovery, duplicate
prevention, corruption, missing-file restoration, database rollback/recovery, and
CLI import/discovery/ingestion. No external provider or real raster is required.

GitHub Actions additionally starts PostGIS and sets `GEOML_TEST_DATABASE_URL` so the
integration test verifies schema initialization, repeated registration, foreign
keys, and spatial geometry queries. To run it locally, point that variable at a
**disposable empty database** and run `pytest tests/test_ingestion.py -k postgis`.

On 2026-10-18, live reference-event discovery selected the same July 8/August 2
items as the feasibility spike. A bounded live smoke ingested both SCL assets
(1,604,675 and 2,908,132 bytes), registered provenance using the SQLite test adapter,
then reproduced the same manifest checksum with a client that rejected all network
requests. The manifest SHA-256 was
`ef7610919c5e4995d5629a64e0474b1f3af77a8296774ba2755a2d32790ee54d`.
Docker-backed verification also passed locally on 2026-10-18, after accessing the
running Docker Desktop engine outside the Windows sandbox. An isolated Compose
project ran PostgreSQL 17.5 and PostGIS 3.5.2. All **38 tests passed with no skips**
on both the Windows CPython environment and a clean `python:3.12-slim` Linux
container; the Linux run also passed Ruff and mypy using the pinned dependencies.
The integration test exercised repeatable schema setup, duplicate prevention,
foreign-key enforcement, and EPSG:4326 spatial geometry queries. Temporary test
containers, databases, and the dedicated volume were removed after verification.

The slim Linux image initially failed to import Rasterio because it lacked
`libexpat.so.1`. Installing Debian's `libexpat1` resolved the failure, and CI now
installs that system dependency explicitly. For a Debian-based Python container,
install it before the Python requirements:

```sh
apt-get update
apt-get install -y --no-install-recommends libexpat1
```

The full reference ingestion subsequently completed on 2026-10-18: all seven
assets for both acquisitions are stored under `data/raw/imagery/sentinel-2-l2a/`,
totaling **1,999,327,524 bytes** (approximately 2.0 GB). The twelve spectral files
were downloaded and the two previously verified SCL files were reused. The
canonical manifest is `data/raw/ingestion/effis-2022-206702/manifest.json`, SHA-256
`8c80309b87a2950a16bd76cbd534950622e05c6ce4318359b9dc17e705c31834`.

The persistent `geoml-platform` Compose project registered one event, two scenes,
fourteen assets, and one completed ingestion in PostGIS. Verification opened all
fourteen GeoTIFFs, checked their single-band EPSG:32629 grids and native 10/20 m
resolutions, and read a 128 × 128 pixel sample from each. A complete rerun with
network requests rejected verified every asset checksum and reproduced the same
manifest without adding database rows. The local verification report is
`data/validation/full-ingestion-report.json`. The persistent PostGIS service and
its named volume remain available; connection settings are in the ignored `.env`.

## Sources

- [PySTAC Client search, pagination, and retry configuration](https://pystac-client.readthedocs.io/en/stable/usage.html)
- [Planetary Computer shared access signatures](https://planetarycomputer.microsoft.com/docs/concepts/sas/)
- [HTTPX streaming responses](https://www.python-httpx.org/quickstart/)
- [SQLAlchemy schema metadata](https://docs.sqlalchemy.org/en/20/core/metadata.html)
