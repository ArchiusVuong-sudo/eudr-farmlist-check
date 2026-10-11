"""Versioned rule packs (YAML): severities, thresholds and the citation or 'demo rule' tag of each."""

from __future__ import annotations

import hashlib
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field

from . import RULES_DIR

Severity = Literal["error", "review", "repaired", "warning"]
DEMO_NOTE = "demo rule, not legal advice"
REQUIRED_RULES = (
    "missing_geolocation", "unparseable_value", "coordinate_precision", "zero_padded_precision", "point_over_4ha",
    "point_area_missing", "self_intersection", "ring_not_closed", "coordinate_repair", "outside_country",
    "area_mismatch", "implausible_area", "duplicate_plot", "plot_overlap", "loss_after_cutoff", "protected_area",
)


class Param(BaseModel):
    value: Any
    source: str  # an article reference, or "demo"

    @property
    def is_demo(self) -> bool:
        return self.source == "demo"


class Rule(BaseModel):
    code: str
    title: str
    severity: Severity
    citation: str | None = None
    fix_hint: str
    params: dict[str, Param] = Field(default_factory=dict)

    @property
    def cite(self) -> str:
        return self.citation or DEMO_NOTE

    def p(self, name: str) -> Any:
        return self.params[name].value


class RulePack(BaseModel):
    version: str
    title: str
    note: str
    regulation: str
    rules: dict[str, Rule]
    checksum: str = ""

    def rule(self, key: str) -> Rule:
        return self.rules[key]

    def by_code(self) -> dict[str, tuple[str, Rule]]:
        return {r.code: (k, r) for k, r in self.rules.items()}

    def flat_params(self) -> dict[str, Any]:
        """'rule_key.param' and 'rule_key.severity' -> value, for diffs between packs."""
        out: dict[str, Any] = {}
        for key, r in self.rules.items():
            out[f"{key}.severity"] = r.severity
            out[f"{key}.code"] = r.code
            for name, prm in r.params.items():
                out[f"{key}.{name}"] = prm.value
        return out


def load_pack(path: Path) -> RulePack:
    raw = path.read_bytes()
    data = yaml.safe_load(raw)
    pack = RulePack.model_validate(data)
    missing = [k for k in REQUIRED_RULES if k not in pack.rules]
    if missing:
        raise ValueError(f"rule pack {path.name} lacks rules: {', '.join(missing)}")
    return pack.model_copy(update={"checksum": hashlib.sha256(raw).hexdigest()[:12]})


@lru_cache(maxsize=8)
def get_pack(version: str) -> RulePack:
    path = RULES_DIR / f"{version}.yaml"
    if not path.exists() or not version.isalnum():
        raise KeyError(f"unknown rule pack {version!r}")
    return load_pack(path)


def available_versions() -> list[str]:
    return sorted(p.stem for p in RULES_DIR.glob("*.yaml"))


def changed_params(old: RulePack, new: RulePack) -> dict[str, tuple[Any, Any]]:
    a, b = old.flat_params(), new.flat_params()
    return {k: (a.get(k), b.get(k)) for k in sorted(set(a) | set(b)) if a.get(k) != b.get(k)}
