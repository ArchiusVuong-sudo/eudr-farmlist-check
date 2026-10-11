"""Plain-English summary and next step for Support, built from the trace by templates (no model)."""

from __future__ import annotations

from typing import Any

from .rulepack import RulePack

STATUS_LABELS = {
    "ready": "Ready",
    "repaired": "Repaired, confirm with supplier",
    "review": "Review",
    "needs_fix": "Needs supplier fix",
}


def _ha(x: float | None) -> str:
    return "?" if x is None else f"{x:.2f} ha"


def _repair_sentence(p: dict[str, Any], country: str) -> str | None:
    rep = p.get("repair") or {}
    if rep.get("as_given_ok") or rep.get("n_fit") != 1:
        return None
    kind = rep.get("fit_kind") or ""
    where = f"{p['lat']}, {p['lon']}"
    if kind.startswith("utm_"):
        zone = int(kind.split("_")[1]) - 32600
        return (f"The supplier gave the location in metres; read as UTM zone {zone}N it is the only candidate inside "
                f"{country}, so it was converted to {where} (WGS 84).")
    if kind == "swapped":
        return f"Latitude and longitude were swapped; the swapped pair is the only reading inside {country}, so the point was set to {where}."
    return f"A minus sign was missing; with it added the point is the only reading inside {country}, so it was set to {where}."


def summarize(p: dict[str, Any], issues: list[dict[str, Any]], loss: list[dict[str, Any]], protected: list[dict[str, Any]],
              pack: RulePack, country: str) -> tuple[str, str]:
    code = p["plot_code"]
    status = p["status"]
    by_key = {i["rule_key"]: i for i in issues}
    parts: list[str] = []
    steps: list[str] = []

    if status == "needs_fix":
        errors = [i for i in issues if i["severity"] == "error"]
        parts.append(f"{code} needs a fix from the supplier: " + "; ".join(i["message"].rstrip(".") for i in errors) + ".")
        for i in errors:
            steps.append(pack.rule(i["rule_key"]).fix_hint)
    elif status == "review":
        counted = [h for h in loss if h["counted"]]
        if counted:
            total = sum(h["overlap_ha"] for h in counted)
            top = counted[0]
            parts.append(
                f"{code} was flagged for review because its assessed area overlaps {_ha(total)} of forest loss detected after "
                f"the {pack.rule('loss_after_cutoff').p('cutoff_date')} cut-off (patch {top['id']}, detected {top['detected_on']}, "
                f"confidence {top['confidence']:.2f})."
            )
            steps.append("Review the plot against recent imagery before accepting it")
        if protected and "protected_area" in by_key:
            pa = protected[0]
            parts.append(f"{code} overlaps {_ha(pa['overlap_ha'])} of {pa['name']}.")
            steps.append("Check the plot's location against the protected area")
    elif status == "repaired":
        repaired = [i for i in issues if i["severity"] == "repaired"]
        parts.append(f"{code} was repaired automatically and passes the checks after the repair: "
                     + "; ".join(i["message"].split(". Corrected")[0].rstrip(".") for i in repaired) + ".")
    else:
        warnings = [i for i in issues if i["severity"] == "warning"]
        if warnings:
            parts.append(f"{code} is ready under rule pack {pack.version}, with {len(warnings)} warning"
                         f"{'s' if len(warnings) > 1 else ''}: " + "; ".join(i["message"].rstrip(".") for i in warnings) + ".")
            steps += [pack.rule(i["rule_key"]).fix_hint for i in warnings]
        else:
            parts.append(f"{code} passed every check in rule pack {pack.version}.")

    rep = _repair_sentence(p, country)
    if rep:
        parts.append(rep)
        steps.append("Ask the supplier to confirm the corrected coordinates")
    if "self_intersection" in by_key or "ring_not_closed" in by_key:
        steps.append("Ask the supplier to confirm the repaired boundary")

    basis = p.get("assess_basis")
    if basis == "circle_declared":
        parts.append(f"No boundary was given, so the plot was assessed as a {_ha(p['declared_area_ha'])} circle "
                     f"(radius {p['circle_radius_m']:.0f} m) around the point; that is an assumption.")
        if status in ("review", "repaired"):
            steps.insert(0, "Ask the supplier for the plot boundary polygon")
    elif basis == "circle_assumed" and "point_area_missing" not in by_key:
        parts.append(f"No boundary and no area were given, so the plot was assessed as an assumed "
                     f"{pack.rule('point_area_missing').p('assumed_area_ha')} ha circle around the point.")
        steps.append("Ask the supplier for the plot area or boundary")

    small = [h for h in loss if not h["counted"]]
    if small and status != "needs_fix":
        h = small[0]
        parts.append(f"It also touches loss patch {h['id']} by {_ha(h['overlap_ha'])}, which is {h['why']} in this rule pack.")

    seen: set[str] = set()
    uniq = [s for s in steps if not (s in seen or seen.add(s))]
    uniq = [uniq[0]] + [x[0].lower() + x[1:] for x in uniq[1:]] if uniq else []
    next_step = "; ".join(uniq) + "." if uniq else "No action needed."
    return " ".join(parts), next_step
