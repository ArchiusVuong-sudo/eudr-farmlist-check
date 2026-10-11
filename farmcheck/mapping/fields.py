"""Canonical fields a farm list is mapped onto."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

FieldName = Literal[
    "plot_code",
    "producer",
    "admin_area",
    "municipality",
    "province",
    "country",
    "latitude",
    "longitude",
    "area_ha",
    "boundary_wkt",
    "row_ref",
    "notes",
    "ignore",
]


@dataclass(frozen=True)
class FieldSpec:
    name: FieldName
    label: str
    description: str


FIELDS: tuple[FieldSpec, ...] = (
    FieldSpec("plot_code", "Plot code", "Supplier's id for the plot of land (unique per row)"),
    FieldSpec("producer", "Producer", "Farmer or household who works the plot"),
    FieldSpec("admin_area", "Admin area", "Smallest admin unit: village, commune or vereda"),
    FieldSpec("municipality", "Municipality", "District or municipality"),
    FieldSpec("province", "Province", "Province, department or state"),
    FieldSpec("country", "Country", "Country name or code"),
    FieldSpec("latitude", "Latitude", "Latitude of the plot point (degrees, or northing in metres)"),
    FieldSpec("longitude", "Longitude", "Longitude of the plot point (degrees, or easting in metres)"),
    FieldSpec("area_ha", "Area (ha)", "Declared plot area in hectares"),
    FieldSpec("boundary_wkt", "Boundary (WKT)", "Plot boundary polygon as WKT text"),
    FieldSpec("row_ref", "Row number", "Running row number in the supplier's file"),
    FieldSpec("notes", "Notes", "Free-text remarks"),
    FieldSpec("ignore", "Ignore", "Not used by the checks"),
)
FIELD_NAMES: tuple[str, ...] = tuple(f.name for f in FIELDS)
FIELD_LABELS: dict[str, str] = {f.name: f.label for f in FIELDS}
REQUIRED_ANY_GEOMETRY = ("latitude", "longitude", "boundary_wkt")


def missing_required(fields: set[str]) -> list[str]:
    """What a mapping lacks before it can run: a plot code, and a point or a boundary."""
    missing = []
    if "plot_code" not in fields:
        missing.append("plot_code")
    has_point = "latitude" in fields and "longitude" in fields
    if not has_point and "boundary_wkt" not in fields:
        missing.append("latitude and longitude, or boundary_wkt")
    elif ("latitude" in fields) != ("longitude" in fields):
        missing.append("longitude" if "latitude" in fields else "latitude")
    return missing
