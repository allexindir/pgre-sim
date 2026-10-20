"""Importer tests. Synthetic inputs only: no ETS content."""
import json
import subprocess
import sys
from pathlib import Path

import pymupdf
import pytest

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent / "fixtures"))

import importer as I  # noqa: E402
import make_fixture  # noqa: E402

TOOLS = Path(__file__).parent


def cand(n, page=1, col=0, y=0.0):
    return I.Cand(n, page, I.Word(54, y, 62, y + 10, f"{n}."), col)


def numbers(run):
    return [c.n for c in run]


# ---------------------------------------------------------------- anchor sequencing

def test_sequence_rejects_out_of_order_candidates():
    cands = [cand(1, y=100), cand(2, y=200), cand(2, y=250), cand(3, y=300), cand(7, y=350), cand(4, y=400)]
    run, missing = I.sequence_run(cands)
    assert numbers(run) == [1, 2, 3, 4] and missing == []
    assert run[1].word.y0 == 200


def test_sequence_skips_a_missed_anchor():
    cands = [cand(1, y=100), cand(2, y=200), cand(4, y=400), cand(5, y=500), cand(6, y=600)]
    run, missing = I.sequence_run(cands)
    assert numbers(run) == [1, 2, 4, 5, 6] and missing == [3]


def test_sequence_ignores_a_false_anchor_two_ahead():
    # a "4." inside question 2's text, before the real 3
    cands = [cand(1, y=100), cand(2, y=200), cand(4, y=240), cand(3, y=300), cand(4, y=400), cand(5, y=500)]
    run, missing = I.sequence_run(cands)
    assert numbers(run) == [1, 2, 3, 4, 5] and missing == []
    assert run[3].word.y0 == 400


