(function () {
  // 页面本身不等 QuickSight（见 app/api/users.py user_list 的说明），
  // 加载完之后单独发一个后台请求去 /users/subscription-status 补上这一列，
  // 查询中的格子保持"查询中…"占位，不阻塞首屏。
  var cells = document.querySelectorAll("td.qs-status[data-user-id]");
  if (!cells.length) return;

  fetch("/users/subscription-status", { credentials: "same-origin" })
    .then(function (resp) {
      if (!resp.ok) throw new Error("HTTP " + resp.status);
      return resp.json();
    })
    .then(function (statusMap) {
      cells.forEach(function (cell) {
        var userId = cell.getAttribute("data-user-id");
        var status = statusMap[userId];
        cell.textContent = status || "未知";
      });
    })
    .catch(function () {
      // 查询失败（网络问题/接口报错），别让格子永远转圈——如实显示查不到，
      // 不影响页面其它部分已经可用。
      cells.forEach(function (cell) {
        cell.textContent = "查询失败";
      });
    });
})();
