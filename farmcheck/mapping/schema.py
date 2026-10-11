"""The mapping proposal schema: the model's only way to answer is a tool call validated against it."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError, field_validator

from .fields import FIELD_NAMES, FieldName


class ColumnProposal(BaseModel):
    column: str = Field(min_length=1, max_length=200)
    field: FieldName
    confidence: float = Field(ge=0.0, le=1.0)
    reason: str = Field(min_length=3, max_length=240)

    @field_validator("reason")
    @classmethod
    def one_line(cls, v: str) -> str:
        return " ".join(v.split())


class MappingProposal(BaseModel):
    columns: list[ColumnProposal] = Field(min_length=1, max_length=200)

    def as_mapping(self) -> dict[str, str]:
        return {c.column: c.field for c in self.columns}


def validate_against_headers(proposal: MappingProposal, headers: list[str]) -> list[str]:
    """Problems that make a proposal unusable for this file; empty when it is fine."""
    problems: list[str] = []
    seen = [c.column for c in proposal.columns]
    for h in headers:
        if h not in seen:
            problems.append(f"column {h!r} has no proposal")
    for c in seen:
        if c not in headers:
            problems.append(f"{c!r} is not a column of this file")
    if len(seen) != len(set(seen)):
        problems.append("a column is listed twice")
    fields = [c.field for c in proposal.columns if c.field not in ("ignore", "notes")]
    dupes = sorted({f for f in fields if fields.count(f) > 1})
    if dupes:
        problems.append(f"fields used by more than one column: {', '.join(dupes)}")
    return problems


def parse_proposal(data: Any, headers: list[str]) -> tuple[MappingProposal | None, list[str]]:
    try:
        proposal = MappingProposal.model_validate(data)
    except ValidationError as exc:
        return None, [f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()[:8]]
    problems = validate_against_headers(proposal, headers)
    return (proposal if not problems else None), problems


TOOL_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "columns": {
            "type": "array",
            "description": "One entry per column of the file, in file order.",
            "items": {
                "type": "object",
                "properties": {
                    "column": {"type": "string", "description": "The header exactly as given"},
                    "field": {"type": "string", "enum": list(FIELD_NAMES)},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    "reason": {"type": "string", "description": "One short English sentence"},
                },
                "required": ["column", "field", "confidence", "reason"],
            },
        }
    },
    "required": ["columns"],
}

Source = Literal["rules", "model", "profile", "person"]
