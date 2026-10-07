# Milestone 1 — Burned-area data feasibility

**Decision:** go, with EFFIS `modis.ba.poly` perimeters as a provisional label source for a small Spanish study set. The label boundary is not a 10 m ground-truth boundary, so later dataset generation must define an ignored boundary band and report area-level error.

## Inputs assessed

| Download | What it contains | Result | Choice |
| --- | --- | --- | --- |
| `D:\Descargas\modis.ba.poly.shp` plus `.dbf`, `.shx`, `.prj` | 106,786 EFFIS `modis.ba.poly` polygons in WGS 84, with fire dates, administrative names, area, land-cover proportions, and class | Usable only as the complete four-file set. GDAL reports invalid ring winding, which it corrects when reading. | Do not use as the operational source. |
| `D:\Descargas\effis_layer.db` | The same 106,786 features and extent in a SpatiaLite database; `modis.ba.poly` is a multipolygon layer with a spatial index. | Usable and preferred. It keeps the layer self-contained, retains untruncated field names, and supports spatial SQL/indexing. | **Use this source.** |

The files are alternate EFFIS downloads, not independent label sets: their feature count (106,786), WGS 84 extent (`-28.467085, 27.733107, 44.816148, 69.621167`), attributes, and dates match. The database's internal generic `data_licenses` table does **not** assign a licence to this layer; provenance therefore comes from the official EFFIS download page and licence below.

## Study candidates and imagery audit

All candidates are EFFIS `FireSeason` records in Spain in 2022. Planetary Computer STAC searches used `sentinel-2-l2a`, candidate bounds, and cloud cover below 20%. Its STAC documentation confirms that `eo:cloud_cover` is searchable for this collection; the official CDSE STAC endpoint is retained as a future provider alternative.

| EFFIS ID | Place | EFFIS fire interval | Area | Sentinel-2 finding | Suitability |
| --- | --- | --- | ---: | --- | --- |
| 206702 | Folgoso do Courel, Lugo | 2022-07-14 22:29 to 2022-07-23 12:46 UTC | 13,612 ha | Clean before (2022-07-08, 0.57% cloud) and after (2022-08-02, 5.69%); both in tile 29TPH. | **Reference sample.** |
| 209196 | Bejís, Castellón | 2022-08-15 to 2022-08-22 UTC | 19,362 ha | Suitable low-cloud scenes exist on 2022-08-08 and 2022-08-23/28, but the perimeter crosses tiles 30TXK and 30TYK. | Keep as a mosaic test event. |
| 206474 | Tábara, Zamora | 2022-07-17 to 2022-07-24 UTC | 32,528 ha | Low-cloud scenes exist before and after; the AOI straddles 29TQG and 30TTM. | Keep as a mosaic/UTM-zone test event. |

The reference event avoids the extra ambiguity of a multi-tile mosaic. The 10-day post-fire delay makes burn scar visibility clear but means that regrowth, management, or clouds could contribute to change. It is not suitable for modeling fire progression.

## Reproducible aligned sample

Run from the repository root, with GDAL command-line tools on `PATH` and network access to Planetary Computer:

```powershell
python scripts/milestone1_feasibility.py --effis-db D:\Descargas\effis_layer.db
```

The script preserves the original STAC item JSON under `data/raw/stac-items/`, extracts only EFFIS feature `206702`, and range-reads the AOI windows from the two COGs. It writes these derived local artifacts (ignored by Git):

- `data/interim/aligned/effis-2022-206702/before.tif`
- `data/interim/aligned/effis-2022-206702/after.tif`
- `data/interim/aligned/effis-2022-206702/label.tif`
- `data/interim/aligned/effis-2022-206702/preview/{before,after,label}.png`

| Item | Acquisition UTC | Scene cloud metadata | Grid |
| --- | --- | ---: | --- |
| `S2A_MSIL2A_20220708T112131_R037_T29TPH_20220708T235242` | 2022-07-08 11:21:31 | 0.570343% | before |
| `S2B_MSIL2A_20220802T112119_R037_T29TPH_20220803T002053` | 2022-08-02 11:21:19 | 5.693525% | after |

