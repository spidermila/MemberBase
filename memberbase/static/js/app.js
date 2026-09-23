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
