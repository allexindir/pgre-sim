// Scoring: pure functions, no DOM. See SPEC.md §6.4.
//
//   R = number correct, W = number wrong (blanks excluded)
//   raw    = R             if rule == "R"
//   raw    = R - W/4       if rule == "R-W/4"
//   rawInt = round half-up
//   scaled = scale lookup of rawInt, clamped to the table's min..max

export const LETTERS = ["A", "B", "C", "D", "E"];

export function roundRaw(x, rounding = "half-up") {
  if (rounding === "down") return Math.floor(x);
  return Math.floor(x + 0.5);
}

export function rawScore(R, W, rule) {
  if (rule === "R") return R;
  if (rule === "R-W/4") return R - W / 4;
  throw new Error(`unknown scoring rule: ${rule}`);
}

// Returns the scale entry for rawInt, clamped to the table's range. The importer
// expands ranges to one entry per integer, so lookups are exact; a gap (flagged at
// import) falls back to the nearest lower entry.
export function lookupScaled(rawInt, scale) {
  if (!scale || !scale.length) return null;
  const rows = [...scale].sort((a, b) => a.raw - b.raw);
  if (rawInt <= rows[0].raw) return rows[0];
  if (rawInt >= rows[rows.length - 1].raw) return rows[rows.length - 1];
  let hit = rows[0];
  for (const row of rows) {
    if (row.raw > rawInt) break;
    hit = row;
  }
  return hit;
}

// Status of one question: "correct", "wrong", "blank", or "unscored" (no key).
export function questionStatus(key, given) {
  if (!key) return "unscored";
  if (!given) return "blank";
  return given === key ? "correct" : "wrong";
}

const fmt = (x) => (Number.isInteger(x) ? String(x) : String(Math.round(x * 100) / 100));

// "62 − 20/4 = 57", "62 − 21/4 = 56.75 → 57", "57 right = 57"
export function formatFormula(R, W, rule, raw, rawInt) {
  const lhs = rule === "R-W/4" ? `${R} − ${W}/4` : `${R} right`;
  return raw === rawInt ? `${lhs} = ${rawInt}` : `${lhs} = ${fmt(raw)} → ${rawInt}`;
}

export function categoryTallies(exam, answers) {
  const byCat = new Map();
  for (const q of exam.questions) {
    if (!q.category || !q.answer) continue;
    const t = byCat.get(q.category) ?? { category: q.category, correct: 0, wrong: 0, blank: 0, total: 0 };
    t[questionStatus(q.answer, answers[q.n])] += 1;
    t.total += 1;
    byCat.set(q.category, t);
  }
  return [...byCat.values()].sort((a, b) => a.category.localeCompare(b.category));
}

// Grade an attempt from the current exam.json. answers: { [n]: "A".."E" }.
export function grade(exam, answers) {
  const counts = { correct: 0, wrong: 0, blank: 0, unscored: 0 };
  const items = exam.questions.map((q) => {
    const given = answers[q.n] ?? null;
    const status = questionStatus(q.answer, given);
    counts[status] += 1;
    return { n: q.n, given, key: q.answer ?? null, status };
  });
  const { raw: rule, rounding, scale } = exam.scoring;
  const R = counts.correct;
  const W = counts.wrong;
  const raw = rawScore(R, W, rule);
  const rawInt = roundRaw(raw, rounding);
  const entry = lookupScaled(rawInt, scale);
  return {
    R, W, B: counts.blank, unscored: counts.unscored, rule, raw, rawInt,
    scaled: entry ? entry.scaled : null,
    pct: entry && entry.pct != null ? entry.pct : null,
    formula: formatFormula(R, W, rule, raw, rawInt),
    items,
    categories: categoryTallies(exam, answers),
  };
}

export function median(xs) {
  if (!xs.length) return 0;
  const s = [...xs].sort((a, b) => a - b);
  const m = s.length >> 1;
  return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2;
}
