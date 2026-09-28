"""Numeric value and unit extraction, dimensional checks and Pint conversions.

Principles
----------
* The original printed value is always preserved (``raw_text``/``value_text``).
  Converted values are additional, never replacements.
* Conversions are flagged ``unsafe`` whenever an assumption is involved
  (ambiguous symbol, decimal comma vs thousands separator, gauge vs absolute
  pressure, calendar month length, kgf assumed for "kg", temperature
  differences, ...). Unsafe conversions are not used for automatic
  consistency decisions without a human looking at them.
* A number in a technical context without a unit is flagged (UNIT_MISSING).
* A unit whose dimensionality does not fit the parameter named in the text is
  flagged (UNIT_DIMENSION_MISMATCH).
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

import pint

# (aliases, pint expression, flags, context parameter required)
_UNIT_TABLE: list[tuple[list[str], str, list[str], str | None]] = [
    # torque
    (["N·m", "N⋅m", "N.m", "N-m", "Nm", "Newton metres", "Newton meters", "newton metres", "newton meters"], "N*m", [], None),
    (["kN·m", "kNm", "kN.m", "kN-m"], "kN*m", [], None),
    (["daN·m", "daNm", "daN.m"], "daN*m", [], None),
    (["ft-lbf", "ft·lbf", "ft-lbs", "ft-lb", "ft·lb", "ft.lb", "ft lbs", "ft lb", "lbf·ft", "lbf-ft", "lbf.ft",
      "lb-ft", "lb·ft", "lb.ft", "foot-pounds", "foot pounds", "ftlb"], "lbf*ft", [], None),
    (["in-lbf", "in·lbf", "in-lbs", "in-lb", "in·lb", "in.lb", "lbf·in", "lbf-in", "lb-in", "lb·in", "inch-pounds",
      "inch pounds"], "lbf*inch", [], None),
    (["kgf·m", "kgf-m", "kgf.m", "kp·m", "kpm", "kp-m"], "kgf*m", [], None),
    (["kg·m", "kg-m", "kgm"], "kgf*m", ["kgf_assumed_for_kg"], "torque"),
    (["kgf·cm", "kgf-cm", "kgfcm"], "kgf*cm", [], None),
    (["kg·cm", "kg-cm", "kgcm"], "kgf*cm", ["kgf_assumed_for_kg"], "torque"),
    # pressure
    (["bar(g)", "bar (g)", "barg", "bar g"], "bar", ["gauge_pressure"], None),
    (["bar(a)", "bar (a)", "bara"], "bar", ["absolute_pressure"], None),
    (["mbar"], "millibar", [], None),
    (["bar"], "bar", ["gauge_or_absolute_unspecified"], None),
    (["psig"], "psi", ["gauge_pressure"], None),
    (["psia"], "psi", ["absolute_pressure"], None),
    (["psi"], "psi", ["gauge_or_absolute_unspecified"], None),
    (["kPa"], "kPa", [], None), (["MPa"], "MPa", [], None), (["hPa"], "hPa", [], None), (["Pa"], "Pa", [], None),
    (["N/mm²", "N/mm2", "N/mm^2"], "N/mm**2", [], None),
    (["kgf/cm²", "kgf/cm2", "kp/cm²", "kp/cm2"], "kgf/cm**2", [], None),
    (["kg/cm²", "kg/cm2"], "kgf/cm**2", ["kgf_assumed_for_kg"], None),
    (["atm"], "atm", [], None), (["inHg", "in Hg"], "inHg", [], None), (["mmHg", "mm Hg"], "mmHg", [], None),
    # temperature
    (["°C", "° C", "ºC", "℃", "degC", "deg C", "deg. C", "degrees C", "degrees Celsius", "Celsius"], "degC", [], None),
    (["°F", "° F", "ºF", "℉", "degF", "deg F", "degrees F", "degrees Fahrenheit", "Fahrenheit"], "degF", [], None),
    (["K"], "kelvin", ["single_letter_symbol"], "temperature"),
    # time / intervals
    (["operating hours", "operating hrs", "operating h", "service hours", "running hours", "hours of operation",
      "op. hours", "op hours", "Betriebsstunden", "Bh", "OH"], "hour", ["operating_hours"], None),
    (["hours", "hour", "hrs", "hr", "h"], "hour", [], None),
    (["minutes", "minute", "mins", "min"], "minute", [], None),
    (["seconds", "second", "secs", "sec", "s"], "second", [], None),
    (["days", "day"], "day", ["calendar"], None),
    (["weeks", "week", "wks", "wk"], "week", ["calendar"], None),
    (["months", "month"], "month", ["calendar", "calendar_month_average"], None),
    (["years", "year", "yrs", "yr"], "year", ["calendar", "calendar_year_average"], None),
    # length
    (["µm", "μm", "um", "microns", "micron"], "micrometer", [], None),
    (["mm"], "mm", [], None), (["cm"], "cm", [], None), (["km"], "km", [], None), (["m"], "m", [], None),
    (["inches", "inch", "in."], "inch", [], None),
    (["in"], "inch", ["in_symbol_ambiguous"], "length"),
    (['"', "″"], "inch", ["inch_symbol_ambiguous"], "length"),
    (["feet", "foot", "ft"], "ft", [], None),
    (["thou"], "thou", [], None), (["mil", "mils"], "thou", ["mil_ambiguous"], None),
    # volume
    (["litres", "liters", "litre", "liter", "ltr", "l", "L"], "L", [], None),
    (["ml", "mL"], "mL", [], None),
    (["cm³", "cm3", "ccm", "cc"], "cm**3", [], None),
    (["m³", "m3"], "m**3", [], None), (["dm³", "dm3"], "dm**3", [], None),
    (["US gal", "US gallons", "gal (US)"], "gallon", [], None),
    (["imp gal", "imperial gallons", "gal (UK)"], "imperial_gallon", [], None),
    (["gallons", "gallon", "gal"], "gallon", ["gallon_us_assumed"], None),
    (["quarts", "quart", "qt"], "quart", ["quart_us_assumed"], None),
    # mass
    (["kg"], "kg", [], None), (["g"], "g", [], None), (["mg"], "mg", [], None),
    (["tonnes", "tonne"], "metric_ton", [], None), (["t"], "metric_ton", ["single_letter_symbol"], "mass"),
    (["lbs", "lb"], "lb", [], None), (["oz"], "ounce", [], None),
    # force
    (["kN"], "kN", [], None), (["daN"], "daN", [], None), (["N"], "N", [], None),
    (["lbf"], "lbf", [], None), (["kgf"], "kgf", [], None), (["kp"], "kilopond", [], None),
    # speed / frequency
    (["rpm", "r/min", "rev/min", "min-1", "min⁻¹", "1/min", "U/min"], "rpm", [], None),
    (["kHz"], "kHz", [], None), (["Hz"], "Hz", [], None),
    # flow
    (["l/min", "L/min", "lpm", "ltr/min"], "L/min", [], None), (["l/h", "L/h"], "L/h", [], None),
    (["m³/h", "m3/h"], "m**3/h", [], None), (["gpm", "GPM"], "gallon/minute", ["gallon_us_assumed"], None),
    # electrical / power
    (["kVA"], "kV*A", [], None), (["kV"], "kV", [], None), (["mV"], "mV", [], None),
    (["VAC", "VDC", "V AC", "V DC", "V"], "V", [], None),
    (["mA"], "mA", [], None), (["kA"], "kA", [], None), (["A"], "A", ["single_letter_symbol"], None),
    (["MW"], "MW", [], None), (["kW"], "kW", [], None), (["W"], "W", [], None),
    (["hp", "HP", "bhp"], "hp", ["horsepower_mechanical_assumed"], None),
    (["kΩ", "kohm"], "kiloohm", [], None), (["Ω", "ohms", "ohm"], "ohm", [], None),
    # viscosity
    (["cSt", "mm²/s", "mm2/s"], "mm**2/s", [], None),
    (["cP", "mPa·s", "mPas", "mPa s"], "cP", [], None),
    # misc
    (["%"], "percent", [], None),
    (["°", "deg", "degrees"], "degree", ["degree_symbol_angle_assumed"], None),
]

_ALIAS: dict[str, tuple[str, list[str], str | None]] = {}
for _aliases, _expr, _flags, _ctx in _UNIT_TABLE:
    for _a in _aliases:
        _ALIAS.setdefault(_a, (_expr, _flags, _ctx))

# Case-sensitive, longest alias first; must not be followed by a letter/digit
_UNIT_ALT = "|".join(re.escape(a).replace(r"\ ", r"\s?") for a in sorted(_ALIAS, key=len, reverse=True))
_NUM = r"(?:\d{1,3}(?:[,  ]\d{3})+(?:\.\d+)?|\d+(?:[.,]\d+)?)"
_FRACTION = r"(?:\d+\s+\d+/\d+|\d+/\d+)"

QTY_RE = re.compile(
    r"(?P<qual>(?:max(?:imum)?|min(?:imum)?|approx(?:imately)?|ca|nominal|not\s+(?:to\s+)?exceed(?:ing)?|"
    r"up\s+to|at\s+least|below|above|under|over)\.?\s*[:=]?\s*|[≤≥<>]\s*)?"
    r"(?<![A-Za-z0-9_./\-])(?P<sign>[-−+](?=\d))?"
    rf"(?P<num1>{_FRACTION}|{_NUM})"
    rf"(?:\s*(?:–|—|-|to|\.\.\.|…)\s*(?P<num2>{_NUM}))?"
    rf"(?:\s*(?:±|\+/-|\+/−|\+-)\s*(?P<tol>{_NUM}))?"
    rf"(?:\s*(?P<unit>{_UNIT_ALT})(?![A-Za-z0-9²³]))?"
    rf"(?:\s*(?:±|\+/-|\+-)\s*(?P<tol2>{_NUM})\s*(?:{_UNIT_ALT})?(?![A-Za-z0-9]))?",
)
_DATE_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b|\b\d{1,2}[./]\d{1,2}[./]\d{2,4}\b|\b\d{1,2}:\d{2}(?::\d{2})?\b")
# step/list numbers at the start of a statement: "4. Tighten", "(2) Remove", "Step 3 Fit"
_LIST_MARK = re.compile(r"^\s*(?:\(?(\d{1,3}(?:\.\d{1,3})*)[.)]|(?i:step)\s*(\d{1,3})\b[:.)]?)\s")
_REF_BEFORE = re.compile(r"(?i)(?:table|tab|figure|fig|page|p|step|rev|revision|item|no|nr|section|chapter|"
                         r"sheet|drawing|pos|position|issue|version|ver|qty|quantity|x|size|class|grade|iso|din|"
                         r"en|sae|api|nlgi|vg|ep|type|model|series|part|p/n|pn|art|order)\.?\s*[:#]?\s*$")
_WORD_INTERVALS = {"daily": (1, "day"), "weekly": (1, "week"), "fortnightly": (2, "week"), "monthly": (1, "month"),
                   "quarterly": (3, "month"), "annually": (1, "year"), "yearly": (1, "year"),
                   "semi-annually": (6, "month"), "hourly": (1, "hour")}
_WORD_INTERVAL_RE = re.compile(r"(?i)\b(" + "|".join(re.escape(k) for k in _WORD_INTERVALS) + r")\b")
_INTERVAL_CUES = re.compile(r"(?i)\b(every|each|interval|intervals|after\s+(?:the\s+first\s+)?|once\s+per|per|"
                            r"alle|nach)\b")
_DIFF_CUES = re.compile(r"(?i)\b(increase|decrease|rise|drop|difference|delta|by|raise|lower)\b|Δ")

# representative unit per physical parameter; dimensionality is compared structurally
_PARAM_REPRESENTATIVE = {
    "torque": "N*m", "pressure": "Pa", "temperature": "kelvin", "time": "second", "length": "m",
    "volume": "m**3", "mass": "kg", "force": "N", "speed": "1/s", "flow": "m**3/s", "viscosity": "m**2/s",
    "viscosity_dynamic": "Pa*s", "power": "W", "current": "A", "voltage": "V", "resistance": "ohm",
    "dimensionless": "percent",
}
# parameters that are the same physical dimension (checked against keywords)
_COMPATIBLE = {"interval": {"time"}, "duration": {"time"}, "frequency": {"speed"}, "speed": {"speed"}}


@lru_cache(maxsize=1)
def ureg() -> pint.UnitRegistry:
    reg = pint.UnitRegistry(autoconvert_offset_to_baseunit=False)
    return reg


def _dim_items(expr: str) -> frozenset:
    return frozenset(dict(ureg().Quantity(1, expr).dimensionality).items())


@lru_cache(maxsize=1)
def _param_by_dim() -> dict[frozenset, str]:
    return {_dim_items(u): p for p, u in _PARAM_REPRESENTATIVE.items()}


@lru_cache(maxsize=256)
def _dim_key(expr: str) -> str:
    """Canonical, order-independent dimensionality string, e.g. '[length]^2*[mass]*[time]^-2'."""
    items = sorted(_dim_items(expr))
    return "*".join(f"{k}^{v:g}" if v != 1 else k for k, v in items) or "dimensionless"


def param_for_expr(expr: str) -> str:
    return _param_by_dim().get(_dim_items(expr), _dim_key(expr))


@dataclass
class Quantity:
    raw_text: str
    value_text: str
    value: float | None
    value_min: float | None = None
    value_max: float | None = None
    tolerance: float | None = None
    unit_raw: str | None = None
    unit_norm: str | None = None
    dimensionality: str | None = None
    parameter: str | None = None
    interval_basis: str | None = None
    qualifier: str | None = None
    subject: str | None = None
    canonical_unit: str | None = None
    canonical_value: float | None = None
    canonical_min: float | None = None
    canonical_max: float | None = None
    conversion_status: str = "no_unit"
    flags: list[str] = field(default_factory=list)
    alternate_of: int | None = None
    char_start: int | None = None
    char_end: int | None = None

    def as_row(self) -> dict[str, Any]:
        return dict(self.__dict__)


def parse_number(text: str, mode: str = "auto") -> tuple[float | None, list[str]]:
    """Parse a printed number. Returns (value, flags)."""
    flags: list[str] = []
    t = text.replace(" ", "").replace(" ", "").strip()
    if "/" in t:
        parts = t.split()
        try:
            if len(parts) == 2:
                n, d = parts[1].split("/")
                return float(parts[0]) + float(n) / float(d), ["fraction"]
            n, d = t.split("/")
            return float(n) / float(d), ["fraction"]
        except (ValueError, ZeroDivisionError):
            return None, ["unparsable_number"]
    if re.fullmatch(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?", t):
        # "1,500" is 1500 (thousands) in English documents but 1.5 with a decimal comma
        if mode == "comma" and "." not in t and t.count(",") == 1:
            return float(t.replace(",", ".")), ["decimal_comma"]
        if mode == "auto" and "." not in t and t.count(",") == 1:
            flags.append("decimal_comma_or_thousands_ambiguous")
        return float(t.replace(",", "")), flags
    if re.fullmatch(r"\d+,\d+", t):
        if mode == "point":
            return float(t.replace(",", "")), ["comma_read_as_thousands"]
        return float(t.replace(",", ".")), ["decimal_comma"]
    try:
        return float(t), flags
    except ValueError:
        return None, ["unparsable_number"]


class UnitExtractor:
    def __init__(self, cfg):
        self.cfg = cfg
        self.decimal_mode = cfg.get("units.decimal_separator", "auto")
        self.window = int(cfg.get("units.context_window_chars", 80))
        self.dual_tol = float(cfg.get("units.dual_unit_tolerance", 0.03))
        self.canonical = dict(cfg.get("units.canonical_units", {}) or {})
        kws = cfg.get("units.parameter_keywords", {}) or {}
        self.kw_res: list[tuple[str, re.Pattern]] = []
        for param, words in kws.items():
            if words:
                alt = "|".join(re.escape(w) for w in sorted(words, key=len, reverse=True))
                self.kw_res.append((param, re.compile(rf"(?i)(?<![\w])(?:{alt})(?![\w])")))
        self.stop = {w.lower() for w in cfg.get("conflicts.stopwords", []) or []}
        self.synonyms = {k.lower(): str(v).lower() for k, v in (cfg.get("conflicts.synonyms", {}) or {}).items()}

    # ------------------------------------------------------------------ context helpers
    def _keywords_before(self, text: str, pos: int, floor: int = 0) -> list[tuple[int, str]]:
        start = max(0, floor, pos - self.window)
        seg = text[start:pos]
        # stay within the sentence / clause and after the previous value
        cut = max(seg.rfind(". "), seg.rfind("; "), seg.rfind("\n"), seg.rfind(", "))
        if cut >= 0:
            seg = seg[cut + 1:]
            start = pos - len(seg)
        hits = []
        for param, rx in self.kw_res:
            for m in rx.finditer(seg):
                hits.append((start + m.start(), param))
        return sorted(hits)

    def subject_of(self, text: str, pos: int, section_heading: str | None = None) -> str:
        seg = text[:pos]
        cut = max(seg.rfind(". "), seg.rfind("; "), seg.rfind("\n"), seg.rfind(", "))
        clause = seg[cut + 1:] if cut >= 0 else seg
        clause = QTY_RE.sub(" ", clause)
        tokens = self.subject_tokens(clause)
        if len(tokens) <= 1 and section_heading:
            tokens = sorted(set(tokens) | set(self.subject_tokens(section_heading)))
        return " ".join(tokens)

    def subject_tokens(self, text: str) -> list[str]:
        out = []
        for tok in re.findall(r"[a-z][a-z\-]+", unicodedata.normalize("NFKC", text).lower()):
            tok = tok.strip("-")
            if not tok or tok in self.stop or tok in _ALIAS:
                continue
            tok = self.synonyms.get(tok, tok)
            if len(tok) > 4 and tok.endswith("s") and not tok.endswith(("ss", "us", "is")):
                tok = tok[:-1]
            tok = self.synonyms.get(tok, tok)
            if tok not in self.stop:
                out.append(tok)
        return sorted(set(out))

    # ------------------------------------------------------------------ extraction
    def extract(self, text: str, section_heading: str | None = None,
                table_rows: list[list[str | None]] | None = None) -> list[Quantity]:
        if table_rows:
            return self.extract_table(table_rows, section_heading)
        return self._extract_text(text, section_heading)

    def _masked(self, text: str) -> str:
        return _DATE_RE.sub(lambda m: "#" * len(m.group(0)), text)

    def _extract_text(self, text: str, section_heading: str | None, subject_override: str | None = None,
                      offset: int = 0) -> list[Quantity]:
        masked = self._masked(text)
        out: list[Quantity] = []
        list_mark = _LIST_MARK.match(masked)
        prev_end = 0
        for m in QTY_RE.finditer(masked):
            start = m.start("num1") - (1 if m.group("sign") else 0)
            unit = m.group("unit")
            if list_mark and m.start("num1") in (list_mark.start(1), list_mark.start(2)):
                continue  # step / list number
            before = masked[max(0, start - 20):start]
            if _REF_BEFORE.search(before) and not unit:
                continue
            if start > 0 and masked[start - 1] in "#":
                continue
            q = self._build(text, m, start, unit, section_heading, subject_override, prev_end)
            prev_end = m.end()
            if q is None:
                continue
            q.char_start, q.char_end = start + offset, m.end() + offset
            out.append(q)
        self._link_dual_units(out, text)
        # word intervals ("daily", "weekly")
        for m in _WORD_INTERVAL_RE.finditer(text):
            val, unit = _WORD_INTERVALS[m.group(1).lower()]
            q = Quantity(raw_text=m.group(0), value_text=m.group(0), value=float(val), unit_raw=m.group(0),
                         unit_norm=unit, parameter="interval", interval_basis="calendar",
                         flags=["interval_from_word", "calendar"], char_start=m.start() + offset,
                         char_end=m.end() + offset)
            q.subject = subject_override or self.subject_of(text, m.start(), section_heading)
            self._convert(q, unit)
            out.append(q)
        return out

    def _build(self, text: str, m: re.Match, start: int, unit: str | None, section_heading: str | None,
               subject_override: str | None, prev_end: int = 0) -> Quantity | None:
        flags: list[str] = []
        num1 = m.group("num1")
        v1, f1 = parse_number(num1, self.decimal_mode)
        flags.extend(f1)
        if v1 is None:
            return None
        if m.group("sign") and m.group("sign") in "-−":
            v1 = -v1
        v2 = None
        if m.group("num2"):
            v2, f2 = parse_number(m.group("num2"), self.decimal_mode)
            flags.extend(f2)
        tol = None
        tol_txt = m.group("tol") or m.group("tol2")
        if tol_txt:
            tol, ft = parse_number(tol_txt, self.decimal_mode)
            flags.extend(ft)
        kw = self._keywords_before(text, start, prev_end)
        kw_params = [p for _, p in kw]
        nearest_kw = kw[-1][1] if kw else None
        interval_cue = bool(_INTERVAL_CUES.search(text[max(0, start - 40):start]))
        qual = (m.group("qual") or "").strip().lower().rstrip(".:= ")
        qualifier = None
        if qual:
            if qual.startswith(("max", "not", "up", "below", "under", "≤", "<")):
                qualifier = "max"
            elif qual.startswith(("min", "at least", "above", "over", "≥", ">")):
                qualifier = "min"
            elif qual.startswith(("approx", "ca")):
                qualifier = "approx"
            elif qual.startswith("nominal"):
                qualifier = "nominal"
        if unit is None:
            # numbers without a unit are only reported in a technical parameter context
            technical = [p for p in kw_params if p not in ("interval",)]
            if not technical:
                return None
            if num1.count(".") > 1 or re.fullmatch(r"\d+\.\d+\.\d+", num1):
                return None
            raw = text[start:m.end()].strip()
            return Quantity(raw_text=raw, value_text=num1, value=v1, value_min=min(v1, v2) if v2 is not None else None,
                            value_max=max(v1, v2) if v2 is not None else None, tolerance=tol, parameter=nearest_kw,
                            qualifier=qualifier, conversion_status="no_unit",
                            subject=subject_override or self.subject_of(text, start, section_heading),
                            flags=sorted(set(flags + ["missing_unit"])))
        unit_key = re.sub(r"\s+", " ", unit)
        entry = _ALIAS.get(unit_key) or _ALIAS.get(unit_key.replace(" ", "")) or _lookup_spaced(unit)
        if entry is None:
            return None
        expr, uflags, ctx_needed = entry
        if ctx_needed and ctx_needed not in kw_params:
            if ctx_needed in ("length", "mass", "temperature", "torque"):
                return None  # e.g. "2 in sequence", "5 t" without context - not a quantity
        flags.extend(uflags)
        dim = _dim_key(expr)
        dim_param = param_for_expr(expr)
        parameter = dim_param
        if dim_param == "time":
            if "operating_hours" in flags or interval_cue or nearest_kw == "interval":
                parameter = "interval"
            else:
                parameter = "duration"
        if dim_param == "speed" and expr in ("Hz", "kHz"):
            parameter = "frequency"
        if dim_param == "dimensionless" and expr == "degree":
            parameter = "angle"
            if "temperature" in kw_params:
                flags.append("degree_symbol_with_temperature_context")
        elif dim_param == "dimensionless":
            parameter = "percentage"
        # dimensional plausibility against the text
        if nearest_kw and nearest_kw not in ("interval",):
            expected = nearest_kw
            acceptable = {expected} | _COMPATIBLE.get(expected, set())
            if dim_param not in acceptable and parameter not in acceptable and dim_param not in kw_params \
                    and parameter not in kw_params:
                flags.append(f"dimension_mismatch:{expected}_vs_{dim_param}")
        basis = None
        if parameter == "interval":
            basis = "operating_hours" if "operating_hours" in flags else (
                "calendar" if "calendar" in flags else "ambiguous_hours" if expr == "hour" else "calendar")
        v_min = v_max = None
        if v2 is not None:
            v_min, v_max = min(v1, v2), max(v1, v2)
        elif tol is not None:
            v_min, v_max = v1 - tol, v1 + tol
        q = Quantity(raw_text=text[start:m.end()].strip(), value_text=(m.group("sign") or "") + num1, value=v1,
                     value_min=v_min, value_max=v_max, tolerance=tol, unit_raw=unit, unit_norm=expr,
                     dimensionality=dim, parameter=parameter, interval_basis=basis, qualifier=qualifier,
                     subject=subject_override or self.subject_of(text, start, section_heading),
                     flags=sorted(set(flags)))
        if parameter == "temperature" and _DIFF_CUES.search(text[max(0, start - 40):start]):
            q.flags.append("temperature_difference_possible")
        self._convert(q, expr)
        return q

    def _convert(self, q: Quantity, expr: str) -> None:
        target = self.canonical.get(q.parameter or "") or self.canonical.get(
            "interval" if q.parameter in ("interval", "duration") else "")
        if not target:
            q.conversion_status = "not_convertible" if q.parameter not in ("percentage", "angle") else "ok"
            return
        try:
            reg = ureg()
            def conv(v):
                return None if v is None else float(reg.Quantity(v, expr).to(target).magnitude)
            if q.parameter == "temperature" and q.tolerance is not None:
                # a tolerance is a temperature difference: convert it separately (no offset)
                q.canonical_value = conv(q.value)
                scale = float(reg.Quantity(1, "delta_" + expr if expr in ("degC", "degF") else expr)
                              .to("delta_" + target if target in ("degC", "degF") else target).magnitude)
                q.canonical_min = q.canonical_value - q.tolerance * scale
                q.canonical_max = q.canonical_value + q.tolerance * scale
            else:
                q.canonical_value = conv(q.value)
                q.canonical_min = conv(q.value_min)
                q.canonical_max = conv(q.value_max)
            q.canonical_unit = target
        except (pint.errors.DimensionalityError, pint.errors.OffsetUnitCalculusError,
                pint.errors.UndefinedUnitError) as exc:
            q.conversion_status = "not_convertible"
            q.flags.append(f"conversion_error:{type(exc).__name__}")
            return
        unsafe = {"decimal_comma_or_thousands_ambiguous", "kgf_assumed_for_kg", "calendar_month_average",
                  "calendar_year_average", "gallon_us_assumed", "quart_us_assumed", "in_symbol_ambiguous",
                  "inch_symbol_ambiguous", "mil_ambiguous", "single_letter_symbol", "temperature_difference_possible",
                  "degree_symbol_with_temperature_context", "horsepower_mechanical_assumed", "fraction"}
        assumed = {"gauge_or_absolute_unspecified", "decimal_comma", "interval_from_word"}
        mismatch = any(f.startswith("dimension_mismatch") for f in q.flags)
        if mismatch or unsafe & set(q.flags):
            q.conversion_status = "unsafe"
        elif q.parameter == "temperature" and expr != target:
            q.conversion_status = "ok_with_assumption"
            q.flags.append("absolute_temperature_assumed")
        elif assumed & set(q.flags):
            q.conversion_status = "ok_with_assumption"
        else:
            q.conversion_status = "ok"
        q.flags = sorted(set(q.flags))

    def _link_dual_units(self, qs: list[Quantity], text: str) -> None:
        """'45 Nm (33 ft-lb)': second value restates the first in another unit - check they agree."""
        for i in range(1, len(qs)):
            a, b = qs[i - 1], qs[i]
            if a.unit_norm is None or b.unit_norm is None or a.dimensionality != b.dimensionality:
                continue
            between = text[a.char_end:b.char_start] if a.char_end is not None and b.char_start is not None else ""
            if not re.fullmatch(r"\s*[(\[/=]\s*|\s*(?:or|bzw\.?|resp\.?)\s*", between):
                continue
            b.alternate_of = i - 1
            b.subject = a.subject
            if a.canonical_value is not None and b.canonical_value is not None and a.canonical_value != 0:
                rel = abs(a.canonical_value - b.canonical_value) / abs(a.canonical_value)
                if rel > self.dual_tol:
                    b.flags = sorted(set(b.flags + [f"dual_unit_inconsistent:{rel:.1%}"]))
                    a.flags = sorted(set(a.flags + [f"dual_unit_inconsistent:{rel:.1%}"]))

    def extract_table(self, rows: list[list[str | None]], section_heading: str | None) -> list[Quantity]:
        """Tables: combine value and unit columns; the row label is the subject."""
        if not rows:
            return []
        header = [(c or "").strip().lower() for c in rows[0]]
        unit_col = next((i for i, h in enumerate(header) if h in ("unit", "units", "einheit", "unité", "dim.")), None)
        out: list[Quantity] = []
        body = rows[1:] if any(header) else rows
        for r in body:
            cells = [(c or "").strip() for c in r]
            if not any(cells):
                continue
            label_cells = [c for i, c in enumerate(cells) if c and i != unit_col and not re.search(r"\d", c)]
            label = " ".join(label_cells[:2])
            subject = " ".join(self.subject_tokens(label)) or None
            unit = cells[unit_col] if unit_col is not None and unit_col < len(cells) else ""
            for i, c in enumerate(cells):
                if i == unit_col or not c or not re.search(r"\d", c):
                    continue
                if unit and re.fullmatch(rf"[≤≥<>]?\s*[-−+]?(?:{_FRACTION}|{_NUM})"
                                         rf"(?:\s*(?:-|–|to)\s*{_NUM})?(?:\s*(?:±|\+/-)\s*{_NUM})?", c):
                    text = f"{label}: {c} {unit}"
                    qs = self._extract_text(text, section_heading, subject_override=subject)
                    for q in qs:
                        q.raw_text = f"{c} {unit}"
                        q.char_start = q.char_end = None
                        q.flags = sorted(set(q.flags + ["from_table"]))
                    out.extend(qs)
                else:
                    text = f"{label}: {c}" if label else c
                    qs = self._extract_text(text, section_heading, subject_override=subject)
                    for q in qs:
                        q.char_start = q.char_end = None
                        q.flags = sorted(set(q.flags + ["from_table"]))
                    out.extend(q for q in qs if q.unit_norm is not None)
        return out


def _lookup_spaced(unit: str):
    compact = re.sub(r"\s+", "", unit)
    for alias, entry in _ALIAS.items():
        if re.sub(r"\s+", "", alias) == compact:
            return entry
    return None


def quantity_signature(qs: list[Quantity]) -> list[tuple[str, str | None]]:
    """Order-independent identity used for numeric identity checks (value as printed + normalised unit)."""
    return sorted(((q.value_text or "").replace("−", "-"), q.unit_norm) for q in qs)
