// PGRE Simulator runner: vanilla ES modules, no dependencies. See SPEC.md §6.
import { LETTERS, grade, median } from "./scoring.js";

const EXAMS = "../exams";
const K_PROGRESS = "pgre.inprogress.v1";
const K_ATTEMPTS = "pgre.attempts.v1";
const IMG_SCALE = 0.72;         // crops are rendered at 2.5x; show them at 1.8 px per PDF point
const SAVE_MS = 5000;
const WARN_MS = 5 * 60 * 1000;

const app = document.getElementById("app");
const params = new URLSearchParams(location.search);
let T = null;                   // the running test, see showTest()
let notice = null;              // one-shot message for the next screen

// ---------------------------------------------------------------- DOM helpers

function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  setAttrs(el, attrs);
  append(el, kids);
  return el;
}

function s(tag, attrs, ...kids) {
  const el = document.createElementNS("http://www.w3.org/2000/svg", tag);
  setAttrs(el, attrs);
  append(el, kids);
  return el;
}

function setAttrs(el, attrs) {
  for (const [k, v] of Object.entries(attrs ?? {})) {
    if (v == null || v === false) continue;
    if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? "" : v);
  }
}

function append(el, kids) {
  for (const k of kids.flat(Infinity)) {
    if (k == null || k === false) continue;
    el.append(k instanceof Node ? k : String(k));
  }
}

function mount(...nodes) {
  app.replaceChildren(...nodes);
  window.scrollTo(0, 0);
}

function button(label, onclick, attrs = {}) {
  return h("button", { type: "button", onclick, ...attrs }, label);
}

function go(hash) {
  if (location.hash === hash) route();
  else location.hash = hash;
}

// ---------------------------------------------------------------- storage

// Every storage call is wrapped. If storage fails, the session keeps working from
// memory and a warning is shown on every screen.
const memory = {};
let storageOK = true;

function storageFailed() {
  if (!storageOK) return;
  storageOK = false;
  document.querySelector(".page-banners")?.prepend(storageWarning());
}

function storageWarning() {
  return h("div", { class: "banner warn", role: "alert" },
    "Browser storage is unavailable. This session still works, but progress and history won't survive a reload.");
}

const store = {
  get(key, fallback) {
    if (key in memory) return structuredClone(memory[key] ?? fallback);
    let raw = null;
    try {
      raw = localStorage.getItem(key);
    } catch {
      storageFailed();
      return fallback;
    }
    if (raw == null) return fallback;
    try {
      return JSON.parse(raw);
    } catch {
      return fallback;
    }
  },
  set(key, value) {
    try {
      localStorage.setItem(key, JSON.stringify(value));
      delete memory[key];
    } catch {
      storageFailed();
      memory[key] = structuredClone(value);
    }
  },
  remove(key) {
    try {
      localStorage.removeItem(key);
      delete memory[key];
    } catch {
      storageFailed();
      memory[key] = null;
    }
  },
};

try {
  localStorage.setItem("pgre.probe", "1");
  localStorage.removeItem("pgre.probe");
} catch {
  storageOK = false;
}

// ---------------------------------------------------------------- data

const examCache = new Map();

async function fetchJSON(url) {
  const r = await fetch(url, { cache: "no-store" });
  if (!r.ok) throw new Error(`Couldn't load ${url} (HTTP ${r.status})`);
  return r.json();
}

function loadExam(id) {
  if (!examCache.has(id)) {
    const p = fetchJSON(`${EXAMS}/${encodeURIComponent(id)}/exam.json`);
    p.catch(() => examCache.delete(id));
    examCache.set(id, p);
  }
  return examCache.get(id);
}

const src = (exam, path) => `${EXAMS}/${encodeURIComponent(exam.id)}/${path}`;

// Crops display at a uniform scale so text is the same size in every image, never
// upscaled past natural size, and never wider than the content column.
function examImg(exam, path, alt) {
  const img = h("img", { src: src(exam, path), alt, class: "qimg", decoding: "async" });
  const size = () => { img.style.width = `${Math.round(img.naturalWidth * IMG_SCALE)}px`; img.classList.add("ready"); };
  if (img.complete && img.naturalWidth) size();
  img.addEventListener("load", size);
  img.addEventListener("error", () => img.classList.add("broken", "ready"));
  return img;
}

function attempts() {
  return store.get(K_ATTEMPTS, []);
}

// ---------------------------------------------------------------- formatting

const pad = (n) => String(n).padStart(2, "0");

function clock(ms, round = Math.floor) {
  const t = Math.max(0, round(ms / 1000));
  return `${Math.floor(t / 3600)}:${pad(Math.floor((t % 3600) / 60))}:${pad(t % 60)}`;
}

function dur(ms) {
  const t = Math.round(Math.max(0, ms) / 1000);
  return t >= 3600 ? clock(ms, Math.round) : `${Math.floor(t / 60)}:${pad(t % 60)}`;
}

