from __future__ import annotations

import pytest

from maintdoc.analysis.units import UnitExtractor, parse_number, quantity_signature
from maintdoc.approval import numeric_identity
from maintdoc.config import Config
from maintdoc.utils import numeric_tokens

from conftest import REPO


@pytest.fixture(scope="module")
def ux():
    return UnitExtractor(Config.load(REPO / "config.yaml"))


def one(ux, text, **kw):
    qs = ux.extract(text, **kw)
    assert len(qs) == 1, [q.raw_text for q in qs]
    return qs[0]


def test_parse_number_formats():
    assert parse_number("12.5") == (12.5, [])
    assert parse_number("0,5") == (0.5, ["decimal_comma"])
    v, f = parse_number("1,500")
    assert v == 1500 and "decimal_comma_or_thousands_ambiguous" in f
    assert parse_number("1,500", "comma")[0] == 1.5
    assert parse_number("3/8") == (0.375, ["fraction"])
    assert parse_number("1 1/2")[0] == 1.5


def test_torque_conversion_preserves_original(ux):
    qs = ux.extract("4. Tighten the coupling bolts to 45 Nm (33 ft-lb).")
    assert [q.raw_text for q in qs] == ["45 Nm", "33 ft-lb"]
    nm, ftlb = qs
    assert nm.parameter == ftlb.parameter == "torque"
    assert nm.value_text == "45" and nm.unit_raw == "Nm"          # original kept verbatim
    assert ftlb.canonical_unit == "N*m" and abs(ftlb.canonical_value - 44.742) < 0.01
    assert ftlb.alternate_of == 0 and not any("dual_unit" in f for f in ftlb.flags)
    assert nm.subject == "bolt coupling"


def test_inconsistent_dual_units_flagged(ux):
    qs = ux.extract("Tighten to 50 Nm (30 ft-lb).")
    assert any(f.startswith("dual_unit_inconsistent") for f in qs[1].flags)


def test_missing_unit_and_dimension_mismatch(ux):
    q = one(ux, "Tighten the drain plug to a torque of 25.")
    assert q.conversion_status == "no_unit" and "missing_unit" in q.flags and q.parameter == "torque"
    q = one(ux, "Set the pump coupling torque to 40 bar.")
    assert q.conversion_status == "unsafe" and any(f.startswith("dimension_mismatch") for f in q.flags)


def test_ambiguous_and_assumption_flags(ux):
    q = ux.extract("Oil capacity 1,500 L.")[0]
    assert q.conversion_status == "unsafe"
    q = one(ux, "Allow the oil to cool below 104 °F.")
    assert abs(q.canonical_value - 40.0) < 1e-6 and q.qualifier == "max"
    assert q.conversion_status == "ok_with_assumption" and "absolute_temperature_assumed" in q.flags
    q = one(ux, "Pre-charge pressure 90 bar(g).")
    assert "gauge_pressure" in q.flags and q.conversion_status == "ok"
    q = one(ux, "Set pressure to 3 kg/cm2.")
    assert "kgf_assumed_for_kg" in q.flags and q.conversion_status == "unsafe"
    q = one(ux, "Replace the belts every 6 months.")
    assert q.parameter == "interval" and q.conversion_status == "unsafe"   # calendar month length


def test_ranges_tolerances_intervals(ux):
    q = one(ux, "Operating temperature -10 to 45 °C.")
    assert (q.value_min, q.value_max) == (-10, 45)
    q = one(ux, "Adjust the gap to 0,5 mm ± 0,05 mm.")
    assert q.value == 0.5 and abs(q.value_min - 0.45) < 1e-9 and "decimal_comma" in q.flags
    q = one(ux, "Replace the filter element every 500 operating hours.")
    assert q.parameter == "interval" and q.interval_basis == "operating_hours"
    q = one(ux, "Check the oil level daily.")
    assert q.parameter == "interval" and q.value == 1 and q.unit_norm == "day"


def test_no_false_positives(ux):
    for text in ("Rev. A 2020-01-10 Initial release.", "Document No. MM-HP200 Revision: B",
                 "2. Remove the four coupling guard bolts.", "Order part HF-1001 and SK-2002.",
                 "See Table 3 and Figure 12 on page 4.", "Remove 2 in sequence."):
        assert ux.extract(text) == [], text


def test_table_values_use_unit_column(ux):
    rows = [["Parameter", "Value", "Unit"], ["Maximum operating pressure", "210", "bar"],
            ["Maximum oil temperature", "65", "°C"]]
    qs = ux.extract("", table_rows=rows)
    assert [(q.raw_text, q.parameter, q.subject) for q in qs] == [
        ("210 bar", "pressure", "maximum operating pressure"), ("65 °C", "temperature", "maximum oil temperature")]


def test_numeric_identity(ux):
    cfg = Config.load(REPO / "config.yaml")
    orig = "4. Tighten the coupling bolts to 45 Nm (33 ft-lb)."
    assert numeric_identity(orig, "4. Torque the coupling bolts to 45 N·m (33 ft-lb).", cfg) == []
    assert numeric_identity(orig, "4. Tighten the coupling bolts to 50 Nm (33 ft-lb).", cfg)
    assert numeric_identity(orig, "4. Tighten the coupling bolts to 45 Nm.", cfg)          # number dropped
    assert numeric_identity(orig, "4. Tighten the coupling bolts to 45 bar (33 ft-lb).", cfg)  # unit changed
    assert numeric_tokens("HP-200 at -5 °C") == numeric_tokens("-5 °C HP-200")
    assert quantity_signature(ux.extract("45 Nm torque")) == quantity_signature(ux.extract("torque 45 N·m"))
