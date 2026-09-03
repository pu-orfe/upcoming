const assert = require("node:assert/strict");
const test = require("node:test");
// Tests the exact file the Pages site loads, so the two cannot drift.
const S = require("../../site/feed-simulator.js");

// Calendar facts: 2026-09-07 and 2026-09-21 are Mondays; Labor Day 2026 is Mon 09-07.

test("weekdayOf uses Monday=0 like Python's date.weekday()", () => {
  assert.equal(S.weekdayOf("2026-09-07"), 0);
  assert.equal(S.weekdayOf("2026-09-08"), 1);
  assert.equal(S.weekdayOf("2026-09-13"), 6);
  assert.equal(S.weekdayName("2026-09-08"), "Tuesday");
});

test("weekStartFor returns the Monday of the week", () => {
  assert.equal(S.weekStartFor("2026-09-13"), "2026-09-07"); // a Sunday
  assert.equal(S.weekStartFor("2026-09-07"), "2026-09-07");
});

test("addDays crosses month and year boundaries", () => {
  assert.equal(S.addDays("2026-09-30", 1), "2026-10-01");
  assert.equal(S.addDays("2027-01-01", -1), "2026-12-31");
});

test("default deadline is the Tuesday preceding publication", () => {
  assert.equal(S.defaultDeadlineDate("2026-09-21"), "2026-09-15");
  assert.equal(S.weekdayName(S.defaultDeadlineDate("2026-09-21")), "Tuesday");
});

test("a plain week resolves to the documented bounds", () => {
  const e = S.resolveEdition({ publicationDate: "2026-09-21" });
  assert.equal(e.id, "2026-09-21");
  assert.equal(e.publicationAt, "2026-09-21T12:00:00");
  assert.equal(e.deadlineAt, "2026-09-15T12:00:00");
  assert.equal(e.coverageStart, "2026-09-21T00:00:00");
  assert.equal(e.coverageEnd, "2026-09-27T23:59:59");
  assert.equal(e.shifted, false);
});

test("Labor Day week: publication shifts but the edition id and Sunday end do not", () => {
  const e = S.resolveEdition({ publicationDate: "2026-09-08" });
  assert.equal(e.id, "2026-09-07", "the id is the week anchor, not the publication date");
  assert.equal(e.coverageStart, "2026-09-08T00:00:00", "coverage start follows the shift");
  assert.equal(e.coverageEnd, "2026-09-13T23:59:59", "coverage end stays pinned to the week");
  assert.equal(e.deadlineAt, "2026-09-01T12:00:00", "the deadline does not get dragged along");
  assert.equal(e.shifted, true);
});

test("an explicit deadline overrides the derived one", () => {
  const e = S.resolveEdition({
    publicationDate: "2026-09-08",
    deadlineDate: "2026-09-02",
    deadlineTime: "17:30",
  });
  assert.equal(e.deadlineAt, "2026-09-02T17:30:00");
});

test("times accept HH:MM and HH:MM:SS", () => {
  assert.equal(S.normalizeTime("09:30", "12:00:00"), "09:30:00");
  assert.equal(S.normalizeTime("09:30:15", "12:00:00"), "09:30:15");
  assert.equal(S.normalizeTime("", "12:00:00"), "12:00:00");
  assert.throws(() => S.normalizeTime("noon", "12:00:00"), /HH:MM/);
});

test("bad dates are rejected rather than coerced", () => {
  assert.throws(() => S.parseDate("2026-02-30"), /not a real date/);
  assert.throws(() => S.parseDate("21-09-2026"), /YYYY-MM-DD/);
});

test("window edges are inclusive at both ends", () => {
  const e = S.resolveEdition({ publicationDate: "2026-09-21" });
  assert.equal(S.inWindow("2026-09-20T23:59:59", e), false);
  assert.equal(S.inWindow("2026-09-21T00:00:00", e), true);
  assert.equal(S.inWindow("2026-09-27T23:59:59", e), true);
  assert.equal(S.inWindow("2026-09-28T00:00:00", e), false);
});

test("a timestamp with a timezone suffix is not treated as in-window", () => {
  const e = S.resolveEdition({ publicationDate: "2026-09-21" });
  assert.equal(S.inWindow("2026-09-22T12:15:00Z", e), false);
});

test("results do not depend on the viewer's timezone", () => {
  // The whole point of comparing naive strings: an editor in California must see
  // exactly what WordPress sees.
  const events = [
    { guid: "a", startTime: "2026-09-21T00:15:00", title: "Early Monday" },
    { guid: "b", startTime: "2026-09-27T23:45:00", title: "Late Sunday" },
    { guid: "c", startTime: "2026-09-28T00:15:00", title: "Next week" },
  ];
  const original = process.env.TZ;
  const seen = [];
  for (const tz of ["America/New_York", "America/Los_Angeles", "Asia/Tokyo", "UTC"]) {
    process.env.TZ = tz;
    const e = S.resolveEdition({ publicationDate: "2026-09-21" });
    seen.push(S.partition(events, e).included.map((i) => i.guid).join(","));
  }
  process.env.TZ = original;
  assert.deepEqual(new Set(seen), new Set(["a,b"]), `got ${JSON.stringify(seen)}`);
});

