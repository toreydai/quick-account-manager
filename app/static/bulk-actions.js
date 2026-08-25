(function () {
  var table = document.getElementById("userTable");
  var bulkCount = document.getElementById("bulkCount");
  var selectAll = document.getElementById("selectAll");
  if (!table || !bulkCount) return;

  function rowCheckboxes() {
    return Array.prototype.slice.call(table.querySelectorAll(".row-select"));
  }

  function selectedIds() {
    return rowCheckboxes()
      .filter(function (c) { return c.checked; })
      .map(function (c) { return c.value; });
  }

  function updateCount() {
    bulkCount.textContent = "已选 " + selectedIds().length + " 人";
  }

  if (selectAll) {
    selectAll.addEventListener("change", function () {
      // 只对当前搜索框筛选后仍可见的行生效（跟 table-filter.js 的筛选结果保持一致），
      // 避免"全选"把已经被搜索框隐藏掉的行也悄悄选中。
      rowCheckboxes().forEach(function (c) {
        var row = c.closest("tr");
        if (!row || row.style.display !== "none") c.checked = selectAll.checked;
      });
      updateCount();
    });
  }

  table.addEventListener("change", function (e) {
    if (e.target.classList.contains("row-select")) updateCount();
  });

  function submitBulk(action, extraFields) {
    var ids = selectedIds();
    if (!ids.length) {
      alert("先勾选至少一个用户");
      return;
    }
    var form = document.createElement("form");
    form.method = "post";
    form.action = action;
    ids.forEach(function (id) {
      var input = document.createElement("input");
      input.type = "hidden";
      input.name = "user_ids";
      input.value = id;
      form.appendChild(input);
    });
    Object.keys(extraFields).forEach(function (key) {
      var input = document.createElement("input");
      input.type = "hidden";
      input.name = key;
      input.value = extraFields[key];
      form.appendChild(input);
    });
    document.body.appendChild(form);
    form.submit();
  }

  var tierBtn = document.getElementById("bulkChangeTierBtn");
  if (tierBtn) {
    tierBtn.addEventListener("click", function () {
      var role = document.getElementById("bulkRole").value;
      var ids = selectedIds();
      if (!ids.length) {
        alert("先勾选至少一个用户");
        return;
      }
      if (!confirm("确认把选中的 " + ids.length + " 人切换到「" + role + "」？")) return;
      submitBulk("/users/batch-change-tier", { new_role: role });
    });
  }

  var disableBtn = document.getElementById("bulkDisableBtn");
  if (disableBtn) {
    disableBtn.addEventListener("click", function () {
      var ids = selectedIds();
      if (!ids.length) {
        alert("先勾选至少一个用户");
        return;
      }
      if (!confirm("确认停用选中的 " + ids.length + " 人？停用后这些账号无法再登录 SSO。")) return;
      submitBulk("/users/batch-set-enabled", { enabled: "false" });
    });
  }

  var enableBtn = document.getElementById("bulkEnableBtn");
  if (enableBtn) {
    enableBtn.addEventListener("click", function () {
      var ids = selectedIds();
      if (!ids.length) {
        alert("先勾选至少一个用户");
        return;
      }
      submitBulk("/users/batch-set-enabled", { enabled: "true" });
    });
  }

  updateCount();
})();