function when(ts) {
  return new Date(ts).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

function ordinal(n) {
  const tens = n % 100;
  const suffix = tens >= 11 && tens <= 13 ? "th" : { 1: "st", 2: "nd", 3: "rd" }[n % 10] ?? "th";
  return `${n}${suffix}`;
}

function ruleText(exam) {
  return exam.scoring.raw === "R-W/4"
    ? "There is a guessing penalty: your raw score is the number right minus one-fourth of the number wrong. Blank answers cost nothing."
    : "There is no guessing penalty: your raw score is the number of questions answered correctly, so answer every question.";
}

const STATUS = {
  correct: { icon: "✓", label: "Correct" },
  wrong: { icon: "✗", label: "Wrong" },
  blank: { icon: "–", label: "Blank" },
  unscored: { icon: "?", label: "No key" },
};

// ---------------------------------------------------------------- page frame

function page(title, ...body) {
  document.title = title ? `${title} · PGRE Simulator` : "PGRE Simulator";
  const banners = h("div", { class: "page-banners" }, !storageOK && storageWarning(),
    notice && h("div", { class: "banner info", role: "status" }, notice));
  notice = null;
  mount(h("div", { class: "page" },
    h("header", { class: "page-head" }, h("a", { href: "./", class: "brand" }, "PGRE Simulator")),
    banners, h("main", { class: "page-main" }, ...body)));
}

function showError(err) {
  page("Error", h("section", { class: "card" }, h("h1", {}, "Something went wrong"),
    h("p", {}, err.message ?? String(err)), h("p", {}, h("a", { href: "./" }, "Back to the exam list"))));
}

// ---------------------------------------------------------------- routing

async function route() {
  for (const d of document.querySelectorAll("dialog[open]")) d.close();
  try {
    if (params.has("proof")) return await showProof(params.get("proof"));
    const [view, arg] = location.hash.replace(/^#\/?/, "").split("/");
    if (view !== "test") stopTest();
    if (view === "start") return await showStart(decodeURIComponent(arg));
    if (view === "test") return await showTest();
    if (view === "results") return await showResults(decodeURIComponent(arg));
    return await showHome();
  } catch (err) {
    showError(err);
  }
}

window.addEventListener("hashchange", route);

// ---------------------------------------------------------------- home

async function showHome() {
  let index = null;
  let loadError = null;
  try {
    index = await fetchJSON(`${EXAMS}/index.json`);
  } catch (err) {
    loadError = err;
  }
  const exams = new Map();
  await Promise.all((index ?? []).map(async (e) => {
    try {
      exams.set(e.id, await loadExam(e.id));
    } catch { /* listed but unreadable: shown without scores */ }
  }));
  const all = attempts();
  const scored = (a) => (exams.has(a.examId) ? grade(exams.get(a.examId), a.answers) : null);
  const progress = store.get(K_PROGRESS, null);

  const examList = index?.length
    ? h("ul", { class: "exam-list" }, index.map((e) => {
      const exam = exams.get(e.id);
      const mine = all.filter((a) => a.examId === e.id).sort((a, b) => a.finishedAt - b.finishedAt);
      const scores = mine.map(scored).filter(Boolean).map((g) => g.scaled ?? g.rawInt);
      const flags = exam?.importFlags?.length ?? 0;
      return h("li", { class: "exam-row" },
        h("div", { class: "exam-info" },
          h("h3", {}, e.title),
          h("p", { class: "meta" }, `${e.questionCount} questions · ${e.timeLimitMin} min`,
            exam && ` · ${exam.scoring.raw === "R-W/4" ? "R − W/4" : "number right"}`),
          flags > 0 && h("p", { class: "meta warn-text" }, `⚠ ${flags} unresolved import flag${flags > 1 ? "s" : ""}`)),
        h("dl", { class: "exam-scores" },
          h("div", {}, h("dt", {}, "Last"), h("dd", {}, scores.length ? scores.at(-1) : "—")),
          h("div", {}, h("dt", {}, "Best"), h("dd", {}, scores.length ? Math.max(...scores) : "—"))),
        h("div", { class: "exam-actions" },
          h("a", { class: "btn primary", href: `#/start/${encodeURIComponent(e.id)}` }, "Start"),
          h("a", { class: "btn", href: `?proof=${encodeURIComponent(e.id)}` }, "Proof")));
    }))
    : h("div", { class: "empty" },
      h("p", {}, loadError ? "No exams found." : "No exams imported yet."),
      h("p", {}, "Import a practice book with ", h("code", {}, "python tools/importer.py pdfs/<file>.pdf"),
        ", then serve the repo root with ", h("code", {}, "python -m http.server 8000"), "."));

  let resume = null;
  if (progress) {
    const exam = exams.get(progress.examId);
    const left = progress.deadline ? progress.deadline - Date.now() : null;
    const i = exam ? exam.questions.findIndex((q) => q.n === progress.current) + 1 : null;
    resume = h("section", { class: "banner resume", role: "status" },
      h("div", {}, h("strong", {}, "Test in progress: "), exam?.title ?? progress.examId,
        i ? ` · question ${i} of ${exam.questions.length}` : "",
        left == null ? ` · untimed, ${dur(Date.now() - progress.startedAt)} elapsed`
          : left > 0 ? ` · ${clock(left, Math.ceil)} left` : " · time has run out"),
      h("div", { class: "actions" },
        h("a", { class: "btn primary", href: "#/test" }, left != null && left <= 0 ? "See results" : "Resume"),
        button("Abandon", () => {
          if (!confirm("Abandon this attempt? It will be discarded without a score.")) return;
          store.remove(K_PROGRESS);
          route();
        }, { class: "btn" })));
  }

  const history = [...all].sort((a, b) => b.finishedAt - a.finishedAt);
  const fileInput = h("input", { type: "file", accept: "application/json,.json", hidden: true, onchange: importHistory });
  page("", resume,
    h("section", { class: "card" }, h("h1", {}, "Practice tests"), examList),
    h("section", { class: "card" },
      h("div", { class: "card-head" }, h("h2", {}, "History"),
        h("div", { class: "actions" },
          button("Export history", exportHistory, { class: "btn", disabled: !all.length }),
          button("Import history", () => fileInput.click(), { class: "btn" }), fileInput)),
      history.length
        ? h("div", { class: "table-wrap" }, h("table", { class: "data" },
          h("thead", {}, h("tr", {}, ["Date", "Exam", "Mode", "Scaled", "Raw", "Right / wrong / blank", "Time", ""]
            .map((t) => h("th", { scope: "col" }, t)))),
          h("tbody", {}, history.map((a) => {
            const g = scored(a);
            return h("tr", {},
              h("td", {}, when(a.finishedAt)),
              h("td", {}, exams.get(a.examId)?.title ?? `${a.examId} (missing)`),
              h("td", {}, a.mode === "timed" ? "Timed" : "Untimed", a.endedBy === "time" ? " · time expired" : ""),
              h("td", { class: "num" }, g?.scaled ?? "—"),
              h("td", { class: "num" }, g ? g.rawInt : "—"),
              h("td", { class: "num" }, g ? `${g.R} / ${g.W} / ${g.B}` : "—"),
              h("td", { class: "num" }, dur(a.timeUsedMs)),
              h("td", { class: "row-actions" },
                g && h("a", { href: `#/results/${encodeURIComponent(a.id)}` }, "Results"),
                button("Delete", () => {
                  if (!confirm("Delete this attempt from history?")) return;
                  store.set(K_ATTEMPTS, attempts().filter((x) => x.id !== a.id));
                  route();
                }, { class: "linklike danger" })));
          }))))
        : h("p", { class: "muted" }, "Finished attempts appear here. Scores are recomputed from the current exam.json, so a corrected key fixes old attempts.")));
}

function exportHistory() {
  const data = { app: "pgre-sim", version: 1, exportedAt: new Date().toISOString(), attempts: attempts() };
  const url = URL.createObjectURL(new Blob([JSON.stringify(data, null, 1)], { type: "application/json" }));
  const a = h("a", { href: url, download: `pgre-history-${new Date().toISOString().slice(0, 10)}.json` });
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

async function importHistory(e) {
  const file = e.target.files?.[0];
  if (!file) return;
  try {
    const data = JSON.parse(await file.text());
    const incoming = Array.isArray(data) ? data : data.attempts;
    if (!Array.isArray(incoming)) throw new Error("no attempts array");
    const valid = incoming.filter((a) => a && typeof a.id === "string" && typeof a.examId === "string"
      && a.answers && typeof a.answers === "object" && Number.isFinite(a.finishedAt));
    const current = attempts();
    const have = new Set(current.map((a) => a.id));
    const added = valid.filter((a) => !have.has(a.id));
    store.set(K_ATTEMPTS, [...current, ...added]);
    notice = `Imported ${added.length} attempt${added.length === 1 ? "" : "s"}`
      + (valid.length > added.length ? ` (${valid.length - added.length} already present)` : "")
      + (incoming.length > valid.length ? `; skipped ${incoming.length - valid.length} invalid` : "") + ".";
  } catch (err) {
    notice = `Couldn't import ${file.name}: ${err.message}`;
  }
  route();
}

// ---------------------------------------------------------------- start

async function showStart(id) {
  const exam = await loadExam(id);
  const progress = store.get(K_PROGRESS, null);
  let mode = "timed";
  const modeChoice = (value, label, detail) => h("label", { class: "mode" },
    h("input", { type: "radio", name: "mode", value, checked: value === mode, onchange: () => { mode = value; } }),
    h("span", {}, h("strong", {}, label), h("span", { class: "muted" }, detail)));
  const flags = exam.importFlags?.length ?? 0;
  page(exam.title, h("section", { class: "card start" },
    h("h1", {}, exam.title),
    h("p", { class: "meta" }, `${exam.questionCount} questions · ${exam.timeLimitMin} minutes`),
    flags > 0 && h("p", { class: "banner warn" }, `This import has ${flags} unresolved flag${flags > 1 ? "s" : ""}. `,
      h("a", { href: `?proof=${encodeURIComponent(exam.id)}` }, "Check it in proof mode"), " before relying on the score."),
    h("h2", {}, "Directions"),
    h("p", {}, "Each question is followed by five answer choices, (A) through (E). Select the best answer for each question."),
    h("p", {}, ruleText(exam)),
    h("p", {}, "Use Mark to flag a question to revisit, and Review to see which questions are unanswered or marked. ",
      exam.reference.length ? "Reference shows the Table of Information; the clock keeps running while it is open." : ""),
    h("p", { class: "muted" }, "Keyboard: A–E choose · ← → move · M mark · R review."),
    h("fieldset", { class: "modes" }, h("legend", {}, "Mode"),
      modeChoice("timed", "Timed", `${exam.timeLimitMin} minutes, submitted automatically when time runs out`),
      modeChoice("untimed", "Untimed", "No clock; elapsed time is shown")),
    progress
      ? h("p", { class: "banner warn" }, "Another attempt is in progress. ",
        h("a", { href: "#/test" }, "Resume it"), " or abandon it from the exam list first.")
      : h("div", { class: "actions" },
        button("Begin", () => beginAttempt(exam, mode), { class: "btn primary big" }),
        h("a", { class: "btn", href: "#/" }, "Cancel"))));
}

function beginAttempt(exam, mode) {
  const now = Date.now();
  store.set(K_PROGRESS, {
    examId: exam.id, mode, startedAt: now,
    deadline: mode === "timed" ? now + exam.timeLimitMin * 60000 : null,
    current: exam.questions[0].n, answers: {}, marked: {}, timeSpent: {},
  });
  go("#/test");
}

// ---------------------------------------------------------------- test

async function showTest() {
  const state = store.get(K_PROGRESS, null);
  if (!state) return go("#/");
  const exam = await loadExam(state.examId);
  stopTest();
  T = {
    exam, state, view: "question", activeSince: null, hideTime: false, warned: false, filter: "all",
    index: new Map(exam.questions.map((q, i) => [q.n, i])), els: {}, timers: [],
  };
  if (!T.index.has(state.current)) state.current = exam.questions[0].n;
  if (state.deadline && state.deadline <= Date.now()) {
    notice = "Time ran out while the test was closed, so it was submitted automatically.";
    return finish("time", true);
  }
  document.title = `${exam.title} · PGRE Simulator`;
  renderShell();
  showQuestionView();
  T.timers.push(setInterval(tick, 250), setInterval(flush, SAVE_MS));
}

function stopTest() {
  if (!T) return;
  clockStop();
  save();
  T.timers.forEach(clearInterval);
  T = null;
}

function save() {
  if (T) store.set(K_PROGRESS, T.state);
}

// Time per question accumulates only while the question is on screen and the tab is visible.
function clockStart() {
  if (T && T.view === "question" && !document.hidden && T.activeSince == null) T.activeSince = performance.now();
}

function clockStop() {
  if (!T || T.activeSince == null) return;
  const n = T.state.current;
  T.state.timeSpent[n] = Math.round((T.state.timeSpent[n] ?? 0) + performance.now() - T.activeSince);
  T.activeSince = null;
}

function flush() {
  clockStop();
  clockStart();
  save();
}

document.addEventListener("visibilitychange", () => {
  if (!T) return;
  if (document.hidden) {
    clockStop();
    save();
  } else {
    clockStart();
    tick();
  }
});
window.addEventListener("pagehide", () => {
  clockStop();
  save();
});

function renderShell() {
  const els = T.els;
  els.counter = h("span", { class: "counter" });
  els.flag = h("span", { class: "mark-flag", hidden: true }, "⚑ Marked");
  els.timeLabel = h("span", { class: "time-label" }, T.state.deadline ? "Time left" : "Elapsed");
  els.time = h("span", { class: "time-digits", role: "timer" });
  els.timeBtn = button("Hide Time", () => { T.hideTime = !T.hideTime; updateTimer(); }, { class: "time-toggle" });
  els.toolbar = h("nav", { class: "toolbar", "aria-label": "Test controls" });
  els.banner = h("div", { class: "test-banners" }, !storageOK && storageWarning());
  els.main = h("main", { class: "test-main" });
  mount(h("div", { class: "test" },
    h("div", { class: "test-bars" },
      h("header", { class: "test-top" },
        h("div", { class: "test-title" }, T.exam.title),
        h("div", { class: "test-counter" }, els.counter, els.flag),
        h("div", { class: "test-timer" }, els.timeBtn, els.timeLabel, els.time)),
      els.toolbar),
    els.banner, els.main));
  updateTimer();
}

function tick() {
  if (!T) return;
  const { deadline } = T.state;
  if (deadline) {
    const left = deadline - Date.now();
    if (left <= 0) {
      notice = "Time is up. Your test was submitted automatically.";
      return finish("time");
    }
    if (left <= WARN_MS && !T.warned) {
      T.warned = true;
      const banner = h("div", { class: "banner warn", role: "status" },
        h("span", {}, "5 minutes remaining."),
        button("Dismiss", () => banner.remove(), { class: "linklike" }));
      T.els.banner.append(banner);
    }
  }
  updateTimer();
}

function updateTimer() {
  const { deadline, startedAt } = T.state;
  const { time, timeLabel, timeBtn } = T.els;
  time.textContent = deadline ? clock(deadline - Date.now(), Math.ceil) : clock(Date.now() - startedAt);
  time.hidden = timeLabel.hidden = T.hideTime;
  timeBtn.textContent = T.hideTime ? "Show Time" : "Hide Time";
}

const currentQ = () => T.exam.questions[T.index.get(T.state.current)];

function toolButton(label, onclick, { class: cls = "", ...attrs } = {}) {
  return button(label, onclick, { ...attrs, class: `tool ${cls}`.trim() });
}

function showQuestionView() {
  T.view = "question";
  const q = currentQ();
  const i = T.index.get(q.n);
  const marked = !!T.state.marked[q.n];
  const N = T.exam.questions.length;
  T.els.counter.textContent = `Question ${i + 1} of ${N}`;
  T.els.flag.hidden = !marked;
  T.els.toolbar.replaceChildren(
    h("div", { class: "tb-group" },
      toolButton("End Test", openEndDialog),
      toolButton("Reference", openReference, { disabled: !T.exam.reference.length })),
    h("div", { class: "tb-group" },
      toolButton("Review", showReviewView),
      toolButton(marked ? "⚑ Marked" : "Mark", toggleMark, { "aria-pressed": String(marked), class: marked ? "on" : "" }),
      toolButton("Back", () => step(-1), { disabled: i === 0 }),
      toolButton("Next", () => step(1), { class: "primary" })));

  const set = q.set ? T.exam.sets.find((x) => x.id === q.set) : null;
  const given = T.state.answers[q.n] ?? null;
  T.els.choices = h("fieldset", { class: "choices" },
    h("legend", { class: "sr-only" }, `Answer for question ${i + 1}`),
    LETTERS.map((L) => h("label", { class: `choice${given === L ? " selected" : ""}`, "data-letter": L },
      h("input", { type: "radio", name: "answer", value: L, checked: given === L, onchange: () => choose(L) }),
      h("span", {}, `(${L})`))));
  T.els.clear = button("Clear answer", () => choose(null), { class: "linklike clear", disabled: !given });
  T.els.main.replaceChildren(h("article", { class: "question" },
    h("div", { class: "paper" },
      set && h("div", { class: "stem" }, examImg(T.exam, set.image, `Shared stem for questions ${set.first}–${set.last}`)),
      q.images.map((p, k) => examImg(T.exam, p, `Question ${i + 1}${q.images.length > 1 ? `, part ${k + 1}` : ""}`))),
    T.els.choices, T.els.clear));
  window.scrollTo(0, 0);
  clockStart();
}

function choose(letter) {
  const n = T.state.current;
  if (letter) T.state.answers[n] = letter;
  else delete T.state.answers[n];
  save();
  for (const label of T.els.choices.querySelectorAll(".choice")) {
    const on = label.dataset.letter === letter;
    label.classList.toggle("selected", on);
    label.querySelector("input").checked = on;
  }
  T.els.clear.disabled = !letter;
}

function toggleMark() {
  const n = T.state.current;
  if (T.state.marked[n]) delete T.state.marked[n];
  else T.state.marked[n] = true;
  save();
  clockStop();
  showQuestionView();
}

function goTo(n) {
  clockStop();
  T.state.current = n;
  save();
  showQuestionView();
}

function step(delta) {
  const i = T.index.get(T.state.current) + delta;
  if (i < 0) return;
  if (i >= T.exam.questions.length) return showReviewView();
  goTo(T.exam.questions[i].n);
}

function showReviewView() {
  clockStop();
  T.view = "review";
  save();
  const { exam, state } = T;
  const answered = exam.questions.filter((q) => state.answers[q.n]).length;
  const markedCount = exam.questions.filter((q) => state.marked[q.n]).length;
  T.els.counter.textContent = "Review";
  T.els.flag.hidden = true;
  T.els.toolbar.replaceChildren(
    h("div", { class: "tb-group" }, toolButton("End Test", openEndDialog)),
    h("div", { class: "tb-group" }, toolButton("Return", returnFromReview, { class: "primary" })));
  const filters = [["all", "All"], ["unanswered", "Unanswered"], ["marked", "Marked"]];
  const rows = exam.questions.map((q, i) => ({ q, i, answered: !!state.answers[q.n], marked: !!state.marked[q.n] }))
    .filter((r) => T.filter === "all" || (T.filter === "unanswered" ? !r.answered : r.marked));
  T.els.main.replaceChildren(h("section", { class: "review" },
    h("h1", {}, "Review"),
    h("p", { class: "meta" }, `Answered ${answered} · Not answered ${exam.questions.length - answered} · Marked ${markedCount}`),
    h("div", { class: "chips", role: "group", "aria-label": "Filter" }, filters.map(([key, label]) =>
      button(label, () => { T.filter = key; showReviewView(); },
        { class: `chip${T.filter === key ? " on" : ""}`, "aria-pressed": String(T.filter === key) }))),
    h("p", { class: "muted" }, "Select a question to go to it."),
    h("div", { class: "table-wrap" }, h("table", { class: "data review-table" },
      h("thead", {}, h("tr", {}, h("th", { scope: "col" }, "Question"), h("th", { scope: "col" }, "Status"),
        h("th", { scope: "col" }, "Marked"))),
      h("tbody", {}, rows.length ? rows.map((r) => h("tr", {
        tabindex: "0", class: `${r.answered ? "" : "unanswered"}${r.q.n === state.current ? " current" : ""}`,
        onclick: () => goTo(r.q.n),
        onkeydown: (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); goTo(r.q.n); } },
      }, h("td", { class: "num" }, r.i + 1), h("td", {}, r.answered ? "Answered" : "Not Answered"),
      h("td", {}, r.marked ? "⚑ Marked" : "")))
        : h("tr", {}, h("td", { colspan: "3", class: "muted" }, "No questions match this filter.")))))));
  window.scrollTo(0, 0);
}

function returnFromReview() {
  showQuestionView();
}

document.addEventListener("keydown", (e) => {
  if (!T || e.metaKey || e.ctrlKey || e.altKey || document.querySelector("dialog[open]")) return;
  const key = e.key.length === 1 ? e.key.toUpperCase() : e.key;
  if (T.view === "question") {
    if (LETTERS.includes(key)) choose(key);
    else if (key === "ArrowLeft") step(-1);
    else if (key === "ArrowRight") step(1);
    else if (key === "M") toggleMark();
    else if (key === "R") showReviewView();
    else return;
  } else if (T.view === "review") {
    if (key === "R" || key === "Escape") returnFromReview();
    else return;
  } else return;
  e.preventDefault();
});

function dialog(...content) {
  const d = h("dialog", { class: "dialog" }, ...content);
  d.addEventListener("close", () => d.remove());
  document.body.append(d);
  d.showModal();
  return d;
}

function openReference() {
  const d = dialog(
    h("div", { class: "dialog-head" }, h("h2", {}, "Table of Information"),
      button("Close", () => d.close(), { class: "btn" })),
    h("div", { class: "paper reference" }, T.exam.reference.map((p, i) => examImg(T.exam, p, `Reference page ${i + 1}`))));
  d.classList.add("wide");
}

function openEndDialog() {
  const { exam, state } = T;
  const unanswered = exam.questions.filter((q) => !state.answers[q.n]).length;
  const marked = exam.questions.filter((q) => state.marked[q.n]).length;
  const d = dialog(
    h("h2", {}, "End the test?"),
    h("p", {}, `You have ${unanswered} unanswered question${unanswered === 1 ? "" : "s"} and ${marked} marked question${marked === 1 ? "" : "s"}.`),
    h("p", { class: "muted" }, "Once you end the test you can't return to it."),
    h("div", { class: "actions" },
      button("Return to Test", () => d.close(), { class: "btn", autofocus: true }),
      button("End Test", () => { d.close(); finish("user"); }, { class: "btn danger" })));
}

function finish(endedBy, alreadyStopped = false) {
  if (!T) return;
  if (!alreadyStopped) clockStop();
  const { exam, state } = T;
  T.timers.forEach(clearInterval);
  const finishedAt = Date.now();
  const end = state.deadline ? Math.min(finishedAt, state.deadline) : finishedAt;
  const attempt = {
    id: `a${state.startedAt.toString(36)}${Math.random().toString(36).slice(2, 6)}`,
    examId: exam.id, mode: state.mode, startedAt: state.startedAt, finishedAt, endedBy,
    timeLimitMin: state.deadline ? exam.timeLimitMin : null, timeUsedMs: end - state.startedAt,
    answers: state.answers, marked: state.marked, timeSpent: state.timeSpent,
  };
  store.set(K_ATTEMPTS, [...attempts(), attempt]);
  store.remove(K_PROGRESS);
  T = null;
  go(`#/results/${attempt.id}`);
}

// ---------------------------------------------------------------- results

async function showResults(id) {
  const attempt = attempts().find((a) => a.id === id);
  if (!attempt) throw new Error("That attempt isn't in this browser's history.");
  const exam = await loadExam(attempt.examId);
  const g = grade(exam, attempt.answers);
  const times = Object.fromEntries(exam.questions.map((q) => [q.n, attempt.timeSpent?.[q.n] ?? 0]));
  const med = median(Object.values(times).filter((t) => t > 0));
  const rows = g.items.map((it) => {
    const q = exam.questions.find((x) => x.n === it.n);
    return { ...it, q, i: exam.questions.indexOf(q), time: times[it.n], marked: !!attempt.marked?.[it.n],
      slow: med > 0 && times[it.n] > 2 * med };
  });
  const hasPplus = exam.questions.some((q) => q.pplus != null);
  let filter = "all";
  const filters = {
    all: ["All", () => true], wrong: ["Wrong", (r) => r.status === "wrong"], blank: ["Blank", (r) => r.status === "blank"],
    marked: ["Marked", (r) => r.marked], slow: ["Slow", (r) => r.slow],
  };
  const shown = () => rows.filter(filters[filter][1]);
  const tableBody = h("tbody");
  const chips = h("div", { class: "chips", role: "group", "aria-label": "Filter questions" });
  const renderTable = () => {
    chips.replaceChildren(...Object.entries(filters).map(([key, [label, fn]]) =>
      button(`${label} ${rows.filter(fn).length}`, () => { filter = key; renderTable(); },
        { class: `chip${filter === key ? " on" : ""}`, "aria-pressed": String(filter === key) })));
    const list = shown();
    tableBody.replaceChildren(...(list.length ? list.map((r) => h("tr", {
      tabindex: "0", class: `status-${r.status}`,
      onclick: () => openDetail(exam, list, r),
      onkeydown: (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); openDetail(exam, list, r); } },
    },
    h("td", { class: "num" }, r.i + 1),
    h("td", { class: "num" }, r.given ?? "—"),
    h("td", { class: "num" }, r.key ?? "?"),
    h("td", { class: `mark-cell ${r.status}` }, h("span", { "aria-hidden": "true" }, STATUS[r.status].icon),
      h("span", { class: "sr-only" }, STATUS[r.status].label)),
    h("td", { class: "num" }, r.time ? dur(r.time) : "—", r.slow ? h("span", { class: "slow-tag" }, " slow") : ""),
    hasPplus && h("td", { class: "num" }, r.q.pplus ?? "—"),
    h("td", {}, r.marked ? "⚑" : ""))) : [h("tr", {}, h("td", { colspan: "7", class: "muted" }, "No questions match this filter."))]));
  };
  renderTable();

  const pacing = rows.some((r) => r.time > 0)
    ? pacingChart(rows, med, (r) => openDetail(exam, rows, r))
    : h("p", { class: "muted" }, "No time was recorded for this attempt.");
  const hero = g.scaled != null
    ? h("div", { class: "hero" }, h("div", { class: "hero-label" }, "Scaled score"),
      h("div", { class: "hero-value" }, g.scaled),
      h("div", { class: "hero-sub" }, g.pct != null ? `${ordinal(g.pct)} percentile` : "Percentile not available"))
    : h("div", { class: "hero" }, h("div", { class: "hero-label" }, "Raw score"),
      h("div", { class: "hero-value" }, g.rawInt), h("div", { class: "hero-sub" }, "No conversion table in this import"));
  const tile = (label, value, sub, cls = "") => h("div", { class: `tile ${cls}` }, h("div", { class: "tile-label" }, label),
    h("div", { class: "tile-value" }, value), sub && h("div", { class: "tile-sub" }, sub));

  page(`${exam.title} results`,
    h("section", { class: "card" },
      h("div", { class: "card-head" },
        h("div", {}, h("h1", {}, `${exam.title} results`),
          h("p", { class: "meta" }, when(attempt.finishedAt), " · ", attempt.mode === "timed" ? "Timed" : "Untimed",
            attempt.endedBy === "time" ? " · time expired" : "")),
        h("div", { class: "actions" }, h("a", { class: "btn", href: "#/" }, "Exam list"),
          h("a", { class: "btn", href: `#/start/${encodeURIComponent(exam.id)}` }, "Take again"))),
      h("div", { class: "headline" }, hero,
        h("div", { class: "tiles" },
          tile("Raw score", g.formula, exam.scoring.raw === "R-W/4" ? "right − wrong/4, rounded half-up" : "number right", "wide"),
          tile("Correct", g.R), tile("Wrong", g.W), tile("Blank", g.B),
          tile("Time used", dur(attempt.timeUsedMs), attempt.timeLimitMin ? `of ${attempt.timeLimitMin} min` : "untimed"))),
      g.unscored > 0 && h("p", { class: "banner warn" }, `${g.unscored} question${g.unscored > 1 ? "s have" : " has"} no answer key and ${g.unscored > 1 ? "were" : "was"} not scored.`)),
    g.categories.length > 0 && h("section", { class: "card" }, h("h2", {}, "By category"),
      h("table", { class: "data cats" },
        h("thead", {}, h("tr", {}, ["Category", "Correct", ""].map((t) => h("th", { scope: "col" }, t)))),
        h("tbody", {}, g.categories.map((c) => h("tr", {},
          h("td", {}, c.category), h("td", { class: "num" }, `${c.correct} / ${c.total}`),
          h("td", { class: "meter-cell" }, h("div", { class: "meter", role: "img", "aria-label": `${Math.round(100 * c.correct / c.total)}% correct` },
            h("div", { class: "meter-fill", style: `width:${(100 * c.correct) / c.total}%` })))))))),
    h("section", { class: "card" }, h("h2", {}, "Pacing"),
      h("p", { class: "muted" }, med > 0 ? `Median ${dur(med)} per question. Slow means over twice the median.` : ""), pacing),
    h("section", { class: "card" }, h("h2", {}, "Questions"), chips,
      h("div", { class: "table-wrap" }, h("table", { class: "data results-table" },
        h("thead", {}, h("tr", {}, ["#", "Yours", "Key", "Result", "Time", hasPplus && "P+", "Marked"]
          .filter(Boolean).map((t) => h("th", { scope: "col" }, t)))),
        tableBody))));
}