test("the feed's own placeholder flag wins", () => {
  assert.deepEqual(
    S.placeholderState({ titleIsPlaceholder: true, titleSource: "fallback-series" }),
    { placeholder: true, inferred: false, source: "fallback-series" }
  );
  assert.deepEqual(
    S.placeholderState({ titleIsPlaceholder: false, titleSource: "enriched" }),
    { placeholder: false, inferred: false, source: "enriched" }
  );
});

test("titleSource alone is enough when the boolean is missing", () => {
  assert.equal(S.placeholderState({ titleSource: "fallback-template" }).placeholder, true);
  assert.equal(S.placeholderState({ titleSource: "ics" }).placeholder, false);
});

test("a feed with no provenance falls back to inference, and says so", () => {
  const synthesized = S.placeholderState({
    title: "An ORFE Departmental Colloquia Talk", speaker: "Alice",
  });
  assert.equal(synthesized.placeholder, true);
  assert.equal(synthesized.inferred, true);

  const speakerTitled = S.placeholderState({ title: "Alice Smith", speaker: "Alice Smith" });
  assert.equal(speakerTitled.placeholder, true);

  const real = S.placeholderState({ title: "Transfer Treatment Effects", speaker: "Alice" });
  assert.equal(real.placeholder, false);
  assert.equal(real.inferred, true);
});

test("ICS escaping is undone without eating ordinary punctuation", () => {
  assert.equal(S.unescapeIcs("Chen\\, New York University"), "Chen, New York University");
  assert.equal(S.unescapeIcs("a\;b"), "a;b");
  // Regression: a naive /\;/ pattern would strip every semicolon in the string.
  assert.equal(S.unescapeIcs("Optimization; Learning"), "Optimization; Learning");
  assert.equal(S.unescapeIcs(null), "");
});

test("locations render as name plus detail", () => {
  assert.equal(S.formatLocation({ name: "Sherrerd Hall", id: "", detail: "101" }), "Sherrerd Hall 101");
  assert.equal(S.formatLocation({ name: "", id: "", detail: "101" }), "101");
  assert.equal(S.formatLocation(null), "");
});

test("partition splits the feed and counts placeholders", () => {
  const e = S.resolveEdition({ publicationDate: "2026-09-21" });
  const events = [
    { guid: "in1", startTime: "2026-09-22T16:15:00", title: "Real", titleIsPlaceholder: false },
    { guid: "in2", startTime: "2026-09-24T12:00:00", title: "A Seminar Talk", titleIsPlaceholder: true },
    { guid: "out", startTime: "2026-10-05T12:00:00", title: "Real", titleIsPlaceholder: false },
  ];
  const result = S.partition(events, e);
  assert.deepEqual(result.included.map((i) => i.guid), ["in1", "in2"]);
  assert.deepEqual(result.excluded.map((i) => i.guid), ["out"]);
  assert.equal(result.placeholderCount, 1);
  assert.equal(result.malformed.length, 0);
});

test("included events come out in start order", () => {
  const e = S.resolveEdition({ publicationDate: "2026-09-21" });
  const events = [
    { guid: "late", startTime: "2026-09-25T09:00:00" },
    { guid: "early", startTime: "2026-09-21T09:00:00" },
  ];
  assert.deepEqual(S.partition(events, e).included.map((i) => i.guid), ["early", "late"]);
});

test("a malformed start time is quarantined, never silently included", () => {
  const e = S.resolveEdition({ publicationDate: "2026-09-21" });
  const result = S.partition([{ guid: "bad", startTime: "not a date" }], e);
  assert.equal(result.included.length, 0);
  assert.equal(result.excluded.length, 0);
  assert.deepEqual(result.malformed.map((i) => i.guid), ["bad"]);
});

test("phase transitions at the deadline and at publication", () => {
  const e = S.resolveEdition({ publicationDate: "2026-09-21" });
  assert.equal(S.phaseAt("2026-09-15T11:59:59", e), "open");
  assert.equal(S.phaseAt("2026-09-15T12:00:00", e), "closed");
  assert.equal(S.phaseAt("2026-09-21T11:59:59", e), "closed");
  assert.equal(S.phaseAt("2026-09-21T12:00:00", e), "published");
});

test("hoursBetween measures wall-clock hours in both directions", () => {
  assert.equal(S.hoursBetween("2026-09-14T12:00:00", "2026-09-15T12:00:00"), 24);
  assert.equal(S.hoursBetween("2026-09-15T12:00:00", "2026-09-14T12:00:00"), -24);
});

