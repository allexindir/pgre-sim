#!/usr/bin/env python3
"""Import a GRE Physics practice-test PDF into exams/<id>/ (see SPEC.md §5).

Output is one summary line, then FLAGS. Exit 0 clean, 1 flags, 2 fatal.
"""
from __future__ import annotations

import argparse
import copy
import json
import math
import os
import re
import shutil
import statistics
import subprocess
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import pymupdf
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
EXAMS_DIR = Path(os.environ.get("PGRE_EXAMS_DIR", ROOT / "exams"))     # override for tests

ZOOM = 2.5            # crop render zoom (~180 dpi)
REVIEW_ZOOM = 1.2     # review overlay zoom
PAD = 6               # padding around tightened crops (pt)
LEAD = 4              # regions start this far above their anchor (pt)
COL_PAD = 8           # columns extend this far left of their leftmost anchor (pt)
BLANK_WORDS = 20      # pages with fewer words (scratch-work pages) don't count as pages
BAND = 0.15           # headers and footers live in the top and bottom 15% of the page
LINE_GAP = 12         # a word this close on the left means "not first on its line" (pt)
MAX_SET = 8           # larger shared-stem ranges are suspicious
MAX_REPORT = 25

ANCHOR_RE = re.compile(r"^(\d{1,3})\.$")
GLUED_RE = re.compile(r"^(\d{1,3})\.(?=[A-Za-z(\[])")
STEM_RE = re.compile(r"^Questions?\s+(\d+)\s*(?:-|–|—|and|through|to)\s*(\d+)", re.I)
CHOICE_RE = re.compile(r"^[({\[]([A-E])[)}\]]|^[({\[](C)c[)}\]]")    # OCR also reads "{B)", "(Cc)"
# How OCR renders choice labels on scans: "©", "®)", "()", "‘D)", "(BE)", "(B". Never a bare letter.
OCR_CHOICE_RE = re.compile(r"^(?:[©®@]\)?|\(\)|[(‘'`{\[|]?[A-Ea-e8©@®]{1,2}[)}\]|]|[({\[|][A-Ea-e8©@®]{1,2}[)}\]|]?)$")
OCR_ANCHOR_RE = re.compile(r"^([0-9ilI|!OoBSZ]{1,3})[.,]$")    # OCR reads "14." as "i4." or "14,", "80." as "BO."
OCR_SETTINGS = "pgre-ocr-v2: --deskew --rotate-pages --force-ocr --pages <scanned> --tesseract-pagesegmode 4"
LETTER_RE = re.compile(r"^\(?([A-E])\)?$")
KEYNUM_RE = re.compile(r"^\d{1,3}\.?$")
PPLUS_RE = re.compile(r"^(\d{1,3})%?$")
FOOTER_RE = re.compile(
    r"^(go\s*on to the next page[.,]?|go\s*on to the|next page[.,]?|stop\.?|.*\btime is called\b.*|.*\bcheck your work\b.*"
    r"|.*\bwork on this test\b.*|this test\.|do not turn this page.*|scratch work"
    r"|.*\bunauthorized copying\b.*|.*\bpage is illegal\.?)$", re.I)

KEY_PHRASES = ("answer key", "worksheet", "correct answer")
SCALE_PHRASES = ("score conversion", "total score")

FLAG_ORDER = ["KEY", "SCALE", "RULE", "COUNT", "NUM_GAP", "SET", "REF", "CHOICES", "HEIGHT", "SPLIT"]

pymupdf.TOOLS.mupdf_display_errors(False)
pymupdf.TOOLS.mupdf_display_warnings(False)


class Fatal(Exception):
    pass


# ---------------------------------------------------------------- geometry

def union(boxes):
    boxes = list(boxes)
    return (min(b[0] for b in boxes), min(b[1] for b in boxes),
            max(b[2] for b in boxes), max(b[3] for b in boxes))


def intersect(a, b):
    r = (max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3]))
    return r if r[0] <= r[2] and r[1] <= r[3] else None


def grow(b, d):
    return (b[0] - d, b[1] - d, b[2] + d, b[3] + d)


def area(b):
    return max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])


def center_in(b, region):
    cx, cy = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
    return region[0] <= cx <= region[2] and region[1] <= cy <= region[3]


def rounded(b):
    return [round(v, 1) for v in b]


# ---------------------------------------------------------------- pages

@dataclass(eq=False)
class Word:
    x0: float
    y0: float
    x1: float
    y1: float
    text: str

    @property
    def bbox(self):
        return (self.x0, self.y0, self.x1, self.y1)

    @property
    def cy(self):
        return (self.y0 + self.y1) / 2


@dataclass(eq=False)
class Line:
    words: list

    @property
    def text(self):
        return " ".join(w.text for w in self.words)

    @property
    def bbox(self):
        return union(w.bbox for w in self.words)


class Page:
    def __init__(self, pg, num):
        self.pg = pg
        self.num = num
        self.width, self.height = pg.rect.width, pg.rect.height
        self.words = []
        groups = defaultdict(list)
        for x0, y0, x1, y1, text, block, line, _ in pg.get_text("words"):
            w = Word(x0, y0, x1, y1, text)
            self.words.append(w)
            groups[(block, line)].append(w)
        self.lines = [Line(sorted(ws, key=lambda w: w.x0)) for ws in groups.values()]
        self._drawings = None
        self._images = None
        self._rules = None
        self._blobs = None
        self.scanned = False

    @property
    def text(self):
        return " ".join(w.text for w in self.words)

    def drawings(self):
        if self._drawings is None:
            self._drawings = []
            for d in self.pg.get_drawings():
                r = d.get("rect")
                if r is None or r.width + r.height <= 0:
                    continue
                fill, stroke = d.get("fill"), d.get("color")
                if stroke is None and (fill is None or min(fill) > 0.95):
                    continue
                self._drawings.append((r.x0, r.y0, r.x1, r.y1))
        return self._drawings

    def images(self):
        if self._images is None:
            self._images = [tuple(i["bbox"]) for i in self.pg.get_image_info()]
        return self._images

    def blobs(self):
        if self._blobs is None:
            self._blobs = ink_blobs(self) if self.scanned else []
        return self._blobs

    def rules(self):
        """Isolated full-width horizontal lines. They divide a page into stacked bands.
        On a scan they are thin, page-wide runs of ink."""
        if self._rules is None:
            wide = 0.35 if self.scanned else 0.45
            ds = self.drawings() + [b for b, _ in self.blobs() if b[3] - b[1] <= 4 and b[2] - b[0] >= wide * self.width]
            self._rules = []
            for i, r in enumerate(ds):
                if r[3] - r[1] > (4 if self.scanned else 1.5) or r[2] - r[0] < wide * self.width:
                    continue
                if not any(j != i and intersect(grow(d, 3), r) and max(abs(a - b) for a, b in zip(d, r)) > 1
                           for j, d in enumerate(ds)):
                    self._rules.append(r)
        return self._rules

    def band(self, y):
        return sum(1 for r in self.rules() if r[1] <= y)


def band_of(page, box):
    cy = (box[1] + box[3]) / 2
    if cy < BAND * page.height:
        return "top"
    if cy > (1 - BAND) * page.height:
        return "bot"
    return None


def line_key(text):
    return re.sub(r"\s+", " ", re.sub(r"\d", "", text)).strip().lower()


class RunningFilter:
    """Headers and footers: band lines whose digit-free text repeats at the same y on >=40% of exam pages."""

    def __init__(self, pages):
        thresh = max(2, math.ceil(0.4 * len(pages)))
        seen = defaultdict(list)
        rects = defaultdict(set)
        for p in pages:
            for ln in p.lines:
                b = band_of(p, ln.bbox)
                if b:
                    seen[(b, line_key(ln.text))].append((p.num, ln.bbox[1]))
            for r in p.drawings():
                rects[tuple(round(v / 2) for v in r)].add(p.num)
        self.lines = {}
        for key, hits in seen.items():
            ys = [y for _, y in hits]
            best = max(ys, key=lambda y: sum(abs(y - z) <= 3 for z in ys))
            if len({n for n, y in hits if abs(y - best) <= 3}) >= thresh:
                self.lines[key] = best
        self.rects = {r for r, nums in rects.items() if len(nums) >= thresh}

    def is_running(self, page, ln):
        text = ln.text.strip()
        if FOOTER_RE.match(text):
            return True
        b = band_of(page, ln.bbox)
        if not b:
            return False
        if re.fullmatch(r"[-–—\s]*\d{1,3}[-–—\s]*", text):     # a page number
            return True
        cy = (ln.bbox[1] + ln.bbox[3]) / 2
        if page.scanned and len(text) <= 3 and re.search(r"\d", text) and not re.search(r"[.,:;]$", text) \
                and not 0.1 * page.height < cy < 0.9 * page.height:     # OCR reads page "18" as "1s"
            return True
        best = self.lines.get((b, line_key(text)))
        return best is not None and abs(ln.bbox[1] - best) <= 3

    def is_running_rect(self, r):
        return tuple(round(v / 2) for v in r) in self.rects


@dataclass(eq=False)
class Item:
    bbox: tuple
    kind: str           # "w" word, "d" drawing, "i" image
    text: str = ""


