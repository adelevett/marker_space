"""
layout_profiler.py  –  PyMuPDF font-profile geometric profiling
===============================================================

PUBLIC API
==========

Entry point
-----------
    from layout_profiler import profile_layout, get_dominant_profile

    lp = profile_layout("path/to/document.pdf")   # -> LayoutProfile

LayoutProfile fields
--------------------
    lp.body_font            : FontKey
        Elected body font (normalized family + quantised size + dominant flags).

    lp.election             : ElectionResult
        Page-plurality statistics for the body-font election.
        .leader, .page_wins, .total_pages, .win_rate

    lp.body_regime          : XRegime
        Recto/verso-aware x-coordinate regime for the body font.
        .kind        "unified" | "recto_verso"
        .spans       Dict[str, XSpan]  keys: {"unified"} or {"left","right"}
        .get_span(page_index) -> Optional[XSpan]  per-page body reference interval

    lp.body_column          : ColumnResult
        .layout      "single_column" | "multi_column"
        .gutter_x    Optional[float]  absolute x of elected column gutter

    lp.paragraph_gap_pt     : float
        Elected inter-paragraph spacing in points, derived from the document's
        own body-text vertical rhythm via page-level voting on the gap
        distribution between consecutive body-font spans within the body lane.

        Use this as the y-proximity threshold when grouping non-body bboxes
        into structural clusters.  Apply NO lane filtering here; compose with
        FontProfile.x_relative in the consuming code to restrict to a lane.

    lp.font_profiles        : Dict[FontKey, FontProfile]
        One entry for every font profile (normalized-family + size) that meets
        PROFILE_MIN_PAGES and PROFILE_MIN_CHARS thresholds, including the body
        font (FontProfile.is_body == True).

    lp.raw_x_votes          : Dict[int, XSpan]
        Per-page raw x-envelope for the body font.  page_index -> XSpan.

LayoutProfile convenience accessors
-------------------------------------
    lp.body_profile()        -> Optional[FontProfile]
    lp.non_body_profiles()   -> List[FontProfile]  sorted by char_count desc
    lp.get_profile(key)      -> Optional[FontProfile]
    lp.summary()             -> str  human-readable multi-line report

    get_dominant_profile(bbox, page_data, lp) -> Optional[FontProfile]
        Finds the font profile that contributes the most character area to a given bounding box.

FontProfile fields
------------------
    fp.key          : FontKey
        Representative key for this profile (most-common raw font name and
        flags variant within the normalized-family + size group).

    fp.char_count   : int
        Total non-whitespace character count across the entire document.

    fp.page_count   : int
        Number of distinct pages this profile appears on.

    fp.regime       : XRegime
        X-coordinate regime derived from this font's own span envelopes, using
        the same recto/verso detection as the body font.  Provides the font's
        intrinsic geometric footprint independent of the body reference.

    fp.column       : Optional[ColumnResult]
        Column-occupancy result within this font's own regime.
        None when the profile appeared on fewer than 2 pages.

    fp.is_body      : bool

    fp.x_relative   : Optional[XRelativePosition]
        X-position of this font's spans expressed relative to the body x-span
        on each page.  Automatically parity-corrected: a right-margin font
        alternating between x=420 and x=380 in absolute terms due to
        recto/verso margins consistently reads as "right_margin" here.

        None when no pages with this profile also had a body regime span.

        .inside_frac        float  fraction of chars within body x-span
        .left_margin_frac   float  fraction of chars entirely left of body x-span
        .right_margin_frac  float  fraction of chars entirely right of body x-span
        .spanning_frac      float  fraction of chars in spans >= SPANNING_WIDTH_FRAC
                                   of body width (full-width headings, display math)
        .dominant           str    "inside"|"left_margin"|"right_margin"|"spanning"|"mixed"
        .mixed              bool   True when no category reaches DOMINANCE_THRESHOLD

        Classification logic per span (char-count weighted):
          spanning     span_width >= body_width * SPANNING_WIDTH_FRAC
          left_margin  x1 <= body.x_min  (entirely left)
          right_margin x0 >= body.x_max  (entirely right)
          inside       contained within body, or centre-x within body
          (partial overlaps not qualifying as spanning are resolved by centre-x)

    fp.vertical     : VerticalProfile
        Vertical distribution of span occurrences, y normalised to page height
        (0 = top, 1 = bottom).  One observation per span occurrence (not per
        character), so a running header appearing once per page contributes
        exactly one observation per page.

        .top_third_frac    float  fraction of occurrences with y_norm < 0.333
        .mid_third_frac    float  fraction with 0.333 <= y_norm < 0.667
        .bot_third_frac    float  fraction with y_norm >= 0.667
        .median_y_norm     float  median normalised y-centre across all occurrences

        Interpretation guidance:
          Running header     top_third_frac > 0.85,  median_y_norm < 0.10
          Running footer     bot_third_frac > 0.85,  median_y_norm > 0.90
          Section heading    top_third_frac dominant, median_y_norm 0.10-0.35
          Body-inline font   mid_third_frac dominant, median_y_norm 0.40-0.60

    fp.distribution : DocumentDistribution
        Document-wide presence pattern for detecting structurally global vs
        sectional fonts.

        .quintile_coverage  Tuple[float, float, float, float, float]
            For each fifth of the document (by total page count), the fraction
            of pages in that quintile where this profile appears at least once.

            Interpretation guidance:
              Structurally global   all quintiles >= 0.7
              Preamble-only         quintile[0] high, quintiles[1-4] near zero
              Appendix-only         quintile[4] high, quintiles[0-3] near zero
              Chapter markers       intermittent across all quintiles with regular gaps

        .page_density_chars float
            char_count / page_count.  High = substantial prose role;
            Low = sparse structural or decorative use.

Canonical downstream usage patterns
-------------------------------------

1.  Classify a layout-model bbox against the body lane for its page:

        body_span = lp.body_regime.get_span(page_idx)   # -> XSpan | None
        if body_span:
            in_body = body_span.x_min <= bbox_cx <= body_span.x_max

2.  Cluster non-body bboxes by vertical proximity using self-calibrated spacing:

        gap = lp.paragraph_gap_pt
        # Sort candidate bboxes by y0; open a new cluster when:
        #   y0[i+1] - y1[i] > gap

3.  Identify margin annotations (asides, line numbers, margin notes):

        for fp in lp.non_body_profiles():
            if fp.x_relative and fp.x_relative.dominant in ("left_margin", "right_margin"):
                ...

4.  Distinguish running headers from structural headings:

        for fp in lp.non_body_profiles():
            if fp.x_relative and fp.x_relative.dominant in ("inside", "spanning"):
                if fp.vertical.top_third_frac > 0.80:
                    # running header, not a structural heading
                elif fp.vertical.bot_third_frac > 0.80:
                    # running footer

5.  Detect preamble / appendix-only fonts:

        for fp in lp.non_body_profiles():
            q = fp.distribution.quintile_coverage
            if q[0] > 0.7 and max(q[1:]) < 0.2:   # front matter only
                ...
            if q[4] > 0.7 and max(q[:4]) < 0.2:   # back matter only
                ...

6.  Bundle aside heading + aside body as a structural cluster:
    Sort all non-body bboxes in a non-inside x lane by y0; consecutive bboxes
    with gap < lp.paragraph_gap_pt belong to the same structural unit regardless
    of their individual layout-model labels.

Pipeline stages (internal)
---------------------------
1.  extract_page_data          – single fitz pass, populates PageData list
2.  elect_global_body_font     – page-level plurality election for body FontKey
3.  derive_global_x_regime     – body x-votes -> XRegime (unified | recto_verso)
4.  tally_column_votes_for_profile – 1-D occupancy -> ColumnResult
5.  _collect_all_profile_data  – single combined scan collecting:
      a) font inventory (chars, pages, flags, raw names)
      b) x_relative classification vs body regime (char-count weighted)
      c) vertical y_norm observations (one per span occurrence)
      d) per-page body-span y-gaps for paragraph gap election
6.  elect_paragraph_gap        – page-level vote -> paragraph_gap_pt scalar
7.  enumerate_font_profiles    – threshold + construct FontProfile objects
8.  profile_layout             – public orchestrator

Dependencies: PyMuPDF (fitz >= 1.23), numpy
"""

