/* Newsletter feed-view simulator: the edition arithmetic, ported from src/newsletter.py.
 *
 * Feed timestamps are naive local wall clock in America/New_York
 * ("2026-09-21T12:15:00", no suffix -- the schema pattern forbids one). Because every
 * timestamp shares that one format and one zone, lexicographic string order IS
 * chronological order, so the whole window filter is string comparison. That is
 * deliberate: it makes the simulator show identical results to a reader in Princeton,
 * California or Singapore, where Date parsing would silently shift the window by the
 * viewer's own UTC offset.
 *
 * Calendar arithmetic still needs real dates, so it goes through Date.UTC and the
 * getUTC* accessors -- never the local-time accessors.
 */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.NewsletterSim = api;
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  const DATE_RE = /^\d{4}-\d{2}-\d{2}$/;
  const TIME_RE = /^\d{2}:\d{2}(:\d{2})?$/;
  const STAMP_RE = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$/;
  const DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];

  function parseDate(text) {
    if (!DATE_RE.test(text || "")) throw new Error(`expected YYYY-MM-DD, got "${text}"`);
    const [y, m, d] = text.split("-").map(Number);
    const stamp = Date.UTC(y, m - 1, d);
    const back = new Date(stamp);
    if (back.getUTCFullYear() !== y || back.getUTCMonth() !== m - 1 || back.getUTCDate() !== d) {
      throw new Error(`not a real date: "${text}"`);
    }
    return stamp;
  }

  function formatDate(stamp) {
    return new Date(stamp).toISOString().slice(0, 10);
  }

  function addDays(text, n) {
    return formatDate(parseDate(text) + n * 86400000);
  }

  /** Monday = 0 ... Sunday = 6, matching Python's date.weekday(). */
  function weekdayOf(text) {
    return (new Date(parseDate(text)).getUTCDay() + 6) % 7;
  }

  function weekdayName(text) {
    return DAY_NAMES[weekdayOf(text)];
  }

  function weekStartFor(text) {
    return addDays(text, -weekdayOf(text));
  }

  function normalizeTime(text, fallback) {
    const value = (text || "").trim() || fallback;
    if (!TIME_RE.test(value)) throw new Error(`expected HH:MM or HH:MM:SS, got "${text}"`);
    return value.length === 5 ? `${value}:00` : value;
  }

  function stamp(dateText, timeText) {
    return `${dateText}T${timeText}`;
  }

  /** The deadline the standard schedule implies: the Tuesday before publication. */
  function defaultDeadlineDate(publicationDate) {
    return addDays(weekStartFor(publicationDate), -6);
  }

  /** The control values a chosen week implies, before any manual override.
   *
   * Publication is that week's Monday at noon and the deadline the Tuesday six days
   * earlier, matching the standard schedule. A week that publishes on a different
   * day -- Labor Day week -- is expressed by overriding the publication date.
   */
  function defaultsForWeek(dateText) {
    const weekStart = weekStartFor(dateText);
    return {
      weekStart: weekStart,
      publicationDate: weekStart,
      publicationTime: "12:00",
      deadlineDate: defaultDeadlineDate(weekStart),
      deadlineTime: "12:00",
    };
  }

  /** Resolve one edition. Mirrors newsletter.build_edition's anchor model. */
  function resolveEdition(input) {
    const publicationDate = input.publicationDate;
    parseDate(publicationDate);
    const publicationTime = normalizeTime(input.publicationTime, "12:00:00");
    const weekStart = weekStartFor(publicationDate);

    const deadlineDate = input.deadlineDate || defaultDeadlineDate(publicationDate);
    parseDate(deadlineDate);
    const deadlineTime = normalizeTime(input.deadlineTime, "12:00:00");

    // Coverage start follows a publication shift; coverage end stays pinned to the
    // week. That mixed anchoring is what makes Labor Day week come out right.
    const coverageStart = stamp(publicationDate, "00:00:00");
    // publicationDate always lies within its own week, so coverageEnd >= coverageStart
    // by construction; a Sunday publication simply yields a one-day window.
    const coverageEnd = stamp(addDays(weekStart, 6), "23:59:59");

    return {
      id: weekStart,
      weekStart: weekStart,
      publicationDate: publicationDate,
      publicationAt: stamp(publicationDate, publicationTime),
      publicationWeekday: weekdayName(publicationDate),
      deadlineAt: stamp(deadlineDate, deadlineTime),
      deadlineWeekday: weekdayName(deadlineDate),
      coverageStart: coverageStart,
      coverageEnd: coverageEnd,
      shifted: publicationDate !== weekStart,
    };
  }

  function isValidStamp(text) {
    return STAMP_RE.test(text || "");
  }

  /** Inclusive at both ends, by string comparison. */
  function inWindow(startTime, edition) {
    if (!isValidStamp(startTime)) return false;
    return startTime >= edition.coverageStart && startTime <= edition.coverageEnd;
  }

  function unescapeIcs(value) {
    return String(value == null ? "" : value).replace(/\\,/g, ",").replace(/\\;/g, ";");
  }

  /** Is this title synthesized? Prefer the feed's own flag; infer only if absent. */
  function placeholderState(event) {
    if (typeof event.titleIsPlaceholder === "boolean") {
      return { placeholder: event.titleIsPlaceholder, inferred: false, source: event.titleSource || null };
    }
    if (typeof event.titleSource === "string") {
      return {
        placeholder: event.titleSource.indexOf("fallback-") === 0,
        inferred: false,
        source: event.titleSource,
      };
    }
    // Older feed with no provenance: fall back to the shapes fill_title_fallback emits.
    const title = unescapeIcs(event.title).trim();
    const speaker = unescapeIcs(event.speaker).trim();
    const looksSynthesized =
      (title !== "" && title === speaker) || /^(an?|the)\s+.+\s+talk$/i.test(title);
    return { placeholder: looksSynthesized, inferred: true, source: null };
  }

  function phaseAt(nowStamp, edition) {
    if (!isValidStamp(nowStamp)) return null;
    if (nowStamp >= edition.publicationAt) return "published";
    if (nowStamp >= edition.deadlineAt) return "closed";
    return "open";
  }

  /** Whole numbers of hours between two naive stamps, positive when b is later. */
  function hoursBetween(a, b) {
    if (!isValidStamp(a) || !isValidStamp(b)) return null;
    const toMs = (s) => Date.UTC(
      +s.slice(0, 4), +s.slice(5, 7) - 1, +s.slice(8, 10),
      +s.slice(11, 13), +s.slice(14, 16), +s.slice(17, 19)
    );
    return (toMs(b) - toMs(a)) / 3600000;
  }

  function decorate(event) {
    const state = placeholderState(event);
    return {
      raw: event,
      guid: event.guid || "",
      startTime: event.startTime || "",
      endTime: event.endTime || "",
      title: unescapeIcs(event.title),
      speaker: unescapeIcs(event.speaker),
      series: unescapeIcs(event.series),
      location: formatLocation(event.location),
      urlRef: event.urlRef || "",
      titleSource: state.source,
      placeholder: state.placeholder,
      inferred: state.inferred,
      malformedStart: !isValidStamp(event.startTime),
    };
  }

  function formatLocation(location) {
    if (!location || typeof location !== "object") return "";
    const name = (location.name || "").trim();
    const detail = (location.detail || "").trim();
    if (name && detail) return `${name} ${detail}`;
    return name || detail || "";
  }

  /** Split a feed into what WordPress would ingest for this edition, and what it would not. */
  function partition(events, edition) {
    const included = [];
    const excluded = [];
    const malformed = [];
    (events || []).forEach(function (event) {
      const item = decorate(event);
      if (item.malformedStart) {
        malformed.push(item);
      } else if (inWindow(item.startTime, edition)) {
        included.push(item);
      } else {
        excluded.push(item);
      }
    });
    const byStart = (a, b) => (a.startTime < b.startTime ? -1 : a.startTime > b.startTime ? 1 : 0);
    included.sort(byStart);
    excluded.sort(byStart);
    return {
      included: included,
      excluded: excluded,
      malformed: malformed,
      placeholderCount: included.filter((e) => e.placeholder).length,
      inferredCount: included.filter((e) => e.inferred).length,
    };
  }

  return {
    parseDate: parseDate,
    formatDate: formatDate,
    addDays: addDays,
    weekdayOf: weekdayOf,
    weekdayName: weekdayName,
    weekStartFor: weekStartFor,
    normalizeTime: normalizeTime,
    defaultDeadlineDate: defaultDeadlineDate,
    defaultsForWeek: defaultsForWeek,
    resolveEdition: resolveEdition,
    inWindow: inWindow,
    placeholderState: placeholderState,
    phaseAt: phaseAt,
    hoursBetween: hoursBetween,
    unescapeIcs: unescapeIcs,
    formatLocation: formatLocation,
    decorate: decorate,
    partition: partition,
  };
});