def ink_blobs(page, cell=2.0, thresh=160):
    """Connected regions of ink on a scanned page as (bbox in PDF points, cell count)."""
    pix = page.pg.get_pixmap(matrix=pymupdf.Matrix(1, 1), colorspace=pymupdf.csGRAY)
    img = Image.frombytes("L", (pix.width, pix.height), pix.samples)
    sx, sy = page.width / pix.width, page.height / pix.height
    gw, gh = max(1, round(pix.width / cell)), max(1, round(pix.height / cell))
    cells = img.point(lambda v: 255 if v < thresh else 0).resize((gw, gh), Image.BOX).tobytes()
    seen = bytearray(gw * gh)
    blobs = []
    for start in range(gw * gh):
        if cells[start] <= 20 or seen[start]:
            continue
        seen[start] = 1
        stack, count = [start], 0
        x0 = x1 = start % gw
        y0 = y1 = start // gw
        while stack:
            j = stack.pop()
            count += 1
            x, y = j % gw, j // gw
            x0, x1, y0, y1 = min(x0, x), max(x1, x), min(y0, y), max(y1, y)
            for ny in (y - 1, y, y + 1):
                for nx in (x - 1, x, x + 1):
                    if 0 <= nx < gw and 0 <= ny < gh:
                        k = ny * gw + nx
                        if cells[k] > 20 and not seen[k]:
                            seen[k] = 1
                            stack.append(k)
        cw, ch = pix.width / gw * sx, pix.height / gh * sy
        blobs.append(((x0 * cw, y0 * ch, (x1 + 1) * cw, (y1 + 1) * ch), count))
    return blobs


def build_items(page, rf, scanned=False):
    """Content items on a page, plus the marks deliberately left out (headers,
    footers, page rules, column dividers) so crops can be kept clear of them.

    On a scanned page the text comes from OCR and figures from the ink itself: ink
    blobs that OCR words don't already cover become drawing items."""
    items, excluded = [], []
    for ln in page.lines:
        if rf.is_running(page, ln):
            excluded += [w.bbox for w in ln.words]
        else:       # OCR turns specks into "." and "'" words
            items += [Item(w.bbox, "w", w.text) for w in ln.words
                      if not (scanned and re.fullmatch(r"[\W_]{1,2}", w.text))]
    W, H = page.width, page.height
    words = [it.bbox for it in items]
    rules = page.rules()
    for r in page.drawings():
        w, h = r[2] - r[0], r[3] - r[1]
        boxed = [b for b in excluded if center_in(b, grow(r, 2))]
        if rf.is_running_rect(r) or w * h > 0.4 * W * H or (min(w, h) < 2 and (h > 0.8 * H or w > 0.8 * W)) \
                or r in rules or (boxed and not any(center_in(b, grow(r, 2)) for b in words)):
            excluded.append(r)      # page furniture, band rules, the box around a footer
        else:
            items.append(Item(r, "d"))
    for r in page.images():
        if area(r) <= 0.6 * W * H:
            items.append(Item(r, "i"))
    if scanned:
        all_words = [w.bbox for w in page.words]
        for box, count in page.blobs():
            if count < 3 or box[0] < 2 or box[1] < 2 or box[2] > W - 2 or box[3] > H - 2:
                continue        # specks and scanner edges
            if box in rules:
                excluded.append(box)
                continue
            near = [w for w in all_words if intersect(box, grow(w, 1))]
            if near and all(any(intersect(w, grow(x, 0.5)) for x in excluded) for w in near):
                excluded.append(box)        # the ink of a footer or page number
                continue
            covered = sum(area(intersect(box, grow(w, 1)) or (0, 0, 0, 0)) for w in near)
            # Keep ink that OCR missed: a mostly-text blob still counts if it reaches
            # well above or below the words covering it (a choice line OCR dropped).
            reach = union(near) if near else None
            if covered < 0.5 * area(box) or reach[1] - box[1] > 5 or box[3] - reach[3] > 5:
                items.append(Item(box, "d"))
        # A page number OCR missed: small ink alone in the top or bottom band.
        for it in [i for i in items if i.kind == "d"]:
            b = it.bbox
            if band_of(page, b) and b[2] - b[0] <= 40 and b[3] - b[1] <= 16 and not any(
                    o is not it and intersect(grow(b, 15), o.bbox) for o in items):
                items.remove(it)
                excluded.append(b)
    return items, excluded


def clear_of(box, content, excluded, gap=1.0):
    """Pull the padded crop's edges back from excluded marks that lie outside the content."""
    x0, y0, x1, y1 = box
    for r in excluded:
        if not intersect(r, (x0, y0, x1, y1)) or intersect(r, content):
            continue
        if r[0] >= content[2]:
            x1 = min(x1, r[0] - gap)
        elif r[2] <= content[0]:
            x0 = max(x0, r[2] + gap)
        elif r[1] >= content[3]:
            y1 = min(y1, r[1] - gap)
        elif r[3] <= content[1]:
            y0 = max(y0, r[3] + gap)
    return (x0, y0, x1, y1)


def boxes_in(items, region):
    out = []
    for it in items:
        if it.kind == "w":
            if center_in(it.bbox, region):
                out.append(it.bbox)
        else:
            r = intersect(it.bbox, region)
            if r:
                out.append(r)
    return out


# ---------------------------------------------------------------- anchors and columns

@dataclass(eq=False)
class Cand:
    n: int
    page: int
    word: Word
    col: int = 0
    band: int = 0               # horizontal band of the page (bands are split by full-width rules)
    rank: int | None = None     # page position, not counting blank or scratch-work pages


def voverlap(a, b):
    ov = min(a.y1, b.y1) - max(a.y0, b.y0)
    return ov > 0.5 * min(a.y1 - a.y0, b.y1 - b.y0)


def first_on_line(w, words):
    return not any(v is not w and v.x1 <= w.x0 + 1 and v.x1 >= w.x0 - LINE_GAP and voverlap(v, w)
                   for v in words)


def following_tokens(w, words, k):
    right = sorted((v for v in words if v is not w and v.x0 >= w.x1 - 1 and voverlap(v, w)),
                   key=lambda v: v.x0)
    out, x = [], w.x1
    for v in right:
        if v.x0 - x > 30 or len(out) == k:
            break
        out.append(v.text)
        x = v.x1
    return out


def looks_like_table_row(w, words):
    """Answer-key rows ("1. C 72 26. B") and bubble rows ("1. (A) (B) ...") are not questions.

    "1. A 2 kg block" is a question: a table's P+ is followed by a number or nothing."""
    t = following_tokens(w, words, 6)
    numlike = re.compile(r"^\d{1,3}[.%]?$")
    if t and LETTER_RE.match(t[0]):
        if len(t) == 1 or LETTER_RE.match(t[1]):
            return True
        if numlike.match(t[1]) and (len(t) == 2 or numlike.match(t[2])):
            return True
    return sum(1 for x in t if LETTER_RE.match(x)) >= 3


def wraps_in_paragraph(w, words):
    """A number that starts a wrapped line ("... a factor of / 2. What is ...") lines up
    with the line below it. A question number hangs: its text is indented past it."""
    h = w.y1 - w.y0
    for v in words:
        if v is w or not (w.y1 <= v.y0 <= w.y1 + 1.5 * h) or abs(v.x0 - w.x0) > 1.5:
            continue
        if first_on_line(v, words) and not (ANCHOR_RE.match(v.text) or GLUED_RE.match(v.text)):
            return True
    return False


def find_candidates(pages, scanned=frozenset()):
    out = []
    for p in pages:
        for w in p.words:
            n = anchor_number(w.text, p.num in scanned)
            if not n:
                continue
            if first_on_line(w, p.words) and not looks_like_table_row(w, p.words) \
                    and not wraps_in_paragraph(w, p.words):
                out.append(Cand(n, p.num, w))
    return out


def choice_of(text):
    m = CHOICE_RE.match(text)
    return (m.group(1) or m.group(2)) if m else None


def anchor_number(text, ocr=False):
    m = ANCHOR_RE.match(text) or GLUED_RE.match(text)
    if m:
        return int(m.group(1))
    if ocr:     # needs a digit or a digit look-alike other than I, so "II." never qualifies
        m = OCR_ANCHOR_RE.match(text)
        if m and re.search(r"[\dOoBSZ]", m.group(1)):
            return int(m.group(1).translate(str.maketrans("ilI|!OoBSZ", "1111100852")))
    return None


def cluster_positions(xs, gap=20):
    clusters = []
    for x in sorted(xs):
        if clusters and x - clusters[-1][-1] <= gap:
            clusters[-1].append(x)
        else:
            clusters.append([x])
    return clusters


def cluster_of(clusters, x):
    return next(i for i, c in enumerate(clusters) if c[0] <= x <= c[-1])


def assign_page_columns(cands):
    """Reading order within a page: bands top to bottom, then each band's anchor x0
    values cluster into its columns."""
    by_band = defaultdict(list)
    for c in cands:
        by_band[(c.page, c.band)].append(c)
    for cs in by_band.values():
        clusters = cluster_positions([c.word.x0 for c in cs])
        for c in cs:
            c.col = cluster_of(clusters, c.word.x0)


MAX_PAGE_JUMP = 3      # the next question is never more than this many (non-blank) pages on


def _rank(c):
    return c.page if c.rank is None else c.rank


def _find(cands, i, pred, max_rank):
    for j in range(i, len(cands)):
        if _rank(cands[j]) > max_rank:
            return None
        if pred(cands[j].n):
            return j
    return None


def _question_like(acc):
    """Anchors followed by >=30 pt of their own content (table rows sit ~12 pt apart)."""
    count = 0
    for a, b in zip(acc, acc[1:] + [None]):
        if b is None or b.page != a.page or b.col != a.col or b.word.y0 - a.word.y0 >= 30:
            count += 1
    return count