from __future__ import annotations

import math
from collections import defaultdict, Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

import fitz
import numpy as np


# ══════════════════════════════════════════════════════════════════════════════
#  Configuration
# ══════════════════════════════════════════════════════════════════════════════

SIZE_QUANTISE    : float = 0.5    # round raw pt sizes to nearest 0.5 pt
HIST_BIN_PT      : float = 4.0    # bin width (pt) for x_min vote histogram
CLUSTER_MIN_GAP  : float = 18.0   # min peak separation (pt) to declare recto/verso
CLUSTER_MIN_FRAC : float = 0.20   # each cluster must hold >= this share of votes
GUTTER_MIN_PT    : float = 8.0    # min contiguous zero-run (pt) to declare a gutter
GUTTER_ZONE      : Tuple[float, float] = (0.25, 0.75)
X_SPAN_LO_PCT    : float = 1.0    # percentile for robust x_min of each cluster
X_SPAN_HI_PCT    : float = 99.0   # percentile for robust x_max of each cluster

# Font-profile enumeration thresholds
PROFILE_MIN_PAGES    : int   = 2
PROFILE_MIN_CHARS    : int   = 20
PROFILE_MIN_PAGE_FRAC: float = 0.0   # raise to e.g. 0.02 to suppress rare fonts

# Paragraph gap election
PARAGRAPH_GAP_MIN_GAPS   : int   = 3     # min samples for a page to cast a vote

# X-relative classification
SPANNING_WIDTH_FRAC : float = 0.85  # span >= this fraction of body width -> spanning
DOMINANCE_THRESHOLD : float = 0.60  # fraction required for a non-"mixed" dominant label


# ══════════════════════════════════════════════════════════════════════════════
#  Data model
# ══════════════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class FontKey:
    """
    Canonical identity of a font variant used throughout all elections.

    font   – raw font name string from the span (e.g. 'TimesNewRomanPS-BoldMT')
    size   – point size quantised to multiples of SIZE_QUANTISE
    flags  – low 3 bits of PyMuPDF span flags (bit 0 = bold, bit 1 = italic)
    """
    font:  str
    size:  float
    flags: int = 0

    def __str__(self) -> str:
        f = ("B" if self.flags & 1 else "") + ("I" if self.flags & 2 else "")
        return f"{self.font} {self.size:.1f}pt{f}"


@dataclass
class ElectionResult:
    """Outcome of the global body-font plurality election."""
    leader:      FontKey
    page_wins:   int    # pages on which this font held a plurality by char count
    total_pages: int    # pages that contributed at least one character
    win_rate:    float = field(init=False)

    def __post_init__(self) -> None:
        self.win_rate = self.page_wins / max(self.total_pages, 1)

    def __str__(self) -> str:
        return (f"ElectionResult(leader={self.leader}  "
                f"wins={self.page_wins}/{self.total_pages} [{self.win_rate:.0%}])")


@dataclass
class XSpan:
    """A closed x-interval in PDF points."""
    x_min: float
    x_max: float

    @property
    def width(self) -> float:
        return self.x_max - self.x_min


@dataclass
class RectoVerso:
    """Internal: result of resolve_recto_verso_spans before XRegime construction."""
    alternating: bool
    left:  XSpan   # smaller elected x_min (or unified span when not alternating)
    right: XSpan   # larger  elected x_min (same as left when not alternating)
    left_center_xmin:  float  # assignment center for left cluster
    right_center_xmin: float  # assignment center for right cluster


@dataclass
class XRegime:
    """
    Document-level x-coordinate regime for a font profile.

    kind     – "unified" | "recto_verso"
    spans    – label -> XSpan
                 keys: {"unified"}  or  {"left", "right"}
    page_map – page_index -> label

    Use get_span(page_index) for per-page lookup; returns None for pages
    that abstained from the x-vote (no qualifying spans on that page).
    """
    kind:     str
    spans:    Dict[str, XSpan]
    page_map: Dict[int, str]

    def get_span(self, page_index: int) -> Optional[XSpan]:
        label = self.page_map.get(page_index)
        return self.spans.get(label) if label is not None else None

    def __str__(self) -> str:
        parts = {k: f"[{v.x_min:.1f},{v.x_max:.1f}]" for k, v in self.spans.items()}
        return f"XRegime(kind={self.kind!r} spans={parts})"


@dataclass
class ColumnResult:
    """Aggregated outcome of the column-occupancy vote."""
    layout:       str             # "single_column" | "multi_column"
    single_votes: int
    multi_votes:  int
    abstentions:  int
    gutter_x:     Optional[float] = None

    def __str__(self) -> str:
        g = f"gutter_x={self.gutter_x:.1f}" if self.gutter_x is not None else "no_gutter"
        return (f"ColumnResult({self.layout!r} "
                f"single={self.single_votes} multi={self.multi_votes} "
                f"abstain={self.abstentions} {g})")


@dataclass
class XRelativePosition:
    """
    X-position of a font profile's spans relative to the body x-span on each
    page, aggregated across all pages where both the profile and a body regime
    span are available.  Fractions are weighted by character count.

    See module docstring for full classification logic and interpretation notes.
    """
    inside_frac:       float  # chars within body x-span
    left_margin_frac:  float  # chars entirely left of body x-span
    right_margin_frac: float  # chars entirely right of body x-span
    spanning_frac:     float  # chars in spans >= SPANNING_WIDTH_FRAC of body width
    dominant:          str    # "inside"|"left_margin"|"right_margin"|"spanning"|"mixed"
    mixed:             bool   # True when no category reaches DOMINANCE_THRESHOLD

    def __str__(self) -> str:
        return (f"XRelPos(dominant={self.dominant!r} "
                f"in={self.inside_frac:.2f} left={self.left_margin_frac:.2f} "
                f"right={self.right_margin_frac:.2f} span={self.spanning_frac:.2f})")


