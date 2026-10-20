import { test } from "node:test";
import assert from "node:assert/strict";
import {
  categoryTallies, formatFormula, grade, lookupScaled, median, rawScore, roundRaw,
} from "./scoring.js";

// Expanded table: 0..2 -> 200, 3 -> 300, ..., 10 -> 990 (ranges already expanded).
const SCALE = [
  { raw: 0, scaled: 200, pct: 1 }, { raw: 1, scaled: 200, pct: 1 }, { raw: 2, scaled: 200, pct: 1 },
  { raw: 3, scaled: 300 }, { raw: 4, scaled: 400 }, { raw: 5, scaled: 500 }, { raw: 6, scaled: 600 },
  { raw: 7, scaled: 700 }, { raw: 8, scaled: 800, pct: 60 }, { raw: 9, scaled: 990, pct: 94 },
  { raw: 10, scaled: 990, pct: 94 },
];

function exam(rule, keys, extra = {}) {
  return {
    scoring: { raw: rule, rounding: "half-up", scale: SCALE },
    questions: keys.map((answer, i) => ({ n: i + 1, answer, ...(extra[i + 1] ?? {}) })),
  };
}

test("rule R counts only correct answers", () => {
  const g = grade(exam("R", ["A", "B", "C", "D", "E"]), { 1: "A", 2: "B", 3: "A", 5: "E" });
  assert.equal(g.R, 3);
  assert.equal(g.W, 1);
  assert.equal(g.B, 1);
  assert.equal(g.raw, 3);
  assert.equal(g.rawInt, 3);
  assert.equal(g.scaled, 300);
  assert.equal(g.formula, "3 right = 3");
  assert.deepEqual(g.items.map((i) => i.status), ["correct", "correct", "wrong", "blank", "correct"]);
});

test("rule R-W/4 subtracts a quarter per wrong answer, blanks are never wrong", () => {
  const keys = Array(10).fill("A");
  const answers = { 1: "A", 2: "A", 3: "A", 4: "A", 5: "A", 6: "A", 7: "A", 8: "B", 9: "C" }; // 7 right, 2 wrong, 1 blank
  const g = grade(exam("R-W/4", keys), answers);
  assert.equal(g.raw, 6.5);
  assert.equal(g.rawInt, 7);
  assert.equal(g.scaled, 700);
  assert.equal(g.B, 1);
  assert.equal(g.formula, "7 − 2/4 = 6.5 → 7");
});

test("R-W/4 rounding cases", () => {
  assert.equal(rawScore(62, 19, "R-W/4"), 57.25);
  assert.equal(roundRaw(57.25), 57);
  assert.equal(roundRaw(rawScore(60, 10, "R-W/4")), 58); // 57.5 -> 58
  assert.equal(roundRaw(57.75), 58);
  assert.equal(roundRaw(rawScore(62, 20, "R-W/4")), 57);
  assert.equal(formatFormula(62, 20, "R-W/4", 57, 57), "62 − 20/4 = 57");
  // negative raw scores round half-up too
  assert.equal(roundRaw(rawScore(0, 2, "R-W/4")), 0); // -0.5 -> 0
  assert.equal(roundRaw(rawScore(0, 3, "R-W/4")), -1); // -0.75 -> -1
  assert.equal(roundRaw(rawScore(1, 10, "R-W/4")), -1); // -1.5 -> -1
  assert.equal(roundRaw(-2.25), -2);
  assert.equal(roundRaw(6.9, "down"), 6);
  assert.throws(() => rawScore(1, 1, "R-W/5"));
});

test("scaled score clamps below the table's minimum and above its maximum", () => {
  assert.equal(lookupScaled(-4, SCALE).scaled, 200);
  assert.equal(lookupScaled(12, SCALE).scaled, 990);
  const g = grade(exam("R-W/4", ["A", "A", "A", "A"]), { 1: "B", 2: "B", 3: "B", 4: "B" });
  assert.equal(g.rawInt, -1);
  assert.equal(g.scaled, 200);
  assert.equal(g.pct, 1);
  assert.equal(lookupScaled(3, []), null);
});

test("lookups through expanded ranges", () => {
  assert.equal(lookupScaled(1, SCALE).scaled, 200);
  assert.equal(lookupScaled(9, SCALE).pct, 94);
  assert.equal(lookupScaled(10, SCALE).scaled, 990);
  assert.equal(lookupScaled(5, [...SCALE].reverse()).scaled, 500); // order-independent
  const gappy = [{ raw: 0, scaled: 200 }, { raw: 5, scaled: 500 }];
  assert.equal(lookupScaled(3, gappy).scaled, 200); // nearest lower entry
});

test("category tallies", () => {
  const e = exam("R", ["A", "B", "C", "D", null], {
    1: { category: "Optics" }, 2: { category: "Mechanics" }, 3: { category: "Mechanics" },
    5: { category: "Optics" },
  });
  const tallies = categoryTallies(e, { 1: "A", 2: "C", 4: "D" });
  assert.deepEqual(tallies, [
    { category: "Mechanics", correct: 0, wrong: 1, blank: 1, total: 2 },
    { category: "Optics", correct: 1, wrong: 0, blank: 0, total: 1 },
  ]);
  assert.deepEqual(grade(e, {}).categories.map((c) => c.category), ["Mechanics", "Optics"]);
});

test("questions without a key are not scored", () => {
  const g = grade(exam("R-W/4", ["A", null]), { 1: "B", 2: "C" });
  assert.equal(g.W, 1);
  assert.equal(g.unscored, 1);
  assert.equal(g.items[1].status, "unscored");
});

test("median", () => {
  assert.equal(median([]), 0);
  assert.equal(median([3, 1, 2]), 2);
  assert.equal(median([4, 1, 3, 2]), 2.5);
});