def sequence_run(cands, max_skip=2):
    """Best run of anchors numbered 1, 2, 3, ... in reading order.

    A candidate is accepted only if its number is the last accepted + 1 and it is at
    most MAX_PAGE_JUMP non-blank pages on. If a later number (up to `max_skip` ahead)
    comes first and is itself followed by its successor before the wanted number shows
    up, the wanted number was missed: skip it and record it as missing.

    The run with the most question-like anchors wins (table rows from worksheets and
    answer sheets don't count), less one per jump over a non-blank page with no
    anchors; ties go to the latest start (front-matter lists that merge into the real
    exam). Returns (accepted, missing_numbers).
    """
    best = None
    first = 1 if any(c.n == 1 for c in cands) else 1 + max_skip     # OCR can lose "1." too
    for s, c in enumerate(cands):
        if c.n > first or (first == 1 and c.n != 1):
            continue
        acc, missing, i = [c], list(range(1, c.n)), s + 1
        while i < len(cands):
            want, limit = acc[-1].n + 1, _rank(acc[-1]) + MAX_PAGE_JUMP
            j = _find(cands, i, lambda n: n == want, limit)
            g = _find(cands, i, lambda n: want < n <= want + max_skip, limit)
            if g is not None and (j is None or g < j):
                conf = _find(cands, g + 1, lambda n: n == cands[g].n + 1, _rank(cands[g]) + MAX_PAGE_JUMP)
                if (conf is None and j is None) or (conf is not None and (j is None or j > conf)):
                    missing += range(want, cands[g].n)
                    acc.append(cands[g])
                    i = g + 1
                    continue
            if j is None:
                break
            acc.append(cands[j])
            i = j + 1
        jumps = sum(1 for a, b in zip(acc, acc[1:]) if _rank(b) - _rank(a) > 1)
        key = (_question_like(acc) - jumps, s)
        if best is None or key > best[0]:
            best = (key, acc, missing)
    return (best[1], best[2]) if best else ([], [])


def locate_anchors(pages, scanned=frozenset()):
    rank, r = {}, 0
    for p in pages:
        r += len(p.words) >= BLANK_WORDS
        rank[p.num] = r
    by_num = {p.num: p for p in pages}
    cands = find_candidates(pages, scanned)
    for c in cands:
        c.rank = rank[c.page]
        c.band = by_num[c.page].band(c.word.y0)
    assign_page_columns(cands)
    cands.sort(key=lambda c: (c.page, c.band, c.col, c.word.y0))
    accepted, missing = sequence_run(cands, max_skip=4 if scanned else 2)
    return (accepted, missing) if len(accepted) >= 2 else ([], [])


def layout_slots(layout, t):
    """The (band, column) a line start sits in, if it is near that column's question numbers."""
    num, b, col, _, w = t
    lefts = layout.cols(num, b)
    return [(b, col)] if lefts and -5 <= w.x0 - lefts[col] <= 25 else []


def after_choices(span, layout):
    """Line starts that open a new question: the first unindented, non-choice line after
    a run of choice lines (x measured from each column's edge)."""
    out, last = [], None
    for t in span:
        num, b, col, _, w = t
        rel = w.x0 - layout.cols(num, b)[col]
        label = choice_of(w.text) or (num in layout.ocr and OCR_CHOICE_RE.match(w.text))
        if label:
            last = rel
        elif last is not None and not STEM_RE.match(w.text):
            if rel <= last + 3:
                out.append(t)
                last = None
        elif STEM_RE.match(w.text):
            last = None
    return out


def fill_gaps(anchors, missing, layout, by_num):
    """Recover question numbers OCR lost, from structure: every question ends with an
    (E) choice. Between found anchors n and m there must be exactly m - n (E) labels;
    question n+i then starts at the first unindented line after the i-th (E) block.
    Gaps that don't add up stay missing."""
    if not missing:
        return anchors, missing
    starts = []         # line-start words in reading order
    for num in sorted({a.page for a in anchors} | set(layout.exam_nums)):
        p = by_num.get(num)
        if not p or not layout.items(num):
            continue
        for w in p.words:
            if first_on_line(w, p.words) and not layout.rf.is_running(p, Line([w])):
                b = p.band(w.y0)
                starts.append((num, b, layout.col_of(num, b, w.x0), w.y0, w))
    starts.sort(key=lambda t: t[:4])
    pos = {id(t[4]): i for i, t in enumerate(starts)}
    found, still = list(anchors), []
    by_n = {a.n: a for a in anchors}
    gaps = defaultdict(list)
    for n in missing:
        lo = max((a.n for a in anchors if a.n < n), default=None)
        hi = min((a.n for a in anchors if a.n > n), default=None)
        gaps[(lo, hi)].append(n)
    for (lo, hi), ns in gaps.items():
        if lo is None or hi is None or id(by_n[lo].word) not in pos or id(by_n[hi].word) not in pos:
            still += ns
            continue
        span = starts[pos[id(by_n[lo].word)] + 1:pos[id(by_n[hi].word)]]
        # OCR sometimes keeps only the period of a number: ". A cylindrical tube ..."
        dots = [t for t in span if re.fullmatch(r"[.,:;]", t[4].text) and t[1:3] in layout_slots(layout, t)
                and following_tokens(t[4], by_num[t[0]].words, 1)]
        if len(dots) == hi - lo - 1:
            found += [Cand(lo + 1 + k, num, w, col, b) for k, (num, b, col, _, w) in enumerate(dots)]
            continue
        after = after_choices(span, layout)
        if len(after) == hi - lo - 1:
            found += [Cand(lo + 1 + k, num, w, col, b) for k, (num, b, col, _, w) in enumerate(after)]
            continue
        es = [i for i, t in enumerate(span) if choice_of(t[4].text) == "E"]
        if len(es) != hi - lo:
            still += ns
            continue
        new = []
        for k, i in enumerate(es[:-1]):
            e = span[i][4]
            nxt = next((t for t in span[i + 1:] if t[4].x0 <= e.x0 + 3 and not choice_of(t[4].text)
                        and not STEM_RE.match(t[4].text)), None)
            if nxt is None:
                break
            num, b, col, _, w = nxt
            new.append(Cand(lo + 1 + k, num, w, col, b))
        if len(new) == hi - lo - 1:
            found += new
        else:
            still += ns
    found.sort(key=lambda a: a.n)
    return found, sorted(still)


# ---------------------------------------------------------------- regions

@dataclass(eq=False)
class Boundary:
    kind: str           # "q" or "set"
    n: int              # question number, or the set's first question
    last: int
    page: int
    band: int
    col: int
    y0: float


