"""Versioned event and search contracts; no host paths enter provenance."""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator
from shapely.geometry import shape

STAC_URL = "https://planetarycomputer.microsoft.com/api/stac/v1"
BANDS = ("B02", "B03", "B04", "B08", "B11", "B12", "SCL")


def identifier(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,199}", value):
        raise ValueError(
            "Identifiers must use only letters, digits, underscores, dots, and hyphens"
        )
    return value


class EventConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: str
    source: str = "EFFIS/JRC modis.ba.poly"
    source_feature_id: str
    source_url: str = "https://forest-fire.emergency.copernicus.eu/applications/data-and-services"
    source_license: str = "CC-BY-4.0; credit EFFIS/JRC and indicate modifications"
    source_downloaded_at: AwareDatetime
    source_properties: dict[str, Any] = Field(default_factory=dict)
    source_export_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    fire_start: AwareDatetime
    fire_end: AwareDatetime
    bbox: tuple[float, float, float, float]
    geometry: dict[str, Any]
    mgrs_tile: str
    before_days: int = Field(default=30, ge=1, le=365)
    after_days: int = Field(default=30, ge=1, le=365)
    max_cloud_cover: float = Field(default=20, ge=0, le=100)
    assets: tuple[str, ...] = BANDS

    @model_validator(mode="after")
    def validate_event(self) -> EventConfig:
        identifier(self.event_id)
        if self.fire_start >= self.fire_end:
            raise ValueError("fire_start must precede fire_end")
        west, south, east, north = self.bbox
        if not (-180 <= west < east <= 180 and -90 <= south < north <= 90):
            raise ValueError("bbox must be ordered WGS84 west,south,east,north")
        if not re.fullmatch(r"[0-9]{2}[A-Z]{3}", self.mgrs_tile):
            raise ValueError("mgrs_tile must be a five-character tile such as 29TPH")
        if not self.assets or len(set(self.assets)) != len(self.assets):
            raise ValueError("assets must be non-empty and unique")
        if any(asset not in BANDS for asset in self.assets):
            raise ValueError(f"Supported assets: {BANDS}")
        if self.geometry.get("type") not in {"Polygon", "MultiPolygon"}:
            raise ValueError("Event geometry must be a WGS84 Polygon or MultiPolygon")
        geometry = shape(self.geometry)
        if geometry.is_empty or not geometry.is_valid:
            raise ValueError("Invalid event geometry")
        gw, gs, ge, gn = geometry.bounds
        if not (west <= gw <= ge <= east and south <= gs <= gn <= north):
            raise ValueError("bbox must contain the event geometry")
        return self

    def interval(self, role: str) -> tuple[datetime, datetime]:
        if role == "before":
            return self.fire_start - timedelta(days=self.before_days), self.fire_start
        if role == "after":
            return self.fire_end, self.fire_end + timedelta(days=self.after_days)
        raise ValueError("Unknown scene role")