@dataclass
class VerticalProfile:
    """
    Vertical distribution of a font profile's span occurrences, y normalised
    to page height (0 = top, 1 = bottom).  One observation per span occurrence.

    See module docstring for interpretation guidance.
    """
    top_third_frac: float   # fraction of occurrences with y_norm < 0.333
    mid_third_frac: float   # fraction with 0.333 <= y_norm < 0.667
    bot_third_frac: float   # fraction with y_norm >= 0.667
    median_y_norm:  float   # median normalised y-centre across all occurrences

    def __str__(self) -> str:
        return (f"VerticalProfile(top={self.top_third_frac:.2f} "
                f"mid={self.mid_third_frac:.2f} bot={self.bot_third_frac:.2f} "
                f"median_y={self.median_y_norm:.3f})")


@dataclass
class DocumentDistribution:
    """
    Document-wide presence pattern for a font profile.

    See module docstring for full field documentation and interpretation guidance.
    """
    quintile_coverage:  Tuple[float, float, float, float, float]
    page_density_chars: float   # char_count / page_count

    def __str__(self) -> str:
        q = "  ".join(f"{v:.2f}" for v in self.quintile_coverage)
        return f"DocDist(quintiles=[{q}] density={self.page_density_chars:.1f})"


@dataclass
class FontProfile:
    """
    Complete geometric profile for a single font variant (normalised family + size).

    This is the primary unit of the public API.  Downstream consumers receive
    one FontProfile per significant font and can answer structural questions
    without knowledge of the internal pipeline.

    See module docstring for full field documentation and usage patterns.
    """
    key:          FontKey
    char_count:   int
    page_count:   int
    regime:       XRegime
    column:       Optional[ColumnResult]
    is_body:      bool
    x_relative:   Optional[XRelativePosition]
    vertical:     VerticalProfile
    distribution: DocumentDistribution

    def __str__(self) -> str:
        col = self.column.layout if self.column else "n/a"
        dom = self.x_relative.dominant if self.x_relative else "n/a"
        return (f"FontProfile({self.key}  chars={self.char_count}  "
                f"pages={self.page_count}  regime={self.regime.kind}  "
                f"col={col}  x_rel={dom}  "
                f"y_med={self.vertical.median_y_norm:.2f}  body={self.is_body})")


@dataclass
class PageData:
    """Raw extracted data from a single page."""
    page_index:   int
    page_width:   float
    page_height:  float
    text_blocks:  List[dict]
    image_blocks: List[dict]
    complexity_ratio: float = 0.0

    @property
    def page_area(self) -> float:
        return self.page_width * self.page_height

    def iter_text_spans(self):
        """Yield every text span from this page's text_blocks."""
        for blk in self.text_blocks:
            for line in blk.get("lines", []):
                for span in line.get("spans", []):
                    yield span

    def char_count(self, span: dict) -> int:
        """Count non-whitespace characters in a span."""
        return sum(1 for ch in span.get("text", "") if not ch.isspace())


@dataclass
class LayoutProfile:
    """
    Complete output of profile_layout().

    See module docstring for full API documentation and usage patterns.
    """
    election:         ElectionResult
    body_regime:      XRegime
    body_column:      ColumnResult
    paragraph_gap_pt: float
    font_profiles:    Dict[FontKey, FontProfile]
    raw_x_votes:      Dict[int, XSpan]   # page_index -> body-font XSpan
    page_image_ratios: Dict[int, float]  # page_index -> image_area / page_area
    page_complexities: Dict[int, float]  # page_index -> ratio of high-variance non-text cells
    page_data:        List[PageData] = field(default_factory=list)

    @property
    def body_font(self) -> FontKey:
        """The elected body FontKey."""
        return self.election.leader

    def body_profile(self) -> Optional[FontProfile]:
        """FontProfile for the elected body font."""
        return self.font_profiles.get(self.election.leader)

    def non_body_profiles(self) -> List[FontProfile]:
        """All non-body FontProfiles, sorted by char_count descending."""
        return sorted(
            (p for p in self.font_profiles.values() if not p.is_body),
            key=lambda p: p.char_count,
            reverse=True,
        )

    def get_profile(self, key: FontKey) -> Optional[FontProfile]:
        """Return the FontProfile for *key*, or None if not enumerated."""
        return self.font_profiles.get(key)

    def summary(self) -> str:
        lines = [
            "── Layout Profile " + "─" * 50,
            f"  Body font       : {self.election.leader}",
            f"  Election        : {self.election.page_wins}/{self.election.total_pages}"
            f" pages ({self.election.win_rate:.0%})",
            f"  X regime        : {self.body_regime.kind}",
        ]
        for label, span in sorted(self.body_regime.spans.items()):
            lines.append(f"    {label:8s}  x=[{span.x_min:.1f}, {span.x_max:.1f}]"
                         f"  width={span.width:.1f} pt")
        lines += [
            f"  Column layout   : {self.body_column.layout}",
            f"    single={self.body_column.single_votes}"
            f"  multi={self.body_column.multi_votes}"
            f"  abstain={self.body_column.abstentions}",
        ]
        if self.body_column.gutter_x is not None:
            lines.append(f"    gutter_x={self.body_column.gutter_x:.1f}")
        lines.append(f"  Paragraph gap   : {self.paragraph_gap_pt:.1f} pt")

        non_body = self.non_body_profiles()
        lines.append(f"\n  Other font profiles  ({len(non_body)} qualifying):")
        col_w = 36
        lines.append(
            f"    {'font+size':<{col_w}}  {'pg':>4}  {'chars':>7}  "
            f"{'x_dominant':<12}  {'y_med':>5}  {'quintiles (Q1-Q5)':<26}  col"
        )
        lines.append("    " + "─" * 105)
        for p in non_body:
            q   = "  ".join(f"{v:.2f}" for v in p.distribution.quintile_coverage)
            dom = p.x_relative.dominant if p.x_relative else "n/a"
            col = p.column.layout.replace("_column", "") if p.column else "n/a"
            lines.append(
                f"    {str(p.key):<{col_w}}  {p.page_count:>4}  {p.char_count:>7}  "
                f"{dom:<12}  {p.vertical.median_y_norm:>5.3f}  {q}  {col}"
            )
        lines.append("─" * 68)
        return "\n".join(lines)

    def to_dict(self) -> dict:
        import dataclasses
        safe_self = dataclasses.replace(
            self,
            font_profiles={str(k): v for k, v in self.font_profiles.items()}
        )
        d = dataclasses.asdict(safe_self)
        if "page_data" in d:
            simplified = []
            for p in d["page_data"]:
                p_copy = dict(p)
                p_copy.pop("text_blocks", None)
                p_copy.pop("image_blocks", None)
                simplified.append(p_copy)
            d["page_data"] = simplified
        return d


# ══════════════════════════════════════════════════════════════════════════════
#  Internal helpers
# ══════════════════════════════════════════════════════════════════════════════