class Layout:
    """Where each question's crop comes from.

    A page is a stack of bands split by full-width rules. A band holds one or more
    columns: the book's usual column layout (the clusters of anchor x0 that share a
    band most often), or its own columns when its anchors sit elsewhere, such as a
    single question in an inset column. A band whose content runs across the gutter
    is one full-width column.
    """

    def __init__(self, pages, exam_nums, anchors, rf, ocr):
        self.pages = pages              # num -> Page
        self.exam_nums = exam_nums      # sorted page numbers
        self.rf = rf
        self.ocr = ocr                  # scanned page numbers: OCR text plus ink blobs
        self._items = {}
        self._cols = {}
        self.area_top = {}
        self.clusters = cluster_positions([a.word.x0 for a in anchors])
        self.in_band = defaultdict(list)
        for a in anchors:
            self.in_band[(a.page, a.band)].append(a)
        combos = defaultdict(int)
        for group in self.in_band.values():
            ids = frozenset(self.cluster(a) for a in group)
            if len(ids) > 1:
                combos[ids] += 1
        if combos:
            self.standard = set(max(combos, key=lambda k: (combos[k], len(k))))
        else:
            self.standard = {max(range(len(self.clusters)), key=lambda i: len(self.clusters[i]))} if self.clusters else set()
        self.standard_lefts = [self.clusters[i][0] for i in sorted(self.standard)]
        # Scanned pages shift sideways (left and right pages of a book). Estimate each
        # page's shift from its own anchors, or a same-parity neighbour's.
        self.offset = {}
        for num in exam_nums:
            deltas = [min(a.word.x0 for g in self.in_band for a in self.in_band[g] if g[0] == num and self.cluster(a) == i)
                      - self.clusters[i][0] for i in self.standard
                      if any(g[0] == num and self.cluster(a) == i for g in self.in_band for a in self.in_band[g])]
            if deltas:
                self.offset[num] = statistics.median(deltas)
        for num in exam_nums:
            if num not in self.offset:
                near = sorted((abs(n - num), n) for n in self.offset if (n - num) % 2 == 0)
                self.offset[num] = self.offset[near[0][1]] if near else 0.0
        for num in exam_nums:
            if abs(self.offset[num]) < 15:
                self.offset[num] = 0.0

    def page_lefts(self, num):
        return [x + self.offset.get(num, 0.0) for x in self.standard_lefts]

    def cluster(self, a):
        return cluster_of(self.clusters, a.word.x0)

    def items(self, num):
        if num not in self._items:
            self._items[num] = build_items(self.pages[num], self.rf, num in self.ocr)
        return self._items[num][0]

    def excluded(self, num):
        self.items(num)
        return self._items[num][1]

    def content_box(self, num):
        its = self.items(num)
        if not its:
            p = self.pages[num]
            return (0, 0, p.width, p.height)
        return union(i.bbox for i in its)

    def bottom(self, num):
        return min(self.content_box(num)[3] + PAD, self.pages[num].height)

    # bands

    def nbands(self, num):
        return len(self.pages[num].rules()) + 1

    def band_range(self, num, b):
        p = self.pages[num]
        edges = [0.0] + sorted(r[1] for r in p.rules()) + [p.height]
        return edges[b], edges[b + 1]

    def band_items(self, num, b):
        y0, y1 = self.band_range(num, b)
        return [it for it in self.items(num) if y0 <= (it.bbox[1] + it.bbox[3]) / 2 < y1]

    def band_box(self, num, b):
        its = self.band_items(num, b)
        return union(i.bbox for i in its) if its else None

    def band_limits(self, num, b):
        """Top and bottom of a band's content, or None if it has none."""
        box = self.band_box(num, b)
        if not box:
            return None
        y0, y1 = self.band_range(num, b)
        top = max(box[1], self.area_top.get(num, box[1])) - LEAD
        bot = box[3] + PAD
        top, bot = max(top, y0), min(bot, y1)
        return (top, bot) if bot - top > 2 else None

    # columns

    def cols(self, num, b):
        if (num, b) not in self._cols:
            self._cols[(num, b)] = self._band_cols(num, b)
        return self._cols[(num, b)]

    def _band_cols(self, num, b):
        anchors = self.in_band.get((num, b), [])
        ids = {self.cluster(a) for a in anchors}
        if ids and not ids <= self.standard:
            return [min(a.word.x0 for a in anchors if self.cluster(a) == i) for i in sorted(ids)]
        if len(ids) > 1 or (len(self.standard_lefts) > 1 and self._split_by_gutters(num, b)):
            return self.page_lefts(num)
        if anchors:
            return [min(a.word.x0 for a in anchors)]
        box = self.band_box(num, b)
        return [box[0] if box else 0.0]

    def _split_by_gutters(self, num, b):
        """Content on both sides of every gutter and nothing running across one."""
        its = self.band_items(num, b)
        for left in self.page_lefts(num)[1:]:
            g = left - 3
            if any(it.bbox[0] < g - 1 and it.bbox[2] > g + 1 for it in its):
                return False
            if not any(it.bbox[2] <= g for it in its) or not any(it.bbox[0] >= g for it in its):
                return False
        return True

    def col_of(self, num, b, x):
        lefts = self.cols(num, b)
        return max([k for k, left in enumerate(lefts) if left - (COL_PAD if k == 0 else 3) <= x + 2] or [0])

    def col_range(self, num, b, k):
        lefts = self.cols(num, b)
        box = self.band_box(num, b) or self.content_box(num)
        W = self.pages[num].width
        if len(lefts) == 1:         # a single column spans the band
            return max(0, min(lefts[0] - COL_PAD, box[0] - PAD)), min(W, box[2] + PAD)
        left = lefts[k] - (COL_PAD if k == 0 else 3)
        right = lefts[k + 1] - 4 if k + 1 < len(lefts) else box[2] + PAD
        return max(0, left), min(W, right)

    # crops

    def tighten(self, num, region):
        boxes = boxes_in(self.items(num), region)
        if not boxes:
            return region
        content = union(boxes)
        return clear_of(intersect(grow(content, PAD), region) or region, content, self.excluded(num))

    def has_content(self, num, region):
        return bool(boxes_in(self.items(num), region))

    def tail_split(self, num, region):
        """Where a complete question's content ends inside its region, or None.

        Used when the next question says "above": its figure may sit right after this
        question's (E) choice. The split is the first gap of 5 pt or more below the
        (E) row; a graphical (E) choice is one connected block, so it stays put."""
        its = [it for it in self.items(num) if (center_in(it.bbox, region) if it.kind == "w" else intersect(it.bbox, region))]
        starts = self.choice_starts(num, region)
        complete = self.complete(num, region)
        if not starts or not (complete or (num in self.ocr and len(starts) >= 2)):
            return None
        e = starts[-1]
        end = max([it.bbox[3] for it in its if it.bbox[1] < e.y1 and it.bbox[3] > e.y0] + [e.y1])
        gaps = []
        for y0, y1 in sorted((it.bbox[1], it.bbox[3]) for it in its if it.bbox[1] >= e.y0):
            if y0 > end + 5:
                if complete:
                    return y0
                gaps.append((y0 - end, y0))
            end = max(end, y1)
        # A scan whose labels OCR lost: the true (E) row may be below the last label
        # seen, so take the widest gap instead of the first.
        best = max(gaps, default=None)
        return best[1] if best and best[0] >= 8 else None

    def says_above(self, num, x0, x1, y):
        words = [it.text for it in self.items(num) if it.kind == "w" and center_in(it.bbox, (x0, y - 2, x1, y + 80))]
        return "above" in words or "above." in words or "above," in words or "above?" in words

    def choice_starts(self, num, box):
        """Words that start a line and look like a choice label, top to bottom. On
        scans OCR misreads labels ("©)", "(BE)"), so any label shape counts."""
        p = self.pages[num]
        pattern = OCR_CHOICE_RE if num in self.ocr else CHOICE_RE
        out = [w for w in p.words if center_in(w.bbox, box) and (choice_of(w.text) or pattern.match(w.text))
               and first_on_line(w, p.words)]
        return sorted(out, key=lambda w: w.y0)

    def complete(self, num, box):
        """The box holds a full set of answer choices."""
        if len(self.choice_letters(num, box)) == 5:
            return True
        return num in self.ocr and len(self.choice_starts(num, box)) >= 4

    def choice_letters(self, num, box):
        return {choice_of(it.text) for it in self.items(num)
                if it.kind == "w" and choice_of(it.text) and center_in(it.bbox, box)}

    def segments(self, bounds):
        """Map each boundary to its segments [(page, bbox)], walking slots (page, band,
        column) in reading order.

        Content above a slot's first boundary is a lead-in, such as a figure "shown
        above" its question, unless the previous boundary still lacks some of its
        answer choices (or is a set stem): then it is that boundary's continuation."""
        grid = defaultdict(list)
        for bd in bounds:
            grid[(bd.page, bd.band, bd.col)].append(bd)
        for v in grid.values():
            v.sort(key=lambda bd: bd.y0)
        out, prev, prev_region = {}, None, None
        for num in self.exam_nums:
            if not self.items(num):
                continue        # blank and scratch-work pages
            for b in range(self.nbands(num)):
                lim = self.band_limits(num, b)
                if not lim:
                    continue
                top, bot = lim
                for k in range(len(self.cols(num, b))):
                    x0, x1 = self.col_range(num, b, k)
                    col = [bd for bd in grid.get((num, b, k), []) if bd.y0 - LEAD < bot]
                    starts = [bd.y0 - LEAD for bd in col]
                    for i in range(len(col) - 1):
                        if col[i].kind == "q" and self.says_above(num, x0, x1, col[i + 1].y0):
                            split = self.tail_split(num, (x0, starts[i], x1, starts[i + 1]))
                            if split is not None:
                                starts[i + 1] = split - LEAD
                    lead = (x0, top, x1, starts[0] if col else bot)
                    has_lead = lead[3] - top > 2 and self.has_content(num, lead)
                    if (not has_lead and col and col[0].kind == "q" and prev is not None and prev.kind == "q"
                            and len(out[prev]) == 1 and self.says_above(num, x0, x1, col[0].y0)):
                        # Its figure "above" ended the previous column or page.
                        pnum, (px0, py0, px1, py1) = prev_region
                        split = self.tail_split(pnum, (px0, py0, px1, py1))
                        if split is not None:
                            out[prev] = [(pnum, self.tighten(pnum, (px0, py0, px1, split - LEAD)))]
                            carried = (pnum, self.tighten(pnum, (px0, split - LEAD, px1, py1)))
                            col_first_extra = carried
                        else:
                            col_first_extra = None
                    else:
                        col_first_extra = None
                    if has_lead:
                        wants_more = prev is not None and len(out[prev]) == 1 and (
                            prev.kind == "set" or not self.complete(*out[prev][0]))
                        if wants_more and col and col[0].kind == "q" and self.says_above(num, x0, x1, col[0].y0):
                            wants_more = False      # "shown above": it's this question's figure
                        if wants_more:
                            out[prev].append((num, self.tighten(num, lead)))
                        elif col:
                            starts[0] = top
                    for i, bd in enumerate(col):
                        y1 = starts[i + 1] if i + 1 < len(col) else bot
                        out[bd] = [(num, self.tighten(num, (x0, starts[i], x1, y1)))]
                        if i == 0 and col_first_extra:
                            out[bd].insert(0, col_first_extra)
                        prev, prev_region = bd, (num, (x0, starts[i], x1, y1))
        for bd in bounds:       # anything outside a band with content keeps its own line
            if bd not in out:
                x0, x1 = self.col_range(bd.page, bd.band, bd.col)
                out[bd] = [(bd.page, self.tighten(bd.page, (x0, bd.y0 - LEAD, x1, self.bottom(bd.page))))]
        return out


def find_stems(layout, pages):
    out = []
    for p in pages:
        for ln in p.lines:
            m = STEM_RE.match(ln.text.strip())
            if not m or layout.rf.is_running(p, ln):
                continue
            b = p.band(ln.bbox[1])
            k = layout.col_of(p.num, b, ln.bbox[0])
            out.append(Boundary("set", int(m.group(1)), int(m.group(2)), p.num, b, k, ln.bbox[1]))
    return out


def find_reference(layout, pages, anchors):
    """The TABLE OF INFORMATION block before question 1."""
    q1 = anchors[0]
    hit = None
    for p in pages:
        if p.num > q1.page:
            break
        for r in p.pg.search_for("TABLE OF INFORMATION"):
            if p.num < q1.page or r.y1 < q1.word.y0:
                hit = (p, r)
                break
    if not hit:
        return []
    p, r = hit
    box = layout.content_box(p.num)
    y1 = layout.bottom(p.num)
    if p.num == q1.page:
        y1 = min(a.word.y0 for a in anchors if a.page == p.num) - LEAD
    for ln in p.lines:
        if ln.bbox[1] > r.y1 and ln.text.lower().startswith("directions"):
            y1 = min(y1, ln.bbox[1] - LEAD)
    region = (max(0, box[0] - PAD), r.y0 - LEAD, min(p.width, box[2] + PAD), y1)
    return [(p.num, layout.tighten(p.num, region))]


# ---------------------------------------------------------------- answer key

def page_rows(page, tol=2.0):
    rows = []
    for w in sorted(page.words, key=lambda w: w.cy):
        if rows and abs(w.cy - rows[-1][0]) <= tol:
            rows[-1][1].append(w)
        else:
            rows.append([w.cy, [w]])
    return [[w.text for w in sorted(ws, key=lambda w: w.x0)] for _, ws in rows]


