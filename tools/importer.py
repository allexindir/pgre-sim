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
COL_TOL = 15          # an anchor must sit this close to a column's left edge (pt)
COL_PAD = 8           # columns extend this far left of their leftmost anchor (pt)
LINE_GAP = 12         # a word this close on the left means "not first on its line" (pt)
MAX_SET = 8           # larger shared-stem ranges are suspicious
MAX_REPORT = 25

ANCHOR_RE = re.compile(r"^(\d{1,3})\.$")
GLUED_RE = re.compile(r"^(\d{1,3})\.(?=[A-Za-z(\[])")
STEM_RE = re.compile(r"^Questions?\s+(\d+)\s*(?:-|–|—|and|through|to)\s*(\d+)", re.I)
CHOICE_RE = re.compile(r"^\(([A-E])\)")
LETTER_RE = re.compile(r"^\(?([A-E])\)?$")
KEYNUM_RE = re.compile(r"^\d{1,3}\.?$")
PPLUS_RE = re.compile(r"^(\d{1,3})%?$")
FOOTER_RE = re.compile(
    r"^(go on to the next page\.?|stop\.?|.*\btime is called\b.*|.*\bcheck your work\b.*"
    r"|.*\bwork on this test\b.*|this test\.|do not turn this page.*)$", re.I)

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


def band_of(page, box):
    cy = (box[1] + box[3]) / 2
    if cy < 0.1 * page.height:
        return "top"
    if cy > 0.9 * page.height:
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
        best = self.lines.get((b, line_key(text)))
        return best is not None and abs(ln.bbox[1] - best) <= 3

    def is_running_rect(self, r):
        return tuple(round(v / 2) for v in r) in self.rects


@dataclass(eq=False)
class Item:
    bbox: tuple
    kind: str           # "w" word, "d" drawing, "i" image
    text: str = ""


def build_items(page, rf):
    items = []
    for ln in page.lines:
        if not rf.is_running(page, ln):
            items += [Item(w.bbox, "w", w.text) for w in ln.words]
    W, H = page.width, page.height
    for r in page.drawings():
        w, h = r[2] - r[0], r[3] - r[1]
        if rf.is_running_rect(r) or w * h > 0.4 * W * H:
            continue
        if min(w, h) < 2 and (h > 0.8 * H or w > 0.8 * W):
            continue
        items.append(Item(r, "d"))
    for r in page.images():
        if area(r) <= 0.6 * W * H:
            items.append(Item(r, "i"))
    return items


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
    col: int = -1


@dataclass
class Column:
    edge: float         # median anchor x0
    left: float         # leftmost anchor x0


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


def find_candidates(pages):
    out = []
    for p in pages:
        for w in p.words:
            m = ANCHOR_RE.match(w.text) or GLUED_RE.match(w.text)
            if not m or int(m.group(1)) < 1:
                continue
            if first_on_line(w, p.words) and not looks_like_table_row(w, p.words):
                out.append(Cand(int(m.group(1)), p.num, w))
    return out


def cluster_positions(xs, gap=20):
    clusters = []
    for x in sorted(xs):
        if clusters and x - clusters[-1][-1] <= gap:
            clusters[-1].append(x)
        else:
            clusters.append([x])
    return clusters


def detect_columns(xs, min_members=5):
    """Column left edges from anchor x0 values. Two or more big clusters = multi-column."""
    clusters = cluster_positions(xs)
    big = [c for c in clusters if len(c) >= min_members]
    if not big and clusters:
        big = [max(clusters, key=len)]
    return [Column(statistics.median(c), min(c)) for c in big]


def nearest_col(cols, x, tol=COL_TOL):
    if not cols:
        return None
    k = min(range(len(cols)), key=lambda i: abs(x - cols[i].edge))
    return k if tol is None or abs(x - cols[k].edge) <= tol else None


MAX_PAGE_JUMP = 3      # the next question is never more than this many pages on


