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


# ---------------------------------------------------------------- column detection

def test_columns_two_clusters():
    xs = [54, 58, 61] * 3 + [318, 322, 325] * 3
    cols = I.detect_columns(xs)
    assert len(cols) == 2
    assert cols[0].left == 54 and cols[1].left == 318


def test_columns_single_cluster_with_outliers():
    xs = [54, 59, 62, 62, 57, 60, 200, 201]
    cols = I.detect_columns(xs)
    assert len(cols) == 1 and cols[0].left == 54


def test_columns_small_input_uses_largest_cluster():
    cols = I.detect_columns([54, 55, 56, 300])
    assert len(cols) == 1 and cols[0].left == 54
    assert I.nearest_col(cols, 66) == 0 and I.nearest_col(cols, 90) is None


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


def test_pixel_trim_finds_ink():
    doc = pymupdf.open()
    p = doc.new_page(width=612, height=792)
    p.draw_rect(pymupdf.Rect(200, 300, 260, 340), color=(0, 0, 0), fill=(0, 0, 0))
    box = I.pixel_trim(p, (100, 200, 400, 500))
    assert box == pytest.approx((194, 294, 266, 346), abs=1)