def test_sequence_prefers_exam_over_front_matter_list():
    front = [cand(n, page=1, y=100 + 14 * n) for n in (1, 2, 3)]
    exam = [cand(n, page=3 + (n - 1) // 3, y=100 + 120 * ((n - 1) % 3)) for n in range(1, 10)]
    run, _ = I.sequence_run(front + exam)
    assert numbers(run) == list(range(1, 10)) and all(c.page >= 3 for c in run)


def test_sequence_prefers_question_spacing_over_table_rows():
    exam = [cand(n, page=2 + (n - 1) // 4, y=100 + 150 * ((n - 1) % 4)) for n in range(1, 9)]
    sheet = [cand(n, page=9, y=100 + 13 * n) for n in range(1, 21)]
    run, _ = I.sequence_run(exam + sheet)
    assert numbers(run) == list(range(1, 9)) and run[0].page == 2
    # an answer sheet right after the exam that numbers past the last question
    near = [cand(n, page=4, y=100 + 13 * n) for n in range(1, 21)]
    run, _ = I.sequence_run(exam + near)
    assert run[0].page == 2


def test_sequence_prefers_complete_exam_over_front_matter_with_a_gap():
    front = [cand(n, page=1, y=100 + 14 * n) for n in range(1, 6)]
    exam = [cand(n, page=3 + (n - 1) // 4, y=100 + 150 * ((n - 1) % 4)) for n in range(1, 13) if n != 5]
    run, missing = I.sequence_run(front + exam)
    assert run[0].page == 3 and missing == [5]


def _page_with(lines):
    doc = pymupdf.open()
    p = doc.new_page(width=612, height=792)
    for x, y, s in lines:
        p.insert_text((x, y), s, fontsize=10, fontname="helv")
    return doc, I.Page(p, 1)


def test_candidates_need_first_token_and_not_table_rows():
    doc, page = _page_with([
        (54, 100, "1. A 2 kg block slides down a ramp."),
        (54, 130, "2. C 72 26. B 51"),
        (54, 160, "3. (A) (B) (C) (D) (E)"),
        (54, 190, "4.Which glued anchor is accepted?"),
        (90, 220, "the speed is v = 5. m/s here"),
        (54, 250, "6. A"),
    ])
    assert [c.n for c in I.find_candidates([page])] == [1, 4]


# ---------------------------------------------------------------- layout: columns, bands, lead-ins

def _doc(pages):
    """pages: list of [(x, y, text) | ("rule", y) | ("box", x0, y0, x1, y1)]."""
    doc = pymupdf.open()
    for items in pages:
        p = doc.new_page(width=612, height=792)
        for it in items:
            if it[0] == "rule":
                p.draw_line((54, it[1]), (558, it[1]), color=(0, 0, 0), width=0.8)
            elif it[0] == "box":
                p.draw_rect(pymupdf.Rect(*it[1:]), color=(0, 0, 0), width=1)
            else:
                p.insert_text((it[0], it[1]), it[2], fontsize=10, fontname="helv")
    return doc


def _question(x, y, n, lines=2, above=False):
    """A question at (x, y): number, text lines, five choices. Returns (items, bottom y)."""
    out = [(x, y, f"{n}."), (x + 18, y, "As shown above, what is the answer?" if above else "Consider a block on a ramp.")]
    out += [(x + 18, y + 14 * (i + 1), "with some more words in the text") for i in range(lines - 1)]
    y += 14 * lines + 4
    for letter in "ABCDE":
        out.append((x + 18, y, f"({letter}) {letter.lower()} value"))
        y += 14
    return out, y


def _layout(doc):
    pages = [I.Page(doc[i], i + 1) for i in range(doc.page_count)]
    anchors, missing = I.locate_anchors(pages)
    nums = [p.num for p in pages]
    by_num = {p.num: p for p in pages}
    lay = I.Layout(by_num, nums, anchors, I.RunningFilter(pages), set())
    for a in anchors:
        a.col = lay.col_of(a.page, a.band, a.word.x0)
    bounds = [I.Boundary("q", a.n, a.n, a.page, a.band, a.col, a.word.y0) for a in anchors]
    return lay, anchors, {b.n: segs for b, segs in lay.segments(bounds).items()}


def test_columns_per_page_and_band():
    two = []
    for i, (x, y) in enumerate([(54, 80), (54, 260), (318, 80), (318, 260)]):
        two += _question(x, y, i + 1)[0]
    inset = _question(160, 80, 5)[0]
    wide, _ = _question(54, 80, 6)
    wide = [(x, y, t + " that runs right across the gutter of the page" if x > 60 and not t.startswith("(") else t)
            for x, y, t in wide]
    mixed = wide + [("rule", 220)] + _question(160, 240, 7)[0]
    lay, anchors, segs = _layout(_doc([two, inset, mixed]))
    assert [a.n for a in anchors] == list(range(1, 8))
    assert lay.cols(1, 0) == pytest.approx([54, 318], abs=1)
    assert lay.cols(2, 0) == pytest.approx([160], abs=1)
    assert lay.nbands(3) == 2 and len(lay.cols(3, 0)) == 1 and len(lay.cols(3, 1)) == 1
    (page, box), = segs[6]
    assert page == 3 and box[2] > 360      # the full-width question isn't cut at the x=318 gutter
    assert segs[7][0][1][1] > 220          # the inset question starts below the rule
    for n in (1, 2):
        assert segs[n][0][1][2] < 318      # left-column crops stay in the left column


def test_figure_shown_above_belongs_to_the_next_question():
    q1, y = _question(54, 80, 1)
    q2, _ = _question(54, y + 120, 2, above=True)
    q3, _ = _question(318, 180, 3, above=True)
    doc = _doc([q1 + [("box", 90, y + 20, 200, y + 100)] + q2 + [("box", 340, 80, 450, 160)] + q3])
    _, _, segs = _layout(doc)
    (_, b1), = segs[1]
    (_, b2), = segs[2]
    (_, b3), = segs[3]
    assert b1[3] < y + 20 and b2[1] <= y + 20      # between questions: after (E), it's q2's
    assert b3[1] <= 80                              # at the top of a column: q3's lead-in


def test_graphical_choice_keeps_its_picture():
    q1, y = _question(54, 80, 1)
    q1[-1] = (72, y - 14, "(E)")
    q2, _ = _question(54, y + 120, 2, above=True)
    doc = _doc([q1 + [("box", 100, y - 22, 180, y + 30)] + q2])
    _, _, segs = _layout(doc)
    assert segs[1][0][1][3] >= y + 30               # the (E) drawing stays with q1


def test_ocr_tolerance():
    assert I.anchor_number("14.") == 14 and I.anchor_number("14,") is None
    assert I.anchor_number("i4.", ocr=True) == 14 and I.anchor_number("25,", ocr=True) == 25
    assert I.anchor_number("4|.", ocr=True) == 41
    assert I.anchor_number("II.", ocr=True) is None and I.anchor_number("I.", ocr=True) is None
    assert I.choice_of("(A)") == "A" and I.choice_of("{B)") == "B" and I.choice_of("(Cc)") == "C"
    assert I.choice_of("(Be)") is None
    for t in ("©", "®)", "()", "‘D)", "(BE)", "(B", "A)"):
        assert I.OCR_CHOICE_RE.match(t), t
    for t in ("A", "the", "B", "V(x)", "2"):
        assert not I.OCR_CHOICE_RE.match(t), t


def test_sequence_without_question_one():
    cands = [cand(n, y=100 * n) for n in (2, 3, 4, 5)]
    run, missing = I.sequence_run(cands, max_skip=4)
    assert numbers(run) == [2, 3, 4, 5] and missing == [1]


def test_gap_filled_from_a_bare_period():
    q1, y = _question(54, 80, 1)
    q2, y2 = _question(54, y + 20, 2)
    q2[0] = (60, q2[0][1], ".")         # OCR kept only the period of "2."
    q3, _ = _question(54, y2 + 20, 3)
    lay, anchors, _ = _layout(_doc([q1 + q2 + q3]))
    found, missing = I.fill_gaps(anchors, [2], lay, lay.pages)
    assert [a.n for a in found] == [1, 2, 3] and missing == []


def test_page_numbers_are_footers():
    doc = _doc([[(300, 760, "12")], [(300, 760, "- 13 -")]])
    pages = [I.Page(doc[i], i + 1) for i in range(2)]
    rf = I.RunningFilter(pages[:1])
    assert all(rf.is_running(p, ln) for p in pages for ln in p.lines)


def _image_page(doc, frac):
    p = doc.new_page(width=612, height=792)
    pix = pymupdf.Pixmap(pymupdf.csGRAY, pymupdf.IRect(0, 0, 60, 80), 0)
    pix.clear_with(200)
    p.insert_image(pymupdf.Rect(0, 0, 612, 792 * frac), pixmap=pix, keep_proportion=False)


def test_mixed_scans_need_ocr():
    doc = pymupdf.open()
    for _ in range(3):
        doc.new_page().insert_text((50, 50), " ".join(["word"] * 60), fontsize=4)
    for _ in range(4):
        _image_page(doc, 0.45)
    assert I.scanned_pages(doc) == {4, 5, 6, 7} and I.needs_ocr(doc)
    for _ in range(2):
        doc.new_page().insert_text((50, 50), " ".join(["word"] * 60), fontsize=4)
    assert not I.needs_ocr(doc)


def test_ink_blobs():
    doc = pymupdf.open()
    p = doc.new_page(width=612, height=792)
    p.draw_rect(pymupdf.Rect(200, 300, 260, 340), color=(0, 0, 0), fill=(0, 0, 0))
    page = I.Page(p, 1)
    page.scanned = True
    (box, _), = page.blobs()
    assert box == pytest.approx((200, 300, 260, 340), abs=2.5)


def test_scanned_key_is_never_trusted():
    ex = {"questions": {}, "expected": 3, "answers": {}, "keyPages": [9], "keyConflicts": [],
          "overriddenAnswers": set(), "scale": [{"raw": 0, "scaled": 200}], "scaleDups": [], "scalePage": 10,
          "scaleSource": "text", "rule": ("R", "text"), "time": (120, "text"), "reference": [(1, (0, 0, 1, 1))],
          "sets": [], "gapPages": {}, "ignore": set(), "keyScanned": True}
    flags = [f for f in I.validate(ex, None) if f.code == "KEY"]
    assert [f.target for f in flags] == ["key"] and "scan" in flags[0].detail


# ---------------------------------------------------------------- answer key

def test_key_rows_in_column_groups_with_pplus():
    rows = [["Number", "Answer", "P+", "Number", "Answer", "P+"],
            ["1", "C", "72", "26", "B", "52", "51", "A", "47", "76", "D", "31"],
            ["2", "E", "65%", "27", "A", "50", "52", "C", "45", "77", "B", "30"]]
    key, conflicts = I.parse_key_rows(rows)
    assert conflicts == []
    assert {n: (v["answer"], v["pplus"]) for n, v in key.items()} == {
        1: ("C", 72), 26: ("B", 52), 51: ("A", 47), 76: ("D", 31),
        2: ("E", 65), 27: ("A", 50), 52: ("C", 45), 77: ("B", 30)}


def test_key_rows_without_pplus_and_with_categories():
    key, _ = I.parse_key_rows([["1", "C", "2", "D"], ["3.", "(A)", "4.", "B"]])
    assert {n: v["answer"] for n, v in key.items()} == {1: "C", 2: "D", 3: "A", 4: "B"}
    assert all(v["pplus"] is None for v in key.values())
    key, _ = I.parse_key_rows([["1", "C", "72", "Mechanics", "4", "B", "55", "Optics"],
                               ["2", "A", "60", "Quantum", "Mechanics", "5", "E", "41", "Optics"]])
    assert key[2]["category"] == "Quantum Mechanics" and key[4]["category"] == "Optics"


def test_key_ignores_bubble_rows_and_reports_conflicts():
    key, conflicts = I.parse_key_rows([["1", "A", "B", "C", "D", "E"], ["2", "B"], ["2", "C"], ["3", "D", "3", "D"]])
    assert set(key) == {2, 3} and conflicts == [2]


# ---------------------------------------------------------------- conversion table

def test_scale_rows_ranges_groups_and_percentiles():
    assert I.parse_scale_tokens(["85-100", "990", "97", "56", "760", "62", "28", "500", "21"]) == [
        (85, 100, 990, 97), (56, 56, 760, 62), (28, 28, 500, 21)]
    assert I.parse_scale_tokens(["85–87", "980", "60", "750"]) == [(85, 87, 980, None), (60, 60, 750, None)]
    assert I.parse_scale_tokens(["85", "–", "87", "980", "<1", "0", "200", "1"]) == [
        (85, 87, 980, 0), (0, 0, 200, 1)]
    assert I.parse_scale_tokens(["Raw", "Score", "Scaled", "Score", "%"]) == []


def test_scale_expand_and_validate():
    scale, dups = I.expand_scale([(4, 6, 900, 90), (3, 3, 800, None), (0, 2, 700, 10)])
    assert [e["raw"] for e in scale] == list(range(7)) and dups == []
    assert scale[5] == {"raw": 5, "scaled": 900, "pct": 90} and "pct" not in scale[3]
    assert I.validate_scale(scale) == []
    gap, _ = I.expand_scale([(0, 1, 500, None), (4, 5, 600, None)])
    assert I.validate_scale(gap) == ["gap at raw 2..3"]
    dec, _ = I.expand_scale([(0, 0, 600, None), (1, 1, 500, None)])
    assert I.validate_scale(dec) == ["decreases at raw 1"]
    bad, _ = I.expand_scale([(0, 0, 195, None), (1, 1, 1000, None), (2, 2, 1005, None)])
    assert len(I.validate_scale(bad)) == 3
    dup_scale, dups = I.expand_scale([(0, 3, 500, None), (3, 4, 600, None)])
    assert I.validate_scale(dup_scale, dups) == ["duplicate raw 3"]
    assert I.validate_scale([]) == ["missing"]


# ---------------------------------------------------------------- scoring rule and time

def test_rule_detection():
    old = "Your score will be the number answered correctly minus one-fourth the number answered incorrectly."
    assert I.detect_rule([(2, old)])[:3] == ("R-W/4", "text", 2)
    assert I.detect_rule([(9, "Total Score: C - I/4 = ____")])[:2] == ("R-W/4", "text")
    new = "Your score is the number of questions you answer correctly. Nothing is subtracted for wrong answers."
    assert I.detect_rule([(1, new)])[:3] == ("R", "text", 1)
    assert I.detect_rule([(1, "Read each question carefully.")])[:2] == ("R", "inferred")
    assert I.detect_rule([(1, old), (5, new)])[3] is not None


def test_time_and_rounding_detection():
    assert I.detect_time([(3, "PHYSICS TEST Time—170 minutes 100 Questions")]) == (170, 3)
    assert I.detect_time([(1, "The test lasts 2 hours and 50 minutes.")]) == (170, 1)
    assert I.detect_time([(1, "Total testing time is two hours.")]) == (120, 1)
    assert I.detect_time([(1, "Allow 10 minutes to read this."), (2, "testing time is 120 minutes")]) == (120, 2)
    assert I.detect_time([(1, "No time stated here.")]) == (None, None)
    assert I.detect_rounding([(1, "round to the nearest whole number")]) == "half-up"
    assert I.detect_rounding([(1, "drop any fraction")]) == "down"


# ---------------------------------------------------------------- overrides

def _extracted():
    return {
        "questions": {1: {"segments": [(9, (54, 80, 300, 200))], "override": False},
                      2: {"segments": [(9, (54, 210, 300, 330))], "override": False}},
        "answers": {1: {"answer": "C", "pplus": 72, "category": None}},
        "sets": [{"first": 1, "last": 2, "segments": [(9, (54, 60, 300, 78))], "stemFirst": 1}],
        "reference": [], "scale": [{"raw": 0, "scaled": 200}], "scaleDups": [],
        "rule": ("R", "inferred"), "rounding": "half-up", "time": (None, "inferred"),
        "expected": 2, "ignore": set(),
    }


def test_merge_overrides_replaces_only_named_keys():
    ov = {"answers": {"2": "d"}, "crops": {"2": [{"page": 10, "bbox": [54, 70, 300, 400]}]},
          "scoring": {"raw": "R-W/4"}, "timeLimitMin": 170, "expected": 100,
          "reference": [{"page": 3, "bbox": [40, 60, 572, 740]}],
          "scale": [{"raw": 1, "scaled": 300, "pct": 5}, {"raw": 0, "scaled": 200}],
          "ignoreFlags": ["CHOICES:q058", "split:Q5"]}
    ex = I.apply_overrides(_extracted(), ov)
    assert ex["answers"][1]["answer"] == "C" and ex["answers"][1]["pplus"] == 72
    assert ex["answers"][2]["answer"] == "D"
    assert ex["questions"][1]["segments"] == [(9, (54, 80, 300, 200))]
    assert ex["questions"][2] == {"segments": [(10, (54.0, 70.0, 300.0, 400.0))], "override": True}
    assert ex["rule"] == ("R-W/4", "override") and ex["rounding"] == "half-up"
    assert ex["time"] == (170, "override") and ex["expected"] == 100
    assert ex["reference"] == [(3, (40.0, 60.0, 572.0, 740.0))]
    assert ex["scale"] == [{"raw": 0, "scaled": 200}, {"raw": 1, "scaled": 300, "pct": 5}]
    assert ex["ignore"] == {"CHOICES:q58", "SPLIT:q5"}


def test_merge_overrides_sets():
    ex = I.apply_overrides(_extracted(), {"sets": [{"first": 1, "last": 3, "page": 8, "bbox": [54, 90, 560, 210]}]})
    assert [(s["first"], s["last"], s["segments"]) for s in ex["sets"]] == [(1, 3, [(8, (54.0, 90.0, 560.0, 210.0))])]
    ex = I.apply_overrides(_extracted(), {"sets": [{"first": 1, "last": 3}]})
    assert ex["sets"][0]["last"] == 3 and ex["sets"][0]["segments"] == [(9, (54, 60, 300, 78))]
    ex = I.apply_overrides(_extracted(), {"sets": [{"first": 1, "last": 2, "remove": True}]})
    assert ex["sets"] == []


def test_merge_overrides_rejects_malformed_input():
    with pytest.raises(I.Fatal):
        I.apply_overrides(_extracted(), {"crops": {"2": [{"page": 10, "bbox": [1, 2, 3]}]}})
    with pytest.raises(I.Fatal):
        I.apply_overrides(_extracted(), {"scoring": {"raw": "R-W/5"}})


# ---------------------------------------------------------------- report

def test_report_is_capped_at_25_lines():
    ex = {"expected": 3, "questions": {1: {}, 2: {}, 3: {}}, "answers": {}, "scale": [], "sets": [],
          "reference": [], "rule": ("R", "inferred"), "time": (120, "inferred")}
    flags = [I.Flag("CHOICES", f"q{n}", "found=0", page=n, pages=[n]) for n in range(1, 41)]
    lines = I.report("x", ex, flags, False)
    assert len(lines) == 25 and lines[-1] == "+18 more"
    assert lines[1] == "FLAGS (40):"
    assert lines[2] == "CHOICES q001 p1 found=0 -> review/p001.png"


# ---------------------------------------------------------------- end to end

def run(pdf, exams, *args):
    r = subprocess.run([sys.executable, str(TOOLS / "importer.py"), str(pdf), *args],
                       capture_output=True, text=True, cwd=TOOLS.parent,
                       env={"PATH": "/usr/bin:/bin", "PGRE_EXAMS_DIR": str(exams)})
    return r.returncode, r.stdout.splitlines(), r.stderr


def run_in(pdf, exams, capsys, *args):
    code = I.main([str(pdf), *args], exams_dir=exams)
    return code, capsys.readouterr().out.splitlines()


@pytest.fixture(scope="module")
def small_pdf(tmp_path_factory):
    path = tmp_path_factory.mktemp("pdf") / "synth.pdf"
    meta = make_fixture.make_small(path)
    return path, meta


def test_end_to_end_small_fixture(small_pdf, tmp_path, capsys):
    pdf, meta = small_pdf
    code, out = run_in(pdf, tmp_path, capsys)
    assert out[0] == "synth: q=6/6 key=6/6 scale=0..6 sets=1 ref=1 ocr=no rule=R-W/4(text) time=30(text)"
    assert out[1:] == ["FLAGS (1):", "SPLIT   q005 p3 continues on p4 -> review/p003.png review/p004.png"]
    assert code == 1
    assert sorted(p.name for p in (tmp_path / "synth" / "review").iterdir()) == ["p003.png", "p004.png"]

    (tmp_path / "synth" / "overrides.json").write_text(json.dumps({"ignoreFlags": ["SPLIT:q5"]}))
    code, out = run_in(pdf, tmp_path, capsys)
    assert code == 0 and len(out) == 1
    assert not (tmp_path / "synth" / "review").exists()

    exam = json.loads((tmp_path / "synth" / "exam.json").read_text())
    assert exam["version"] == 1 and exam["id"] == "synth" and exam["source"] == "synth.pdf"
    assert exam["questionCount"] == 6 and exam["timeLimitMin"] == 30 and exam["timeLimitSource"] == "text"
    assert exam["scoring"]["raw"] == "R-W/4" and exam["scoring"]["rawSource"] == "text"
    assert exam["scoring"]["rounding"] == "half-up"
    assert [(e["raw"], e["scaled"]) for e in exam["scoring"]["scale"]] == [
        (0, 400), (1, 520), (2, 520), (3, 640), (4, 760), (5, 880), (6, 990)]
    assert exam["reference"] == ["img/reference-1.png"]
    assert exam["sets"] == [{"id": "2-3", "first": 2, "last": 3, "image": "img/set-2-3.png"}]
    qs = {q["n"]: q for q in exam["questions"]}
    for n, (letter, pplus, cat) in meta["key"].items():
        assert (qs[n]["answer"], qs[n]["pplus"], qs[n]["category"]) == (letter, pplus, cat)
    assert qs[5]["images"] == ["img/q005.png", "img/q005-2.png"] and qs[5]["page"] == 3
    assert qs[4]["images"] == ["img/q004.png"]
    assert [q["set"] for q in exam["questions"]] == [None, "2-3", "2-3", None, None, None]
    assert exam["importFlags"] == []
    for name in [i for q in exam["questions"] for i in q["images"]] + exam["reference"] + ["img/set-2-3.png"]:
        assert (tmp_path / "synth" / name).stat().st_size > 500
    assert json.loads((tmp_path / "index.json").read_text()) == [
        {"id": "synth", "title": "synth", "questionCount": 6, "timeLimitMin": 30}]


def test_end_to_end_overrides_fix_flags(small_pdf, tmp_path, capsys):
    pdf, _ = small_pdf
    (tmp_path / "t").mkdir()
    (tmp_path / "t" / "overrides.json").write_text(json.dumps({
        "answers": {"3": "B"}, "scoring": {"raw": "R"}, "timeLimitMin": 45,
        "crops": {"5": [{"page": 3, "bbox": [50, 650, 560, 716]}, {"page": 4, "bbox": [50, 78, 560, 124]}]},
        "title": "Synthetic Test"}))
    code, out = run_in(pdf, tmp_path, capsys, "--id", "t")
    assert code == 0, out
    assert "rule=R(override) time=45(override)" in out[0]
    exam = json.loads((tmp_path / "t" / "exam.json").read_text())
    assert exam["title"] == "Synthetic Test"
    assert exam["questions"][2]["answer"] == "B"
    assert exam["questions"][4]["images"] == ["img/q005.png", "img/q005-2.png"]


def test_end_to_end_cli_exit_codes(small_pdf, tmp_path):
    pdf, _ = small_pdf
    code, out, err = run(tmp_path / "missing.pdf", tmp_path)
    assert code == 2 and out == ["missing: FATAL " + str(tmp_path / "missing.pdf") + " not found"] and err == ""
    blank = tmp_path / "blank.pdf"
    doc = pymupdf.open()
    doc.new_page()
    doc.save(blank)
    code, out, _ = run(blank, tmp_path, "--debug-page", "1")
    assert code == 0 and out == ["p1 595x842"]
    code, out, _ = run(blank, tmp_path)
    assert code == 2 and out == ["blank: FATAL no questions found"]


def test_debug_page_format(small_pdf, tmp_path, capsys):
    pdf, _ = small_pdf
    code, out = run_in(pdf, tmp_path, capsys, "--debug-page", "3")
    assert code == 0 and out[0] == "p3 612x792"
    assert "54 79 1." in out and "72 79 A block of mass 2.0 kg rests on a frictionless table. A horizontal force" in out
    assert any(line.startswith("fig 150 222 330 282") for line in out)


def test_end_to_end_big_two_column_book(tmp_path, capsys):
    pdf = tmp_path / "gr-synth.pdf"
    meta = make_fixture.make_big(pdf)
    code, out = run_in(pdf, tmp_path, capsys)
    assert out[0] == ("gr-synth: q=100/100 key=100/100 scale=0..100 sets=4 ref=1 ocr=no "
                      "rule=R-W/4(text) time=170(text)")
    split_flags = [line.split()[1] for line in out[2:]]
    assert split_flags == [f"q{n:03d}" for n in sorted(meta["splits"])]
    assert all(line.startswith("SPLIT") for line in out[2:]) and code == 1
    exam = json.loads((tmp_path / "gr-synth" / "exam.json").read_text())
    for q in exam["questions"]:
        assert (q["answer"], q["pplus"]) == meta["key"][q["n"]][:2]
        assert len(q["images"]) == (2 if q["n"] in meta["splits"] else 1)
    assert [(s["first"], s["last"]) for s in exam["sets"]] == meta["sets"]
    expected = {r: s for (lo, hi), s, _ in meta["scale"] for r in range(lo, hi + 1)}
    assert {e["raw"]: e["scaled"] for e in exam["scoring"]["scale"]} == expected
    assert exam["title"] == "gr synth"


def test_crops_stay_clear_of_excluded_marks():
    content = (60, 100, 300, 200)
    padded = (54, 94, 306, 206)
    divider, header_rule, inside = (306, 64, 306, 724), (54, 96, 558, 96), (100, 150, 558, 150)
    assert I.clear_of(padded, content, [divider]) == (54, 94, 305, 206)
    assert I.clear_of(padded, content, [header_rule]) == (54, 97, 306, 206)
    assert I.clear_of(padded, content, [(54, 203, 300, 203)]) == (54, 94, 306, 202)
    assert I.clear_of(padded, content, [inside]) == padded  # crosses the content: kept
