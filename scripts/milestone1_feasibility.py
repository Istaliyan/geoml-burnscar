"""Create the Milestone 1 Sentinel-2/EFFIS alignment sample.

This is an exploration script, deliberately outside the application package.  It
uses only Python's standard library plus GDAL command-line programs so the data
feasibility gate can be reproduced before the project package is scaffolded.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
from pathlib import Path
from urllib import request

STAC_ROOT = "https://planetarycomputer.microsoft.com/api/stac/v1"
COLLECTION = "sentinel-2-l2a"
EVENT_ID = "effis-2022-206702"
EFFIS_FEATURE_ID = "206702"
# EFFIS fire dates: 2022-07-14 to 2022-07-23.  Both items cover MGRS 29TPH.
BEFORE_ITEM = "S2A_MSIL2A_20220708T112131_R037_T29TPH_20220708T235242"
AFTER_ITEM = "S2B_MSIL2A_20220802T112119_R037_T29TPH_20220803T002053"
AOI_WGS84 = (-7.346122, 42.510371, -7.110770, 42.668640)
BANDS = ("B02", "B03", "B04", "B08", "B11", "B12")


def run(*command: str) -> None:
    display = [
        part.split("?", 1)[0] + "?<signed-url>"
        if part.startswith("/vsicurl/https://")
        else part
        for part in command
    ]
    print("+", " ".join(display))
    subprocess.run(command, check=True)


def get_json(url: str) -> dict:
    with request.urlopen(url, timeout=60) as response:  # nosec B310: fixed HTTPS endpoints
        return json.load(response)


def signed_href(href: str, token: str) -> str:
    separator = "&" if "?" in href else "?"
    return f"/vsicurl/{href}{separator}{token}"


def gdal_info(path: Path) -> dict:
    result = subprocess.run(
        ("gdalinfo", "-json", str(path)), check=True, capture_output=True, text=True
    )
    return json.loads(result.stdout)


def warp_band(source: str, target: Path, resampling: str) -> None:
    west, south, east, north = (str(value) for value in AOI_WGS84)
    run(
        "gdalwarp",
        "-overwrite",
        "-t_srs",
        "EPSG:32629",
        "-te_srs",
        "EPSG:4326",
        "-te",
        west,
        south,
        east,
        north,
        "-tr",
        "10",
        "10",
        "-tap",
        "-r",
        resampling,
        "-co",
        "COMPRESS=DEFLATE",
        source,
        str(target),
    )


def create_stack(directory: Path, when: str, item: dict, token: str) -> Path:
    temporary_bands = []
    for band in BANDS:
        temporary = directory / f"{when}_{band}.tif"
        temporary_bands.append(temporary)
        resampling = "bilinear" if band in {"B11", "B12"} else "nearest"
        warp_band(signed_href(item["assets"][band]["href"], token), temporary, resampling)

    vrt = directory / f"{when}.vrt"
    output = directory / f"{when}.tif"
    run("gdalbuildvrt", "-separate", str(vrt), *(str(path) for path in temporary_bands))
    run("gdal_translate", "-of", "GTiff", "-co", "COMPRESS=DEFLATE", str(vrt), str(output))
    vrt.unlink(missing_ok=True)
    for temporary in temporary_bands:
        temporary.unlink()
    return output


def create_label(label_source: Path, target: Path, reference: Path) -> None:
    info = gdal_info(reference)
    upper_left = info["cornerCoordinates"]["upperLeft"]
    lower_right = info["cornerCoordinates"]["lowerRight"]
    width, height = (str(value) for value in info["size"])
    projected_source = label_source.with_name("effis_perimeter_32629.geojson")
    projected_source.unlink(missing_ok=True)
    run("ogr2ogr", "-t_srs", "EPSG:32629", str(projected_source), str(label_source))
    target.unlink(missing_ok=True)
    run(
        "gdal_rasterize",
        "-burn",
        "1",
        "-ot",
        "Byte",
        "-te",
        str(upper_left[0]),
        str(lower_right[1]),
        str(lower_right[0]),
        str(upper_left[1]),
        "-ts",
        width,
        height,
        "-co",
        "COMPRESS=DEFLATE",
        str(projected_source),
        str(target),
    )


def create_previews(directory: Path, before: Path, after: Path, mask: Path) -> None:
    preview = directory / "preview"
    preview.mkdir(exist_ok=True)
    for source, name in ((before, "before"), (after, "after")):
        run(
            "gdal_translate",
            "-of",
            "PNG",
            "-ot",
            "Byte",
            "-b",
            "3",
            "-b",
            "2",
            "-b",
            "1",
            "-scale",
            "0",
            "3000",
            "0",
            "255",
            str(source),
            str(preview / f"{name}.png"),
        )
    run(
        "gdal_translate",
        "-of",
        "PNG",
        "-ot",
        "Byte",
        "-scale",
        "0",
        "1",
        "0",
        "255",
        str(mask),
        str(preview / "label.png"),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--effis-db", type=Path, required=True, help="Downloaded effis_layer.db")
    parser.add_argument("--data-root", type=Path, default=Path("data"))
    args = parser.parse_args()
    if not args.effis_db.is_file():
        parser.error(f"EFFIS database not found: {args.effis_db}")
    for executable in ("gdalwarp", "gdalbuildvrt", "gdal_translate", "gdal_rasterize", "ogr2ogr"):
        if shutil.which(executable) is None:
            parser.error(f"{executable} must be on PATH")

    os.environ.setdefault("GDAL_DISABLE_READDIR_ON_OPEN", "EMPTY_DIR")
    os.environ.setdefault("CPL_VSIL_CURL_ALLOWED_EXTENSIONS", ".tif")
    raw_stac = args.data_root / "raw" / "stac-items"
    output = args.data_root / "interim" / "aligned" / EVENT_ID
    raw_stac.mkdir(parents=True, exist_ok=True)
    output.mkdir(parents=True, exist_ok=True)

    before = get_json(f"{STAC_ROOT}/collections/{COLLECTION}/items/{BEFORE_ITEM}")
    after = get_json(f"{STAC_ROOT}/collections/{COLLECTION}/items/{AFTER_ITEM}")
    (raw_stac / f"{BEFORE_ITEM}.json").write_text(json.dumps(before, indent=2), encoding="utf-8")
    (raw_stac / f"{AFTER_ITEM}.json").write_text(json.dumps(after, indent=2), encoding="utf-8")
    token = get_json("https://planetarycomputer.microsoft.com/api/sas/v1/token/sentinel-2-l2a")["token"]

    label_geojson = output / "effis_perimeter.geojson"
    label_geojson.unlink(missing_ok=True)
    run(
        "ogr2ogr",
        "-f",
        "GeoJSON",
        str(label_geojson),
        str(args.effis_db),
        "-dialect",
        "SQLITE",
        "-sql",
        f"SELECT * FROM [modis.ba.poly] WHERE id = '{EFFIS_FEATURE_ID}'",
    )
    before_raster = create_stack(output, "before", before, token)
    after_raster = create_stack(output, "after", after, token)
    label_raster = output / "label.tif"
    create_label(label_geojson, label_raster, after_raster)
    create_previews(output, before_raster, after_raster, label_raster)
    print(f"Created aligned sample in {output}")


if __name__ == "__main__":
    main()
