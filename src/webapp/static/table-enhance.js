/* Progressive table enhancement: click-to-sort headers + live text filter.
 * No dependencies. Idempotent, and re-runs after htmx swaps.
 *
 * Opt in per table with class="sortable". A column is treated as numeric when
 * its body cells carry the `num` class (matches the existing right-aligned
 * money/quantity columns). Add data-no-sort to a <th> to keep it static.
 *
 * A table with more than one <tbody> is treated as grouped: each <tbody> is one
 * unit keyed off its first row, so grouped detail rows (e.g. an expandable
 * per-lot breakdown) always sort and filter together with their parent record.
 *
 * Live filter: any <input data-filter-for="TABLE_ID"> filters that table.
 * Optional data-filter-col="N" restricts matching to column N (0-based);
 * omit it to match the whole unit. data-filter-count="EL_ID" gets the live count.
 */
(function () {
  "use strict";

  function parseCzechNumber(text) {
    // "1 234,56", "-7 489,46", "12,5 %" -> Number, or null when not numeric.
    var cleaned = text
      .replace(/[\s  ]/g, "")
      .replace(/−/g, "-") // unicode minus
      .replace(",", ".")
      .replace(/[^0-9.\-]/g, "");
    if (cleaned === "" || cleaned === "-" || cleaned === "." || cleaned === "-.") {
      return null;
    }
    var n = parseFloat(cleaned);
    return isNaN(n) ? null : n;
  }

  // Resolve a table into ordered units and the element they get re-appended to.
  // Each unit is { node, keyRow }: `node` is what we move/hide, `keyRow` is the
  // <tr> whose cells drive sorting and column-based filtering. A table with more
  // than one <tbody> is treated as grouped (one unit per <tbody>), so detail
  // rows always travel with their parent record.
  function tableUnits(table) {
    var bodies = table.tBodies;
    if (bodies.length > 1) {
      var groups = [];
      for (var i = 0; i < bodies.length; i++) {
        if (bodies[i].rows.length) {
          groups.push({ node: bodies[i], keyRow: bodies[i].rows[0] });
        }
      }
      return { container: table, units: groups };
    }
    var body = bodies[0];
    var rows = [];
    if (body) {
      for (var r = 0; r < body.rows.length; r++) {
        rows.push({ node: body.rows[r], keyRow: body.rows[r] });
      }
    }
    return { container: body || table, units: rows };
  }

  function columnIsNumeric(units, idx) {
    if (!units.length) return false;
    var cell = units[0].keyRow.cells[idx];
    return cell ? cell.classList.contains("num") : false;
  }

  function cellText(row, idx) {
    var cell = row.cells[idx];
    return cell ? cell.textContent.trim() : "";
  }

  function sortBy(table, idx, th) {
    var group = tableUnits(table);
    if (!group.units.length) return;
    var numeric = columnIsNumeric(group.units, idx);
    var dir = th.getAttribute("aria-sort") === "ascending" ? "descending" : "ascending";
    var asc = dir === "ascending";

    var headCells = table.tHead.rows[0].cells;
    for (var h = 0; h < headCells.length; h++) {
      headCells[h].removeAttribute("aria-sort");
      var ind = headCells[h].querySelector(".sort-ind");
      if (ind) ind.textContent = "";
    }
    th.setAttribute("aria-sort", dir);
    var thisInd = th.querySelector(".sort-ind");
    if (thisInd) thisInd.textContent = asc ? "▲" : "▼";

    var units = group.units.slice();
    units.sort(function (a, b) {
      var av = cellText(a.keyRow, idx);
      var bv = cellText(b.keyRow, idx);
      if (numeric) {
        var an = parseCzechNumber(av);
        var bn = parseCzechNumber(bv);
        // Missing values (–) always sink to the bottom, both directions.
        if (an === null && bn === null) return 0;
        if (an === null) return 1;
        if (bn === null) return -1;
        return asc ? an - bn : bn - an;
      }
      var cmp = av.localeCompare(bv, "cs", { numeric: true, sensitivity: "base" });
      return asc ? cmp : -cmp;
    });
    var frag = document.createDocumentFragment();
    units.forEach(function (u) {
      frag.appendChild(u.node);
    });
    group.container.appendChild(frag);
  }

  function enhanceSortable(table) {
    if (table.__sortableReady || !table.tHead) return;
    table.__sortableReady = true;
    var headRow = table.tHead.rows[0];
    if (!headRow) return;
    Array.prototype.forEach.call(headRow.cells, function (th, idx) {
      if (th.hasAttribute("data-no-sort")) return;
      if (!th.querySelector(".sort-ind")) {
        var ind = document.createElement("span");
        ind.className = "sort-ind";
        ind.setAttribute("aria-hidden", "true");
        th.appendChild(ind);
      }
      th.setAttribute("role", "button");
      th.setAttribute("tabindex", "0");
      th.addEventListener("click", function () {
        sortBy(table, idx, th);
      });
      th.addEventListener("keydown", function (e) {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          sortBy(table, idx, th);
        }
      });
    });
  }

  function wireFilter(input) {
    if (input.__filterReady) return;
    input.__filterReady = true;
    var table = document.getElementById(input.getAttribute("data-filter-for"));
    if (!table || !table.tBodies[0]) return;
    var colAttr = input.getAttribute("data-filter-col");
    var col = colAttr === null ? null : parseInt(colAttr, 10);
    var counter = input.getAttribute("data-filter-count")
      ? document.getElementById(input.getAttribute("data-filter-count"))
      : null;

    function apply() {
      var q = input.value.trim().toLowerCase();
      var units = tableUnits(table).units;
      var shown = 0;
      units.forEach(function (u) {
        var haystack = col === null ? u.node.textContent : cellText(u.keyRow, col);
        var match = q === "" || haystack.toLowerCase().indexOf(q) !== -1;
        u.node.classList.toggle("filtered-out", !match);
        if (match) shown++;
      });
      if (counter) counter.textContent = shown;
    }
    input.addEventListener("input", apply);
    apply();
  }

  function enhanceAll(root) {
    var scope = root && root.querySelectorAll ? root : document;
    scope.querySelectorAll("table.sortable").forEach(enhanceSortable);
    // Filter inputs reference tables by id, so always resolve from document.
    document.querySelectorAll("[data-filter-for]").forEach(wireFilter);
  }

  document.addEventListener("DOMContentLoaded", function () {
    enhanceAll(document);
  });
  // htmx swaps in the dashboard valuation / live portfolio fragments.
  document.body.addEventListener("htmx:afterSwap", function (e) {
    enhanceAll(e.target || document);
  });
})();
