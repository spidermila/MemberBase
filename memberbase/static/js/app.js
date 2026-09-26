// Confirmation for dangerous buttons: <button data-confirm="Opravdu…?">
document.addEventListener("submit", function (event) {
  var button = event.submitter;
  var message = (button && button.dataset.confirm) || event.target.dataset.confirm;
  if (message && !window.confirm(message)) {
    event.preventDefault();
  }
});

// Batch role toolbar on the member list.
(function () {
  var all = document.getElementById("selectAll");
  var toolbar = document.getElementById("batchToolbar");
  if (!all || !toolbar) {
    return;
  }
  var boxes = Array.prototype.slice.call(document.querySelectorAll("input[name=member_ids]"));
  var count = document.getElementById("selCount");
  function update() {
    var n = boxes.filter(function (b) { return b.checked; }).length;
    count.textContent = n + " vybráno";
    toolbar.classList.toggle("d-none", n === 0);
    toolbar.classList.toggle("d-flex", n > 0);
  }
  all.addEventListener("change", function () {
    boxes.forEach(function (b) { b.checked = all.checked; });
    update();
  });
  boxes.forEach(function (b) { b.addEventListener("change", update); });
  document.getElementById("clearSel").addEventListener("click", function () {
    all.checked = false;
    boxes.forEach(function (b) { b.checked = false; });
    update();
  });
})();

// Grant form: person or Místní skupina as the recipient.
(function () {
  var kinds = document.querySelectorAll("input[name=grantee_kind]");
  if (!kinds.length) {
    return;
  }
  function update() {
    var unit = document.getElementById("granteeKindUnit").checked;
    document.getElementById("granteePerson").classList.toggle("d-none", unit);
    document.getElementById("granteeUnit").classList.toggle("d-none", !unit);
  }
  kinds.forEach(function (k) { k.addEventListener("change", update); });
  update();
})();

// Access request form: one more name row.
(function () {
  var add = document.getElementById("addAccessRow");
  if (!add) {
    return;
  }
  add.addEventListener("click", function () {
    var rows = document.querySelectorAll("#accessRows .access-row");
    var row = rows[rows.length - 1].cloneNode(true);
    row.querySelector("input").value = "";
    row.querySelector("select").selectedIndex = 0;
    document.getElementById("accessRows").appendChild(row);
    row.querySelector("input").focus();
  });
})();

// Filter a long <select> by typing, ignoring case and Czech accents.
(function () {
  function plain(text) {
    // NFD splits "č" into "c" plus an invisible combining mark (U+0300–U+036F).
    return text.normalize("NFD").replace(/[\u0300-\u036f]/g, "").toLowerCase();
  }
  document.querySelectorAll("input.select-filter").forEach(function (input) {
    var select = document.getElementById(input.dataset.filter);
    // Enter would submit the decision form, i.e. approve.
    input.addEventListener("keydown", function (event) {
      if (event.key === "Enter") {
        event.preventDefault();
      }
    });
    input.addEventListener("input", function () {
      var words = plain(input.value).split(/\s+/).filter(Boolean);
      Array.prototype.forEach.call(select.options, function (option) {
        var text = plain(option.text);
        option.hidden = option.value !== "" && !words.every(function (w) { return text.indexOf(w) !== -1; });
      });
    });
  });
})();

// Stacked tables on phones label each cell with its column header (main.css).
document.querySelectorAll("table.table-stack").forEach(function (table) {
  var labels = Array.prototype.map.call(table.tHead.rows[0].cells, function (th) {
    return th.textContent.trim();
  });
  Array.prototype.forEach.call(table.tBodies[0].rows, function (row) {
    Array.prototype.forEach.call(row.cells, function (cell, i) {
      if (labels[i] && cell.colSpan === 1) {
        cell.dataset.label = labels[i];
      }
    });
  });
});

// Sort a table by clicking a header: <th data-sort>. A cell's data-sort
// value, if any, sorts instead of its text; empty cells go last. The last
// sort is kept for this page and tab, so it survives the redirect after a
// row action. aria-sort="ascending" on a header marks the initial order.
(function () {
  var headers = document.querySelectorAll("th[data-sort]");
  if (!headers.length) {
    return;
  }
  var collator = new Intl.Collator("cs", { numeric: true, sensitivity: "base" });
  var storageKey = "sort:" + location.pathname;
  function sort(th, dir) {
    var table = th.closest("table");
    var body = table.tBodies[0];
    var index = th.cellIndex;
    table.querySelectorAll("th[aria-sort]").forEach(function (other) {
      other.removeAttribute("aria-sort");
    });
    th.setAttribute("aria-sort", dir === 1 ? "ascending" : "descending");
    Array.prototype.map.call(body.rows, function (row) {
      var cell = row.cells[index];
      var key = !cell ? "" : cell.dataset.sort !== undefined ? cell.dataset.sort : cell.textContent;
      return { row: row, key: key.trim() };
    })
      .sort(function (a, b) {
        return (a.key === "") - (b.key === "") || dir * collator.compare(a.key, b.key);
      })
      .forEach(function (item) { body.appendChild(item.row); });
  }
  headers.forEach(function (th, i) {
    var button = document.createElement("button");
    button.type = "button";
    button.className = "sort-button";
    while (th.firstChild) {
      button.appendChild(th.firstChild);
    }
    th.appendChild(button);
    button.addEventListener("click", function () {
      var dir = th.getAttribute("aria-sort") === "ascending" ? -1 : 1;
      sort(th, dir);
      try {
        sessionStorage.setItem(storageKey, i + ":" + dir);
      } catch (e) {
        // Storage blocked: the sort just isn't remembered.
      }
    });
  });
  var saved = null;
  try {
    saved = sessionStorage.getItem(storageKey);
  } catch (e) {
    // Storage blocked: start from the server's order.
  }
  var parts = (saved || "").split(":");
  var th = headers[parts[0]];
  // A column hidden at this screen width would sort the list invisibly.
  if (th && th.offsetParent !== null) {
    sort(th, Number(parts[1]) === -1 ? -1 : 1);
  } else {
    // Re-sort the server's order here so both directions use the same collation.
    th = document.querySelector("th[data-sort][aria-sort=ascending]");
    if (th) {
      sort(th, 1);
    }
  }
})();

// Date fields: a click anywhere opens the calendar, not only on its icon.
document.addEventListener("click", function (event) {
  var input = event.target;
  if (input instanceof HTMLInputElement && input.type === "date" && input.showPicker) {
    try {
      input.showPicker();
    } catch (e) {
      // Already open, or not allowed here: the browser's own behaviour stays.
    }
  }
});
