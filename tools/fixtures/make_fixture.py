#!/usr/bin/env python3
"""Generate synthetic GRE-Physics-style practice-test PDFs for tests. No ETS content.

    python tools/fixtures/make_fixture.py OUT.pdf          # 6-question fixture (SPEC §5.4)
    python tools/fixtures/make_fixture.py OUT.pdf --big    # 100-question, two-column book
"""
import argparse
import random
import sys

import pymupdf

W, H = 612, 792
FONT = "helv"


def text(page, x, y, s, size=10):
    if any(ord(c) > 0xFF for c in s):       # base-14 fonts can't encode e.g. en dashes
        tw = pymupdf.TextWriter(page.rect)
        tw.append((x, y), s, font=pymupdf.Font("cjk"), fontsize=size)
        tw.write_text(page)
    else:
        page.insert_text((x, y), s, fontsize=size, fontname=FONT)


def width(s, size):
    return pymupdf.get_text_length(s, fontname=FONT, fontsize=size)


# ---------------------------------------------------------------- small fixture

SMALL_KEY = {1: ("C", 72, "Mechanics"), 2: ("A", 60, "Mechanics"), 3: ("D", 48, "Mechanics"),
             4: ("B", 55, "Optics"), 5: ("E", 41, "Electromagnetism"), 6: ("B", 33, "Thermodynamics")}
SMALL_SCALE = [((6, 6), 990, 99), ((3, 3), 640, 45), ((5, 5), 880, 90),
               ((1, 2), 520, 20), ((4, 4), 760, 70), ((0, 0), 400, 5)]


def small_running(page, num, go_on=True):
    text(page, 230, 40, "SYNTHETIC PRACTICE TEST", 9)
    text(page, 300, 770, str(num), 9)
    if go_on:
        text(page, 420, 770, "GO ON TO THE NEXT PAGE.", 9)


def choices(page, x, y, values, letters="ABCDE", step=14):
    for letter, v in zip(letters, values):
        text(page, x, y, f"({letter}) {v}")
        y += step
    return y


