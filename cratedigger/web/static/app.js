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

document.addEventListener("submit", async (event) => {
  const form = event.target;
  if (!(form instanceof HTMLFormElement) || !form.hasAttribute("data-genre-chart")) return;
  event.preventDefault();
  if (form.getAttribute("aria-busy") === "true") return;

  const chart = form.closest("#genre-history");
  const status = chart.querySelector("[data-chart-status]");
  const url = new URL(form.action);
  url.search = new URLSearchParams(new FormData(form)).toString();
  const focusId = document.activeElement.id;
  const controls = [...form.elements];
  form.setAttribute("aria-busy", "true");
  controls.forEach((control) => { control.disabled = true; });
  status.className = "";
  status.textContent = "Updating chart...";

  try {
    const response = await fetch(url, {
      headers: { "X-Chart-Partial": "genre-history" },
      cache: "no-store",
    });
    if (!response.ok) throw new Error(`Server returned ${response.status}`);
    const documentFragment = new DOMParser().parseFromString(await response.text(), "text/html");
    const updatedChart = documentFragment.querySelector("#genre-history");
    if (!updatedChart) throw new Error("The server did not return a chart");
    chart.replaceWith(updatedChart);
    history.replaceState(history.state, "", url);
    const updatedStatus = updatedChart.querySelector("[data-chart-status]");
    if (updatedStatus) updatedStatus.textContent = "Chart updated.";
    if (focusId) document.getElementById(focusId)?.focus({ preventScroll: true });
  } catch (error) {
    status.className = "flash error";
    status.textContent = `Cannot update chart: ${error.message}. Try again or reload this page.`;
  } finally {
    form.removeAttribute("aria-busy");
    controls.forEach((control) => { control.disabled = false; });
  }
});