def parse_key_tokens(toks):
    """Parse one row: `number letter [P+] [category]`, repeated for side-by-side column groups."""
    out = []
    i, n = 0, len(toks)

    def starts_entry(j):
        return j + 1 < n and KEYNUM_RE.match(toks[j]) and LETTER_RE.match(toks[j + 1])

    while i < n:
        if not starts_entry(i):
            i += 1
            continue
        if i + 2 < n and LETTER_RE.match(toks[i + 2]):     # bubble row: 1 A B C D E
            return []
        q, letter = int(toks[i].rstrip(".")), LETTER_RE.match(toks[i + 1]).group(1)
        i += 2
        pplus = None
        if i < n and PPLUS_RE.match(toks[i]) and not starts_entry(i):
            pplus = int(PPLUS_RE.match(toks[i]).group(1))
            i += 1
        cat = []
        while i < n and not starts_entry(i):
            cat.append(toks[i])
            i += 1
        cat = [c for c in cat if re.search(r"[A-Za-z]", c)]
        out.append((q, letter, pplus, " ".join(cat[:4]) or None))
    return out


def parse_key_rows(rows):
    """Returns ({n: {answer, pplus, category}}, conflicts)."""
    key, conflicts = {}, []
    for toks in rows:
        for q, letter, pplus, cat in parse_key_tokens(toks):
            if not 1 <= q <= 200:
                continue
            if q in key and key[q]["answer"] != letter:
                conflicts.append(q)
                continue
            key.setdefault(q, {"answer": letter, "pplus": pplus, "category": cat})
    cats = [v["category"] for v in key.values() if v["category"]]
    if not key or len(cats) < 0.5 * len(key) or not 2 <= len(set(cats)) <= 15:
        for v in key.values():
            v["category"] = None
    return key, conflicts


def find_key(pages, after):
    post = [p for p in pages if p.num > after]
    start = next((i for i, p in enumerate(post)
                  if any(k in re.sub(r"\s+", " ", p.text.lower()) for k in KEY_PHRASES)), None)
    if start is None:
        return {}, [], []
    rows, used = [], []
    for p in post[start:start + 6]:
        entries = [e for r in page_rows(p) for e in parse_key_tokens(r)]
        if len(entries) < 3:
            if used:
                break
            continue
        rows += page_rows(p)
        used.append(p.num)
    key, conflicts = parse_key_rows(rows)
    return key, conflicts, used or [post[start].num]


# ---------------------------------------------------------------- conversion table

SCALE_TOKEN_RE = re.compile(r"(\d+)\s*[-–—−]\s*(\d+)|([<>]?\d+)|(\S+)")


def scale_tokens(toks):
    out = []
    for m in SCALE_TOKEN_RE.finditer(" ".join(toks)):
        if m.group(1):
            a, b = int(m.group(1)), int(m.group(2))
            out.append(("range", min(a, b), max(a, b)))
        elif m.group(3):
            out.append(("int", m.group(3)))
        else:
            out.append(("word", m.group(4)))
    return out


def parse_scale_tokens(toks):
    """One row of `raw|range scaled [pct]`, repeated for side-by-side groups.

    Returns [(lo, hi, scaled, pct)]."""
    t = scale_tokens(toks)

    def raw(x):
        if x[0] == "range":
            return (x[1], x[2]) if x[2] <= 140 else None
        if x[0] == "int" and x[1].isdigit() and int(x[1]) <= 140:
            return (int(x[1]), int(x[1]))
        return None

    def scaled(x):
        return int(x[1]) if x[0] == "int" and x[1].isdigit() and 150 <= int(x[1]) <= 999 else None

    def pct(x):         # "<1" means 0
        if x[0] != "int" or int(x[1].lstrip("<>")) > 100:
            return None
        return max(0, int(x[1][1:]) - 1) if x[1].startswith("<") else int(x[1].lstrip(">"))

    out, i = [], 0
    while i < len(t):
        r = raw(t[i])
        s = scaled(t[i + 1]) if r and i + 1 < len(t) else None
        if s is None:
            i += 1
            continue
        i += 2
        p = None
        if i < len(t) and pct(t[i]) is not None and not (i + 1 < len(t) and scaled(t[i + 1])):
            p = pct(t[i])
            i += 1
        out.append((r[0], r[1], s, p))
    return out


def expand_scale(entries):
    """Expand ranges to one entry per integer raw score. Returns (scale, duplicate_raws)."""
    seen, dups = {}, []
    for lo, hi, s, p in entries:
        for raw in range(lo, hi + 1):
            if raw in seen:
                dups.append(raw)
                continue
            seen[raw] = {"raw": raw, "scaled": s} | ({"pct": p} if p is not None else {})
    return [seen[r] for r in sorted(seen)], dups


def validate_scale(scale, dups=()):
    if not scale:
        return ["missing"]
    probs = [f"duplicate raw {d}" for d in sorted(set(dups))]
    for a, b in zip(scale, scale[1:]):
        if b["raw"] - a["raw"] > 1:
            probs.append(f"gap at raw {a['raw'] + 1}..{b['raw'] - 1}")
        if b["scaled"] < a["scaled"]:
            probs.append(f"decreases at raw {b['raw']}")
    for e in scale:
        if not 200 <= e["scaled"] <= 990 or e["scaled"] % 10:
            probs.append(f"bad scaled {e['scaled']} at raw {e['raw']}")
    return probs


def find_scale(pages, after):
    best = None
    for p in pages:
        if p.num <= after or not any(k in re.sub(r"\s+", " ", p.text.lower()) for k in SCALE_PHRASES):
            continue
        entries = [e for r in page_rows(p) for e in parse_scale_tokens(r)]
        if entries and (best is None or len(entries) > len(best[1])):
            best = (p.num, entries)
    if not best:
        return [], [], None
    scale, dups = expand_scale(best[1])
    return scale, dups, best[0]


# ---------------------------------------------------------------- scoring rule and time

PENALTY_RE = re.compile(
    r"one[-\s]?(?:fourth|quarter)|¼|\b[a-z]\s*/\s*4\b"
    r"|(?:subtract\w*|minus|deduct\w*)\W+(?:\w+\W+){0,4}1\s*[/⁄]\s*4"
    r"|\bdivid\w*\b.{0,60}\bby (?:4|four)\b|\b(?:correction|adjustment)(?: made)? for guessing\b"
    r"|\bC\s*[-–—−]\s*[I1l|]\s*/\s*4\b", re.I)
NO_PENALTY_RE = re.compile(
    r"nothing is (?:subtracted|deducted)|no (?:penalty|deduction)s? for (?:wrong|incorrect|guess)"
    r"|not (?:be )?penalized for (?:wrong|incorrect|guess)", re.I)
NUMBER_RIGHT_RE = re.compile(
    r"number of (?:questions )?(?:you )?answer(?:ed)? correctly|number of correct answers"
    r"|number (?:of questions )?answered correctly", re.I)
ROUND_DOWN_RE = re.compile(r"round(?:ed|ing)?\s+down|truncat|(?:drop|ignore)\w* (?:the |any )?fraction", re.I)

NUM_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "ten": 10, "fifteen": 15, "twenty": 20,
             "thirty": 30, "forty-five": 45, "forty": 40, "fifty": 50}
_NUM = r"(\d+|" + "|".join(NUM_WORDS) + r")"
TIME_RE = re.compile(rf"\b{_NUM}\s*hours?(?:\s*(?:and\s*)?{_NUM}\s*minutes?)?|\b(\d{{2,3}})\s*minutes?\b", re.I)


def _num(s):
    return int(s) if s.isdigit() else NUM_WORDS[s.lower()]


def detect_rule(texts):
    """texts: [(page, text)]. Returns (rule, source, page, problem)."""
    pen = next((p for p, t in texts if PENALTY_RE.search(t)), None)
    nop = next((p for p, t in texts if NO_PENALTY_RE.search(t)), None)
    num = next((p for p, t in texts if NUMBER_RIGHT_RE.search(t)), None)
    if pen and not nop:
        return "R-W/4", "text", pen, None
    if pen and nop:
        return "R", "inferred", nop, "conflicting penalty text, using R"
    if nop or num:
        return "R", "text", nop or num, None
    return "R", "inferred", None, "not found, using R"


def detect_time(texts):
    """Returns (minutes, page) or (None, None). Prefers values introduced by "time"."""
    best = None
    for p, t in texts:
        for m in TIME_RE.finditer(t):
            if m.group(3):
                mins = int(m.group(3))
            else:
                mins = _num(m.group(1)) * 60 + (_num(m.group(2)) if m.group(2) else 0)
            if not 30 <= mins <= 300:
                continue
            score = 2 * bool(re.search(r"time", t[max(0, m.start() - 40):m.start()], re.I))
            score += 90 <= mins <= 240
            if best is None or score > best[0]:
                best = (score, mins, p)
    return (best[1], best[2]) if best else (None, None)


def detect_rounding(texts):
    return "down" if any(ROUND_DOWN_RE.search(t) for _, t in texts) else "half-up"


# ---------------------------------------------------------------- overrides

def _bbox(v):
    if not (isinstance(v, list) and len(v) == 4 and all(isinstance(x, (int, float)) for x in v)):
        raise ValueError(f"bbox must be [x0, y0, x1, y1], got {v!r}")
    return tuple(float(x) for x in v)


def _segs(v):
    return [(int(s["page"]), _bbox(s["bbox"])) for s in v]


def load_overrides(path):
    if not path.exists():
        return {}
    try:
        ov = json.loads(path.read_text())
    except (OSError, ValueError) as e:
        raise Fatal(f"overrides.json: {e}")
    if not isinstance(ov, dict):
        raise Fatal("overrides.json: top level must be an object")
    return ov