STYLE_SUFFIXES: Set[str] = {
    "Regular", "Reg", "Roman",
    "Bold", "Bol", "Bd",
    "Italic", "Ital", "Ita", "It", "Oblique", "Obl",
    "BoldItalic", "BoldIt", "BolIta", "BoldObl",
    "RegularItalic", "RegularIt", "RegIta",
    "LightItalic", "LightIt", "LightObl",
    "MediumItalic", "MediumIt",
    "SemiboldItalic", "SemiboldItal", "SemiboldIta",
    "ExtraboldItalic", "ExtrabldIt",
    "HeavyOblique", "BookOblique",
    "Light", "Lt", "Medium", "Med",
    "Semibold", "SemiBold", "Demibold", "Sb",
    "Extrabold", "Extrabld", "Xbold", "Black", "Heavy", "Book",
    "Cond", "Condensed", "Cn", "LightCn", "BoldCn",
    "SC700", "SC750", "Math",
}


def _normalize_font_name(name: str) -> str:
    """
    Strip common style/weight suffixes to recover the font family name.

      'TimesNewRomanPS-BoldMT'  ->  'TimesNewRomanPS'
      'Arial,Bold'              ->  'Arial'
      'Courier-New'             ->  'Courier-New'   (hyphen preserved, not a suffix)
    """
    if ',' in name:
        name = name.split(',')[0]
    if '-' in name:
        base, suffix = name.rsplit('-', 1)
        if suffix in STYLE_SUFFIXES or suffix.capitalize() in STYLE_SUFFIXES:
            return base
    return name


def _make_font_key(span: dict) -> FontKey:
    """Construct a FontKey from a PyMuPDF span dict."""
    size  = round(span.get("size", 0.0) / SIZE_QUANTISE) * SIZE_QUANTISE
    font  = span.get("font", "") or "<unknown>"
    flags = span.get("flags", 0) & 0b111
    return FontKey(font=font, size=size, flags=flags)


def _profile_pair(key: FontKey) -> Tuple[str, float]:
    """(normalized_family, quantised_size) grouping key — flags-agnostic."""
    return (_normalize_font_name(key.font), key.size)


def _is_body_text(key: FontKey, leader: FontKey) -> bool:
    """True if *key* belongs to the same normalized family+size as *leader*."""
    return _profile_pair(key) == _profile_pair(leader)


def _char_count(span: dict) -> int:
    """Count non-whitespace characters in a span."""
    return sum(1 for ch in span.get("text", "") if not ch.isspace())


def _iter_text_spans(source):
    """
    Yield every text span from a fitz.Page, PageData, or raw block list.
    Non-text blocks (images, etc.) are silently skipped.
    """
    if isinstance(source, fitz.Page):
        try:
            blocks = source.get_text("dict", flags=fitz.TEXT_PRESERVE_WHITESPACE)["blocks"]
        except Exception:
            return
    elif isinstance(source, PageData):
        blocks = source.text_blocks
    else:
        blocks = source

    for blk in blocks:
        if blk.get("type") != 0:
            continue
        for line in blk.get("lines", []):
            for span in line.get("spans", []):
                yield span


def _classify_x_relative(
        bbox:      Tuple[float, float, float, float],
        body_span: XSpan,
) -> str:
    """
    Classify one span bbox into a body-relative x category.

    Order of evaluation matters: spanning is tested first so that a wide
    heading that happens to be contained within the body x-interval still
    registers as spanning when it fills most of the body width.

      spanning     span_width >= body_width * SPANNING_WIDTH_FRAC
      left_margin  x1 <= body.x_min  (entirely left)
      right_margin x0 >= body.x_max  (entirely right)
      inside       contained within body, or centre-x inside body
    """
    x0, x1  = bbox[0], bbox[2]
    bx0, bx1 = body_span.x_min, body_span.x_max
    body_w   = bx1 - bx0
    span_w   = x1 - x0

    if body_w > 0 and span_w >= body_w * SPANNING_WIDTH_FRAC:
        return "spanning"
    if x1 <= bx0:
        return "left_margin"
    if x0 >= bx1:
        return "right_margin"
    if x0 >= bx0 and x1 <= bx1:
        return "inside"
    # Partial overlap not qualifying as spanning: resolve by centre-x
    cx = (x0 + x1) / 2.0
    if cx < bx0:
        return "left_margin"
    if cx > bx1:
        return "right_margin"
    return "inside"


def _build_x_relative(
        counts:         Counter,
        total_eligible: int,
) -> Optional[XRelativePosition]:
    """Construct XRelativePosition from raw category char-count accumulators."""
    if total_eligible == 0:
        return None
    cats  = ("inside", "left_margin", "right_margin", "spanning")
    fracs = {c: counts.get(c, 0) / total_eligible for c in cats}
    best  = max(fracs, key=fracs.__getitem__)
    mixed = fracs[best] < DOMINANCE_THRESHOLD
    return XRelativePosition(
        inside_frac       = fracs["inside"],
        left_margin_frac  = fracs["left_margin"],
        right_margin_frac = fracs["right_margin"],
        spanning_frac     = fracs["spanning"],
        dominant          = "mixed" if mixed else best,
        mixed             = mixed,
    )


def _build_vertical_profile(y_norms: List[float]) -> VerticalProfile:
    """Construct VerticalProfile from per-span normalised y-centre observations."""
    if not y_norms:
        return VerticalProfile(0.0, 0.0, 0.0, 0.5)
    n   = len(y_norms)
    top = sum(1 for y in y_norms if y < 1 / 3) / n
    mid = sum(1 for y in y_norms if 1 / 3 <= y < 2 / 3) / n
    bot = sum(1 for y in y_norms if y >= 2 / 3) / n
    return VerticalProfile(
        top_third_frac = top,
        mid_third_frac = mid,
        bot_third_frac = bot,
        median_y_norm  = float(np.median(y_norms)),
    )


def _build_document_distribution(
        page_set:    Set[int],
        char_count:  int,
        total_pages: int,
) -> DocumentDistribution:
    """Construct DocumentDistribution from a profile's page-presence set."""
    coverage: List[float] = []
    for q in range(5):
        q_start = int(q       * total_pages / 5)
        q_end   = int((q + 1) * total_pages / 5)
        size    = q_end - q_start
        if size == 0:
            coverage.append(0.0)
        else:
            present = sum(1 for p in range(q_start, q_end) if p in page_set)
            coverage.append(present / size)
    return DocumentDistribution(
        quintile_coverage  = tuple(coverage),           # type: ignore[arg-type]
        page_density_chars = char_count / max(len(page_set), 1),
    )


# ══════════════════════════════════════════════════════════════════════════════
#  Extraction
# ══════════════════════════════════════════════════════════════════════════════