test("a Sunday publication yields a valid single-day window", () => {
  const e = S.resolveEdition({ publicationDate: "2026-09-27" });
  assert.equal(e.id, "2026-09-21", "still anchored to that week's Monday");
  assert.equal(e.coverageStart, "2026-09-27T00:00:00");
  assert.equal(e.coverageEnd, "2026-09-27T23:59:59");
  assert.ok(e.coverageEnd > e.coverageStart);
  assert.equal(S.inWindow("2026-09-27T18:00:00", e), true);
  assert.equal(S.inWindow("2026-09-26T18:00:00", e), false);
});

test("the reported window over the full feed includes the 2026-09-28 talk", () => {
  // Reported from the live site: publication 2026-09-28 10:55 with a 2026-09-22
  // deadline appeared to find nothing. The window was right; the simulator had
  // been pointed at the pre-scoped variant. It now only ever reads a full feed.
  const full = [
    { guid: "wilks", startTime: "2026-09-28T12:15:00", title: "Transfer Treatment Effects",
      titleIsPlaceholder: false, titleSource: "enriched", speaker: "Annie Qu" },
    { guid: "colloq", startTime: "2026-09-29T16:30:00", title: "An ORFE Department Colloquia Talk",
      titleIsPlaceholder: true, titleSource: "fallback-template", speaker: "Ankur Moitra" },
    { guid: "earlier", startTime: "2026-09-21T12:15:00", titleIsPlaceholder: false },
  ];
  const edition = S.resolveEdition({
    publicationDate: "2026-09-28", publicationTime: "10:55", deadlineDate: "2026-09-22",
  });
  assert.equal(edition.coverageStart, "2026-09-28T00:00:00");
  assert.equal(edition.coverageEnd, "2026-10-04T23:59:59");
  const result = S.partition(full, edition);
  assert.deepEqual(result.included.map((i) => i.guid), ["wilks", "colloq"]);
  assert.equal(result.placeholderCount, 1, "only the colloquium awaits a title");
  const [qu] = result.included;
  assert.equal(qu.placeholder, false);
  assert.equal(qu.titleSource, "enriched");
});

test("defaultsForWeek snaps any date to that week's Monday", () => {
  const wed = S.defaultsForWeek("2026-09-30");
  assert.deepEqual(wed, {
    weekStart: "2026-09-28",
    publicationDate: "2026-09-28",
    publicationTime: "12:00",
    deadlineDate: "2026-09-22",
    deadlineTime: "12:00",
  });
  // The Monday itself, and that week's Sunday, give the same answer.
  assert.deepEqual(S.defaultsForWeek("2026-09-28"), wed);
  assert.deepEqual(S.defaultsForWeek("2026-10-04"), wed);
});

test("the week defaults reproduce the standard schedule", () => {
  const d = S.defaultsForWeek("2026-09-21");
  assert.equal(S.weekdayName(d.publicationDate), "Monday");
  assert.equal(S.weekdayName(d.deadlineDate), "Tuesday");
  const edition = S.resolveEdition(d);
  assert.equal(edition.id, "2026-09-21");
  assert.equal(edition.publicationAt, "2026-09-21T12:00:00");
  assert.equal(edition.deadlineAt, "2026-09-15T12:00:00");
  assert.equal(edition.shifted, false);
});

test("overriding publication models a shifted week without changing the edition", () => {
  // What the Advanced panel is for: Labor Day week publishes on the Tuesday.
  const d = S.defaultsForWeek("2026-09-07");
  const shifted = S.resolveEdition(Object.assign({}, d, { publicationDate: "2026-09-08" }));
  assert.equal(shifted.id, "2026-09-07", "still anchored to the week");
  assert.equal(shifted.shifted, true);
  assert.equal(shifted.coverageStart, "2026-09-08T00:00:00");
  assert.equal(shifted.coverageEnd, "2026-09-13T23:59:59");
  assert.equal(shifted.deadlineAt, "2026-09-01T12:00:00", "deadline is untouched");
});

test("defaultsForWeek rejects a malformed date rather than guessing", () => {
  assert.throws(() => S.defaultsForWeek("not-a-date"), /YYYY-MM-DD/);
});

test("a new publication date re-derives the deadline from its week", () => {
  // The rule the Advanced panel applies when the publication date is edited.
  assert.equal(S.defaultDeadlineDate("2026-09-28"), "2026-09-22");
  assert.equal(S.defaultDeadlineDate("2026-09-21"), "2026-09-15");
  // A publication shifted within its own week keeps that week's deadline, because
  // the deadline is anchored to the week, not to publication day.
  assert.equal(S.defaultDeadlineDate("2026-09-07"), "2026-09-01");
  assert.equal(S.defaultDeadlineDate("2026-09-08"), "2026-09-01");
});

test("every derived deadline lands on a Tuesday", () => {
  let monday = "2026-01-05";
  for (let i = 0; i < 52; i += 1) {
    assert.equal(S.weekdayName(S.defaultDeadlineDate(monday)), "Tuesday", monday);
    monday = S.addDays(monday, 7);
  }
});