def make_small(path):
    """6 questions, single column: a shared-stem set (2-3), a page-break question (5),
    an equation with a "2." false-positive anchor (in 4), header/footer, key and scale."""
    doc = pymupdf.open()

    p = doc.new_page(width=W, height=H)
    text(p, 54, 80, "SYNTHETIC PHYSICS PRACTICE BOOK", 16)
    for i, s in enumerate([
        "This booklet is synthetic. It exists only to test the importer and contains no ETS material.",
        "The test below has 6 questions. Testing time is 30 minutes.",
        "Your score is the number of questions answered correctly minus one-fourth the number",
        "of questions answered incorrectly. Questions you omit are not counted.",
    ]):
        text(p, 54, 120 + 14 * i, s)

    p = doc.new_page(width=W, height=H)
    text(p, 220, 80, "TABLE OF INFORMATION", 12)
    consts = ["Rest mass of the electron   me = 9.11 x 10^-31 kilogram",
              "Magnitude of the electron charge   e = 1.60 x 10^-19 coulomb",
              "Avogadro's number   NA = 6.02 x 10^23 per mole",
              "Universal gas constant   R = 8.31 joules/(mole K)",
              "Boltzmann's constant   k = 1.38 x 10^-23 joule/K",
              "Speed of light   c = 3.00 x 10^8 m/s",
              "Planck's constant   h = 6.63 x 10^-34 joule second"]
    for i, s in enumerate(consts):
        text(p, 80, 110 + 16 * i, s)
    p.draw_rect(pymupdf.Rect(60, 64, 552, 110 + 16 * len(consts)), color=(0, 0, 0), width=0.8)
    text(p, 54, 260, "Directions: Each of the questions below is followed by five suggested answers.")
    text(p, 54, 274, "Select the one that is best in each case.")

    p = doc.new_page(width=W, height=H)
    small_running(p, 3)
    text(p, 54, 90, "1.")
    text(p, 72, 90, "A block of mass 2.0 kg rests on a frictionless table. A horizontal force")
    text(p, 72, 104, "of 4.0 N acts on it. What is the magnitude of its acceleration?")
    choices(p, 72, 122, ["0.5 m/s^2", "1.0 m/s^2", "2.0 m/s^2", "4.0 m/s^2", "8.0 m/s^2"])
    text(p, 54, 212, "Questions 2-3 refer to the figure below.")
    p.draw_rect(pymupdf.Rect(150, 222, 330, 282), color=(0, 0, 0), width=1)
    p.draw_line((150, 282), (330, 222), color=(0, 0, 0), width=1)
    text(p, 236, 262, "m", 9)
    text(p, 54, 306, "2.")
    text(p, 72, 306, "What is the potential energy of the mass at the top of the incline?")
    choices(p, 72, 324, ["12 J", "24 J", "36 J", "48 J", "60 J"])
    text(p, 54, 410, "3.")
    text(p, 72, 410, "What is the speed of the mass at the bottom of the incline?")
    choices(p, 72, 428, ["2.0 m/s", "4.0 m/s", "6.0 m/s", "8.0 m/s", "10 m/s"])
    text(p, 54, 514, "4.")
    text(p, 72, 514, "A particle moves along the x-axis with the position given below,")
    text(p, 72, 528, "where t is in seconds and x in meters.")
    text(p, 56, 544, "2. x = 3.0 t + 2.0 t^2")
    text(p, 72, 558, "What is its acceleration at t = 1 s?")
    choices(p, 72, 576, ["2.0 m/s^2", "4.0 m/s^2", "6.0 m/s^2", "7.0 m/s^2", "9.0 m/s^2"])
    text(p, 54, 664, "5.")
    text(p, 72, 664, "A charge q moves with speed v perpendicular to a uniform magnetic")
    text(p, 72, 678, "field B. What is the radius of its circular path?")
    q5 = ["qB/(mv)", "mv/(qB^2)", "qvB/m", "m/(qvB)", "mv/(qB)"]
    choices(p, 72, 696, q5[:2], "AB")

    p = doc.new_page(width=W, height=H)
    small_running(p, 4, go_on=False)
    choices(p, 72, 90, q5[2:], "CDE")
    text(p, 54, 150, "6.")
    text(p, 72, 150, "An ideal gas is compressed isothermally to half its volume. By what")
    text(p, 72, 164, "factor does its pressure change?")
    choices(p, 72, 182, ["1/4", "1/2", "1", "2", "4"])
    text(p, 290, 280, "STOP", 12)

    p = doc.new_page(width=W, height=H)
    text(p, 54, 80, "ANSWER KEY AND WORKSHEET", 14)
    text(p, 54, 100, "Total Score = C - I/4, where C is the number correct and I the number incorrect.", 9)
    for gx in (54, 320):
        for dx, h in ((0, "Question"), (56, "Answer"), (106, "P+"), (146, "Category")):
            text(p, gx + dx, 124, h, 9)
    for row in range(3):
        for g, gx in enumerate((54, 320)):
            q = 1 + row + 3 * g
            letter, pplus, cat = SMALL_KEY[q]
            for dx, s in ((0, str(q)), (56, letter), (106, str(pplus)), (146, cat)):
                text(p, gx + dx, 142 + 14 * row, s, 9)

    p = doc.new_page(width=W, height=H)
    text(p, 54, 80, "SCORE CONVERSIONS", 14)
    text(p, 54, 100, "TOTAL SCORE", 11)
    for gx in (54, 320):
        for dx, h in ((0, "Raw Score"), (76, "Scaled Score"), (156, "%")):
            text(p, gx + dx, 124, h, 9)
    for i, ((lo, hi), scaled, pct) in enumerate(SMALL_SCALE):
        gx, y = (54, 320)[i % 2], 142 + 14 * (i // 2)
        raw = str(lo) if lo == hi else f"{lo}-{hi}"
        for dx, s in ((0, raw), (76, str(scaled)), (156, str(pct))):
            text(p, gx + dx, y, s, 9)

    doc.save(path)
    return {"key": SMALL_KEY, "splits": {5}, "sets": [(2, 3)]}


# ---------------------------------------------------------------- big fixture

WORDS = ("mass block spring charge field wire loop lens mirror photon electron nucleus gas piston "
         "pendulum string wave pulse current resistor capacitor inductor magnet orbit planet satellite "
         "rod disk sphere shell cylinder fluid pipe beam slit grating atom ion proton neutron "
         "frequency energy momentum velocity acceleration force torque pressure temperature entropy").split()
FILLER = "the a of is at on in with from by and to which that as its".split()
UNITS = ["m/s", "J", "N", "kg", "eV", "K", "Pa", "W", "V", "A", "T", "Hz", "s", "m"]

COL_X = (54, 318)
NUM_RIGHT = 16      # numbers are right-aligned so their periods line up
TEXT_DX = 20
COL_W = 244
SIZE = 9.5
LH = 12.5
TOP, BOTTOM = 72, 724
BIG_SETS = {12: 13, 40: 42, 66: 67, 85: 86}
FORCED_SPLITS = {23: "col", 57: "page", 78: "col"}
STEM_SPLIT = 40


def sentence(rnd, words):
    out = []
    for i in range(words):
        out.append(rnd.choice(WORDS) if i % 3 == 0 else rnd.choice(FILLER))
    s = " ".join(out)
    return s[0].upper() + s[1:] + "."


def wrap(s, maxw, size=SIZE):
    lines, cur = [], ""
    for w in s.split():
        t = (cur + " " + w).strip()
        if width(t, size) > maxw and cur:
            lines.append(cur)
            cur = w
        else:
            cur = t
    if cur:
        lines.append(cur)
    return lines


class Flow:
    """Places rows into two columns per page, starting new pages as needed."""

    def __init__(self, doc):
        self.doc = doc
        self.page = None
        self.col = 1
        self.y = BOTTOM
        self.exam_pages = []
        self.first_top = None     # column top on the first exam page (below the directions)

    def new_page(self):
        p = self.doc.new_page(width=W, height=H)
        n = self.doc.page_count
        text(p, 230, 40, "SYNTHETIC PHYSICS TEST", 9)
        p.draw_line((54, 50), (558, 50), color=(0, 0, 0), width=0.5)
        p.draw_line((306, TOP - 8), (306, BOTTOM), color=(0.6, 0.6, 0.6), width=0.5)
        text(p, 300, 770, str(n), 9)
        self.exam_pages.append(n - 1)
        self.page, self.col, self.y = p, 0, TOP

    def next_col(self):
        if self.col == 0 and self.page is not None:
            self.col, self.y = 1, TOP if len(self.exam_pages) > 1 else self.first_top or TOP
        else:
            self.new_page()

    def room(self):
        return BOTTOM - self.y

    def place(self, row):
        """row = (height, draw(page, x, y_top)). Returns (page_number, col)."""
        h, draw = row
        if h > self.room():
            self.next_col()
        draw(self.page, COL_X[self.col], self.y)
        self.y += h
        return (self.doc.page_count, self.col)


def text_row(s, dx=TEXT_DX, size=SIZE):
    return (LH, lambda p, x, y: text(p, x + dx, y + SIZE, s, size))


def figure_row(rnd, h):
    kind = rnd.randrange(3)

    def draw(p, x, y):
        x0, y0 = x + TEXT_DX + 20, y + 6
        if kind == 0:
            p.draw_rect(pymupdf.Rect(x0, y0, x0 + 120, y0 + h - 14), color=(0, 0, 0), width=1)
            p.draw_line((x0, y0 + h - 14), (x0 + 120, y0), color=(0, 0, 0), width=1)
        elif kind == 1:
            p.draw_circle((x0 + 60, y0 + (h - 14) / 2), (h - 14) / 2, color=(0, 0, 0), width=1)
            p.draw_line((x0, y0 + (h - 14) / 2), (x0 + 120, y0 + (h - 14) / 2), color=(0, 0, 0), width=0.8)
        else:
            pts = [(x0 + 12 * i, y0 + (h - 14) * (0.5 + 0.45 * ((-1) ** i))) for i in range(11)]
            p.draw_polyline(pts, color=(0, 0, 0), width=1)
        text(p, x0 + 124, y0 + 10, rnd.choice("mqLRv"), 8)
    return (h, draw)


def question_rows(rnd, n):
    first, *rest = wrap(sentence(rnd, rnd.randint(14, 30)), COL_W - TEXT_DX - 6)
    label = f"{n}."

    def draw_first(p, x, y):
        text(p, x + NUM_RIGHT - width(label, SIZE), y + SIZE, label)
        text(p, x + TEXT_DX, y + SIZE, first)
    rows = [(LH, draw_first)] + [text_row(s) for s in rest]
    if rnd.random() < 0.3:
        rows.append(text_row(f"E = 2. {rnd.randint(1, 9)} x 10^3 V/m at x = 2.5 m", dx=TEXT_DX + 30))
    if n == 30:     # in-sequence-looking false anchor two numbers ahead
        rows.append((LH, lambda p, x, y: text(p, x + 6, y + SIZE, "32. kg m/s is the unit used below.")))
    if rnd.random() < 0.25:
        rows.append(figure_row(rnd, rnd.choice((50, 64, 80))))
    rows.append((4, lambda p, x, y: None))
    for letter in "ABCDE":
        rows.append(text_row(f"({letter}) {rnd.randint(1, 99) / 10:g} {rnd.choice(UNITS)}"))
    rows.append((10, lambda p, x, y: None))
    return rows


def stem_rows(rnd, a, b):
    dash = "-" if a % 2 == 0 else "–"
    s = f"Questions {a}{dash}{b} refer to the following figure. Assume all surfaces are frictionless."
    return [text_row(t, dx=0) for t in wrap(s, COL_W - 6)] + [figure_row(rnd, 70), (6, lambda p, x, y: None)]


def make_big(path, n=100, seed=7):
    rnd = random.Random(seed)
    doc = pymupdf.open()
    meta = {"key": {}, "splits": set(), "sets": sorted(BIG_SETS.items()), "pages": None}

    p = doc.new_page(width=W, height=H)
    text(p, 54, 80, "SYNTHETIC PHYSICS TEST GR-SYNTH", 16)
    y = 120
    for s in ["This practice book is synthetic and contains no ETS material.",
              "Your score will be determined by subtracting one-fourth of the number of questions",
              "answered incorrectly from the number of questions answered correctly.",
              "Before you begin:"]:
        text(p, 54, y, s)
        y += 14
    for i, s in enumerate(["Read each question carefully.", "Mark only one answer per question.",
                           "Use the Table of Information as needed.", "Erase changes completely.",
                           "Work steadily through the test."], 1):
        text(p, 54, y, f"{i}.")
        text(p, 72, y, s)
        y += 14

    p = doc.new_page(width=W, height=H)
    text(p, 230, 80, "TABLE OF INFORMATION", 12)
    consts = ["Rest mass of the electron   me = 9.11 x 10^-31 kilogram",
              "Magnitude of the electron charge   e = 1.60 x 10^-19 coulomb",
              "Avogadro's number   NA = 6.02 x 10^23 per mole",
              "Universal gas constant   R = 8.31 joules/(mole K)",
              "Boltzmann's constant   k = 1.38 x 10^-23 joule/K",
              "Speed of light   c = 3.00 x 10^8 m/s",
              "Planck's constant   h = 6.63 x 10^-34 joule second",
              "Vacuum permittivity   e0 = 8.85 x 10^-12 C^2/(N m^2)",
              "Vacuum permeability   u0 = 4 pi x 10^-7 weber/(ampere meter)",
              "Universal gravitational constant   G = 6.67 x 10^-11 m^3/(kg s^2)"]
    for i, s in enumerate(consts):
        text(p, 80, 110 + 16 * i, s)
    p.draw_rect(pymupdf.Rect(60, 64, 552, 110 + 16 * len(consts)), color=(0, 0, 0), width=0.8)

    flow = Flow(doc)
    flow.new_page()
    p0 = flow.page
    text(p0, 270, 84, "PHYSICS TEST", 12)
    text(p0, 250, 100, "Time - 170 minutes", 10)
    text(p0, 270, 114, "100 Questions", 10)
    for i, s in enumerate(wrap("Directions: Each of the questions or incomplete statements below is followed by "
                               "five suggested answers or completions. Select the one that is best in each case.",
                               500, 9.5)):
        text(p0, 54, 134 + 12 * i, s, 9.5)
    flow.y = flow.first_top = 166

    for q in range(1, n + 1):
        if q in BIG_SETS:
            rows = stem_rows(rnd, q, BIG_SETS[q])
            if q == STEM_SPLIT:
                if flow.col != 0:
                    flow.next_col()
                flow.y = BOTTOM - 2 * LH - 1
                for r in rows:
                    flow.place(r)
            else:
                if sum(r[0] for r in rows) > flow.room():
                    flow.next_col()
                for r in rows:
                    flow.place(r)
        rows = question_rows(rnd, q)
        need = sum(r[0] for r in rows)
        if q in FORCED_SPLITS:
            want_col = 1 if FORCED_SPLITS[q] == "page" else 0
            if flow.col != want_col or flow.room() < 3 * LH:
                flow.next_col()
                if flow.col != want_col:
                    flow.next_col()
            flow.y = BOTTOM - 3 * LH - 1
        elif need > flow.room():
            flow.next_col()
        where = {flow.place(r) for r in rows[:-1]}
        flow.place(rows[-1])
        if len(where) > 1:
            meta["splits"].add(q)
        letter = rnd.choice("ABCDE")
        meta["key"][q] = (letter, rnd.randint(12, 95), None)

    lines = ["IF YOU FINISH BEFORE TIME IS CALLED,", "YOU MAY CHECK YOUR WORK ON THIS TEST."]
    for s in lines:
        flow.place(text_row(s, dx=0))
    for i in flow.exam_pages[:-1]:
        text(doc[i], 420, 770, "GO ON TO THE NEXT PAGE.", 9)
    meta["pages"] = (3, doc.page_count)

    p = doc.new_page(width=W, height=H)
    text(p, 54, 70, "WORKSHEET FOR THE SYNTHETIC PHYSICS TEST", 12)
    text(p, 54, 86, "Answer Key and Percentages (P+) of Examinees Answering Each Question Correctly", 9)
    for g in range(4):
        gx = 54 + 132 * g
        for dx, h in ((0, "Number"), (40, "Answer"), (80, "P+")):
            text(p, gx + dx, 112, h, 8)
        for r in range(25):
            q = 1 + r + 25 * g
            letter, pplus, _ = meta["key"][q]
            for dx, s in ((4, str(q)), (48, letter), (82, str(pplus))):
                text(p, gx + dx, 128 + 13 * r, s, 9)
    text(p, 54, 470, "Correct (C) ______   Incorrect (I) ______   Total Score: C - I/4 = ______", 10)

    entries = [((85, 100), 990, 97)]
    for raw in range(84, 12, -1):
        f = (raw - 12) / 72
        entries.append(((raw, raw), 200 + 10 * round(78 * f ** 1.1), max(1, round(97 * f ** 1.3))))
    entries.append(((0, 12), 200, 1))
    meta["scale"] = entries
    p = doc.new_page(width=W, height=H)
    text(p, 54, 70, "SCORE CONVERSIONS FOR THE SYNTHETIC PHYSICS TEST", 12)
    text(p, 54, 86, "TOTAL SCORE", 10)
    for g in range(3):
        gx = 54 + 176 * g
        for dx, h in ((0, "Raw Score"), (60, "Scaled Score"), (128, "%")):
            text(p, gx + dx, 112, h, 8)
    for i, ((lo, hi), scaled, pct) in enumerate(entries):
        g, r = divmod(i, 25)
        gx, y = 54 + 176 * g, 128 + 13 * r
        raw = str(lo) if lo == hi else f"{lo}–{hi}"
        for dx, s in ((0, raw), (64, str(scaled)), (128, str(pct))):
            text(p, gx + dx, y, s, 9)

    p = doc.new_page(width=W, height=H)
    text(p, 54, 70, "ANSWER SHEET", 12)
    for g in range(4):
        for r in range(25):
            q = 1 + r + 25 * g
            gx, y = 54 + 132 * g, 100 + 14 * r
            text(p, gx, y, f"{q}.", 9)
            text(p, gx + 22, y, "(A) (B) (C) (D) (E)", 9)

    doc.save(path)
    return meta


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("out")
    ap.add_argument("--big", action="store_true")
    a = ap.parse_args()
    (make_big if a.big else make_small)(a.out)
    sys.exit(0)
