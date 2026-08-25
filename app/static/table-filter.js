(function () {
  var input = document.getElementById("searchInput");
  var table = document.getElementById("userTable");
  var rowCount = document.getElementById("rowCount");
  if (!input || !table) return;

  var rows = Array.prototype.slice.call(table.querySelectorAll("tbody tr"));

  function updateCount() {
    var visible = rows.filter(function (r) { return r.style.display !== "none"; }).length;
    rowCount.textContent = visible + " / " + rows.length + " 人";
  }

  input.addEventListener("input", function () {
    var q = input.value.trim().toLowerCase();
    rows.forEach(function (r) {
      var hit = r.getAttribute("data-search").toLowerCase().indexOf(q) !== -1;
      r.style.display = hit ? "" : "none";
    });
    updateCount();
  });

  updateCount();
})();
