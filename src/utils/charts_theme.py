"""Chart themes (dark and light), the bundled Open Sans font and the Vega-Lite / Graphviz defaults built from them."""
from __future__ import annotations

import copy
from pathlib import Path
from typing import Any, Final

import vl_convert

FONT_DIR: Final[Path] = Path(__file__).resolve().parent / "chart_fonts"
DEFAULT_THEME: Final[str] = "dark"

THEMES: Final[dict[str, dict[str, Any]]] = {
    "dark": {
        "font": "Open Sans", "background": "#1B2230", "text": "#ECE7DD", "grid": "#33405A",
        "palette": ["#4FB3A6", "#E9C46A", "#F4A261", "#E76F51", "#8AB17D", "#7FA3B8"],
        "node_fill": "#2A3550", "node_border": "#4FB3A6", "edge": "#7FA3B8", "edge_label": "#E9C46A",
    },
    "light": {
        "font": "Open Sans", "background": "#FBF8F3", "text": "#2B2B2B", "grid": "#E8E1D5",
        "palette": ["#264653", "#2A9D8F", "#E9C46A", "#F4A261", "#E76F51", "#8AB17D"],
        "node_fill": "#FFFFFF", "node_border": "#264653", "edge": "#2A9D8F", "edge_label": "#E76F51",
    },
}

_fonts_registered = False  # vl-convert's font registry is process-wide: register once


def theme_names() -> tuple[str, ...]:
    return tuple(THEMES)


def get_theme(name: str) -> dict[str, Any]:
    """The theme called `name` (case-insensitive; empty means the default)."""
    key = (name or DEFAULT_THEME).strip().lower()
    if key not in THEMES:
        raise ValueError(f"unknown theme {name!r}: use one of {', '.join(THEMES)}")
    return THEMES[key]


def register_fonts() -> None:
    """Register the bundled fonts with vl-convert once; without any font it draws all text blank."""
    global _fonts_registered
    if not _fonts_registered:
        vl_convert.register_font_directory(str(FONT_DIR))
        _fonts_registered = True


def _merge_under(base: dict, over: dict) -> dict:
    """`base` with `over` merged on top, recursively: the agent's values win."""
    merged = copy.deepcopy(base)
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge_under(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def vega_lite_config(theme: dict[str, Any]) -> dict[str, Any]:
    fg, grid = theme["text"], theme["grid"]
    return {
        "font": theme["font"],
        "background": theme["background"],
        "range": {"category": list(theme["palette"])},
        "axis": {
            "labelFontSize": 13, "titleFontSize": 13, "labelColor": fg, "titleColor": fg,
            "gridColor": grid, "domainColor": grid, "tickColor": grid,
        },
        "legend": {"labelFontSize": 13, "labelColor": fg, "titleColor": fg},
        "title": {"fontSize": 18, "anchor": "start", "color": fg, "fontWeight": 600},
        "view": {"stroke": None},
    }


def themed_spec(spec: dict[str, Any], theme: dict[str, Any]) -> dict[str, Any]:
    """A copy of `spec` whose `config` is the theme's with the agent's own config merged over it."""
    result = copy.deepcopy(spec)
    result["config"] = _merge_under(vega_lite_config(theme), spec.get("config") or {})
    result.setdefault("padding", 16)
    return result


def graphviz_defaults(theme: dict[str, Any]) -> dict[str, str]:
    """Default graph / node / edge attribute statements for DOT, by statement kind."""
    font = theme["font"]
    return {
        "graph": f'bgcolor="{theme["background"]}", fontname="{font}", fontcolor="{theme["text"]}", pad=0.4',
        "node": (
            f'fontname="{font}", fontsize=13, fontcolor="{theme["text"]}", color="{theme["node_border"]}", '
            f'fillcolor="{theme["node_fill"]}", style="filled"'
        ),
        "edge": f'fontname="{font}", fontsize=11, color="{theme["edge"]}", fontcolor="{theme["edge_label"]}"',
    }