def _find(cands, i, pred, max_page):
    for j in range(i, len(cands)):
        if cands[j].page > max_page:
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
    most MAX_PAGE_JUMP pages on. If a later number (up to `max_skip` ahead) comes
    first and is itself followed by its successor before the wanted number shows up,
    the wanted number was missed: skip it and record it as missing.

    The run with the most question-like anchors wins (table rows from worksheets and
    answer sheets don't count), less one per jump over a page with no anchors; ties
    go to the latest start (front-matter lists that merge into the real exam).
    Returns (accepted, missing_numbers).
    """
    best = None
    for s, c in enumerate(cands):
        if c.n != 1:
            continue
        acc, missing, i = [c], [], s + 1
        while i < len(cands):
            want, limit = acc[-1].n + 1, acc[-1].page + MAX_PAGE_JUMP
            j = _find(cands, i, lambda n: n == want, limit)
            g = _find(cands, i, lambda n: want < n <= want + max_skip, limit)
            if g is not None and (j is None or g < j):
                conf = _find(cands, g + 1, lambda n: n == cands[g].n + 1, cands[g].page + MAX_PAGE_JUMP)
                if (conf is None and j is None) or (conf is not None and (j is None or j > conf)):
                    missing += range(want, cands[g].n)
                    acc.append(cands[g])
                    i = g + 1
                    continue
            if j is None:
                break
            acc.append(cands[j])
            i = j + 1
        jumps = sum(1 for a, b in zip(acc, acc[1:]) if b.page - a.page > 1)
        key = (_question_like(acc) - jumps, s)
        if best is None or key > best[0]:
            best = (key, acc, missing)
    return (best[1], best[2]) if best else ([], [])


def locate_anchors(pages):
    cands = find_candidates(pages)
    prov = detect_columns([c.word.x0 for c in cands])
    kept = []
    for c in cands:
        k = nearest_col(prov, c.word.x0)
        if k is not None:
            c.col = k
            kept.append(c)
    kept.sort(key=lambda c: (c.page, c.col, c.word.y0))
    accepted, missing = sequence_run(kept)
    if len(accepted) < 2:
        return [], [], []
    cols = detect_columns([a.word.x0 for a in accepted])
    for a in accepted:
        a.col = nearest_col(cols, a.word.x0, tol=None)
    for k, col in enumerate(cols):
        xs = [a.word.x0 for a in accepted if a.col == k]
        col.left = min(xs) if xs else col.left
    return accepted, missing, cols


# ---------------------------------------------------------------- regions

@dataclass(eq=False)
class Boundary:
    kind: str           # "q" or "set"
    n: int              # question number, or the set's first question
    last: int
    page: int
    col: int
    y0: float


class Layout:
    def __init__(self, pages, exam_nums, cols, rf, ocr):
        self.pages = pages              # num -> Page
        self.exam_nums = exam_nums      # sorted page numbers
        self.cols = cols
        self.rf = rf
        self.ocr = ocr
        self._items = {}
        self.area_top = {}

    def items(self, num):
        if num not in self._items:
            self._items[num] = build_items(self.pages[num], self.rf)
        return self._items[num]

    def content_box(self, num):
        its = self.items(num)
        if not its:
            p = self.pages[num]
            return (0, 0, p.width, p.height)
        return union(i.bbox for i in its)

    def bottom(self, num):
        b = self.content_box(num)[3] + PAD
        if self.ocr:
            b = max(b, 0.9 * self.pages[num].height - 2)
        return min(b, self.pages[num].height)

    def col_range(self, num, k):
        left = self.cols[k].left - COL_PAD
        if k + 1 < len(self.cols):
            right = self.cols[k + 1].left - COL_PAD - 2
        else:
            right = self.content_box(num)[2] + PAD
        return max(0, left), min(self.pages[num].width, right)

    def tighten(self, num, region):
        if self.ocr:
            return pixel_trim(self.pages[num].pg, region)
        boxes = boxes_in(self.items(num), region)
        if not boxes:
            return region
        return intersect(grow(union(boxes), PAD), region) or region

    def has_content(self, num, region):
        return bool(boxes_in(self.items(num), region))

    def segments(self, bounds):
        """Map each boundary to its segments [(page, bbox)]."""
        grid = defaultdict(list)
        for b in bounds:
            grid[(b.page, b.col)].append(b)
        for v in grid.values():
            v.sort(key=lambda b: b.y0)
        out = {}
        for (num, k), col in grid.items():
            x0, x1 = self.col_range(num, k)
            for i, b in enumerate(col):
                y1 = col[i + 1].y0 - LEAD if i + 1 < len(col) else self.bottom(num)
                segs = [(num, self.tighten(num, (x0, b.y0 - LEAD, x1, y1)))]
                if i + 1 == len(col):
                    cont = self.continuation(num, k, grid)
                    if cont:
                        segs.append(cont)
                out[b] = segs
        return out

    def continuation(self, num, k, grid):
        """Content above the first boundary of the next column/page slot."""
        slots = [(num, j) for j in range(k + 1, len(self.cols))]
        if num + 1 in self.exam_nums:
            slots.append((num + 1, 0))
        for sn, sk in slots:
            x0, x1 = self.col_range(sn, sk)
            first = grid.get((sn, sk))
            top = self.area_top.get(sn, self.content_box(sn)[1]) - LEAD
            bot = first[0].y0 - LEAD if first else self.bottom(sn)
            region = (x0, top, x1, bot)
            if bot - top > 2 and self.has_content(sn, region):
                return (sn, self.tighten(sn, region))
            if first or self.has_content(sn, (x0, 0, x1, self.pages[sn].height)):
                return None
        return None


def pixel_trim(pg, region, thresh=200):
    pix = pg.get_pixmap(matrix=pymupdf.Matrix(ZOOM, ZOOM), clip=pymupdf.Rect(region),
                        colorspace=pymupdf.csGRAY)
    img = Image.frombytes("L", (pix.width, pix.height), pix.samples)
    box = img.point(lambda v: 255 if v < thresh else 0).getbbox()
    if not box:
        return region
    trimmed = (region[0] + box[0] / ZOOM, region[1] + box[1] / ZOOM,
               region[0] + box[2] / ZOOM, region[1] + box[3] / ZOOM)
    return intersect(grow(trimmed, PAD), region) or region


def find_stems(layout, pages):
    out = []
    for p in pages:
        for ln in p.lines:
            m = STEM_RE.match(ln.text.strip())
            if not m or layout.rf.is_running(p, ln):
                continue
            x = ln.bbox[0]
            k = max([i for i, c in enumerate(layout.cols) if c.left - COL_PAD <= x + 2] or [0])
            out.append(Boundary("set", int(m.group(1)), int(m.group(2)), p.num, k, ln.bbox[1]))
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
    r"|(?:subtract\w*|minus|deduct\w*)\W+(?:\w+\W+){0,4}1\s*[/⁄]\s*4", re.I)
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

    # Count and numbering
    if N is None:
        flags.append(Flag("COUNT", "count", f"found={len(qs)} expected unknown (no key, no --expected)"))
    elif len(qs) != N:
        flags.append(Flag("COUNT", "count", f"found={len(qs)} expected={N}"))
    for n in range(1, (max(nums) if nums else 0) + 1):
        if n not in qs:
            flags.append(Flag("NUM_GAP", f"q{n}", "missing", page=ex["gapPages"].get(n),
                              pages=[ex["gapPages"][n]] if n in ex["gapPages"] else []))

    # Answer key
    key_page = ex["keyPages"][0] if ex["keyPages"] else None
    span = range(1, (N or (max(nums) if nums else 0)) + 1)
    missing = [n for n in span if n not in ex["answers"]]
    bad = [n for n in span if n in ex["answers"] and ex["answers"][n].get("answer") not in list("ABCDE")]
    conflicts = [n for n in ex["keyConflicts"] if n not in ex["overriddenAnswers"]]
    kp = [key_page] if key_page else []
    if not ex["answers"]:
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
        letters = []
        for num, box in segs:
            letters += [CHOICE_RE.match(it.text).group(1) for it in layout.items(num)
                        if it.kind == "w" and CHOICE_RE.match(it.text) and center_in(it.bbox, box)]
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
            if med and (h > 2.2 * med or (h < 0.35 * med and len(segs) == 1)):
                flags.append(Flag("HEIGHT", f"q{n}", f"h={h:.0f}pt ({h / med:.1f}x median)",
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
    for label, box, color in boxes:     # labels go top-right, clear of printed numbers
        d.rectangle([X(box[0]), Y(box[1]), X(box[2]), Y(box[3])], outline=color, width=2)
        tw = d.textlength(label, font=font)
        at = (X(box[2]) - tw - 4, Y(box[1]) + 3)
        d.rectangle(grow(d.textbbox(at, label, font=font), 1), fill="white")
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

def needs_ocr(doc):
    scanned = 0
    for pg in doc:
        if len(pg.get_text("words")) >= 50:
            continue
        a = pg.rect.width * pg.rect.height
        if any(area(i["bbox"]) >= 0.5 * a for i in pg.get_image_info()):
            scanned += 1
    return scanned > doc.page_count / 2


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
    doc = open_pdf(pdf)
    ocr_pdf = out / "source-ocr.pdf"
    if not needs_ocr(doc):
        return doc, False
    if not (ocr_pdf.exists() and ocr_pdf.stat().st_mtime >= pdf.stat().st_mtime):
        exe = shutil.which("ocrmypdf")
        if not exe:
            raise Fatal("scanned PDF needs OCR but ocrmypdf is not installed (brew install ocrmypdf)")
        cmd = [exe, "--deskew", "--rotate-pages", str(pdf), str(ocr_pdf)]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode == 6:       # page already has text
            r = subprocess.run([exe, "--force-ocr"] + cmd[1:], capture_output=True, text=True)
        if r.returncode != 0:
            tail = (r.stderr.strip().splitlines() or ["?"])[-1]
            raise Fatal(f"ocrmypdf failed (exit {r.returncode}): {tail}")
    return open_pdf(ocr_pdf), True


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
    by_num = {p.num: p for p in pages}

    # Exam pages and anchors
    rng = parse_pages_arg(args.pages) if args.pages else tuple(ov["pages"]) if "pages" in ov else None
    scope = [p for p in pages if not rng or rng[0] <= p.num <= rng[1]]
    anchors = []
    if not rng:     # the exam starts at or after the Table of Information
        toi = next((p.num for p in pages if p.pg.search_for("TABLE OF INFORMATION")), None)
        if toi:
            anchors, missing, cols = locate_anchors([p for p in scope if p.num >= toi])
    if not anchors:
        anchors, missing, cols = locate_anchors(scope)
    if not anchors and not ov.get("crops"):
        raise Fatal("no questions found")
    if anchors:
        first, last = rng or (anchors[0].page, anchors[-1].page)
    else:
        first, last = rng or (1, doc.page_count)
        cols = [Column(0, 0)]
    exam_nums = list(range(first, last + 1))
    exam_pages = [by_num[n] for n in exam_nums if n in by_num]

    rf = RunningFilter(exam_pages)
    layout = Layout(by_num, exam_nums, cols, rf, ocr)

    questions, sets, reference = {}, [], []
    gap_pages = {}
    if anchors:
        bounds = [Boundary("q", a.n, a.n, a.page, a.col, a.word.y0) for a in anchors]
        bounds += find_stems(layout, exam_pages)
        # On the first page, content above the first question (directions, the
        # Table of Information) never belongs to a question.
        layout.area_top[first] = min(b.y0 for b in bounds if b.page == first)
        segs = layout.segments(bounds)
        order = sorted(bounds, key=lambda b: (b.page, b.col, b.y0))
        for i, b in enumerate(order):
            if b.kind == "q":
                questions[b.n] = {"segments": segs[b], "override": False}
            else:
                nxt = next((x.n for x in order[i + 1:] if x.kind == "q"), None)
                sets.append({"first": b.n, "last": b.last, "segments": segs[b], "stemFirst": nxt})
        reference = find_reference(layout, pages, anchors)
        prev = {a.n: a.page for a in anchors}
        for n in missing:
            gap_pages[n] = prev.get(n - 1) or prev.get(n + 1)

    # Answer key, conversion table, rule and time
    key, conflicts, key_pages = find_key(pages, last)
    scale, dups, scale_page = find_scale(pages, last)
    texts = [(p.num, re.sub(r"\s+", " ", p.text)) for p in pages if p.num <= first or p.num > last]
    rule, rsrc, rule_page, rule_problem = detect_rule(texts)
    minutes, _ = detect_time(texts)
    expected_guess = max(key) if key else None

    ex = {
        "questions": questions, "answers": key, "sets": sets, "reference": reference,
        "scale": scale, "scaleDups": dups, "scalePage": scale_page, "scaleSource": "text",
        "rule": (rule, rsrc), "rulePage": rule_page, "ruleProblem": rule_problem,
        "rounding": detect_rounding(texts),
        "time": (minutes, "text") if minutes else (None, "inferred"),
        "expected": expected_guess, "keyPages": key_pages, "keyConflicts": conflicts,
        "gapPages": gap_pages, "ignore": set(),
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
