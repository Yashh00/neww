"""YAML-configurable, rule-based classification.

* :class:`DocumentClassifier` - document-level equipment, model, component,
  revision, date, document number and title (from filename, folder path, PDF
  metadata and first-page text). Every value records where it came from.
* :class:`ChapterClassifier` - assigns each evidence item to a logical manual
  chapter using heading context, keywords, regexes and block types.
* :func:`applicability` - evidence-level equipment/model/component.

No statistical or generative models are used; every decision is explainable
by the rule that fired.
"""

from __future__ import annotations

import datetime as _dt
import re
from dataclasses import dataclass, field
from typing import Any

from maintdoc.config import Config
from maintdoc.constants import UNCLASSIFIED_CHAPTER


def _kw_regex(keywords: list[str]) -> re.Pattern | None:
    kws = [k.strip() for k in keywords or [] if k and k.strip()]
    if not kws:
        return None
    parts = []
    for k in sorted(kws, key=len, reverse=True):
        esc = re.escape(k).replace(r"\ ", r"\s+")
        # allow simple plurals on the final word
        if k[-1].isalpha():
            esc += r"(?:s|es)?"
        parts.append(esc)
    return re.compile(r"(?<![\w])(?:" + "|".join(parts) + r")(?![\w])", re.IGNORECASE)


def _compile_patterns(patterns: list[str], flags: int = re.IGNORECASE | re.MULTILINE) -> list[re.Pattern]:
    out = []
    for p in patterns or []:
        out.append(re.compile(p, flags))
    return out


def _join(values) -> str | None:
    vals = sorted({v for v in values if v})
    return "; ".join(vals) if vals else None


def split_multi(value: str | None) -> set[str]:
    if not value:
        return set()
    return {v.strip() for v in str(value).split(";") if v.strip()}


# --------------------------------------------------------------------------- documents
_MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august", "september",
     "october", "november", "december"], start=1)}
_MONTHS.update({k[:3]: v for k, v in list(_MONTHS.items())})


def normalize_date(raw: str) -> tuple[str, bool]:
    """Return (normalised value, ambiguous). ISO when unambiguous, else the raw string."""
    s = raw.strip()
    m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", s)
    if m:
        try:
            return _dt.date(int(m[1]), int(m[2]), int(m[3])).isoformat(), False
        except ValueError:
            return s, True
    m = re.fullmatch(r"(\d{1,2})[./](\d{1,2})[./](\d{4})", s)
    if m:
        a, b, y = int(m[1]), int(m[2]), int(m[3])
        if a > 12 and b <= 12:
            return _safe_iso(y, b, a, s)
        if b > 12 and a <= 12:
            return _safe_iso(y, a, b, s)
        if "." in s:  # dotted dates are conventionally day-first
            v, _ = _safe_iso(y, b, a, s)
            return v, a != b
        return s, a != b
    m = re.fullmatch(r"(\d{1,2})\s+([A-Za-z]+)\.?\s+(\d{4})", s)
    if m and m[2].lower() in _MONTHS:
        return _safe_iso(int(m[3]), _MONTHS[m[2].lower()], int(m[1]), s)
    m = re.fullmatch(r"([A-Za-z]+)\.?\s+(\d{1,2}),?\s+(\d{4})", s)
    if m and m[1].lower() in _MONTHS:
        return _safe_iso(int(m[3]), _MONTHS[m[1].lower()], int(m[2]), s)
    m = re.fullmatch(r"([A-Za-z]+)\.?\s+(\d{4})", s)
    if m and m[1].lower() in _MONTHS:
        return f"{int(m[2]):04d}-{_MONTHS[m[1].lower()]:02d}", False
    return s, False


def _safe_iso(y: int, mo: int, d: int, raw: str) -> tuple[str, bool]:
    try:
        return _dt.date(y, mo, d).isoformat(), False
    except ValueError:
        return raw, True


