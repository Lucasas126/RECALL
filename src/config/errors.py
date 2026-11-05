"""Readable, secret-free descriptions of configuration errors (for logs, notices and the admin CLI)."""

from __future__ import annotations

import yaml
from pydantic import ValidationError


def describe_config_error(exc: BaseException) -> str:
    """One line: where and why a config failed. Never echoes input values (a field may hold a hash or a path)."""
    if isinstance(exc, ValidationError):
        parts = []
        for error in exc.errors():
            where = ".".join(str(item) for item in error["loc"]) or "(root)"
            parts.append(f"{where}: {error['msg']}")
        return "; ".join(parts) or "invalid configuration"
    if isinstance(exc, yaml.YAMLError):
        mark = getattr(exc, "problem_mark", None)
        where = f" at line {mark.line + 1}, column {mark.column + 1}" if mark is not None else ""
        problem = getattr(exc, "problem", None) or "invalid YAML"
        return f"{problem}{where}"
    cause = exc.__cause__
    if isinstance(cause, ValidationError):  # AgentConfigError wraps the pydantic error without its details
        file = getattr(exc, "values", {}).get("file")
        details = describe_config_error(cause)
        return f"{file}: {details}" if file else details
    return str(exc) or type(exc).__name__