def apply_overrides(ex, ov):
    """Each override key replaces only what it names."""
    ex = copy.deepcopy(ex)
    try:
        if "expected" in ov:
            ex["expected"] = int(ov["expected"])
        if "timeLimitMin" in ov:
            ex["time"] = (int(ov["timeLimitMin"]), "override")
        scoring = ov.get("scoring", {})
        if "raw" in scoring:
            if scoring["raw"] not in ("R", "R-W/4"):
                raise ValueError(f"scoring.raw must be R or R-W/4, got {scoring['raw']!r}")
            ex["rule"] = (scoring["raw"], "override")
        if "rounding" in scoring:
            ex["rounding"] = scoring["rounding"]
        for q, letter in ov.get("answers", {}).items():
            letter = letter.strip().upper() if isinstance(letter, str) else letter
            ex["answers"].setdefault(int(q), {"pplus": None, "category": None})["answer"] = letter
        for q, segs in ov.get("crops", {}).items():
            ex["questions"][int(q)] = {"segments": _segs(segs), "override": True}
        for s in ov.get("sets", []):
            first, last = int(s["first"]), int(s["last"])
            hit = [x for x in ex["sets"] if not (x["last"] < first or x["first"] > last)]
            ex["sets"] = [x for x in ex["sets"] if all(x is not h for h in hit)]
            if s.get("remove"):
                continue
            if "bbox" in s:
                segs = [(int(s["page"]), _bbox(s["bbox"]))]
            elif hit:
                segs = hit[0]["segments"]
            else:
                raise ValueError(f"set {first}-{last} needs page and bbox")
            ex["sets"].append({"first": first, "last": last, "segments": segs, "override": True})
        ex["sets"].sort(key=lambda x: x["first"])
        if "reference" in ov:
            ex["reference"] = _segs(ov["reference"])
            ex["refOverride"] = True
        if "scale" in ov:
            entries = [(int(e["raw"]), int(e["raw"]), int(e["scaled"]), e.get("pct")) for e in ov["scale"]]
            ex["scale"], ex["scaleDups"] = expand_scale(entries)
            ex["scaleSource"] = "override"
        ex["ignore"] = {normalize_flag_id(f) for f in ov.get("ignoreFlags", [])}
    except (KeyError, TypeError, ValueError, AttributeError) as e:
        raise Fatal(f"overrides.json: {e}")
    return ex


# ---------------------------------------------------------------- flags

@dataclass
class Flag:
    code: str
    target: str
    detail: str
    page: int | None = None
    pages: list = field(default_factory=list)

    @property
    def id(self):
        return normalize_flag_id(f"{self.code}:{self.target}")

    def line(self):
        t = self.target
        if re.fullmatch(r"q\d+", t):
            t = f"q{int(t[1:]):03d}"
        parts = [f"{self.code:<7}", t]
        if self.page:
            parts.append(f"p{self.page}")
        parts.append(self.detail)
        if self.pages:
            parts.append("-> " + " ".join(f"review/p{n:03d}.png" for n in self.pages))
        return " ".join(parts)

    def to_json(self):
        return {"code": self.code, "target": self.target, "page": self.page,
                "detail": self.detail, "review": [f"review/p{n:03d}.png" for n in self.pages]}


def normalize_flag_id(s):
    code, _, target = s.partition(":")
    target = target.strip().lower()
    if re.fullmatch(r"q\d+", target):
        target = f"q{int(target[1:])}"
    return f"{code.strip().upper()}:{target}"


def validate(ex, layout):
    flags = []
    qs, N = ex["questions"], ex["expected"]
    nums = sorted(qs)
    # ETS sometimes prints "90. THIS ITEM WAS NOT SCORED.": no key, no choices.
    unscored = {n for n in nums if re.search(r"\bnot\s+scored\b", " ".join(
        it.text for num, box in qs[n]["segments"] for it in layout.items(num)
        if it.kind == "w" and center_in(it.bbox, box)), re.I)}

    # Count and numbering
    if N is None:
        flags.append(Flag("COUNT", "count", f"found={len(qs)} expected unknown (no key, no --expected)"))
    elif len(qs) != N:
        flags.append(Flag("COUNT", "count", f"found={len(qs)} expected={N}"))
    for n in range(1, (max(nums) if nums else 0) + 1):
        if n not in qs:
            nxt = next((m for m in nums if m > n), None)
            hint = f"; its content is likely inside q{nxt:03d}'s crop, so fix both" if nxt else ""
            flags.append(Flag("NUM_GAP", f"q{n}", "missing" + hint, page=ex["gapPages"].get(n),
                              pages=[ex["gapPages"][n]] if n in ex["gapPages"] else []))

    # Answer key
    key_page = ex["keyPages"][0] if ex["keyPages"] else None
    span = range(1, (N or (max(nums) if nums else 0)) + 1)
    missing = [n for n in span if n not in ex["answers"] and n not in unscored]
    bad = [n for n in span if n in ex["answers"] and ex["answers"][n].get("answer") not in list("ABCDE")]
    conflicts = [n for n in ex["keyConflicts"] if n not in ex["overriddenAnswers"]]
    kp = [key_page] if key_page else []
    if not ex["answers"] and ex.get("keyScanned"):
        flags.append(Flag("KEY", "key", f"key page is a scan: enter answers 1-{len(span)} in overrides.json "
                                        "from this overlay (OCR is not trusted for answers)", page=key_page, pages=kp))
    elif not ex["answers"]:
        flags.append(Flag("KEY", "key", "answer key not found", page=key_page, pages=kp))
    elif len(missing) > 5:
        flags.append(Flag("KEY", "key", f"found={len(span) - len(missing)}/{len(span)} "
                                        f"missing={','.join(map(str, missing[:8]))}"
                                        f"{'...' if len(missing) > 8 else ''}",
                          page=key_page, pages=kp))
    else:
        flags += [Flag("KEY", f"q{n}", "missing answer", page=key_page, pages=kp) for n in missing]
    flags += [Flag("KEY", f"q{n}", f"invalid letter {ex['answers'][n].get('answer')!r}",
                   page=key_page, pages=kp) for n in bad]
    flags += [Flag("KEY", f"q{n}", "conflicting answers in key", page=key_page, pages=kp)
              for n in conflicts]

    # Conversion table
    probs = validate_scale(ex["scale"], ex["scaleDups"])
    if probs:
        sp = ex["scalePage"] if ex["scaleSource"] != "override" else None
        flags.append(Flag("SCALE", "scale", "; ".join(probs[:3]) + (f" +{len(probs) - 3} more" if len(probs) > 3 else ""),
                          page=sp, pages=[sp] if sp else []))

    # Rule and time
    rule, src = ex["rule"]
    if src == "inferred":
        rp = ex["rulePage"] or key_page
        flags.append(Flag("RULE", "rule", ex["ruleProblem"] or "inferred", page=rp, pages=[rp] if rp else []))
    if ex["time"][1] == "inferred":
        flags.append(Flag("RULE", "time", f"time limit not found, inferred {ex['time'][0]} min",
                          page=key_page, pages=kp))

    # Reference
    if not ex["reference"]:
        flags.append(Flag("REF", "ref", "Table of Information not found"))

    # Sets
    claimed = {}
    for s in ex["sets"]:
        sid = f"{s['first']}-{s['last']}"
        page = s["segments"][0][0]
        problem = None
        if s["first"] >= s["last"]:
            problem = "range is empty or reversed"
        elif s["last"] - s["first"] + 1 > MAX_SET:
            problem = f"range covers {s['last'] - s['first'] + 1} questions"
        elif not s.get("override") and s.get("stemFirst") != s["first"]:
            problem = f"next question after stem is {s.get('stemFirst')}"
        elif any(n not in qs for n in range(s["first"], s["last"] + 1)):
            problem = "range includes questions not found"
        elif any(n in claimed for n in range(s["first"], s["last"] + 1)):
            problem = f"overlaps set {claimed[next(n for n in range(s['first'], s['last'] + 1) if n in claimed)]}"
        if problem:
            flags.append(Flag("SET", sid, problem, page=page, pages=[page]))
        for n in range(s["first"], s["last"] + 1):
            claimed.setdefault(n, sid)

    # Per-question geometry
    heights = [b[3] - b[1] for q in qs.values() for _, b in q["segments"]]
    med = statistics.median(heights) if heights else 0
    for n in nums:
        q = qs[n]
        segs = q["segments"]
        page = segs[0][0]
        if n in unscored:
            continue
        letters = []
        for num, box in segs:
            letters += [choice_of(it.text) for it in layout.items(num)
                        if it.kind == "w" and choice_of(it.text) and center_in(it.bbox, box)]
        scanned = all(num in layout.ocr for num, _ in segs)
        if scanned and sorted(letters) != list("ABCDE"):
            # OCR misreads choice labels, so count label-like line starts instead:
            # flag only graphical or clipped choices (<=2) and merged questions (>=7).
            looks = sum(len(layout.choice_starts(num, box)) for num, box in segs)
            if 3 <= looks <= 6 and len(letters) == len(set(letters)):
                letters = list("ABCDE")
        if sorted(letters) != list("ABCDE"):
            if not letters:
                detail = "found=0 (graphical choices?)"
            else:
                miss = [c for c in "ABCDE" if c not in letters]
                dup = sorted({c for c in letters if letters.count(c) > 1})
                detail = f"found={len(letters)}" + (f" missing={','.join(miss)}" if miss else "") \
                         + (f" dup={','.join(dup)}" if dup else "")
            flags.append(Flag("CHOICES", f"q{n}", detail, page=page, pages=[page]))
        if q.get("override"):
            continue
        for num, box in segs:
            h = box[3] - box[1]
            # A tall crop holding exactly (A)-(E) and nothing after its (E) block is
            # a big figure or graphical choices, not a broken region.
            clean = sorted(letters) == list("ABCDE") and layout.tail_split(num, box) is None
            tall = h > 2.2 * med and not clean
            if med and (tall or (h < 0.35 * med and len(segs) == 1)):
                extra = ", content after (E)" if tall and sorted(letters) == list("ABCDE") else ""
                flags.append(Flag("HEIGHT", f"q{n}", f"h={h:.0f}pt ({h / med:.1f}x median{extra})",
                                  page=num, pages=[num]))
                break
        if len(segs) > 1:
            more = sorted({num for num, _ in segs})
            flags.append(Flag("SPLIT", f"q{n}", f"continues on p{segs[1][0]}" if segs[1][0] != page
                              else "continues in next column", page=page, pages=more))

    flags = [f for f in flags if f.id not in ex["ignore"]]
    flags.sort(key=lambda f: (FLAG_ORDER.index(f.code),
                              int(f.target[1:]) if re.fullmatch(r"q\d+", f.target) else 0))
    return flags