@dataclass
class DocMeta:
    values: dict[str, str | None] = field(default_factory=dict)
    origins: dict[str, dict[str, Any]] = field(default_factory=dict)
    ambiguous: dict[str, list[str]] = field(default_factory=dict)


class DocumentClassifier:
    FIELDS = ("equipment", "model", "component", "revision", "doc_date", "doc_number", "doc_title")

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.equipment = []
        for eq in cfg.get("classification.equipment", []) or []:
            models = [{"name": m["name"], "patterns": _compile_patterns(m.get("patterns") or [re.escape(m["name"])])}
                      for m in eq.get("models", []) or []]
            self.equipment.append({"name": eq["name"], "kw": _kw_regex(eq.get("keywords", []) + [eq["name"]]),
                                   "models": models})
        self.components = [{"name": c["name"], "kw": _kw_regex(c.get("keywords", []) + [c["name"]]),
                            "patterns": _compile_patterns(c.get("patterns", []))}
                           for c in cfg.get("classification.components", []) or []]
        self.rev_patterns = [re.compile(p) for p in cfg.get("metadata.revision_patterns", [])]
        self.date_patterns = [re.compile(p) for p in cfg.get("metadata.date_patterns", [])]
        self.docno_patterns = [re.compile(p) for p in cfg.get("metadata.doc_number_patterns", [])]

    # ---- helpers usable for evidence-level applicability as well
    def find_models(self, text: str) -> list[tuple[str, str]]:
        """Return [(equipment, model)] mentioned in text."""
        found = []
        for eq in self.equipment:
            for m in eq["models"]:
                if any(p.search(text) for p in m["patterns"]):
                    found.append((eq["name"], m["name"]))
        return found

    def find_equipment(self, text: str) -> list[str]:
        return [eq["name"] for eq in self.equipment if eq["kw"] is not None and eq["kw"].search(text)]

    def find_components(self, text: str) -> list[tuple[str, int]]:
        out = []
        for c in self.components:
            n = len(c["kw"].findall(text)) if c["kw"] is not None else 0
            n += sum(len(p.findall(text)) for p in c["patterns"])
            if n:
                out.append((c["name"], n))
        return sorted(out, key=lambda x: -x[1])

    def _first(self, patterns: list[re.Pattern], text: str) -> list[str]:
        vals = []
        for p in patterns:
            for m in p.finditer(text):
                v = (m.group(1) if m.groups() else m.group(0)).strip()
                if v and v not in vals:
                    vals.append(v)
        return vals

    def classify(self, rel_path: str, pdf_meta: dict[str, Any], first_text: str,
                 first_page_title: str | None = None) -> DocMeta:
        meta = DocMeta()
        name = rel_path.rsplit("/", 1)[-1]
        stem = re.sub(r"\.pdf$", "", name, flags=re.IGNORECASE)
        fname_text = re.sub(r"[_]+", " ", stem)
        folder_text = re.sub(r"[_/\\]+", " ", rel_path[: -len(name)]) if len(rel_path) > len(name) else ""
        meta_text = " ".join(str(pdf_meta.get(k) or "") for k in ("title", "subject", "keywords"))
        sources = [("filename", fname_text), ("folder", folder_text), ("pdf_metadata", meta_text),
                   ("page_text", first_text or "")]

        def set_field(fname: str, candidates_by_origin: list[tuple[str, list[str]]], join_all: bool = False):
            all_vals: list[str] = []
            for origin, vals in candidates_by_origin:
                if vals:
                    if fname not in meta.values or meta.values[fname] is None:
                        meta.values[fname] = _join(vals) if join_all else vals[0]
                        meta.origins[fname] = {"value": meta.values[fname], "origin": origin, "candidates": vals}
                    all_vals.extend(vals)
            distinct = sorted(set(all_vals), key=all_vals.index)
            if not join_all and len(distinct) > 1:
                meta.ambiguous[fname] = distinct
            meta.values.setdefault(fname, None)

        # models (a document can legitimately cover several models)
        model_hits = []
        for origin, text in sources:
            hits = self.find_models(text)
            if hits:
                model_hits.append((origin, hits))
                break
        if model_hits:
            origin, hits = model_hits[0]
            meta.values["model"] = _join(m for _, m in hits)
            meta.values["equipment"] = _join(e for e, _ in hits)
            meta.origins["model"] = {"value": meta.values["model"], "origin": origin}
            meta.origins["equipment"] = {"value": meta.values["equipment"], "origin": origin + " (via model)"}
        else:
            set_field("equipment", [(o, self.find_equipment(t)) for o, t in sources])
            meta.values.setdefault("model", None)

        # component: filename/folder/title first; page text only if dominant
        comp = None
        for origin, text in sources:
            hits = self.find_components(text)
            if hits and (origin != "page_text" or hits[0][1] >= 2):
                comp = (origin, hits[0][0], [h[0] for h in hits])
                break
        if comp:
            meta.values["component"] = comp[1]
            meta.origins["component"] = {"value": comp[1], "origin": comp[0], "candidates": comp[2]}
        else:
            meta.values["component"] = None

        set_field("revision", [(o, self._first(self.rev_patterns, t)) for o, t in sources])
        date_cands = []
        for o, t in sources:
            raw = self._first(self.date_patterns, t)
            date_cands.append((o, [normalize_date(r)[0] for r in raw]))
        if pdf_meta.get("creationDate") and not any(v for _, v in date_cands):
            pass  # PDF creation date is not the document issue date; never substituted silently
        set_field("doc_date", date_cands)
        set_field("doc_number", [(o, self._first(self.docno_patterns, t)) for o, t in sources])

        title = (pdf_meta.get("title") or "").strip()
        title = re.sub(r"^Microsoft (Word|Excel|PowerPoint) - ", "", title)
        if title and title.lower() not in ("untitled", "document", stem.lower()) and len(title) > 3:
            meta.values["doc_title"] = title[: self.cfg.get("metadata.title_max_chars", 160)]
            meta.origins["doc_title"] = {"value": meta.values["doc_title"], "origin": "pdf_metadata"}
        elif first_page_title:
            meta.values["doc_title"] = first_page_title[: self.cfg.get("metadata.title_max_chars", 160)]
            meta.origins["doc_title"] = {"value": meta.values["doc_title"], "origin": "page_text"}
        else:
            meta.values["doc_title"] = fname_text
            meta.origins["doc_title"] = {"value": fname_text, "origin": "filename"}
        return meta


