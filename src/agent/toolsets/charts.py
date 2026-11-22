from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Callable

from src.agent.tooling import tool
from src.agent.toolsets.common import mark_written, readonly_error, write_locks
from src.agent.toolsets.pdf import slugify
from src.utils.charts_render import (
    DIAGRAM_KINDS, PLANNED_DIAGRAM_KINDS, ChartError, png_size, render_chart_png, render_dot_png,
)

if TYPE_CHECKING:
    from src.agent.agent import Agent

DEFAULT_FOLDER = "mind_palace:charts"
MAX_PNG_BYTES = 10 * 1024 * 1024  # the cap `send_file` applies, so what is drawn can be sent
MANUAL = 'recall skill, references/charts.md'


def build_chart_tools(agent: Agent) -> list[Callable[..., str]]:
    """The `render_chart` and `render_diagram` tools: a description in, a PNG in a writable source out (nothing is sent)."""
    sources = agent.sources

    def free_address(address: str) -> str:
        source, rel = sources.resolve(address)
        stem, _, suffix = rel.rpartition(".")
        for number in range(2, 1000):
            if not source.exists(rel):
                break
            rel = f"{stem}-{number}.{suffix}"
        return f"{source.name}:{rel}"

    def save(data: bytes, target: str, overwrite: bool) -> str:
        if len(data) > MAX_PNG_BYTES:
            raise ChartError(f"the image is {len(data) // 1024} KiB; the limit is {MAX_PNG_BYTES // 1024 // 1024} MiB")
        with write_locks(sources, sources.resolve(target)[0]):
            final = target if overwrite else free_address(target)
            sources.write_bytes(final, data)
        mark_written(agent, final)
        width, height = png_size(data)
        return f"saved {final} ({width}x{height} px, {len(data)} bytes). Look at it with view_image before sending it with send_file."

    def checked_target(path: str, default_name: str) -> str:
        target = path.strip() or f"{DEFAULT_FOLDER}/{slugify(default_name)}.png"
        if not target.lower().endswith(".png"):
            raise ChartError("the path must end in .png")
        if error := readonly_error(sources, target):
            raise ChartError(error.removeprefix("error: "))
        return target

    @tool
    def render_chart(
        spec: Annotated[dict, (
            "a Vega-Lite v5 chart as a JSON object, with its rows inline in data.values (remote or file data is refused). "
            f"Colours and fonts come from the theme; override them in the spec's own config. Recipes: {MANUAL}"
        )],
        path: Annotated[str, (
            "where to save it, as 'source:path' ending in .png, in a source you may write. "
            f"Default: {DEFAULT_FOLDER}/<title>.png"
        )] = "",
        theme: Annotated[str, "dark (default) or light"] = "dark",
        width: Annotated[int, "chart width in px, 0 keeps the spec's own"] = 0,
        height: Annotated[int, "chart height in px, 0 keeps the spec's own"] = 0,
        overwrite: Annotated[bool, "replace the file when it exists; by default a number is added to the name"] = False,
    ) -> str:
        """Draw a chart from a Vega-Lite spec and save it as a PNG (2x resolution); it does not send it (use send_file).
        Returns the file's address and pixel size. Check it with view_image first. Recipes: the recall skill, references/charts.md."""
        try:
            title = spec.get("title") if isinstance(spec, dict) else None
            title = title.get("text") if isinstance(title, dict) else title
            target = checked_target(path, title if isinstance(title, str) and title else "chart")
            data = render_chart_png(spec, theme, width, height)
            return save(data, target, overwrite)
        except (ChartError, ValueError, KeyError, PermissionError, OSError) as exc:
            return f"error: {exc}"

    @tool
    def render_diagram(
        kind: Annotated[str, "graphviz (DOT text); mermaid is planned and not available yet"],
        source: Annotated[str, "the diagram text: DOT for graphviz, e.g. digraph G { rankdir=LR; a -> b }"],
        path: Annotated[str, (
            "where to save it, as 'source:path' ending in .png, in a source you may write. "
            f"Default: {DEFAULT_FOLDER}/diagram.png"
        )] = "",
        theme: Annotated[str, "dark (default) or light; your own attributes in the text win"] = "dark",
        overwrite: Annotated[bool, "replace the file when it exists; by default a number is added to the name"] = False,
    ) -> str:
        """Draw a diagram (graph, flow, tree) from text and save it as a PNG; it does not send it (use send_file).
        Returns the file's address and pixel size. Check it with view_image first. Examples: the recall skill, references/charts.md."""
        try:
            name = (kind or "").strip().lower()
            if name in PLANNED_DIAGRAM_KINDS:
                return f"error: kind '{name}' is not available in this build yet; use one of: {', '.join(DIAGRAM_KINDS)}"
            if name not in DIAGRAM_KINDS:
                return f"error: unknown kind '{kind}'; use one of: {', '.join(DIAGRAM_KINDS)} (planned: {', '.join(PLANNED_DIAGRAM_KINDS)})"
            target = checked_target(path, "diagram")
            return save(render_dot_png(source, theme), target, overwrite)
        except (ChartError, ValueError, KeyError, PermissionError, OSError) as exc:
            return f"error: {exc}"

    return [render_chart, render_diagram]