function openDetail(exam, list, row) {
  let k = list.indexOf(row);
  const body = h("div", { class: "detail-body" });
  const title = h("h2");
  const nav = h("div", { class: "actions" });
  const d = dialog(h("div", { class: "dialog-head" }, title, nav), body);
  d.classList.add("wide");
  const render = () => {
    const r = list[k];
    const set = r.q.set ? exam.sets.find((x) => x.id === r.q.set) : null;
    title.textContent = `Question ${r.i + 1}`;
    nav.replaceChildren(
      button("Previous", () => { k -= 1; render(); }, { class: "btn", disabled: k === 0 }),
      button("Next", () => { k += 1; render(); }, { class: "btn", disabled: k === list.length - 1 }),
      button("Close", () => d.close(), { class: "btn" }));
    body.replaceChildren(
      h("p", { class: "meta" }, `${STATUS[r.status].icon} ${STATUS[r.status].label}`, ` · time ${r.time ? dur(r.time) : "—"}`,
        r.q.pplus != null ? ` · P+ ${r.q.pplus}` : "", r.q.category ? ` · ${r.q.category}` : "", r.marked ? " · ⚑ marked" : ""),
      h("div", { class: "paper" },
        set && h("div", { class: "stem" }, examImg(exam, set.image, `Shared stem for questions ${set.first}–${set.last}`)),
        r.q.images.map((p) => examImg(exam, p, `Question ${r.i + 1}`))),
      h("ul", { class: "choices static" }, LETTERS.map((L) => {
        const isKey = L === r.key;
        const isMine = L === r.given;
        return h("li", { class: `choice${isKey ? " key" : ""}${isMine && !isKey ? " mine" : ""}` },
          h("span", {}, `(${L})`),
          isKey && h("span", { class: "tag" }, isMine ? "✓ Your answer, correct" : "✓ Correct answer"),
          isMine && !isKey && h("span", { class: "tag" }, "✗ Your answer"));
      })));
  };
  d.addEventListener("keydown", (e) => {
    if (e.key === "ArrowLeft" && k > 0) { k -= 1; render(); }
    if (e.key === "ArrowRight" && k < list.length - 1) { k += 1; render(); }
  });
  render();
}

