// Appearance: accent colour and architecture ornaments. Both are per-browser preferences kept in localStorage
// (like the light/dark theme) and applied as data-accent / data-ornaments on <html>.
// This file is loaded synchronously in <head>, so the saved values are applied before first paint (no flash).
(() => {
  "use strict";
  const ACCENTS = ["pink", "blue", "gold", "green", "violet", "cyan"];
  const ACCENT_KEY = "recall.accent";
  const ORNAMENTS_KEY = "recall.ornaments";
  const root = document.documentElement;

  const read = (key) => { try { return localStorage.getItem(key); } catch { return null; } };
  const write = (key, value) => { try { localStorage.setItem(key, value); } catch { /* private mode: the choice lasts for this page only */ } };

  function setAccent(accent) {
    root.dataset.accent = ACCENTS.includes(accent) ? accent : "pink";
  }
  function setOrnaments(on) {
    root.dataset.ornaments = on ? "on" : "off";
  }

  setAccent(read(ACCENT_KEY));
  setOrnaments(read(ORNAMENTS_KEY) !== "off");

  document.addEventListener("DOMContentLoaded", () => {
    const radios = [...document.querySelectorAll('input[name="accent"]')];
    const ornaments = document.getElementById("appearance-ornaments");
    for (const radio of radios) {
      radio.checked = radio.value === root.dataset.accent;
      radio.addEventListener("change", () => {
        if (!radio.checked) return;
        setAccent(radio.value);
        write(ACCENT_KEY, radio.value);
      });
    }
    if (ornaments) {
      ornaments.checked = root.dataset.ornaments !== "off";
      ornaments.addEventListener("change", () => {
        setOrnaments(ornaments.checked);
        write(ORNAMENTS_KEY, ornaments.checked ? "on" : "off");
      });
    }
  });
})();
