function updateBulkBar() {
  const checked = document.querySelectorAll(".bulk-checkbox:checked");
  const bar = document.getElementById("bulk-bar");
  const countEl = document.getElementById("bulk-count");
  if (!bar || !countEl) return;
  countEl.textContent = String(checked.length);
  if (checked.length > 0) {
    bar.classList.add("visible");
  } else {
    bar.classList.remove("visible");
  }
}

document.addEventListener("change", (ev) => {
  const target = ev.target;
  if (!(target instanceof HTMLInputElement)) return;
  if (target.id === "select-all") {
    document.querySelectorAll(".bulk-checkbox").forEach((cb) => {
      if (cb instanceof HTMLInputElement) cb.checked = target.checked;
    });
    updateBulkBar();
  } else if (target.classList.contains("bulk-checkbox")) {
    updateBulkBar();
  }
});

document.addEventListener("DOMContentLoaded", updateBulkBar);