def _compute_page_complexity(page: fitz.Page, text_blocks: List[dict]) -> float:
    """
    Compute ratio of high-variance non-text cells to total cells as a measure
    of visual complexity. Plain typographic pages have low background variance, 
    while pages with photographic backgrounds have high variance uniformly distributed.
    """
    pix = page.get_pixmap(matrix=fitz.Matrix(0.5, 0.5))
    if pix.n >= 3:
        img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
        gray = np.dot(img[...,:3], [0.2989, 0.5870, 0.1140]).astype(np.uint8)
    else:
        img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
        gray = img[...,0]
        
    cell_size = 16
    h, w = gray.shape
    grid_h = h // cell_size
    grid_w = w // cell_size
    
    if grid_h == 0 or grid_w == 0:
        return 0.0
        
    gray_trimmed = gray[:grid_h*cell_size, :grid_w*cell_size]
    cells = gray_trimmed.reshape(grid_h, cell_size, grid_w, cell_size)
    variances = np.var(cells, axis=(1, 3))
    
    high_var_thresh = 50.0
    scale_x = pix.width / page.rect.width
    scale_y = pix.height / page.rect.height
    
    is_text_cell = np.zeros((grid_h, grid_w), dtype=bool)
    for b in text_blocks:
        x0, y0, x1, y1 = b.get("bbox", (0, 0, 0, 0))
        c_x0 = max(0, int((x0 * scale_x) // cell_size))
        c_y0 = max(0, int((y0 * scale_y) // cell_size))
        c_x1 = min(grid_w - 1, int((x1 * scale_x) // cell_size))
        c_y1 = min(grid_h - 1, int((y1 * scale_y) // cell_size))
        is_text_cell[c_y0:c_y1+1, c_x0:c_x1+1] = True
            
    high_var = variances > high_var_thresh
    high_var_non_text = high_var & (~is_text_cell)
    
    return float(np.sum(high_var_non_text) / (grid_h * grid_w))

def extract_page_data(doc: fitz.Document) -> List[PageData]:
    """
    Single fitz pass over the document.  Returns PageData in page order.
    Pages that raise exceptions are silently skipped.
    """
    data: List[PageData] = []
    for page in doc:
        try:
            blocks = page.get_text("dict", flags=fitz.TEXT_PRESERVE_WHITESPACE)["blocks"]
            text_blocks = [b for b in blocks if b["type"] == 0]
            complexity = _compute_page_complexity(page, text_blocks)
            data.append(PageData(
                page_index   = page.number,
                page_width   = page.rect.width,
                page_height  = page.rect.height,
                text_blocks  = text_blocks,
                image_blocks = [b for b in blocks if b["type"] == 1],
                complexity_ratio = complexity,
            ))
        except Exception:
            continue
    return data



# ══════════════════════════════════════════════════════════════════════════════
#  Stage 1 – Global body-font election
# ══════════════════════════════════════════════════════════════════════════════

def elect_global_body_font(pages: List[PageData]) -> ElectionResult:
    """
    Page-level plurality election for the dominant body font.

    Votes are aggregated by (normalized_family, size), ignoring flags, so that
    bold/italic variants of the body font reinforce rather than split its tally.
    The representative FontKey is the most-common raw font name + flags within
    the winning group, recovered in the same scan without a second pass.
    """
    group_wins:  Dict[Tuple[str, float], int]     = defaultdict(int)
    group_flags: Dict[Tuple[str, float], Counter] = defaultdict(Counter)
    group_names: Dict[Tuple[str, float], Counter] = defaultdict(Counter)
    total_pages: int = 0

    for page in pages:
        tally:  Dict[Tuple[str, float], int]     = defaultdict(int)
        flags_: Dict[Tuple[str, float], Counter] = defaultdict(Counter)
        names_: Dict[Tuple[str, float], Counter] = defaultdict(Counter)

        for span in _iter_text_spans(page):
            cc = _char_count(span)
            if cc > 0:
                key  = _make_font_key(span)
                pair = _profile_pair(key)
                tally[pair]            += cc
                flags_[pair][key.flags] += cc
                names_[pair][key.font]  += cc

        if not tally:
            continue

        winner = max(tally, key=tally.__getitem__)
        group_wins[winner] += 1
        for pair, ctr in flags_.items():
            group_flags[pair].update(ctr)
        for pair, ctr in names_.items():
            group_names[pair].update(ctr)
        total_pages += 1

    if not group_wins:
        raise ValueError("No extractable text spans found in the document.")

    leader_pair  = max(group_wins, key=group_wins.__getitem__)
    leader_flags = group_flags[leader_pair].most_common(1)[0][0]
    leader_font  = group_names[leader_pair].most_common(1)[0][0]

    return ElectionResult(
        leader      = FontKey(font=leader_font, size=leader_pair[1], flags=leader_flags),
        page_wins   = group_wins[leader_pair],
        total_pages = total_pages,
    )


# ══════════════════════════════════════════════════════════════════════════════
#  Stage 2 – Per-page x-vote (generalised for any font profile)
# ══════════════════════════════════════════════════════════════════════════════

def get_page_x_vote_for_profile(
        page:         PageData,
        profile_pair: Tuple[str, float],
) -> Optional[XSpan]:
    """
    Return the (x_min, x_max) envelope of all spans matching *profile_pair*
    on *page*.  Returns None when no qualifying spans are found.
    """
    xs0: List[float] = []
    xs1: List[float] = []
    for span in _iter_text_spans(page):
        if _profile_pair(_make_font_key(span)) != profile_pair:
            continue
        if _char_count(span) == 0:
            continue
        xs0.append(span["bbox"][0])
        xs1.append(span["bbox"][2])
    if not xs0:
        return None
    return XSpan(x_min=min(xs0), x_max=max(xs1))


def get_page_x_vote(page: PageData, leader: FontKey) -> Optional[XSpan]:
    """Convenience wrapper: x-vote for the elected body font."""
    return get_page_x_vote_for_profile(page, _profile_pair(leader))


# ══════════════════════════════════════════════════════════════════════════════
#  Stage 3 – Global x-regime (histogram -> unified | recto_verso)
# ══════════════════════════════════════════════════════════════════════════════

def _robust_span(votes: List[XSpan]) -> XSpan:
    lo = float(np.percentile([v.x_min for v in votes], X_SPAN_LO_PCT))
    hi = float(np.percentile([v.x_max for v in votes], X_SPAN_HI_PCT))
    return XSpan(x_min=lo, x_max=hi)


def find_histogram_peaks(
        values:           List[float],
        bin_width:        float,
        start_gap:        float,
        min_cluster_frac: float,
) -> Optional[Tuple[float, float]]:
    """
    Return the centres (lo, hi) of the two dominant histogram peaks when they
    satisfy recto/verso detection criteria.  Returns None otherwise.
    """
    if len(values) < 4:
        return None
    lo, hi = min(values), max(values)
    if hi - lo < start_gap:
        return None
    n_bins = max(2, math.ceil((hi - lo) / bin_width))
    counts, edges = np.histogram(values, bins=n_bins)

    peaks: List[Tuple[int, float]] = []
    for i in range(len(counts)):
        left  = counts[i - 1] if i > 0                else -1
        right = counts[i + 1] if i < len(counts) - 1 else -1
        if counts[i] > 0 and counts[i] >= left and counts[i] >= right:
            peaks.append((int(counts[i]), float((edges[i] + edges[i + 1]) / 2.0)))

    if len(peaks) < 2:
        return None
    peaks.sort(key=lambda t: t[0], reverse=True)
    lo_c, hi_c = sorted([peaks[0][1], peaks[1][1]])
    if hi_c - lo_c < start_gap:
        return None

    boundary   = (lo_c + hi_c) / 2.0
    votes_low  = sum(1 for v in values if v <= boundary)
    votes_high = len(values) - votes_low
    total      = len(values)
    if votes_low / total < min_cluster_frac or votes_high / total < min_cluster_frac:
        return None
    return (lo_c, hi_c)


def resolve_recto_verso_spans(votes: List[XSpan]) -> RectoVerso:
    """Determine whether x-votes form a unified or recto/verso distribution."""
    x_mins = [v.x_min for v in votes]
    peaks  = find_histogram_peaks(x_mins, HIST_BIN_PT, CLUSTER_MIN_GAP, CLUSTER_MIN_FRAC)
    if peaks:
        lo_c, hi_c = peaks
        mid         = (lo_c + hi_c) / 2.0
        left_votes  = [v for v in votes if v.x_min <= mid]
        right_votes = [v for v in votes if v.x_min >  mid]
        left_center = float(np.median([v.x_min for v in left_votes]))
        right_center = float(np.median([v.x_min for v in right_votes]))
        return RectoVerso(
            alternating = True,
            left  = _robust_span(left_votes),
            right = _robust_span(right_votes),
            left_center_xmin = left_center,
            right_center_xmin = right_center,
        )
    span = _robust_span(votes)
    center = float(np.median([v.x_min for v in votes]))
    return RectoVerso(
        alternating=False,
        left=span,
        right=span,
        left_center_xmin=center,
        right_center_xmin=center,
    )


def derive_x_regime_for_votes(raw_x_votes: Dict[int, XSpan]) -> XRegime:
    """
    Build an XRegime from a pre-computed page_index -> XSpan mapping.
    Pages absent from raw_x_votes abstained and are absent from page_map.
    """
    if not raw_x_votes:
        raise ValueError("No x-votes provided; cannot derive a regime.")
    rv    = resolve_recto_verso_spans(list(raw_x_votes.values()))
    kind  = "recto_verso" if rv.alternating else "unified"
    spans = ({"left": rv.left, "right": rv.right} if rv.alternating
             else {"unified": rv.left})

    page_map: Dict[int, str] = {}
    for i, span in raw_x_votes.items():
        if not rv.alternating:
            page_map[i] = "unified"
        else:
            # Assign each page vote by nearest cluster center. Do not use robust
            # span minima for assignment: percentile-based span edges can drift
            # away from the cluster mass and mislabel mid-cluster pages.
            dl = abs(span.x_min - rv.left_center_xmin)
            dr = abs(span.x_min - rv.right_center_xmin)
            page_map[i] = "left" if dl <= dr else "right"
    return XRegime(kind=kind, spans=spans, page_map=page_map)


def derive_global_x_regime(
        pages:  List[PageData],
        leader: FontKey,
) -> Tuple[XRegime, Dict[int, XSpan]]:
    """Collect per-page x-votes for the body font and derive the global XRegime."""
    raw: Dict[int, XSpan] = {}
    for page in pages:
        vote = get_page_x_vote(page, leader)
        if vote is not None:
            raw[page.page_index] = vote
    if not raw:
        raise ValueError(
            "No pages produced an x-vote for the elected body font.  "
            "The document may contain only scanned images."
        )
    return derive_x_regime_for_votes(raw), raw


# ══════════════════════════════════════════════════════════════════════════════
#  Stage 4 – Column vote (1-D occupancy histogram)
# ══════════════════════════════════════════════════════════════════════════════

def analyze_column_occupancy(
        spans:            List[Tuple[float, float]],
        regime_min:       float,
        regime_max:       float,
        gutter_zone:      Tuple[float, float],
        min_gutter_width: float,
) -> Tuple[Optional[str], Optional[float]]:
    """
    Geometric column analysis within a regime x-span.

    Returns ("single_column"|"multi_column"|None, gutter_x|None).
    """
    span_width = regime_max - regime_min
    if span_width < 1.0:
        return (None, None)
    n_bins  = math.ceil(span_width)
    hist    = np.zeros(n_bins, dtype=np.float32)
    painted = False
    for (sx0, sx1) in spans:
        sx0 = max(sx0, regime_min)
        sx1 = min(sx1, regime_max)
        if sx1 <= sx0:
            continue
        b0 = max(0,      int(math.floor(sx0 - regime_min)))
        b1 = min(n_bins, int(math.ceil (sx1 - regime_min)))
        if b1 > b0:
            hist[b0:b1] += 1.0
            painted = True
    if not painted:
        return (None, None)

    z0, z1  = int(n_bins * gutter_zone[0]), int(n_bins * gutter_zone[1])
    central = hist[z0:z1]
    max_run = run = 0
    max_run_end = -1
    for i, v in enumerate(central):
        if v == 0.0:
            run += 1
            if run > max_run:
                max_run, max_run_end = run, i
        else:
            run = 0

    if max_run >= min_gutter_width:
        run_start = max_run_end - max_run + 1
        center    = (run_start + max_run_end) / 2.0
        return ("multi_column", regime_min + z0 + center)
    return ("single_column", None)


def tally_column_votes_for_profile(
        pages:        List[PageData],
        profile_pair: Tuple[str, float],
        regime:       XRegime,
) -> ColumnResult:
    """Aggregate page-level column votes for a given font profile."""
    single = multi = abstain = 0
    gutter_votes: List[float] = []

    for page in pages:
        span_limits = regime.get_span(page.page_index)
        if span_limits is None:
            abstain += 1
            continue
        spans: List[Tuple[float, float]] = [
            (s["bbox"][0], s["bbox"][2])
            for s in _iter_text_spans(page)
            if _profile_pair(_make_font_key(s)) == profile_pair and _char_count(s) > 0
        ]
        vote, gutter_x = analyze_column_occupancy(
            spans, span_limits.x_min, span_limits.x_max, GUTTER_ZONE, GUTTER_MIN_PT
        )
        if   vote == "single_column": single += 1
        elif vote == "multi_column":
            multi += 1
            if gutter_x is not None:
                gutter_votes.append(gutter_x)
        else:                         abstain += 1

    layout       = "multi_column" if multi > single else "single_column"
    final_gutter = (float(np.median(gutter_votes))
                    if layout == "multi_column" and gutter_votes else None)
    return ColumnResult(layout=layout, single_votes=single,
                        multi_votes=multi, abstentions=abstain,
                        gutter_x=final_gutter)


# ══════════════════════════════════════════════════════════════════════════════
#  Stage 5 – Combined single-pass data collection
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class _CollectedProfileData:
    """
    All per-profile-pair statistics gathered in a single span scan.
    Internal use only; never exposed through the public API.
    """
    # Font inventory
    pair_chars:     Dict[Tuple[str, float], int]
    pair_pages:     Dict[Tuple[str, float], Set[int]]
    pair_flags:     Dict[Tuple[str, float], Counter]
    pair_raw_names: Dict[Tuple[str, float], Counter]
    # X-relative enrichment (char-count weighted per category)
    pair_xrel:          Dict[Tuple[str, float], Counter]
    pair_xrel_eligible: Dict[Tuple[str, float], int]
    # Vertical enrichment (one y_norm per span occurrence)
    pair_y_norms:   Dict[Tuple[str, float], List[float]]
    # Paragraph gap: per-page positive inter-line baseline gaps for body font
    # within the body lane
    page_body_gaps: Dict[int, List[float]]


def _collect_all_profile_data(
        pages:       List[PageData],
        leader:      FontKey,
        body_regime: XRegime,
) -> _CollectedProfileData:
    """
    Single scan over all pages and spans collecting:
      a) Font inventory (chars, pages, flags, raw names) for every profile
      b) X-relative classification vs body regime (char-count weighted)
      c) Vertical y_norm observations (one per span occurrence)
      d) Per-page body-span y-gap samples for paragraph gap election

    This is the only post-election span scan in the pipeline.  All enrichment
    data for FontProfile construction comes from this single pass.
    """
    pair_chars:     Dict[Tuple[str, float], int]           = defaultdict(int)
    pair_pages:     Dict[Tuple[str, float], Set[int]]      = defaultdict(set)
    pair_flags:     Dict[Tuple[str, float], Counter]       = defaultdict(Counter)
    pair_raw_names: Dict[Tuple[str, float], Counter]       = defaultdict(Counter)
    pair_xrel:      Dict[Tuple[str, float], Counter]       = defaultdict(Counter)
    pair_xrel_elig: Dict[Tuple[str, float], int]           = defaultdict(int)
    pair_y_norms:   Dict[Tuple[str, float], List[float]]   = defaultdict(list)

    # Accumulate body-font baseline origin_y per page for gap computation.
    # Only spans whose centre-x falls within the body regime lane are included,
    # keeping the gap distribution clean of margin-resident body-font instances.
    page_body_origins: Dict[int, List[float]] = defaultdict(list)

    leader_pair = _profile_pair(leader)

    for page in pages:
        h          = page.page_height if page.page_height > 0 else 1.0
        body_span  = body_regime.get_span(page.page_index)

        for span in _iter_text_spans(page):
            cc = _char_count(span)
            if cc == 0:
                continue

            key  = _make_font_key(span)
            pair = _profile_pair(key)
            bbox = span["bbox"]   # (x0, y0, x1, y1)

            # ── a) Font inventory ─────────────────────────────────────────
            pair_chars[pair]               += cc
            pair_pages[pair].add(page.page_index)
            pair_flags[pair][key.flags]    += cc
            pair_raw_names[pair][key.font] += cc

            # ── b) X-relative (requires body span on this page) ───────────
            if body_span is not None:
                cat = _classify_x_relative(bbox, body_span)
                pair_xrel[pair][cat] += cc
                pair_xrel_elig[pair] += cc

            # ── c) Vertical (one observation per span, normalised) ────────
            y_norm = ((bbox[1] + bbox[3]) / 2.0) / h
            pair_y_norms[pair].append(y_norm)

            # ── d) Body-lane gap accumulation ─────────────────────────────
            if pair == leader_pair and body_span is not None:
                cx = (bbox[0] + bbox[2]) / 2.0
                if body_span.x_min <= cx <= body_span.x_max:
                    page_body_origins[page.page_index].append(span["origin"][1])

    # Convert per-page body-span baseline origin lists to positive gap lists
    page_body_gaps: Dict[int, List[float]] = {}
    for pg_idx, origins in page_body_origins.items():
        # Deduplicate same-line spans (rounding to 0.1 pt to fuse exact baselines)
        unique_origins = sorted(set(round(y, 1) for y in origins))
        gaps = [unique_origins[i + 1] - unique_origins[i] 
                for i in range(len(unique_origins) - 1)]
        # Filter negative/zero gaps (already handled by set+sort, but robust check)
        positive = [g for g in gaps if g > 1.0]  # Min 1pt to avoid micro-gaps
        if positive:
            page_body_gaps[pg_idx] = positive

    return _CollectedProfileData(
        pair_chars          = dict(pair_chars),
        pair_pages          = dict(pair_pages),
        pair_flags          = dict(pair_flags),
        pair_raw_names      = dict(pair_raw_names),
        pair_xrel           = dict(pair_xrel),
        pair_xrel_eligible  = dict(pair_xrel_elig),
        pair_y_norms        = dict(pair_y_norms),
        page_body_gaps      = page_body_gaps,
    )


# ══════════════════════════════════════════════════════════════════════════════
#  Stage 6 – Paragraph gap election
# ══════════════════════════════════════════════════════════════════════════════

def _compute_otsu_threshold(values: List[float], bins: int = 40) -> float:
    """Standard 1D Otsu thresholding on continuous values."""
    if not values:
        return 0.0
    v_min, v_max = min(values), max(values)
    if v_max - v_min < 1e-3:
        return v_max

    counts, edges = np.histogram(values, bins=bins)
    total = sum(counts)
    sum_total = sum(i * counts[i] for i in range(bins))

    weight_b = 0
    sum_b = 0.0
    maximum = -1.0
    threshold_idx = 0

    for i in range(bins):
        weight_b += counts[i]
        if weight_b == 0:
            continue
        weight_f = total - weight_b
        if weight_f == 0:
            break

        sum_b += i * counts[i]
        
        m_b = sum_b / weight_b
        m_f = (sum_total - sum_b) / weight_f
        
        var_between = float(weight_b) * float(weight_f) * (m_b - m_f) ** 2
        
        if var_between > maximum:
            maximum = var_between
            threshold_idx = i

    return float((edges[threshold_idx] + edges[threshold_idx + 1]) / 2.0)


def elect_paragraph_gap(page_body_gaps: Dict[int, List[float]]) -> float:
    """
    Elect a global inter-paragraph spacing threshold from per-page gap samples.

    Per-page vote: an Otsu threshold calculated on the 1D histogram of that 
    page's inter-line baseline gaps. Otsu's method finds the optimal valley 
    between the tight inter-line spacing mode and the loose inter-paragraph 
    spacing mode without relying on a fixed percentile guess.

    Global result: median of all page votes.  Pages with fewer than
    PARAGRAPH_GAP_MIN_GAPS positive gaps abstain.

    Returns 0.0 when no pages can cast a vote (e.g. image-only document or
    no body font found in any column lane).  Callers should treat 0.0 as
    "threshold unavailable" and fall back to a document-dimension heuristic.
    """
    votes: List[float] = []
    for gaps in page_body_gaps.values():
        if len(gaps) >= PARAGRAPH_GAP_MIN_GAPS:
            # Guard against unimodal pages (e.g. one long paragraph with no breaks)
            if max(gaps) <= min(gaps) * 1.5:
                continue
                
            # Drop wild outliers before Otsu to keep histogram bins focused
            # on the core modes (inter-line and inter-paragraph)
            p99 = np.percentile(gaps, 99)
            filtered_gaps = [g for g in gaps if g <= p99]
            if not filtered_gaps:
                continue
            votes.append(_compute_otsu_threshold(filtered_gaps, bins=40))
            
    if not votes:
        return 0.0
    return float(np.median(votes))


# ══════════════════════════════════════════════════════════════════════════════
#  Stage 7 – Enumerate font profiles
# ══════════════════════════════════════════════════════════════════════════════

def enumerate_font_profiles(
        pages:        List[PageData],
        leader:       FontKey,
        body_regime:  XRegime,
        body_column:  ColumnResult,
        collected:    _CollectedProfileData,
) -> Dict[FontKey, FontProfile]:
    """
    Construct a FontProfile for every font profile meeting minimum thresholds.

    The body font profile receives the pre-computed body_regime and body_column
    (is_body=True) without re-running the pipeline.  All other profiles run the
    x-vote -> regime -> column pipeline on their own spans so that each font's
    geometry is measured on its own terms.

    Enrichment fields (x_relative, vertical, distribution) are populated from
    the pre-collected data, requiring no additional span scan.
    """
    total_pages = len(pages)
    min_pages   = max(PROFILE_MIN_PAGES, int(total_pages * PROFILE_MIN_PAGE_FRAC))
    leader_pair = _profile_pair(leader)
    profiles:   Dict[FontKey, FontProfile] = {}

    for pair, char_count in collected.pair_chars.items():
        page_set = collected.pair_pages.get(pair, set())

        if char_count < PROFILE_MIN_CHARS:
            continue
        if len(page_set) < min_pages:
            continue

        is_body  = (pair == leader_pair)
        rep_font  = collected.pair_raw_names[pair].most_common(1)[0][0]
        rep_flags = collected.pair_flags[pair].most_common(1)[0][0]
        rep_key   = FontKey(font=rep_font, size=pair[1], flags=rep_flags)

        # ── Regime + column ───────────────────────────────────────────────
        if is_body:
            regime = body_regime
            column = body_column
        else:
            raw_votes: Dict[int, XSpan] = {}
            for page in pages:
                if page.page_index not in page_set:
                    continue
                vote = get_page_x_vote_for_profile(page, pair)
                if vote is not None:
                    raw_votes[page.page_index] = vote
            if not raw_votes:
                continue
            try:
                regime = derive_x_regime_for_votes(raw_votes)
            except ValueError:
                continue
            column = (tally_column_votes_for_profile(pages, pair, regime)
                      if len(raw_votes) >= 2 else None)

        # ── Enrichment ────────────────────────────────────────────────────
        x_relative   = _build_x_relative(
            counts         = collected.pair_xrel.get(pair, Counter()),
            total_eligible = collected.pair_xrel_eligible.get(pair, 0),
        )
        vertical     = _build_vertical_profile(
            collected.pair_y_norms.get(pair, [])
        )
        distribution = _build_document_distribution(
            page_set    = page_set,
            char_count  = char_count,
            total_pages = total_pages,
        )

        profiles[rep_key] = FontProfile(
            key          = rep_key,
            char_count   = char_count,
            page_count   = len(page_set),
            regime       = regime,
            column       = column,
            is_body      = is_body,
            x_relative   = x_relative,
            vertical     = vertical,
            distribution = distribution,
        )

    return profiles


def get_dominant_profile(
    bbox: Tuple[float, float, float, float],
    page_data: PageData,
    lp: LayoutProfile,
) -> Optional[FontProfile]:
    """Find the font profile that contributes the most character area to this bbox."""
    counts = {}
    
    for span in _iter_text_spans(page_data):
        cc = _char_count(span)
        if cc == 0:
            continue
            
        span_bbox = span.get("bbox")
        if not span_bbox:
            continue
        cx = (span_bbox[0] + span_bbox[2]) / 2.0
        cy = (span_bbox[1] + span_bbox[3]) / 2.0
        
        # Point in bbox
        if bbox[0] <= cx <= bbox[2] and bbox[1] <= cy <= bbox[3]:
            key = _make_font_key(span)
            profile = lp.get_profile(key)
            if profile:
                counts[key] = counts.get(key, 0) + cc
                
    if not counts:
        return None
        
    best_key = max(counts, key=counts.__getitem__)
    return lp.get_profile(best_key)


# ══════════════════════════════════════════════════════════════════════════════
#  Public entry point
# ══════════════════════════════════════════════════════════════════════════════

def profile_layout(source: str | Path) -> LayoutProfile:
    """
    Run the full font-profile geometric pipeline on a PDF file.

    Parameters
    ----------
    source : str | Path  – path to a readable PDF file

    Returns
    -------
    LayoutProfile
        See module docstring for the full API contract.

    Raises
    ------
    FileNotFoundError  – if the path does not exist
    ValueError         – if the document yields no usable text
    """
    path = Path(source)
    if not path.exists():
        raise FileNotFoundError(f"PDF not found: {path}")

    doc = fitz.open(str(path))
    try:
        # Phase 0: Extraction  (single fitz pass)
        pages = extract_page_data(doc)
        if not pages:
            raise ValueError("No extractable pages found in the document.")

        # Phase 1: Body font election
        election = elect_global_body_font(pages)

        # Phase 2: Body x-regime
        body_regime, raw_x_votes = derive_global_x_regime(pages, election.leader)

        # Phase 3: Body column layout
        body_column = tally_column_votes_for_profile(
            pages, _profile_pair(election.leader), body_regime
        )

        # Phase 4: Combined enrichment collection  (single span scan)
        collected = _collect_all_profile_data(pages, election.leader, body_regime)

        # Phase 5: Paragraph gap election
        paragraph_gap_pt = elect_paragraph_gap(collected.page_body_gaps)

        # Phase 6: Enumerate all font profiles
        font_profiles = enumerate_font_profiles(
            pages       = pages,
            leader      = election.leader,
            body_regime = body_regime,
            body_column = body_column,
            collected   = collected,
        )

        page_image_ratios = {}
        page_complexities = {}
        for p in pages:
            area = p.page_area
            if area > 0:
                img_area = sum(
                    max(0, b["bbox"][2] - b["bbox"][0]) * max(0, b["bbox"][3] - b["bbox"][1])
                    for b in p.image_blocks if "bbox" in b
                )
                page_image_ratios[p.page_index] = img_area / area
            else:
                page_image_ratios[p.page_index] = 0.0
            
            page_complexities[p.page_index] = p.complexity_ratio

    finally:
        doc.close()

    return LayoutProfile(
        election         = election,
        body_regime      = body_regime,
        body_column      = body_column,
        paragraph_gap_pt = paragraph_gap_pt,
        font_profiles    = font_profiles,
        raw_x_votes      = raw_x_votes,
        page_image_ratios= page_image_ratios,
        page_complexities= page_complexities,
        page_data        = pages,
    )


# ══════════════════════════════════════════════════════════════════════════════
#  CLI convenience
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage: python layout_profiler.py <file.pdf> [<file.pdf> ...]")
        sys.exit(1)

    for pdf_path in sys.argv[1:]:
        print(f"\nAnalysing: {pdf_path}")
        try:
            print(profile_layout(pdf_path).summary())
        except Exception as exc:
            print(f"  ERROR: {exc}")
