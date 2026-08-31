from __future__ import annotations

import inspect
import types as _types
from typing import Annotated, Callable, Union, get_args, get_origin, get_type_hints

_JSON_TYPES = {str: "string", int: "integer", float: "number", bool: "boolean", list: "array", dict: "object"}


def _json_schema(annotation) -> dict:
    """Map a (possibly Optional/parametrized-list) annotation to a JSON-schema type."""
    origin = get_origin(annotation)
    if origin is Union or (hasattr(_types, "UnionType") and origin is _types.UnionType):
        non_none = [a for a in get_args(annotation) if a is not type(None)]
        if non_none:
            return _json_schema(non_none[0])  # unwrap Optional[X] -> X
    if origin in (list, tuple):
        args = get_args(annotation)
        item = _JSON_TYPES.get(args[0], "string") if args else "string"
        return {"type": "array", "items": {"type": item}}
    return {"type": _JSON_TYPES.get(annotation, "string")}


def openai_spec(func: Callable) -> dict:
    """Derive an OpenAI-style tool spec from a function's signature, type hints and docstring.

    Parameter descriptions come from ``Annotated[type, "description"]``; the tool
    description is the function's docstring and the tool name is the function name.
    """
    hints = get_type_hints(func, include_extras=True)
    properties: dict[str, dict] = {}
    required: list[str] = []
    for name, param in inspect.signature(func).parameters.items():
        annotation = hints.get(name, str)
        description = ""
        if get_origin(annotation) is Annotated:
            args = get_args(annotation)
            annotation = args[0]
            description = next((a for a in args[1:] if isinstance(a, str)), "")
        prop = _json_schema(annotation)
        if description:
            prop["description"] = description
        properties[name] = prop
        if param.default is inspect.Parameter.empty:
            required.append(name)
    return {
        "type": "function",
        "function": {
            "name": func.__name__,
            "description": (inspect.getdoc(func) or "").strip(),
            "parameters": {"type": "object", "properties": properties, "required": required},
        },
    }


def tool(func: Callable) -> Callable:
    """Mark a function as a tool, attaching its generated OpenAI spec as ``.tool_spec``."""
    func.tool_spec = openai_spec(func)  # type: ignore[attr-defined]
    return func