// Time per question as columns colored by result (status palette + icon legend), with
// labeled median and 2× median reference lines. The question table is its table view.
function pacingChart(rows, med, onPick) {
  const W = 900, H = 240, M = { t: 12, r: 84, b: 28, l: 46 };
  const iw = W - M.l - M.r, ih = H - M.t - M.b;
  const slot = iw / rows.length;
  const bw = Math.max(1, Math.min(24, slot - 2));
  const peak = Math.max(...rows.map((r) => r.time), 2 * med, 1000) / 1000;
  const stepS = [10, 15, 30, 60, 120, 180, 300, 600, 900, 1800].find((v) => peak / v <= 5) ?? 3600;
  const yMax = Math.ceil(peak / stepS) * stepS;
  const y = (sec) => M.t + ih - (sec / yMax) * ih;
  const base = M.t + ih;
  const xOf = (i) => M.l + i * slot + slot / 2;

  const grid = [];
  for (let v = 0; v <= yMax; v += stepS) {
    grid.push(s("line", { class: v ? "grid" : "axis", x1: M.l, x2: M.l + iw, y1: y(v), y2: y(v) }),
      s("text", { class: "tick", x: M.l - 6, y: y(v) + 4, "text-anchor": "end" }, dur(v * 1000)));
  }
  const every = rows.length > 60 ? 10 : rows.length > 20 ? 5 : 1;
  const xticks = rows.filter((r, i) => i === 0 || (r.i + 1) % every === 0)
    .map((r) => s("text", { class: "tick", x: xOf(r.i), y: base + 18, "text-anchor": "middle" }, r.i + 1));

  const bars = rows.map((r, i) => {
    if (!r.time) return null;
    const x = xOf(i) - bw / 2;
    const top = y(r.time / 1000);
    const rad = Math.min(4, bw / 2, base - top);
    return s("path", { class: `bar ${r.status}`, "data-i": i,
      d: `M${x},${base}V${top + rad}Q${x},${top} ${x + rad},${top}H${x + bw - rad}Q${x + bw},${top} ${x + bw},${top + rad}V${base}Z` });
  });
  const refs = [];
  if (med > 0) {
    for (const [mult, cls, label] of [[1, "ref", `median ${dur(med)}`], [2, "ref slow", "2× median"]]) {
      const yy = y((mult * med) / 1000);
      refs.push(s("line", { class: cls, x1: M.l, x2: M.l + iw, y1: yy, y2: yy }),
        s("text", { class: `ref-label${mult === 2 ? " slow" : ""}`, x: M.l + iw + 6, y: yy + 4 }, label));
    }
  }
  const hits = rows.map((r, i) => s("rect", { class: "hit", x: M.l + i * slot, y: M.t, width: slot, height: ih, "data-i": i }));

  const chart = s("svg", { class: "pacing", viewBox: `0 0 ${W} ${H}`, tabindex: "0", role: "img",
    "aria-label": "Time per question. Arrow keys move between questions; Enter opens one. The question table below lists the same times." },
  grid, xticks, bars, refs, hits);
  const tip = h("div", { class: "tip", hidden: true });
  const wrap = h("div", { class: "chart-wrap" }, chart, tip);
  let hot = -1;
  const show = (i) => {
    hot = i;
    chart.querySelectorAll(".bar.hot").forEach((b) => b.classList.remove("hot"));
    chart.querySelector(`.bar[data-i="${i}"]`)?.classList.add("hot");
    const r = rows[i];
    tip.replaceChildren(h("strong", {}, r.time ? dur(r.time) : "—"),
      h("span", {}, `Question ${r.i + 1} · ${STATUS[r.status].icon} ${STATUS[r.status].label}`));
    tip.hidden = false;
    const px = (xOf(i) / W) * chart.clientWidth;
    tip.style.left = `${Math.min(Math.max(px, 70), chart.clientWidth - 70)}px`;
  };
  const hide = () => { tip.hidden = true; hot = -1; chart.querySelectorAll(".bar.hot").forEach((b) => b.classList.remove("hot")); };
  chart.addEventListener("pointermove", (e) => { const i = e.target.dataset?.i; if (i != null) show(Number(i)); });
  chart.addEventListener("pointerleave", hide);
  chart.addEventListener("click", (e) => { const i = e.target.dataset?.i; if (i != null) onPick(rows[Number(i)]); });
  chart.addEventListener("focus", () => show(hot >= 0 ? hot : 0));
  chart.addEventListener("blur", hide);
  chart.addEventListener("keydown", (e) => {
    if (e.key === "ArrowRight" || e.key === "ArrowLeft") {
      e.preventDefault();
      show(Math.min(rows.length - 1, Math.max(0, hot + (e.key === "ArrowRight" ? 1 : -1))));
    } else if ((e.key === "Enter" || e.key === " ") && hot >= 0) {
      e.preventDefault();
      onPick(rows[hot]);
    }
  });
  const present = ["correct", "wrong", "blank", "unscored"].filter((k) => rows.some((r) => r.status === k && r.time));
  const legend = h("ul", { class: "legend" },
    present.map((k) => h("li", {}, h("span", { class: `swatch ${k}` }), `${STATUS[k].icon} ${STATUS[k].label}`)),
    med > 0 && h("li", {}, h("span", { class: "line-key" }), "Median"));
  return h("div", { class: "pacing-block" }, legend, wrap);
}

