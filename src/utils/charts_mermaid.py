"""Render Mermaid source text to PNG bytes with headless Chromium (the engine behind `render_diagram` kind mermaid).

The page has no network and no file access: the vendored Mermaid script and the bundled fonts are injected into a
blank page, every request is aborted, and the browser runs in a child process (`python -m src.utils.charts_mermaid`)
in its own process group so a time limit can kill it together with Chromium.
"""
from __future__ import annotations

import base64
import csv
import html
import io
import json
import os
import re
import shutil
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any

from src.utils.charts_theme import FONT_DIR, get_theme, mermaid_config

ROOT = Path(__file__).resolve().parents[2]
MERMAID_JS = ROOT / "src" / "utils" / "chart_vendor" / "mermaid.min.js"
MAX_MERMAID_BYTES = 100_000
MERMAID_TIMEOUT = 30
MAX_SIDE = 4000
SCALE = 2
# The platform fonts Mermaid may name are all mapped to the bundled Open Sans, so text measures the same everywhere.
FONT_ALIASES = ("Open Sans", "Verdana", "Arial", "Trebuchet MS", "Helvetica")
BROWSER_NAMES = ("chromium-headless-shell", "chromium", "chromium-browser", "google-chrome-stable", "google-chrome")
INSTALL_HINT = (
    "Chromium is not installed on this machine, so Mermaid diagrams cannot be drawn: ask the administrator to install it "
    "(apt install chromium-headless-shell; the official Docker image includes it) or set RECALL_CHROMIUM_PATH to its executable"
)
EXIT_BROWSER_MISSING = 3
EXIT_BAD_SOURCE = 4

_PLACEHOLDER = "Nzq{}q"
_PLACEHOLDER_RE = re.compile(r"Nzq(\d+)q")
_TEXT_RE = re.compile(r">([^<>]*)<")


class MermaidError(ValueError):
    """Bad Mermaid source or a failed render, worded for the agent."""


def check_mermaid(source: Any) -> str:
    if not isinstance(source, str) or not source.strip():
        raise MermaidError("'source' must be non-empty Mermaid text, e.g. 'flowchart LR; A --> B'")
    if len(source.encode()) > MAX_MERMAID_BYTES:
        raise MermaidError(f"'source' is larger than {MAX_MERMAID_BYTES // 1000} KB")
    return source


def is_sankey(source: str) -> bool:
    return next((line.strip() for line in source.splitlines() if line.strip()), "").lower().startswith("sankey")


def sankey_placeholders(source: str) -> tuple[str, dict[str, str]]:
    """Sankey source with every node name replaced by an ASCII id (its parser rejects accents), and the id -> name map."""
    names: dict[str, str] = {}
    out: list[str] = []
    header_seen = False
    for line in source.splitlines():
        if not header_seen:
            out.append(line)
            header_seen = bool(line.strip())
            continue
        if not line.strip() or line.strip().startswith("%%"):
            out.append(line)
            continue
        try:
            row = next(csv.reader(io.StringIO(line), skipinitialspace=True))
        except (csv.Error, StopIteration) as exc:
            raise MermaidError(f"invalid sankey row: {line[:80]!r}") from exc
        if len(row) != 3:
            raise MermaidError(f"invalid sankey row {line[:80]!r}: expected 'source,target,value'")
        ids = []
        for name in row[:2]:
            name = name.strip()
            key = next((k for k, v in names.items() if v == name), None)
            if key is None:
                key = _PLACEHOLDER.format(len(names))
                names[key] = name
            ids.append(key)
        out.append(f"{ids[0]},{ids[1]},{row[2].strip()}")
    return "\n".join(out), names


def restore_names(svg: str, names: dict[str, str]) -> str:
    """The SVG with the placeholder ids inside its text content replaced by the original names."""
    def swap(match: re.Match[str]) -> str:
        text = _PLACEHOLDER_RE.sub(lambda m: html.escape(names.get(m.group(0), m.group(0)), quote=False), match.group(1))
        return f">{text}<"

    return _TEXT_RE.sub(swap, svg)


def find_browser() -> str | None:
    """The Chromium executable: RECALL_CHROMIUM_PATH, else one on PATH; None leaves it to Playwright's own download, if any."""
    configured = os.environ.get("RECALL_CHROMIUM_PATH", "").strip()
    if configured:
        return configured
    for name in BROWSER_NAMES:
        if found := shutil.which(name):
            return found
    return None


def needs_no_sandbox() -> bool:
    """Chromium's sandbox cannot start as root or inside a container (no user namespaces): only then turn it off."""
    return os.geteuid() == 0 or Path("/.dockerenv").exists() or os.environ.get("container") is not None


