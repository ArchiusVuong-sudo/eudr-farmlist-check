import pytest

from farmcheck.parsing import ParseError, excel_display_text, parse_area, parse_coordinate, parse_number, parse_wkt


def test_decimal_point_and_comma_keep_shown_decimals():
    a = parse_number("12.683417")
    assert (a.value, a.decimals, a.sig_decimals, a.decimal_comma) == (12.683417, 6, 6, False)
    b = parse_number("-75,812345")
    assert (b.value, b.decimals, b.decimal_comma) == (-75.812345, 6, True)


def test_zero_padding_is_visible_in_significant_decimals():
    p = parse_number("12,683400")
    assert p.decimals == 6 and p.sig_decimals == 4 and p.trailing_zeros == 2


def test_integer_and_metres():
    assert parse_number("1404123").decimals == 0
    assert parse_number("1404123.45").value == pytest.approx(1404123.45)


@pytest.mark.parametrize("text", ["12.7O3341", "N/D", "1,234.5", "", "12..5"])
def test_unreadable_numbers_raise(text):
    with pytest.raises(ParseError):
        parse_coordinate(text)


def test_dms_with_hemisphere_and_precision():
    p = parse_coordinate("2°14'33.123\"N")
    assert p.kind == "dms"
    assert p.value == pytest.approx(2 + 14 / 60 + 33.123 / 3600)
    assert p.decimals == 6  # 3 decimals on seconds are worth 6 decimal-degree digits
    w = parse_coordinate("75°48'12.456\"W")
    assert w.value < 0


def test_dms_whole_seconds_is_low_precision():
    assert parse_coordinate("2°14'33\"N").decimals == 3


def test_dms_rejects_minutes_over_60():
    with pytest.raises(ParseError):
        parse_coordinate("2°75'00\"N")


def test_area_parse():
    assert parse_area("3,50") == 3.5
    with pytest.raises(ParseError):
        parse_area("abc")


@pytest.mark.parametrize(
    ("value", "fmt", "shown"),
    [(12.6834, "0.000000", "12.683400"), (12.6834, "0.0000", "12.6834"), (12.6834, "General", "12.6834"),
     (1404123.45, "0.00", "1404123.45"), (37, "General", "37"), (3.5, "0.00", "3.50")],
)
def test_excel_display_text(value, fmt, shown):
    assert excel_display_text(value, fmt) == shown


def test_wkt_closes_open_ring_and_reads_decimals():
    w = parse_wkt("POLYGON ((-75.1 2.1, -75.0 2.1, -75.0 2.2))")
    assert w.autoclosed and w.polygons[0][0][0] == w.polygons[0][0][-1]
    assert w.decimals == 1
    assert w.to_wkt().startswith("POLYGON ((-75.1 2.1")


def test_wkt_keeps_raw_number_strings():
    w = parse_wkt("POLYGON ((108.100001 12.100001, 108.100900 12.100001, 108.100900 12.100900, 108.100001 12.100001))")
    assert w.decimals == 6 and w.sig_decimals == 4  # '108.100900' is padded


def test_multipolygon():
    w = parse_wkt("MULTIPOLYGON (((0 0, 1 0, 1 1, 0 0)), ((2 2, 3 2, 3 3, 2 2)))")
    assert w.kind == "multipolygon" and len(w.polygons) == 2


@pytest.mark.parametrize("text", ["POINT (1 2)", "POLYGON ((1 2, 3 4", "POLYGON ((0 0, 1 1, 0 0))", "POLYGON ((200 0, 201 0, 201 1, 200 0))", "garbage"])
def test_bad_wkt(text):
    with pytest.raises(ParseError):
        parse_wkt(text)