// ---------------------------------------------------------------- proof mode

async function showProof(id) {
  let exam;
  try {
    exam = await loadExam(id);
  } catch (err) {
    return page("Proof", h("section", { class: "card" }, h("h1", {}, `Proof: ${id}`), h("p", {}, err.message)));
  }
  const keyed = exam.questions.filter((q) => q.answer).length;
  const flagsByQ = new Map();
  for (const f of exam.importFlags ?? []) {
    const m = /^q(\d+)$/.exec(f.target);
    if (m) flagsByQ.set(Number(m[1]), [...(flagsByQ.get(Number(m[1])) ?? []), f]);
  }
  const seenSets = new Set();
  const flagLine = (f) => h("li", {}, h("strong", {}, f.code), ` ${f.target}`, f.page ? ` p${f.page}` : "", ` ${f.detail}`,
    f.review.map((r) => h("details", {}, h("summary", {}, r), h("img", { src: src(exam, r), alt: `Review overlay ${r}`, class: "overlay" }))));
  page(`Proof ${exam.title}`,
    h("section", { class: "card" },
      h("div", { class: "card-head" }, h("h1", {}, `Proof: ${exam.title}`),
        h("a", { class: "btn", href: "./" }, "Exam list")),
      h("p", { class: "meta" },
        `${exam.questionCount} questions · key ${keyed}/${exam.questionCount} · `,
        `time ${exam.timeLimitMin} min (${exam.timeLimitSource}) · rule ${exam.scoring.raw} (${exam.scoring.rawSource}), rounding ${exam.scoring.rounding} · `,
        `${exam.sets.length} sets · ${exam.reference.length} reference · source ${exam.source} · imported ${when(exam.importedAt)}`),
      exam.importFlags?.length
        ? h("div", { class: "banner warn" }, h("strong", {}, `${exam.importFlags.length} unresolved import flags`),
          h("ul", { class: "flags" }, exam.importFlags.map(flagLine)))
        : h("p", { class: "banner ok" }, "No unresolved import flags.")),
    h("section", { class: "card" }, h("h2", {}, "Answer key"),
      h("ol", { class: "key-grid" }, exam.questions.map((q) => h("li", {}, h("span", { class: "kn" }, q.n),
        h("span", { class: q.answer ? "ka" : "ka missing" }, q.answer ?? "?"))))),
    h("section", { class: "card" }, h("h2", {}, "Conversion table"),
      exam.scoring.scale.length
        ? h("ol", { class: "scale-grid" }, [...exam.scoring.scale].reverse().map((e) =>
          h("li", {}, h("span", { class: "kn" }, e.raw), h("span", {}, e.scaled), e.pct != null && h("span", { class: "muted" }, `${e.pct}%`))))
        : h("p", { class: "banner warn" }, "No conversion table.")),
    h("section", { class: "card" }, h("h2", {}, "Reference"),
      exam.reference.length ? h("div", { class: "paper" }, exam.reference.map((p) => examImg(exam, p, p)))
        : h("p", { class: "banner warn" }, "No Table of Information.")),
    h("section", { class: "card" }, h("h2", {}, "Questions"),
      exam.questions.map((q) => {
        const set = q.set && !seenSets.has(q.set) ? exam.sets.find((x) => x.id === q.set) : null;
        if (set) seenSets.add(set.id);
        const flags = flagsByQ.get(q.n) ?? [];
        return [
          set && h("div", { class: "proof-set" }, h("div", { class: "proof-head" }, h("strong", {}, `Set ${set.first}–${set.last}`), " shared stem"),
            h("div", { class: "paper" }, examImg(exam, set.image, `Set ${set.id}`))),
          h("article", { class: `proof-q${flags.length ? " flagged" : ""}`, id: `q${q.n}` },
            h("div", { class: "proof-head" },
              h("span", { class: "qn" }, q.n), h("span", {}, `p${q.page}`),
              h("span", { class: "answer" }, "Answer ", h("strong", {}, q.answer ?? "?")),
              q.pplus != null && h("span", {}, `P+ ${q.pplus}`), q.category && h("span", {}, q.category),
              q.set && h("span", {}, `set ${q.set}`), q.images.length > 1 && h("span", {}, `${q.images.length} segments`)),
            flags.length > 0 && h("ul", { class: "flags" }, flags.map(flagLine)),
            h("div", { class: "paper" }, q.images.map((p) => examImg(exam, p, `Question ${q.n}`)))),
        ];
      })));
}

route();