# ---------------------------------------------------------------- output

def render_png(pg, box, path):
    pg.get_pixmap(matrix=pymupdf.Matrix(ZOOM, ZOOM), clip=pymupdf.Rect(box)).save(path)


def render_stacked(doc, segs, path, gap=12):
    imgs = []
    for num, box in segs:
        pix = doc[num - 1].get_pixmap(matrix=pymupdf.Matrix(ZOOM, ZOOM), clip=pymupdf.Rect(box))
        imgs.append(Image.frombytes("RGB", (pix.width, pix.height), pix.samples))
    out = Image.new("RGB", (max(i.width for i in imgs), sum(i.height for i in imgs) + gap * (len(imgs) - 1)),
                    "white")
    y = 0
    for i in imgs:
        out.paste(i, (0, y))
        y += i.height + gap
    out.save(path)


def render_overlay(pg, boxes, path):
    """Page at 1.2x with every crop box and rulers in PDF points (labels every 50 pt)."""
    pix = pg.get_pixmap(matrix=pymupdf.Matrix(REVIEW_ZOOM, REVIEW_ZOOM))
    page_img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    M = 30
    img = Image.new("RGB", (page_img.width + M, page_img.height + M), "white")
    img.paste(page_img, (M, M))
    d = ImageDraw.Draw(img)
    font = ImageFont.load_default(size=11)
    blue, W, H = (0, 90, 200), pg.rect.width, pg.rect.height

    def X(x):
        return M + x * REVIEW_ZOOM

    def Y(y):
        return M + y * REVIEW_ZOOM

    for v in range(0, int(W) + 1, 10):
        d.line([(X(v), M - (10 if v % 50 == 0 else 4)), (X(v), M - 1)], fill=blue)
        if v % 50 == 0:
            d.text((X(v) + 2, 1), str(v), fill=blue, font=font)
    for v in range(0, int(H) + 1, 10):
        d.line([(M - (10 if v % 50 == 0 else 4), Y(v)), (M - 1, Y(v))], fill=blue)
        if v % 50 == 0:
            d.text((1, Y(v) + 1), str(v), fill=blue, font=font)
    for label, box, color in boxes:     # labels sit above the top-right corner, off the text
        d.rectangle([X(box[0]), Y(box[1]), X(box[2]), Y(box[3])], outline=color, width=2)
        tw = d.textlength(label, font=font)
        at = (X(box[2]) - tw - 2, max(M, Y(box[1]) - 13))
        d.text(at, label, fill=color, font=font)
    img.save(path)


def to_json(v, indent=0):
    """JSON with one line per leaf object (questions, sets, scale rows)."""
    def scalar(x):
        return x is None or isinstance(x, (str, int, float, bool))

    def flat(x):
        return all(scalar(y) or (isinstance(y, list) and all(scalar(z) for z in y)) for y in x.values())

    sp = "  " * indent
    if isinstance(v, dict) and v and not flat(v):
        body = ",\n".join(f"{sp}  {json.dumps(k)}: {to_json(x, indent + 1)}" for k, x in v.items())
        return "{\n" + body + f"\n{sp}}}"
    if isinstance(v, list) and v and not all(scalar(x) for x in v):
        body = ",\n".join(f"{sp}  {to_json(x, indent + 1)}" for x in v)
        return "[\n" + body + f"\n{sp}]"
    return json.dumps(v, ensure_ascii=False)


def write_index(exams_dir):
    index = []
    for f in sorted(exams_dir.glob("*/exam.json")):
        try:
            e = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        index.append({"id": e["id"], "title": e["title"], "questionCount": e["questionCount"],
                      "timeLimitMin": e["timeLimitMin"]})
    (exams_dir / "index.json").write_text(to_json(index) + "\n")


def make_title(stem):
    t = re.sub(r"[_-]+", " ", stem).strip()
    return t.upper() if re.fullmatch(r"[A-Za-z]{1,4}\d{3,}", t) else t


def report(exam_id, ex, flags, ocr):
    N = ex["expected"]
    span = range(1, (N or 0) + 1)
    keyed = sum(1 for n in span if ex["answers"].get(n, {}).get("answer") in list("ABCDE"))
    scale = ex["scale"]
    scale_range = f"{scale[0]['raw']}..{scale[-1]['raw']}" if scale else "none"
    rule, rsrc = ex["rule"]
    head = (f"{exam_id}: q={len(ex['questions'])}/{N or '?'} key={keyed}/{N or '?'} "
            f"scale={scale_range} "
            f"sets={len(ex['sets'])} ref={len(ex['reference'])} ocr={'yes' if ocr else 'no'} "
            f"rule={rule}({rsrc}) time={ex['time'][0]}({ex['time'][1]})")
    lines = [head]
    if flags:
        lines.append(f"FLAGS ({len(flags)}):")
        room = MAX_REPORT - 2
        shown = flags if len(flags) <= room else flags[:room - 1]
        lines += [f.line() for f in shown]
        if len(shown) < len(flags):
            lines.append(f"+{len(flags) - len(shown)} more")
    return lines


# ---------------------------------------------------------------- pipeline

def scanned_pages(doc):
    """Pages that are images with (almost) no text layer. A page can be several images."""
    out = set()
    for pg in doc:
        if len(pg.get_text("words")) >= 50:
            continue
        a = pg.rect.width * pg.rect.height
        if sum(area(intersect(i["bbox"], tuple(pg.rect)) or (0, 0, 0, 0)) for i in pg.get_image_info()) >= 0.4 * a:
            out.add(pg.number + 1)
    return out


def needs_ocr(doc):
    """Scans outnumber text pages. Books can mix them: typeset front matter, scanned test."""
    rich = sum(1 for pg in doc if len(pg.get_text("words")) >= 50)
    scanned = len(scanned_pages(doc))
    return scanned >= 3 and scanned > rich


def open_pdf(pdf):
    try:
        doc = pymupdf.open(pdf)
    except Exception as e:
        raise Fatal(f"cannot open {pdf}: {e}")
    if doc.needs_pass:
        raise Fatal(f"{pdf} is password-protected")
    if doc.page_count == 0:
        raise Fatal(f"{pdf} has no pages")
    return doc


def text_layer(pdf, out):
    """Returns (doc, scanned page numbers). Scanned pages are cropped by pixels."""
    doc = open_pdf(pdf)
    ocr_pdf = out / "source-ocr.pdf"
    if not needs_ocr(doc):
        return doc, set()
    scanned = scanned_pages(doc)
    if not ocr_is_current(ocr_pdf, pdf, scanned):
        exe = shutil.which("ocrmypdf")
        if not exe:
            raise Fatal("scanned PDF needs OCR but ocrmypdf is not installed (brew install ocrmypdf)")
        # OCR exactly the scanned pages, so typeset pages (often the answer key) keep
        # their own text. psm 4 (one column of variable-size text) keeps far more
        # question numbers on low-resolution scans than tesseract's default.
        cmd = [exe, "--deskew", "--rotate-pages", "--force-ocr", "--pages", ",".join(map(str, sorted(scanned))),
               "--tesseract-pagesegmode", "4", "--output-type", "pdf", str(pdf), str(ocr_pdf)]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            tail = (r.stderr.strip().splitlines() or ["?"])[-1]
            raise Fatal(f"ocrmypdf failed (exit {r.returncode}): {tail}")
        marked = pymupdf.open(ocr_pdf)
        marked.set_metadata({**marked.metadata, "keywords": ocr_key(scanned)})
        marked.saveIncr()
        marked.close()
    return open_pdf(ocr_pdf), scanned


def ocr_key(scanned):
    return f"{OCR_SETTINGS} pages={','.join(map(str, sorted(scanned)))}"


def ocr_is_current(ocr_pdf, pdf, scanned):
    if not ocr_pdf.exists() or ocr_pdf.stat().st_mtime < pdf.stat().st_mtime:
        return False
    try:
        return pymupdf.open(ocr_pdf).metadata.get("keywords") == ocr_key(scanned)
    except Exception:
        return False


def parse_pages_arg(s):
    m = re.fullmatch(r"\s*(\d+)\s*-\s*(\d+)\s*", s or "")
    if not m:
        raise Fatal(f"--pages must look like FIRST-LAST, got {s!r}")
    return int(m.group(1)), int(m.group(2))


