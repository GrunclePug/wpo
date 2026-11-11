// Runs before styles load so a saved choice also applies to the first paint.
(() => {
  "use strict";
  const choices = new Set(["system", "light", "dark"]);
  const media = window.matchMedia("(prefers-color-scheme: light)");
  let choice = "system";
  try {
    const saved = localStorage.getItem("wpo.theme");
    if (choices.has(saved)) choice = saved;
  } catch { /* The current page still supports theme changes when storage is unavailable. */ }

  function apply() {
    document.documentElement.dataset.theme = choice === "system" ? (media.matches ? "light" : "dark") : choice;
    const control = document.getElementById("theme-choice");
    if (control) control.value = choice;
  }

  apply();
  media.addEventListener("change", apply);
  document.addEventListener("DOMContentLoaded", apply);
  document.addEventListener("change", (event) => {
    if (event.target.id !== "theme-choice" || !choices.has(event.target.value)) return;
    choice = event.target.value;
    try { localStorage.setItem("wpo.theme", choice); } catch { /* Keep the choice for this page. */ }
    apply();
  });
  window.addEventListener("storage", (event) => {
    if (event.key !== "wpo.theme" && event.key !== null) return;
    choice = choices.has(event.newValue) ? event.newValue : "system";
    apply();
  });
})();
