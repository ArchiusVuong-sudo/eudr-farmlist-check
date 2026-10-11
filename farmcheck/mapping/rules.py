"""Header rules: exact matches on normalised headers in English, Spanish, Portuguese and French.

Deliberately no Vietnamese (or any other language): an unknown layout goes to the model proposal
and a person, and the confirmed mapping is saved as a profile for the next upload.
"""

from __future__ import annotations

import re
import unicodedata

from .fields import FieldName, missing_required

SYNONYMS: dict[FieldName, tuple[str, ...]] = {
    "plot_code": (
        "plot id", "plot code", "plot", "plot ref", "farm id", "farm code", "parcel id", "parcel code", "field id",
        "codigo finca", "codigo de finca", "codigo parcela", "id finca", "id parcela", "codigo lote",
        "codigo talhao", "id talhao", "codigo da parcela",
        "code parcelle", "id parcelle", "identifiant parcelle", "numero parcelle",
    ),
    "producer": (
        "producer", "producer name", "farmer", "farmer name", "grower", "owner",
        "productor", "nombre productor", "nombre del productor", "caficultor",
        "produtor", "nome do produtor", "agricultor",
        "producteur", "nom producteur", "nom du producteur", "agriculteur",
    ),
    "admin_area": ("village", "community", "locality", "vereda", "comunidad", "localidad", "comunidade", "localidade", "localite"),
    "municipality": ("municipality", "district", "county", "municipio", "distrito", "canton", "commune", "departement"),
    "province": ("province", "state", "region", "departamento", "provincia", "estado", "regiao"),
    "country": ("country", "pais", "pays", "country code"),
    "latitude": ("latitude", "lat", "latitud", "lat dd", "y"),
    "longitude": ("longitude", "lon", "long", "lng", "longitud", "lon dd", "x"),
    "area_ha": ("area", "area ha", "hectares", "ha", "size", "plot area", "superficie", "hectareas", "area hectareas", "tamano"),
    "boundary_wkt": ("polygon", "boundary", "geometry", "wkt", "poligono", "geometria", "contorno", "limite", "polygone", "perimetre"),
    "row_ref": ("no", "nr", "row", "n", "nro", "numero", "item"),
    "notes": ("notes", "note", "comments", "remarks", "observaciones", "notas", "observacoes", "remarques", "commentaires"),
}

_LOOKUP: dict[str, FieldName] = {}
for _field, _words in SYNONYMS.items():
    for _w in _words:
        _LOOKUP[_w] = _field


def normalise_header(header: str) -> str:
    """'Área (ha)' -> 'area'; accents, units in brackets and punctuation are dropped."""
    text = unicodedata.normalize("NFKD", header)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.replace("đ", "d").replace("Đ", "D")
    text = re.sub(r"[(\[].*?[)\]]", " ", text)
    text = re.sub(r"[^0-9a-zA-Z]+", " ", text).strip().lower()
    return re.sub(r"\s+", " ", text)


def map_by_rules(headers: list[str]) -> dict[str, FieldName]:
    """Header -> field for every header a rule recognises; the first header wins a field."""
    out: dict[str, FieldName] = {}
    used: set[str] = set()
    for h in headers:
        field = _LOOKUP.get(normalise_header(h))
        if field and field not in used:
            out[h] = field
            used.add(field)
    return out


def rules_are_enough(headers: list[str], mapped: dict[str, FieldName]) -> bool:
    """Rules suffice when the required fields are found and most columns are recognised."""
    return not missing_required(set(mapped.values())) and len(mapped) >= 0.7 * len(headers)
