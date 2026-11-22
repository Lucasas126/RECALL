"""Render Vega-Lite specs and Graphviz DOT text to PNG bytes (the engine behind `render_chart` and `render_diagram`).

No browser and no network: Vega-Lite goes through vl-convert, DOT through the system `dot` and then vl-convert's
SVG rasteriser. The vl-convert work runs in a child process (`python -m src.utils.charts_render`) so a time limit
can really stop it.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import struct
import subprocess
import sys
from pathlib import Path
from typing import Any

from src.utils.charts_theme import (
    FONT_DIR, get_theme, graphviz_defaults, register_fonts, themed_spec,  # noqa: F401  (FONT_DIR re-exported for tests)
)

MAX_SPEC_BYTES = 1_000_000
MAX_DOT_BYTES = 200_000
MAX_SIDE = 4000  # pixels of the PNG, after the 2x scale
SCALE = 2
MAX_LOGICAL_SIDE = MAX_SIDE // SCALE
RENDER_TIMEOUT = 40
DOT_TIMEOUT = 20
DIAGRAM_KINDS = ("graphviz",)
PLANNED_DIAGRAM_KINDS = ("mermaid",)
ROOT = Path(__file__).resolve().parents[2]


class ChartError(ValueError):
    """Bad input or a failed render, worded for the agent."""


def png_size(data: bytes) -> tuple[int, int]:
    if data[:8] != b"\x89PNG\r\n\x1a\n" or len(data) < 24:
        raise ChartError("the renderer did not produce a PNG image")
    return struct.unpack(">II", data[16:24])


def _find_url_keys(node: Any, path: str = "") -> str | None:
    """The path of the first `url` key (a data source, a lookup or an image) outside inline data rows."""
    if isinstance(node, dict):
        for key, value in node.items():
            here = f"{path}.{key}" if path else str(key)
            if key == "url":
                return here
            if key == "datasets" or (key == "values" and isinstance(value, list)):
                continue  # inline rows are plain data, whatever their field names
            if found := _find_url_keys(value, here):
                return found
    elif isinstance(node, list):
        for index, item in enumerate(node):
            if found := _find_url_keys(item, f"{path}[{index}]"):
                return found
    return None


def check_chart_spec(spec: Any) -> dict:
    if not isinstance(spec, dict):
        raise ChartError("'spec' must be a Vega-Lite JSON object (a dict), not text or a list")
    if not spec:
        raise ChartError("'spec' is empty")
    try:
        size = len(json.dumps(spec))
    except (TypeError, ValueError) as exc:
        raise ChartError(f"'spec' is not valid JSON data: {exc}") from exc
    if size > MAX_SPEC_BYTES:
        raise ChartError(f"'spec' is {size // 1000} KB; the limit is {MAX_SPEC_BYTES // 1000} KB. Aggregate or sample the data")
    if found := _find_url_keys(spec):
        raise ChartError(
            f"'{found}': remote and file data are not allowed (there is no network or file access); "
            "put the rows inline in data.values"
        )
    for side in ("width", "height"):
        value = spec.get(side)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value > MAX_LOGICAL_SIDE:
            raise ChartError(f"'{side}' {value} is too large; the image is rendered at {SCALE}x and may be at most {MAX_SIDE} px per side")
    return spec


def _run_worker(request: dict, timeout: int) -> bytes:
    try:
        done = subprocess.run(
            [sys.executable, "-m", "src.utils.charts_render"],
            input=json.dumps(request).encode(), capture_output=True, timeout=timeout, cwd=ROOT, check=False,
            env={**os.environ, "PYTHONPATH": str(ROOT)},
        )
    except subprocess.TimeoutExpired as exc:
        raise ChartError(f"rendering took longer than {timeout} s and was stopped; simplify it") from exc
    if done.returncode != 0:
        detail = done.stderr.decode("utf-8", "replace").strip().splitlines()
        reason = detail[-1][:400] if detail else f"exit code {done.returncode}"
        raise ChartError(f"rendering failed: {reason}")
    return done.stdout


def render_chart_png(spec: Any, theme: str = "", width: int = 0, height: int = 0) -> bytes:
    """A PNG of a Vega-Lite spec in the theme, with the agent's own `config` merged over it."""
    check_chart_spec(spec)
    palette = get_theme(theme)
    for name, value in (("width", width), ("height", height)):
        if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= MAX_LOGICAL_SIDE:
            raise ChartError(f"'{name}' must be a whole number from 1 to {MAX_LOGICAL_SIDE} (or 0 to keep the spec's own)")
    final = themed_spec(spec, palette)
    if width:
        final["width"] = width
    if height:
        final["height"] = height
    data = _run_worker({"kind": "vegalite", "spec": final}, RENDER_TIMEOUT)
    w, h = png_size(data)
    if w > MAX_SIDE or h > MAX_SIDE:
        raise ChartError(f"the image would be {w}x{h} px; the limit is {MAX_SIDE}x{MAX_SIDE}. Use a smaller width, height or fewer facets")
    return data