def run_import(pdf, exam_id, out, args, exams_dir):
    out.mkdir(parents=True, exist_ok=True)
    for sub in ("img", "review"):
        shutil.rmtree(out / sub, ignore_errors=True)
    ov = load_overrides(out / "overrides.json")
    doc, ocr = text_layer(pdf, out)
    pages = [Page(doc[i], i + 1) for i in range(doc.page_count)]
    for p in pages:
        p.scanned = p.num in ocr
    by_num = {p.num: p for p in pages}

    # Exam pages and anchors
    rng = parse_pages_arg(args.pages) if args.pages else tuple(ov["pages"]) if "pages" in ov else None
    scope = [p for p in pages if not rng or rng[0] <= p.num <= rng[1]]
    anchors = []
    if not rng:     # the exam starts at or after the Table of Information
        toi = next((p.num for p in pages if p.pg.search_for("TABLE OF INFORMATION")), None)
        if toi:
            anchors, missing = locate_anchors([p for p in scope if p.num >= toi], ocr)
    if not anchors:
        anchors, missing = locate_anchors(scope, ocr)
    if not anchors and not ov.get("crops"):
        raise Fatal("no questions found")
    if anchors:
        first, last = rng or (anchors[0].page, anchors[-1].page)
    else:
        first, last = rng or (1, doc.page_count)
    exam_nums = list(range(first, last + 1))
    exam_pages = [by_num[n] for n in exam_nums if n in by_num]

    rf = RunningFilter(exam_pages)
    layout = Layout(by_num, exam_nums, anchors, rf, ocr)
    for a in anchors:
        a.col = layout.col_of(a.page, a.band, a.word.x0)
    anchors, missing = fill_gaps(anchors, missing, layout, by_num)

    questions, sets, reference = {}, [], []
    gap_pages = {}
    if anchors:
        bounds = [Boundary("q", a.n, a.n, a.page, a.band, a.col, a.word.y0) for a in anchors]
        bounds += find_stems(layout, exam_pages)
        # On the first page, content above the first question (directions, the
        # Table of Information) never belongs to a question, unless question 1 says
        # its figure is "above": then it starts below the directions paragraph.
        q1_top = min(b.y0 for b in bounds if b.page == first)
        layout.area_top[first] = q1_top
        q1 = anchors[0]
        x0, x1 = layout.col_range(q1.page, q1.band, q1.col)
        if q1.page == first and layout.says_above(first, x0, x1, q1.word.y0):
            p = by_num[first]
            dirs = sorted((ln.bbox for ln in p.lines if ln.bbox[3] < q1_top), key=lambda b: b[1])
            start = next((i for i, ln in enumerate(p.lines) if ln.text.lower().startswith("directions")
                          and ln.bbox[3] < q1_top), None)
            if start is not None:
                y = p.lines[start].bbox[3]
                for b in dirs:      # the rest of the directions paragraph
                    if b[1] > y - 2 and b[1] - y < 6:
                        y = max(y, b[3])
                layout.area_top[first] = y + 2
        segs = layout.segments(bounds)
        order = sorted(bounds, key=lambda b: (b.page, b.band, b.col, b.y0))
        for i, b in enumerate(order):
            if b.kind == "q":
                questions[b.n] = {"segments": segs[b], "override": False}
            else:
                nxt = next((x.n for x in order[i + 1:] if x.kind == "q"), None)
                sets.append({"first": b.n, "last": b.last, "segments": segs[b], "stemFirst": nxt})
        reference = find_reference(layout, pages, anchors)
        for n in missing:       # the page of the nearest question found
            gap_pages[n] = min(anchors, key=lambda a: (abs(a.n - n), a.n)).page

    # Answer key, conversion table, rule and time
    key, conflicts, key_pages = find_key(pages, last)
    key_scanned = bool(key and set(key_pages) & ocr)
    ocr_key_max = max(key) if key else None
    if key_scanned:     # OCR misreads letters silently: answers must come from overrides.json
        key, conflicts = {}, []
    scale, dups, scale_page = find_scale(pages, last)
    texts = [(p.num, re.sub(r"\s+", " ", p.text)) for p in pages if p.num <= first or p.num > last]
    rule, rsrc, rule_page, rule_problem = detect_rule(texts)
    minutes, _ = detect_time(texts)
    expected_guess = max(key) if key else None
    if key_scanned:
        expected_guess = max(ocr_key_max, anchors[-1].n if anchors else 0)

    ex = {
        "questions": questions, "answers": key, "sets": sets, "reference": reference,
        "scale": scale, "scaleDups": dups, "scalePage": scale_page, "scaleSource": "text",
        "rule": (rule, rsrc), "rulePage": rule_page, "ruleProblem": rule_problem,
        "rounding": detect_rounding(texts),
        "time": (minutes, "text") if minutes else (None, "inferred"),
        "expected": expected_guess, "keyPages": key_pages, "keyConflicts": conflicts,
        "gapPages": gap_pages, "ignore": set(), "keyScanned": key_scanned,
    }
    ex = apply_overrides(ex, ov)
    ex["overriddenAnswers"] = {int(q) for q in ov.get("answers", {})}
    if args.expected:
        ex["expected"] = args.expected
    if ex["time"][0] is None:
        n = ex["expected"] or len(ex["questions"])
        ex["time"] = (170 if n >= 90 else 120, "inferred")
    for q in ex["questions"].values():
        for num, _ in q["segments"]:
            if num not in by_num:
                raise Fatal(f"overrides.json: page {num} out of range 1..{doc.page_count}")

    flags = validate(ex, layout)

    # Render crops
    img = out / "img"
    img.mkdir()
    boxes = defaultdict(list)
    qjson = []
    set_of = {}
    sjson = []
    for s in ex["sets"]:
        sid = f"{s['first']}-{s['last']}"
        name = f"img/set-{sid}.png"
        render_stacked(doc, s["segments"], out / name)
        sjson.append({"id": sid, "first": s["first"], "last": s["last"], "image": name})
        for num, box in s["segments"]:
            boxes[num].append((f"set {sid}", box, (0, 140, 60)))
        for n in range(s["first"], s["last"] + 1):
            set_of.setdefault(n, sid)
    for n in sorted(ex["questions"]):
        segs = ex["questions"][n]["segments"]
        names = []
        for i, (num, box) in enumerate(segs):
            name = f"img/q{n:03d}{'' if i == 0 else f'-{i + 1}'}.png"
            render_png(doc[num - 1], box, out / name)
            names.append(name)
            boxes[num].append((str(n), box, (210, 0, 0)))
        a = ex["answers"].get(n, {})
        qjson.append({"n": n, "page": segs[0][0], "images": names, "answer": a.get("answer"),
                      "set": set_of.get(n), "pplus": a.get("pplus"), "category": a.get("category")})
    rjson = []
    for i, (num, box) in enumerate(ex["reference"]):
        name = f"img/reference-{i + 1}.png"
        render_png(doc[num - 1], box, out / name)
        rjson.append(name)
        boxes[num].append(("ref", box, (120, 0, 160)))

    review_pages = sorted({n for f in flags for n in f.pages})
    if review_pages:
        (out / "review").mkdir()
        for num in review_pages:
            render_overlay(doc[num - 1], boxes[num], out / "review" / f"p{num:03d}.png")

    exam = {
        "version": 1,
        "id": exam_id,
        "title": ov.get("title") or make_title(pdf.stem),
        "source": pdf.name,
        "importedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "questionCount": len(qjson),
        "timeLimitMin": ex["time"][0],
        "timeLimitSource": ex["time"][1],
        "scoring": {"raw": ex["rule"][0], "rawSource": ex["rule"][1], "rounding": ex["rounding"],
                    "scale": ex["scale"]},
        "reference": rjson,
        "sets": sjson,
        "questions": qjson,
        "importFlags": [f.to_json() for f in flags],
    }
    (out / "exam.json").write_text(to_json(exam) + "\n")
    write_index(exams_dir)
    return report(exam_id, ex, flags, ocr), (1 if flags else 0)


def debug_page(pdf, out, num):
    ocr_pdf = out / "source-ocr.pdf"
    doc = open_pdf(ocr_pdf if ocr_pdf.exists() else pdf)
    if not 1 <= num <= doc.page_count:
        raise Fatal(f"page {num} out of range 1..{doc.page_count}")
    p = Page(doc[num - 1], num)
    lines = [f"p{num} {p.width:.0f}x{p.height:.0f}"]
    lines += [f"{ln.bbox[0]:.0f} {ln.bbox[1]:.0f} {ln.text}" for ln in p.lines]
    figs = []
    for r in p.drawings():
        r = list(r)
        for f in figs:
            if intersect(grow(f, 4), r):
                f[:] = union([f, r])
                break
        else:
            figs.append(r)
    lines += [f"fig {f[0]:.0f} {f[1]:.0f} {f[2]:.0f} {f[3]:.0f}" for f in figs
              if f[2] - f[0] >= 4 or f[3] - f[1] >= 4]
    lines += [f"img {r[0]:.0f} {r[1]:.0f} {r[2]:.0f} {r[3]:.0f}" for r in p.images()]
    print("\n".join(lines))


def main(argv=None, exams_dir=None):
    ap = argparse.ArgumentParser(description="Import a GRE Physics practice-test PDF.")
    ap.add_argument("pdf")
    ap.add_argument("--id")
    ap.add_argument("--expected", type=int)
    ap.add_argument("--pages")
    ap.add_argument("--debug-page", type=int)
    args = ap.parse_args(argv)
    pdf = Path(args.pdf)
    exam_id = args.id or pdf.stem.lower()
    exams_dir = Path(exams_dir) if exams_dir else EXAMS_DIR
    out = exams_dir / exam_id
    try:
        if sys.version_info < (3, 11):
            raise Fatal("Python 3.11+ required")
        if not pdf.is_file():
            raise Fatal(f"{pdf} not found")
        if args.debug_page is not None:
            debug_page(pdf, out, args.debug_page)
            return 0
        lines, code = run_import(pdf, exam_id, out, args, exams_dir)
    except Fatal as e:
        print(f"{exam_id}: FATAL {e}")
        return 2
    print("\n".join(lines))
    return code


if __name__ == "__main__":
    sys.exit(main())