# --------------------------------------------------------------------------- chapters
@dataclass
class ChapterResult:
    chapter: str
    score: float
    rule: str
    status: str  # classified | ambiguous | unclassified


class ChapterClassifier:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        w = cfg.get("classification.weights", {})
        self.w_section = float(w.get("section_heading", 4.0))
        self.w_ancestor = float(w.get("ancestor_heading", 2.0))
        self.w_own = float(w.get("own_heading", 5.0))
        self.w_kw = float(w.get("keyword", 1.0))
        self.kw_cap = float(w.get("keyword_cap", 4.0))
        self.w_pat = float(w.get("pattern", 2.0))
        self.w_block = float(w.get("block_type", 3.0))
        self.min_score = float(cfg.get("classification.min_score", 2.0))
        self.margin = float(cfg.get("classification.ambiguity_margin", 0.5))
        self.chapters = []
        for order, ch in enumerate(cfg.chapters()):
            self.chapters.append({
                "id": ch["id"],
                "order": order,
                "heading": _kw_regex(ch.get("heading_keywords", [])),
                "kw": _kw_regex(ch.get("keywords", [])),
                "patterns": _compile_patterns(ch.get("patterns", [])),
                "block_types": set(ch.get("block_types", []) or []),
                "block_type_mode": ch.get("block_type_mode", "fallback"),
            })

    def classify(self, text: str, block_type: str, section_heading: str | None,
                 section_path: str | None) -> ChapterResult:
        scores: dict[str, float] = {}
        rules: dict[str, list[str]] = {}
        ancestors = ""
        if section_path:
            parts = [p.strip() for p in section_path.split(" > ")]
            ancestors = " ".join(parts[:-1]) if section_heading and parts and parts[-1] == section_heading.strip() \
                else " ".join(parts)
        context_hit = False
        for ch in self.chapters:
            s = 0.0
            r: list[str] = []
            if block_type == "heading" and ch["heading"] is not None and ch["heading"].search(text):
                s += self.w_own
                r.append("own_heading")
            if section_heading and ch["heading"] is not None and ch["heading"].search(section_heading):
                s += self.w_section
                r.append(f"section:'{section_heading[:40]}'")
                context_hit = True
            elif ancestors and ch["heading"] is not None and ch["heading"].search(ancestors):
                s += self.w_ancestor
                r.append("ancestor_heading")
                context_hit = True
            if ch["kw"] is not None:
                hits = {m.group(0).lower() for m in ch["kw"].finditer(text)}
                if hits:
                    s += min(self.kw_cap, self.w_kw * len(hits))
                    r.append("kw:" + ",".join(sorted(hits))[:60])
            for p in ch["patterns"]:
                if p.search(text):
                    s += self.w_pat
                    r.append(f"re:{p.pattern[:30]}")
            scores[ch["id"]] = s
            rules[ch["id"]] = r
        # block-type rules: 'always' mode adds unconditionally; 'fallback' only without heading context
        for ch in self.chapters:
            if block_type in ch["block_types"] and (ch["block_type_mode"] == "always" or not context_hit):
                scores[ch["id"]] += self.w_block
                rules[ch["id"]].append(f"block_type:{block_type}")
        ranked = sorted(self.chapters, key=lambda c: (-scores[c["id"]], c["order"]))
        if not ranked or scores[ranked[0]["id"]] < self.min_score:
            return ChapterResult(UNCLASSIFIED_CHAPTER, scores[ranked[0]["id"]] if ranked else 0.0,
                                 "no rule reached min_score", "unclassified")
        best = ranked[0]
        status = "classified"
        if len(ranked) > 1 and scores[ranked[1]["id"]] > 0 and \
                scores[best["id"]] - scores[ranked[1]["id"]] < self.margin:
            status = "ambiguous"
        rule = "; ".join(rules[best["id"]])
        if status == "ambiguous":
            rule += f" | runner-up {ranked[1]['id']}={scores[ranked[1]['id']]:.1f}"
        return ChapterResult(best["id"], scores[best["id"]], rule, status)