The six-band order is B02, B03, B04, B08, B11, B12. B02/B03/B04/B08 retain nearest-neighbour sampling; 20 m B11/B12 are bilinearly resampled once to 10 m. All three outputs have EPSG:32629, origin `(635520, 4725750)`, 10 m pixels, and shape 1969 × 1799. Rasterizing the explicitly reprojected EFFIS polygon produces a non-empty mask with 38.42% positive pixels.

| Before (B04/B03/B02) | After (B04/B03/B02) | EFFIS label |
| --- | --- | --- |
| ![Before-fire preview](../data/interim/aligned/effis-2022-206702/preview/before.png) | ![Post-fire preview](../data/interim/aligned/effis-2022-206702/preview/after.png) | ![Rasterized EFFIS label](../data/interim/aligned/effis-2022-206702/preview/label.png) |

The post-fire preview visibly contains a large dark burn scar, and the rasterized perimeter covers the same broad area. This establishes compatibility, not boundary-level accuracy.

## Scale estimate

The sample's two compressed six-band rasters total 65.6 MiB; its uncompressed pixel payload is about 81 MiB. At 256 × 256 pixels it yields 49 full chips without padding (64 with edge padding), and a paired 12-band uint16 chip is 1.5 MiB before masks/augmentation. A full 100 km Sentinel tile for the same six bands is approximately 1.0 GiB uncompressed per acquisition, or about 2.0 GiB per before/after pair; clip/range-read AOIs during the spike and store only selected source assets from Milestone 3 onward.

Alignment/rasterization is CPU and disk I/O work; no GPU is required. A first three-event data set at comparable AOI size is comfortably CPU-feasible. GPU capacity should be measured only when Milestone 5 has a frozen chip size, batch size, and U-Net baseline.

## Provenance, licence, and limitations

- **Labels:** EFFIS, Joint Research Centre/European Commission, real-time Burnt Areas database, `modis.ba.poly` layer, downloaded 2026-09-11. The official page publishes the Shapefile and SpatiaLite forms. EFFIS's licence page says EU-owned content is CC BY 4.0 unless otherwise indicated: credit EFFIS/JRC and indicate modifications.
- **Imagery:** Microsoft Planetary Computer `sentinel-2-l2a` STAC collection, which exposes cloud metadata and COG assets. Sentinel data has free, full, and open access, but distributed/modified outputs must state the source (for example, `Contains modified Copernicus Sentinel data 2022`).
- **Resolution mismatch:** the EFFIS product is derived from MODIS burned-area mapping; it should be treated as a coarse supervisory perimeter, not precise 10 m truth. Do not score the first/last pixels at the perimeter as unambiguous labels.
- **Temporal ambiguity:** EFFIS supplies a start and final date, not a per-pixel burn time. Select the final date plus a documented post-fire window; retain `FIREDATE` and `FINALDATE` in every event record.
- **Coverage:** the downloaded layer covers Europe/Mediterranean/North Africa extents, not a global target. This MVP is deliberately restricted to Spain/Portugal-class EFFIS coverage.

## Gate result

**Pass.** A repeatable sample has compatible labels, dates, imagery, spatial overlap, and a plausible non-empty aligned mask. Continue to Milestone 2 with the assumptions recorded in ADR 002. Reconsider EFFIS if visual QA on the next events shows systematic boundary displacement or if a required event cannot obtain a clean pre/post pair.

## Sources

- [EFFIS data and services](https://forest-fire.emergency.copernicus.eu/applications/data-and-services)
- [EFFIS data licence](https://forest-fire.emergency.copernicus.eu/about-effis/data-license)
- [Planetary Computer STAC reading guide](https://planetarycomputer.microsoft.com/docs/quickstarts/reading-stac/)
- [CDSE STAC documentation](https://documentation.dataspace.copernicus.eu/APIs/STAC.html)
- [Copernicus Sentinel data legal notice](https://sentinels.copernicus.eu/documents/247904/690755/Sentinel_Data_Legal_Notice)
