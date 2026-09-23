// Follow the system light/dark preference (Bootstrap 5.3 colour modes).
(function () {
  var query = window.matchMedia("(prefers-color-scheme: dark)");
  function apply() {
    document.documentElement.setAttribute("data-bs-theme", query.matches ? "dark" : "light");
  }
  apply();
  query.addEventListener("change", apply);
})();