# --------------------------------------------------------------------------- applicability
def applicability(doc_cls: DocumentClassifier, text: str, section_path: str | None,
                  source: dict[str, Any]) -> dict[str, Any]:
    """Evidence-level equipment/model/component. Explicit mentions override document values."""
    context = f"{section_path or ''}\n{text}"
    models = doc_cls.find_models(context)
    out: dict[str, Any] = {"revision": source.get("revision")}
    origin = []
    if models:
        out["model"] = _join(m for _, m in models)
        out["equipment"] = _join(e for e, _ in models)
        origin.append("model:text")
    else:
        out["model"] = source.get("model")
        out["equipment"] = source.get("equipment")
        origin.append("model:source")
    comps = doc_cls.find_components(context)
    if comps:
        out["component"] = comps[0][0]
        origin.append("component:text")
    else:
        out["component"] = source.get("component")
        origin.append("component:source")
    out["applicability_origin"] = ",".join(origin)
    return out


def applicability_compatible(a: dict, b: dict) -> tuple[bool, str]:
    """Return (comparable, relation). Different explicit models/equipment are never comparable."""
    ea, eb = split_multi(a.get("equipment")), split_multi(b.get("equipment"))
    if ea and eb and not (ea & eb):
        return False, "different_equipment"
    ma, mb = split_multi(a.get("model")), split_multi(b.get("model"))
    if ma and mb:
        return (True, "same_model") if (ma & mb) else (False, "different_model")
    if ma or mb:
        return True, "model_unspecified_on_one_side"
    return True, "model_unspecified"