# attributes (and HTML-like label tags) through which Graphviz reads a file
_DOT_FILE_ATTRS = (
    "image", "imagepath", "fontpath", "shapefile", "stylesheet",
    "labelfontpath",
)
_DOT_FILE_RE = re.compile(r"(?<![A-Za-z0-9_])[\"']?(" + "|".join(_DOT_FILE_ATTRS) + r")[\"']?\s*=", re.IGNORECASE)
_DOT_TAG_RE = re.compile(r"<\s*img\b|\bsrc\s*=", re.IGNORECASE)


def check_dot(source: Any) -> str:
    if not isinstance(source, str) or not source.strip():
        raise ChartError("'source' must be non-empty DOT text")
    if len(source.encode()) > MAX_DOT_BYTES:
        raise ChartError(f"'source' is larger than {MAX_DOT_BYTES // 1000} KB")
    if match := _DOT_FILE_RE.search(source):
        raise ChartError(f"the attribute '{match.group(1)}' is not allowed: diagrams cannot read files")
    if _DOT_TAG_RE.search(source):
        raise ChartError("<img> and src= in labels are not allowed: diagrams cannot read files")
    return source


def _body_start(source: str) -> int:
    """The index just after the `{` that opens the graph body (skipping quoted names and comments)."""
    i, n = 0, len(source)
    while i < n:
        char = source[i]
        if char == '"':
            i += 1
            while i < n and source[i] != '"':
                i += 2 if source[i] == "\\" else 1
        elif source.startswith("//", i) or char == "#":
            i = source.find("\n", i)
            i = n if i < 0 else i
        elif source.startswith("/*", i):
            i = source.find("*/", i + 2)
            i = n if i < 0 else i + 1
        elif char == "{":
            return i + 1
        i += 1
    raise ChartError("'source' is not a DOT graph: expected 'digraph name { ... }' or 'graph name { ... }'")


def themed_dot(source: str, theme: str = "") -> str:
    """DOT with the theme's defaults placed first in the body, so anything the agent sets itself wins."""
    check_dot(source)
    defaults = graphviz_defaults(get_theme(theme))
    start = _body_start(source)
    inject = f'\n graph [{defaults["graph"]}]; node [{defaults["node"]}]; edge [{defaults["edge"]}];\n'
    return source[:start] + inject + source[start:]


def render_dot_png(source: Any, theme: str = "") -> bytes:
    themed = themed_dot(check_dot(source), theme)
    executable = shutil.which("dot")
    if executable is None:
        raise ChartError("Graphviz is not installed on this machine, so diagrams cannot be drawn: ask the administrator to install it (apt install graphviz)")
    try:
        done = subprocess.run(
            [executable, "-Tsvg"], input=themed.encode(), capture_output=True, timeout=DOT_TIMEOUT, check=False,
            env={"PATH": os.environ.get("PATH", ""), "GV_FILE_PATH": "/nonexistent", "LANG": "C.UTF-8"},
        )
    except subprocess.TimeoutExpired as exc:
        raise ChartError(f"Graphviz took longer than {DOT_TIMEOUT} s and was stopped; simplify the graph") from exc
    except OSError as exc:
        raise ChartError(f"Graphviz could not be run ({exc.strerror or exc}); it must be installed (apt install graphviz)") from exc
    if done.returncode != 0 or not done.stdout.strip():
        lines = done.stderr.decode("utf-8", "replace").strip().splitlines()
        raise ChartError(f"invalid DOT: {lines[0][:300] if lines else 'dot failed'}")
    data = _run_worker({"kind": "svg", "svg": done.stdout.decode("utf-8", "replace")}, RENDER_TIMEOUT)
    w, h = png_size(data)
    if w > MAX_SIDE or h > MAX_SIDE:
        raise ChartError(f"the diagram would be {w}x{h} px; the limit is {MAX_SIDE}x{MAX_SIDE}. Split it or use fewer nodes")
    return data


def _worker_main() -> None:
    import vl_convert

    request = json.load(sys.stdin)
    register_fonts()
    if request["kind"] == "vegalite":
        data = vl_convert.vegalite_to_png(request["spec"], scale=SCALE, allowed_base_urls=[])
    else:
        data = vl_convert.svg_to_png(request["svg"], scale=SCALE)
    sys.stdout.buffer.write(data)


if __name__ == "__main__":
    _worker_main()