def _kill_group(proc: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def render_mermaid_png(source: Any, theme: str = "") -> bytes:
    check_mermaid(source)
    palette = get_theme(theme)
    code, names = sankey_placeholders(source) if is_sankey(source) else (source, {})
    request = {
        "code": code, "names": names, "config": mermaid_config(palette), "theme": palette,
        "sankey": is_sankey(source), "executable": find_browser(), "no_sandbox": needs_no_sandbox(),
    }
    proc = subprocess.Popen(
        [sys.executable, "-m", "src.utils.charts_mermaid"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, cwd=ROOT, env={**os.environ, "PYTHONPATH": str(ROOT)}, start_new_session=True,
    )
    try:
        out, err = proc.communicate(json.dumps(request).encode(), timeout=MERMAID_TIMEOUT)
    except subprocess.TimeoutExpired as exc:
        _kill_group(proc)
        proc.communicate()
        raise MermaidError(f"rendering took longer than {MERMAID_TIMEOUT} s and was stopped; simplify the diagram") from exc
    finally:
        _kill_group(proc)  # no stray Chromium after a normal exit either
    message = err.decode("utf-8", "replace").strip().splitlines()
    reason = message[-1][:400] if message else f"exit code {proc.returncode}"
    if proc.returncode == EXIT_BROWSER_MISSING:
        raise MermaidError(INSTALL_HINT)
    if proc.returncode == EXIT_BAD_SOURCE:
        raise MermaidError(f"invalid Mermaid: {reason}")
    if proc.returncode != 0:
        raise MermaidError(f"rendering failed: {reason}")
    return out


# ---------- worker (runs in the child process) ----------

_FONT_CSS = "html,body,svg,text,tspan,div,span,p{font-family:\"Open Sans\" !important}"

_RENDER_JS = """async ([code, config]) => {
  mermaid.initialize(config);
  try {
    const out = await mermaid.render('chart', code);
    return {svg: out.svg};
  } catch (e) {
    return {error: String((e && e.message) || e)};
  }
}"""

_PLACE_JS = """([svg, theme, sankey]) => {
  const box = document.getElementById('box');
  box.innerHTML = svg;
  const el = box.querySelector('svg');
  el.style.maxWidth = 'none';
  if (sankey) {
    const pal = theme.palette;
    box.querySelectorAll('.link').forEach(l => l.style.setProperty('mix-blend-mode', 'normal', 'important'));
    box.querySelectorAll('.link path').forEach(p => {
      p.style.setProperty('stroke', theme.edge, 'important');
      p.style.setProperty('stroke-opacity', '0.4', 'important');
    });
    box.querySelectorAll('.node rect').forEach((r, i) => r.style.setProperty('fill', pal[i % pal.length], 'important'));
    box.querySelectorAll('.node text').forEach(t => t.style.setProperty('fill', theme.text, 'important'));
  }
  const r = box.getBoundingClientRect();
  return [r.width, r.height];
}"""


def _font_css() -> str:
    faces = []
    for file, weight in (("OpenSans-Regular.ttf", "400"), ("OpenSans-Semibold.ttf", "600 700")):
        data = base64.b64encode((FONT_DIR / file).read_bytes()).decode()
        for family in FONT_ALIASES:
            faces.append(
                f'@font-face{{font-family:"{family}";font-weight:{weight};src:url(data:font/ttf;base64,{data}) format("truetype")}}'
            )
    return "".join(faces) + _FONT_CSS


def _fail(code: int, message: str) -> None:
    print(" ".join(message.split())[:400], file=sys.stderr)
    sys.exit(code)


def _worker_main() -> None:
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import sync_playwright

    request = json.load(sys.stdin)
    theme = request["theme"]
    with sync_playwright() as p:
        args = ["--host-resolver-rules=MAP * ~NOTFOUND", "--disable-gpu"]
        if request["no_sandbox"]:
            args.append("--no-sandbox")
        try:
            browser = p.chromium.launch(executable_path=request["executable"] or None, args=args)
        except PlaywrightError as exc:
            _fail(EXIT_BROWSER_MISSING, str(exc))
        try:
            context = browser.new_context(device_scale_factor=SCALE, viewport={"width": 1200, "height": 800}, java_script_enabled=True)
            context.route("**/*", lambda route: route.abort())
            page = context.new_page()
            page.set_content(
                f'<!doctype html><html><head><meta charset="utf-8"><style>{_font_css()}'
                f'body{{margin:0;background:{theme["background"]}}}#box{{display:inline-block;padding:16px;background:{theme["background"]}}}'
                '</style></head><body><div id="box"></div></body></html>'
            )
            page.add_script_tag(content=MERMAID_JS.read_text(encoding="utf-8"))
            page.evaluate(
                "async (names) => { for (const n of names) { await document.fonts.load('16px \"' + n + '\"'); "
                "await document.fonts.load('600 16px \"' + n + '\"'); } }", list(FONT_ALIASES),
            )
            page.wait_for_timeout(500)
            result = page.evaluate(_RENDER_JS, [request["code"], request["config"]])
            if "error" in result:
                _fail(EXIT_BAD_SOURCE, result["error"].splitlines()[0] if result["error"] else "syntax error")
            svg = restore_names(result["svg"], request["names"]) if request["names"] else result["svg"]
            width, height = page.evaluate(_PLACE_JS, [svg, theme, request["sankey"]])
            if width * SCALE > MAX_SIDE or height * SCALE > MAX_SIDE:
                _fail(1, f"the diagram would be {int(width * SCALE)}x{int(height * SCALE)} px; the limit is {MAX_SIDE}x{MAX_SIDE}. "
                         "Split it, use fewer nodes or tasks, or a shorter date range")
            sys.stdout.buffer.write(page.locator("#box").screenshot(type="png"))
        finally:
            browser.close()


if __name__ == "__main__":
    _worker_main()
