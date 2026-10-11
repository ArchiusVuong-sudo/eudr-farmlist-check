"""What the model is asked and shown. The payload is also what the UI shows under 'What the model saw'."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from ..profiles import ColumnProfile
from .fields import FIELDS

PROMPT_VERSION = "mapping-v1"

SYSTEM_PROMPT = """You map the columns of a supplier's farm list onto canonical fields for a deforestation \
due-diligence check. You see only the headers and a profile of each column (shape masks where letters are A/a \
and digits are 9, numeric ranges, decimals, shares); you never see cell values.
Rules:
- Propose exactly one field per column, using only the allowed field names. Use "ignore" when unsure or unused.
- Each field except "ignore" and "notes" may be used by one column only.
- Latitude columns usually hold values within -90..90; a column whose values are mostly in that range but \
also has values over 90 or in the hundreds of thousands still counts (swapped or projected rows exist).
- A confidence from 0 to 1 and one short English reason per column (name the header's meaning in English).
- Answer only by calling the submit_mapping tool once. Do not write anything else."""


def model_payload(headers: list[str], profiles: list[ColumnProfile], country: str) -> dict[str, Any]:
    cols = []
    for p in profiles:
        col: dict[str, Any] = {
            "header": p.header,
            "kind": p.kind,
            "filled": f"{p.non_empty}/{p.rows}",
            "distinct_share": p.distinct_share,
            "patterns": [{"mask": m, "share": s} for m, s in p.patterns],
        }
        if p.number_share:
            col.update(
                number_share=p.number_share,
                range=[p.numeric_min, p.numeric_max],
                decimals=[p.decimals_min, p.decimals_max],
                decimal_comma_share=p.decimal_comma_share,
                dms_share=p.dms_share,
                lat_range_share=p.lat_range_share,
                metres_share=p.metres_share,
            )
        if p.wkt_share:
            col["wkt_share"] = p.wkt_share
        cols.append(col)
    return {"declared_country": country, "headers": headers, "columns": cols}


def user_prompt(payload: dict[str, Any]) -> str:
    fields = "\n".join(f"- {f.name}: {f.description}" for f in FIELDS)
    return (
        f"Allowed fields:\n{fields}\n\n"
        f"Farm list layout (JSON):\n{json.dumps(payload, ensure_ascii=False, indent=1)}\n\n"
        "Call submit_mapping with one entry per column."
    )


def request_key(payload: dict[str, Any]) -> str:
    """Fixture key: the exact payload plus the prompt version, never the file bytes."""
    blob = json.dumps({"v": PROMPT_VERSION, "p": payload}, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]