/* ---------------------------------------------------------------------------
 * UI controller. No-ops unless a #feed-simulator section is present, so the
 * same file can be loaded by any page and required by the node tests.
 * ------------------------------------------------------------------------ */
(function () {
  "use strict";
  if (typeof document === "undefined") return;

  const S = (typeof self !== "undefined" ? self : this).NewsletterSim;
  const ZONE = "America/New_York";

  function easternToday() {
    return new Intl.DateTimeFormat("en-CA", {
      timeZone: ZONE, year: "numeric", month: "2-digit", day: "2-digit",
    }).format(new Date());
  }

  function easternNow() {
    const parts = new Intl.DateTimeFormat("en-CA", {
      timeZone: ZONE, year: "numeric", month: "2-digit", day: "2-digit",
      hour: "2-digit", minute: "2-digit", hour12: false,
    }).formatToParts(new Date()).reduce((acc, p) => (acc[p.type] = p.value, acc), {});
    const hour = parts.hour === "24" ? "00" : parts.hour;
    return `${parts.year}-${parts.month}-${parts.day}T${hour}:${parts.minute}`;
  }

  /** The next Monday on or after today, in Eastern. */
  function nextMonday(from) {
    const day = S.weekdayOf(from);
    return day === 0 ? from : S.addDays(from, 7 - day);
  }

  function prettyStamp(stamp) {
    if (!stamp) return "";
    const [date, time] = stamp.split("T");
    return `${S.weekdayName(date).slice(0, 3)} ${date} ${time.slice(0, 5)} ET`;
  }

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text != null) node.textContent = text;
    return node;
  }

  function pill(text, tone) {
    const node = el("span", "nfs-pill", text);
    node.setAttribute("data-tone", tone);
    return node;
  }

  function boot() {
    const root = document.getElementById("feed-simulator");
    if (!root || !S) return;

    const $ = (id) => root.querySelector("#" + id);
    // The production page has a single source and no selector; the dev page
    // offers several. Both resolve through feedUrl().
    const sourceEl = $("nfs-source");
    const weekEl = $("nfs-week");
    const advStateEl = $("nfs-adv-state");
    const fixedFeed = root.getAttribute("data-feed") || "./events.json";
    const pubDateEl = $("nfs-pubdate");
    const pubTimeEl = $("nfs-pubtime");
    const deadlineDateEl = $("nfs-deadlinedate");
    const deadlineTimeEl = $("nfs-deadlinetime");
    const nowEl = $("nfs-now");
    const statusEl = $("nfs-status");
    const resultsEl = $("nfs-results");
    const summaryEl = $("nfs-summary");
    const detailsEl = $("nfs-details-list");
    const captionEl = $("nfs-caption");
    const rowsEl = $("nfs-rows");
    const excludedWrapEl = $("nfs-excluded-wrap");
    const excludedCountEl = $("nfs-excluded-count");
    const excludedRowsEl = $("nfs-excluded-rows");
    const jsonEl = $("nfs-json");

    let feed = null;
    // Once any advanced field is edited by hand, stop re-deriving it from the week.
    let customised = false;

    function setStatus(text, tone) {
      statusEl.textContent = text;
      if (tone) statusEl.setAttribute("data-tone", tone);
      else statusEl.removeAttribute("data-tone");
    }

    /** Fill every control from the standard schedule for the chosen week. */
    function applyWeek(dateText) {
      const d = S.defaultsForWeek(dateText);
      weekEl.value = d.weekStart;
      pubDateEl.value = d.publicationDate;
      pubTimeEl.value = d.publicationTime;
      deadlineDateEl.value = d.deadlineDate;
      deadlineTimeEl.value = d.deadlineTime;
      customised = false;
      labelWeek();
      markCustomised();
    }

    /** True when the controls no longer match what the chosen week implies. */
    function divergesFromWeek() {
      if (!weekEl.value) return false;
      try {
        const d = S.defaultsForWeek(weekEl.value);
        return (
          pubDateEl.value !== d.publicationDate ||
          S.normalizeTime(pubTimeEl.value, "12:00:00") !== S.normalizeTime(d.publicationTime, "12:00:00") ||
          deadlineDateEl.value !== d.deadlineDate ||
          S.normalizeTime(deadlineTimeEl.value, "12:00:00") !== S.normalizeTime(d.deadlineTime, "12:00:00")
        );
      } catch (err) {
        return true;
      }
    }

    /** Label the Advanced panel when it holds overrides. Never opens it: the panel
     *  stays collapsed until the reader asks for it. */
    function markCustomised() {
      const diverged = customised && divergesFromWeek();
      if (advStateEl) advStateEl.textContent = diverged ? "· customised" : "";
    }

    function labelWeek() {
      const target = $("nfs-weekday");
      if (!target) return;
      try {
        target.textContent = weekEl.value
          ? "Week beginning " + S.weekdayName(weekEl.value) + " " + S.weekStartFor(weekEl.value)
          : " ";
      } catch (err) {
        target.textContent = " ";
      }
    }

    const QUERY_KEYS = {
      pub: pubDateEl, pubtime: pubTimeEl,
      deadline: deadlineDateEl, deadlinetime: deadlineTimeEl, now: nowEl,
    };

    /** Seed the controls from ?pub=&deadline=... so a view can be linked to. */
    function applyQueryState() {
      const params = new URLSearchParams(window.location.search);
      Object.keys(QUERY_KEYS).forEach(function (key) {
        const value = params.get(key);
        if (value == null) return;
        QUERY_KEYS[key].value = value;
      });
      const seeded = Object.keys(QUERY_KEYS).some((k) => params.get(k) != null);
      const feed = params.get("feed");
      if (feed && sourceEl) {
        const match = Array.prototype.find.call(
          sourceEl.options, (o) => o.value === feed || o.value.endsWith("/" + feed)
        );
        if (match) sourceEl.value = match.value;
      }
      return seeded;
    }

    /** Reflect the current view in the address bar so it can be shared. */
    function syncQueryState() {
      if (!window.history || !window.history.replaceState) return;
      const params = new URLSearchParams();
      params.set("pub", pubDateEl.value);
      params.set("pubtime", pubTimeEl.value);
      params.set("deadline", deadlineDateEl.value);
      params.set("deadlinetime", deadlineTimeEl.value);
      if (nowEl.value) params.set("now", nowEl.value);
      if (sourceEl && sourceEl.selectedIndex > 0) {
        params.set("feed", sourceEl.value.replace(/^\.\//, ""));
      }
      window.history.replaceState(null, "", "?" + params.toString() + "#feed-simulator");
    }

    function labelWeekday(inputEl, targetId) {
      const target = $(targetId);
      if (!target) return;
      try {
        target.textContent = inputEl.value ? S.weekdayName(inputEl.value) : " ";
      } catch (err) {
        target.textContent = " ";
      }
    }

    function feedUrl() {
      return sourceEl ? sourceEl.value : fixedFeed;
    }

    async function loadFeed() {
      const url = feedUrl();
      feed = null;
      setStatus("Loading " + url + "…");
      try {
        const response = await fetch(url, { cache: "no-store" });
        if (!response.ok) throw new Error("HTTP " + response.status);
        const data = await response.json();
        if (!Array.isArray(data)) throw new Error("expected a JSON array of events");
        feed = data;
        setStatus("");
      } catch (err) {
        resultsEl.hidden = true;
        setStatus("Could not load " + url + " (" + err.message + ").", "error");
      }
      render();
    }

    function render() {
      if (!feed) return;

      let edition;
      try {
        edition = S.resolveEdition({
          publicationDate: pubDateEl.value,
          publicationTime: pubTimeEl.value,
          deadlineDate: deadlineDateEl.value,
          deadlineTime: deadlineTimeEl.value,
        });
      } catch (err) {
        resultsEl.hidden = true;
        setStatus(err.message, "error");
        return;
      }

      const result = S.partition(feed, edition);
      const nowStamp = nowEl.value ? nowEl.value + ":00" : null;
      const phase = nowStamp ? S.phaseAt(nowStamp, edition) : null;
      const hoursToDeadline = nowStamp ? S.hoursBetween(nowStamp, edition.deadlineAt) : null;

      renderSummary(edition, result, phase, hoursToDeadline);
      renderRows(result);
      renderExcluded(result);
      jsonEl.textContent = JSON.stringify(
        result.included.map((item) =>
          Object.assign({}, item.raw, { newsletterEdition: edition.id })
        ),
        null, 2
      );

      resultsEl.hidden = false;
      syncQueryState();
      const notes = [];
      if (result.malformed.length) {
        notes.push(
          result.malformed.length + " event(s) have an unreadable start time and were " +
          "left out of both lists rather than guessed at"
        );
      }
      if (result.inferredCount) {
        notes.push(
          "this feed carries no titleSource field, so placeholder status was inferred " +
          "from the title text"
        );
      }
      setStatus(notes.length ? "Note: " + notes.join("; ") + "." : "", notes.length ? "warn" : null);
    }

    function appendEntry(list, term, value, extra) {
      const wrap = document.createElement("div");
      wrap.appendChild(el("dt", null, term));
      const dd = el("dd", null, value);
      if (extra) { dd.appendChild(document.createTextNode(" ")); dd.appendChild(extra); }
      wrap.appendChild(dd);
      list.appendChild(wrap);
    }

    function renderSummary(edition, result, phase, hoursToDeadline) {
      // Headline: what the editor actually wants to know.
      summaryEl.replaceChildren();
      appendEntry(summaryEl, "Events the editors receive", String(result.included.length));
      appendEntry(summaryEl, "Awaiting a real title", String(result.placeholderCount),
        result.included.length === 0
          ? pill("no events", "muted")
          : result.placeholderCount ? pill("needs chasing", "bad") : pill("all set", "good"));
      if (phase) {
        const labels = { open: "Submissions open", closed: "Deadline passed", published: "Published" };
        appendEntry(summaryEl, "Status", labels[phase], pill(phase, phase));
      }

      // Detail: the dates the simulator worked out, folded away by default.
      if (!detailsEl) return;
      detailsEl.replaceChildren();
      appendEntry(detailsEl, "Edition", edition.id,
        edition.shifted ? pill("shifted", "closed") : null);
      appendEntry(detailsEl, "Publishes", prettyStamp(edition.publicationAt));
      appendEntry(detailsEl, "Deadline", prettyStamp(edition.deadlineAt),
        hoursToDeadline == null ? null
          : pill(hoursToDeadline >= 0
              ? "in " + Math.round(hoursToDeadline) + "h"
              : Math.abs(Math.round(hoursToDeadline)) + "h ago",
            hoursToDeadline >= 0 ? "good" : "bad"));
      appendEntry(detailsEl, "Coverage window",
        prettyStamp(edition.coverageStart) + " → " + prettyStamp(edition.coverageEnd));
    }

    function renderRows(result) {
      rowsEl.replaceChildren();
      captionEl.textContent = result.included.length
        ? "What the editorial system ingests for this edition, in running order."
        : "";

      if (!result.included.length) {
        const row = document.createElement("tr");
        const cell = el("td", "nfs-empty",
          "No events in the feed start inside this coverage window. On a normal " +
          "teaching week that usually means the dates are wrong rather than the " +
          "calendar being empty.");
        cell.colSpan = 5;
        row.appendChild(cell);
        rowsEl.appendChild(row);
        return;
      }

      result.included.forEach((item) => {
        const row = document.createElement("tr");
        row.setAttribute("data-placeholder", String(item.placeholder));

        row.appendChild(el("td", "nfs-when", prettyStamp(item.startTime)));

        const titleCell = document.createElement("td");
        const title = el("span", "nfs-title", item.title || "(no title)");
        titleCell.appendChild(title);
        if (item.placeholder) {
          titleCell.appendChild(document.createTextNode(" "));
          titleCell.appendChild(pill(item.inferred ? "placeholder?" : "placeholder", "bad"));
          titleCell.appendChild(el("span", "nfs-note",
            item.inferred
              ? "Inferred from the title text — this feed predates titleSource."
              : "Synthesised by the pipeline (" + item.titleSource +
                "); the speaker has not supplied a title."));
        } else if (item.titleSource) {
          titleCell.appendChild(document.createTextNode(" "));
          titleCell.appendChild(pill(item.titleSource, "muted"));
        }
        row.appendChild(titleCell);

        row.appendChild(el("td", null, item.series));
        row.appendChild(el("td", null, item.speaker));
        row.appendChild(el("td", null, item.location));
        rowsEl.appendChild(row);
      });
    }

    function renderExcluded(result) {
      excludedRowsEl.replaceChildren();
      const rows = result.excluded.concat(result.malformed);
      excludedWrapEl.hidden = rows.length === 0;
      excludedCountEl.textContent =
        rows.length + " event" + (rows.length === 1 ? "" : "s") + " in the feed but outside this edition";

      rows.forEach((item) => {
        const row = document.createElement("tr");
        row.appendChild(el("td", "nfs-when", item.malformedStart ? item.startTime || "(missing)" : prettyStamp(item.startTime)));
        row.appendChild(el("td", null, item.title || "(no title)"));
        row.appendChild(el("td", null, item.series));
        row.appendChild(el("td", null,
          item.malformedStart ? "Unreadable start time" : "Starts outside the coverage window"));
        excludedRowsEl.appendChild(row);
      });
    }

    // -- wiring ------------------------------------------------------------

    const today = easternToday();
    applyWeek(nextMonday(today));
    nowEl.value = easternNow();
    if (applyQueryState()) {
      // A shared link carries exact dates; keep them and show the week they fall in.
      weekEl.value = S.weekStartFor(pubDateEl.value || nextMonday(today));
      customised = true;
      labelWeek();
      markCustomised();
    }
    labelWeekday(pubDateEl, "nfs-pubday");
    labelWeekday(deadlineDateEl, "nfs-deadlineday");

    weekEl.addEventListener("change", () => {
      if (!weekEl.value) return;
      applyWeek(weekEl.value);
      labelWeekday(pubDateEl, "nfs-pubday");
      labelWeekday(deadlineDateEl, "nfs-deadlineday");
      render();
    });

    // A new publication date re-derives the deadline from it, the same rule the week
    // selector uses. The deadline can then be moved on its own and will stick until
    // the publication date or the week changes again.
    pubDateEl.addEventListener("change", () => {
      customised = true;
      if (pubDateEl.value) {
        try {
          deadlineDateEl.value = S.defaultDeadlineDate(pubDateEl.value);
        } catch (err) { /* invalid date; render() reports it */ }
      }
      afterAdvancedEdit();
    });

    [pubTimeEl, deadlineDateEl, deadlineTimeEl].forEach((node) =>
      node.addEventListener("change", () => {
        customised = true;
        afterAdvancedEdit();
      })
    );

    function afterAdvancedEdit() {
      labelWeekday(pubDateEl, "nfs-pubday");
      labelWeekday(deadlineDateEl, "nfs-deadlineday");
      markCustomised();
      render();
    }
    nowEl.addEventListener("change", render);
    if (sourceEl) sourceEl.addEventListener("change", loadFeed);

    const resetBtn = $("nfs-reset");
    if (resetBtn) resetBtn.addEventListener("click", () => {
      applyWeek(nextMonday(easternToday()));
      nowEl.value = easternNow();
      labelWeekday(pubDateEl, "nfs-pubday");
      labelWeekday(deadlineDateEl, "nfs-deadlineday");
      render();
    });

    loadFeed();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
