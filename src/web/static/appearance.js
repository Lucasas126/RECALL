// Appearance: accent colour and architecture ornaments. Both are per-browser preferences kept in localStorage
// (like the light/dark theme) and applied as data-accent / data-ornaments on <html>.
// The accent is one of the named swatches or "custom": any colour picked with the colour input, whose variables are derived
// here for the current theme (kept at least AA, 4.5:1, against the theme's surfaces, like the swatches).
// This file is loaded synchronously in <head>, so the saved values are applied before first paint (no flash).
(() => {
  "use strict";
  const ACCENTS = ["pink", "blue", "gold", "green", "violet", "cyan"];
  const ACCENT_KEY = "recall.accent";
  const CUSTOM_KEY = "recall.accentColor";
  const ORNAMENTS_KEY = "recall.ornaments";
  const DEFAULT_CUSTOM = "#ff6b9d";
  const HEX = /^#[0-9a-f]{6}$/i;
  const MIN_CONTRAST = 4.5;
  // the surfaces text sits on, by theme (the values of --bg, --panel and --raised in style.css)
  const SURFACES = { dark: ["#111111", "#171717", "#1f1f1f"], light: ["#fafaf8", "#ffffff", "#ebebe6"] };
  const ON_ACCENT = { dark: "#0f0f14", light: "#ffffff" };
  const VARIABLES = ["--accent", "--accent-strong", "--on-accent", "--accent-soft"];
  const root = document.documentElement;

  const read = (key) => { try { return localStorage.getItem(key); } catch { return null; } };
  const write = (key, value) => { try { localStorage.setItem(key, value); } catch { /* private mode: the choice lasts for this page only */ } };

  // ---- colour maths (sRGB, WCAG relative luminance)
  const toRgb = (hex) => [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16));
  const toHex = (rgb) => "#" + rgb.map((v) => Math.round(Math.min(255, Math.max(0, v))).toString(16).padStart(2, "0")).join("");
  const luminance = (hex) => {
    const [r, g, b] = toRgb(hex).map((v) => { const c = v / 255; return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4; });
    return 0.2126 * r + 0.7152 * g + 0.0722 * b;
  };
  const contrast = (a, b) => { const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x); return (hi + 0.05) / (lo + 0.05); };
  const toHsl = (hex) => {
    const [r, g, b] = toRgb(hex).map((v) => v / 255);
    const max = Math.max(r, g, b), min = Math.min(r, g, b), l = (max + min) / 2, d = max - min;
    if (d === 0) return [0, 0, l];
    const s = d / (1 - Math.abs(2 * l - 1));
    const h = max === r ? ((g - b) / d) % 6 : max === g ? (b - r) / d + 2 : (r - g) / d + 4;
    return [(h * 60 + 360) % 360, s, l];
  };
  const fromHsl = (h, s, l) => {
    const c = (1 - Math.abs(2 * l - 1)) * s, x = c * (1 - Math.abs(((h / 60) % 2) - 1)), m = l - c / 2;
    const [r, g, b] = h < 60 ? [c, x, 0] : h < 120 ? [x, c, 0] : h < 180 ? [0, c, x] : h < 240 ? [0, x, c] : h < 300 ? [x, 0, c] : [c, 0, x];
    return toHex([(r + m) * 255, (g + m) * 255, (b + m) * 255]);
  };
  const mix = (a, b, share) => toHex(toRgb(a).map((v, i) => v * share + toRgb(b)[i] * (1 - share)));
  const worst = (hex, surfaces) => Math.min(...surfaces.map((surface) => contrast(hex, surface)));

  // The accent variables for a picked colour in a theme: the colour itself when it reads well on the surfaces, else the same hue
  // and saturation with its lightness moved (lighter on dark, darker on light) just until it does.
  function accentVariables(hex, theme) {
    const surfaces = SURFACES[theme];
    const [h, s, l0] = toHsl(hex);
    const step = theme === "dark" ? 0.01 : -0.01;
    let l = l0, accent = hex;
    for (let i = 0; i < 100 && worst(accent, surfaces) < MIN_CONTRAST; i++) { l = Math.min(1, Math.max(0, l + step)); accent = fromHsl(h, s, l); }
    const strong = fromHsl(h, s, Math.min(1, Math.max(0, l + (theme === "dark" ? 0.08 : -0.08))));
    const onAccent = contrast(accent, ON_ACCENT.dark) >= contrast(accent, ON_ACCENT.light) ? ON_ACCENT.dark : ON_ACCENT.light;
    return {
      "--accent": accent,
      "--accent-strong": worst(strong, surfaces) >= worst(accent, surfaces) ? strong : accent,
      "--on-accent": onAccent,
      "--accent-soft": mix(accent, surfaces[0], theme === "dark" ? 0.16 : 0.1),
    };
  }

  const prefersLight = () => { try { return matchMedia("(prefers-color-scheme: light)").matches; } catch { return false; } };
  const currentTheme = () => (root.dataset.theme === "light" || (!root.dataset.theme && prefersLight()) ? "light" : "dark");
  const validColour = (value) => (HEX.test(value ?? "") ? value.toLowerCase() : null);

  let custom = validColour(read(CUSTOM_KEY)) ?? DEFAULT_CUSTOM;

  function clearCustomVariables() { for (const name of VARIABLES) root.style.removeProperty(name); }
  function applyCustom() {
    const variables = accentVariables(custom, currentTheme());
    for (const [name, value] of Object.entries(variables)) root.style.setProperty(name, value);
  }
  function setAccent(accent) {
    if (accent === "custom") {
      root.dataset.accent = "custom";
      applyCustom();
      return;
    }
    clearCustomVariables();
    root.dataset.accent = ACCENTS.includes(accent) ? accent : "pink";
  }
  function setOrnaments(on) {
    root.dataset.ornaments = on ? "on" : "off";
  }

  setAccent(read(ACCENT_KEY));
  setOrnaments(read(ORNAMENTS_KEY) !== "off");

  // the derived variables depend on the theme: recompute when it changes (the toggle, or the system setting when automatic)
  const retheme = () => { if (root.dataset.accent === "custom") applyCustom(); };
  try { new MutationObserver(retheme).observe(root, { attributes: true, attributeFilter: ["data-theme"] }); } catch { /* no observer: the next load applies it */ }
  try { matchMedia("(prefers-color-scheme: light)").addEventListener("change", retheme); } catch { /* idem */ }

  document.addEventListener("DOMContentLoaded", () => {
    const radios = [...document.querySelectorAll('input[name="accent"]')];
    const picker = document.getElementById("appearance-accent-color");
    const customSwatch = document.getElementById("appearance-accent-custom");
    const ornaments = document.getElementById("appearance-ornaments");
    const showCustom = () => {
      if (picker) picker.value = custom;
      if (customSwatch) { customSwatch.style.setProperty("--d", custom); customSwatch.style.setProperty("--l", custom); }
    };
    showCustom();
    for (const radio of radios) {
      radio.checked = radio.value === root.dataset.accent;
      radio.addEventListener("change", () => {
        if (!radio.checked) return;
        setAccent(radio.value);
        write(ACCENT_KEY, radio.value);
      });
    }
    if (picker) {
      picker.addEventListener("input", () => {
        custom = validColour(picker.value) ?? custom;
        showCustom();
        setAccent("custom");
        write(CUSTOM_KEY, custom);
        write(ACCENT_KEY, "custom");
        for (const radio of radios) radio.checked = radio.value === "custom";
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
