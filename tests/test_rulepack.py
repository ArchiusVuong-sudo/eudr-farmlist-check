import re

from farmcheck.rulepack import DEMO_NOTE, available_versions, changed_params, get_pack

ALLOWED_ARTICLES = {"Art. 2(4)", "Art. 2(13)", "Art. 2(28)", "Art. 9(1)(d)"}


def test_both_packs_load_with_checksums():
    assert available_versions() == ["v1", "v2"]
    v1, v2 = get_pack("v1"), get_pack("v2")
    assert v1.checksum and v2.checksum and v1.checksum != v2.checksum


def test_citations_are_only_the_allowed_articles_and_the_rest_say_demo():
    for v in available_versions():
        pack = get_pack(v)
        for key, rule in pack.rules.items():
            if rule.citation:
                arts = set(re.findall(r"Art\. \d+\(\d+\)(?:\([a-z]\))?", rule.citation))
                assert arts and arts <= ALLOWED_ARTICLES, key
            else:
                assert rule.cite == DEMO_NOTE
            for name, prm in rule.params.items():
                assert prm.source == "demo" or prm.source in ALLOWED_ARTICLES, f"{key}.{name}"


def test_v2_is_labelled_as_a_simulated_update():
    assert "Simulated update for this demo, not an EU text" in get_pack("v2").note


def test_v2_changes_exactly_three_rules():
    changed = changed_params(get_pack("v1"), get_pack("v2"))
    assert set(changed) == {
        "zero_padded_precision.severity", "point_area_missing.code", "point_area_missing.severity",
        "point_area_missing.mode", "loss_after_cutoff.min_overlap_ha",
    }
    assert changed["loss_after_cutoff.min_overlap_ha"] == (0.10, 0.02)
    assert get_pack("v1").rule("loss_after_cutoff").p("cutoff_date") == "2020-12-31"
