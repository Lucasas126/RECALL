// Appearance: accent colour, a user preference kept on the server (synced by app.js) with a local copy in localStorage
// (like the light/dark theme) so it is applied without network and applied as data-accent on <html>.
// The accent is one of the named swatches or "custom": any colour picked with the colour input, whose variables are derived
// here for the current theme (kept at least AA, 4.5:1, against the theme's surfaces, like the swatches).
// This file is loaded synchronously in <head>, so the saved values are applied before first paint (no flash).
(() => {
  "use strict";
  const ACCENTS = ["pink", "blue", "gold", "green", "violet", "cyan"];
  const ACCENT_KEY = "recall.accent";
  const CUSTOM_KEY = "recall.accentColor";
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
  const toHsv = (hex) => {
    const [r, g, b] = toRgb(hex).map((v) => v / 255);
    const max = Math.max(r, g, b), d = max - Math.min(r, g, b);
    let h = 0;
    if (d) { h = max === r ? ((g - b) / d) % 6 : max === g ? (b - r) / d + 2 : (r - g) / d + 4; h = (h * 60 + 360) % 360; }
    return [h, max ? d / max : 0, max];
  };
  const fromHsv = (h, s, v) => {
    const hh = ((h % 360) + 360) % 360, c = v * s, x = c * (1 - Math.abs(((hh / 60) % 2) - 1)), m = v - c;
    const [r, g, b] = hh < 60 ? [c, x, 0] : hh < 120 ? [x, c, 0] : hh < 180 ? [0, c, x] : hh < 240 ? [0, x, c] : hh < 300 ? [x, 0, c] : [c, 0, x];
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

  setAccent(read(ACCENT_KEY));

  // The accent is also a user preference on the server (synced across devices): app.js reads the local copy with `snapshot`,
  // applies the server's values with `apply`, and is told with `onChange` when the user picks another accent.
  const listeners = [];
  let repaint = () => {};
  const notify = () => { for (const listener of listeners) { try { listener(snapshot()); } catch { /* best effort */ } } };
  const snapshot = () => {
    const accent = read(ACCENT_KEY);
    return { accent: accent === "custom" || ACCENTS.includes(accent) ? accent : null, accent_color: validColour(read(CUSTOM_KEY)) };
  };
  globalThis.recallAppearance = {
    snapshot,
    onChange: (listener) => { listeners.push(listener); },
    // apply values from the server (keys left out or null are kept) and refresh the local copy
    apply({ accent, accent_color: colour } = {}) {
      const picked = validColour(colour);
      if (picked) { custom = picked; write(CUSTOM_KEY, picked); }
      if (accent === "custom" || ACCENTS.includes(accent)) { setAccent(accent); write(ACCENT_KEY, accent); }
      repaint();
    },
  };

  // the derived variables depend on the theme: recompute when it changes (the toggle, or the system setting when automatic)
  const retheme = () => { if (root.dataset.accent === "custom") applyCustom(); };
  try { new MutationObserver(retheme).observe(root, { attributes: true, attributeFilter: ["data-theme"] }); } catch { /* no observer: the next load applies it */ }
  try { matchMedia("(prefers-color-scheme: light)").addEventListener("change", retheme); } catch { /* idem */ }

  document.addEventListener("DOMContentLoaded", () => {
    const $ = (id) => document.getElementById(id);
    const radios = [...document.querySelectorAll('input[name="accent"]')];
    const trigger = $("appearance-accent-color"), panel = $("accent-panel"), area = $("accent-sv"), thumb = $("accent-sv-thumb");
    const hue = $("accent-hue"), hexField = $("accent-hex"), preview = $("accent-preview"), note = $("accent-note");
    const customSwatch = $("appearance-accent-custom");
    let hsv = toHsv(custom);

    const paint = (fromHexField = false) => {
      if (customSwatch) { customSwatch.style.setProperty("--d", custom); customSwatch.style.setProperty("--l", custom); }
      if (trigger) trigger.style.background = custom;
      if (!panel) return;
      const [h, s, v] = hsv;
      area.style.setProperty("--h", String(Math.round(h)));
      thumb.style.left = `${s * 100}%`;
      thumb.style.top = `${(1 - v) * 100}%`;
      hue.value = String(Math.round(h));
      if (!fromHexField) hexField.value = custom;
      const applied = accentVariables(custom, currentTheme())["--accent"];
      preview.style.background = applied;
      note.textContent = applied === custom ? "" : "Adjusted slightly so it stays readable in this theme.";
    };
    const commit = (fromHexField = false) => {
      custom = fromHsv(...hsv);
      setAccent("custom");
      write(CUSTOM_KEY, custom);
      write(ACCENT_KEY, "custom");
      notify();
      for (const radio of radios) radio.checked = radio.value === "custom";
      paint(fromHexField);
    };

    paint();
    repaint = () => {
      hsv = toHsv(custom);
      paint();
      for (const radio of radios) radio.checked = radio.value === root.dataset.accent;
    };
    for (const radio of radios) {
      radio.checked = radio.value === root.dataset.accent;
      radio.addEventListener("change", () => {
        if (!radio.checked) return;
        setAccent(radio.value);
        write(ACCENT_KEY, radio.value);
        notify();
      });
    }
    if (trigger && panel) {
      const toggle = (open) => { panel.hidden = !open; trigger.setAttribute("aria-expanded", String(open)); };
      trigger.addEventListener("click", () => toggle(panel.hidden));
      panel.addEventListener("keydown", (event) => { if (event.key === "Escape") { toggle(false); trigger.focus?.(); } });

      // saturation (x) and brightness (y) on the square; arrows move 1%, with Shift 10%
      const pick = (event) => {
        const box = area.getBoundingClientRect();
        const clamp = (value) => Math.min(1, Math.max(0, value));
        hsv = [hsv[0], clamp((event.clientX - box.left) / (box.width || 1)), clamp(1 - (event.clientY - box.top) / (box.height || 1))];
        commit();
      };
      area.addEventListener("pointerdown", (event) => { area.setPointerCapture?.(event.pointerId); area.dragging = true; pick(event); });
      area.addEventListener("pointermove", (event) => { if (area.dragging) pick(event); });
      const stop = () => { area.dragging = false; };
      area.addEventListener("pointerup", stop);
      area.addEventListener("pointercancel", stop);
      area.addEventListener("keydown", (event) => {
        const amount = event.shiftKey ? 0.1 : 0.01;
        const move = { ArrowLeft: [-amount, 0], ArrowRight: [amount, 0], ArrowUp: [0, amount], ArrowDown: [0, -amount] }[event.key];
        if (!move) return;
        event.preventDefault();
        hsv = [hsv[0], Math.min(1, Math.max(0, hsv[1] + move[0])), Math.min(1, Math.max(0, hsv[2] + move[1]))];
        commit();
      });
      hue.addEventListener("input", () => { hsv = [Number(hue.value) % 360, hsv[1], hsv[2]]; commit(); });
      hexField.addEventListener("input", () => {
        const typed = hexField.value.trim();
        const hex = validColour(typed.startsWith("#") ? typed : `#${typed}`);
        if (!hex) return;
        const [h, s, v] = toHsv(hex);
        hsv = [s && v ? h : hsv[0], s, v];   // grey and black carry no hue: keep the one on the slider
        commit(true);
      });
    }
  });
})();
