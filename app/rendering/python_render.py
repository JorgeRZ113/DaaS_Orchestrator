"""Renderizado de expresiones `@data.values` en Python puro, sin binario `ytt`.

Implementacion anterior al paso a `ytt` nativo por subproceso. Hoy **no la llama
nadie**: el pipeline entero pasa por `app.rendering.ytt.run_ytt_cli`. Se conserva
aislada en su propio modulo por dos razones: deja el camino vivo (`ytt.py`) sin
codigo muerto mezclado, y mantiene disponible la opcion de renderizar sin
depender de que el binario este en el PATH. Si se decide retirarla, el borrado es
este fichero entero.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import yaml

from app.rendering.paths import resolve_template_path


def _resolve_data_value(values: dict[str, Any], path: str) -> Any:
    current: Any = values
    for part in path.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            # If a requested path does not exist in the provided values,
            # return None instead of raising. This makes template rendering
            # resilient to missing optional sections (the ytt-style
            # expressions will be replaced with a YAML `null`).
            return None
    return current


def _ytt_inline_repr(value: Any) -> str:
    rendered = yaml.safe_dump(value, default_flow_style=True, sort_keys=False, allow_unicode=True)
    # yaml.safe_dump may append document end markers like "..." and newlines
    # (e.g. "null\n...\n"). Normalize to a single-line inline representation
    # suitable for embedding inside other YAML structures.
    rendered = rendered.replace("\n...\n", "").replace("\n...", "").replace("...\n", "")
    return rendered.strip()


def _render_ytt_expressions(template_text: str, values: dict[str, Any]) -> str:
    pattern = re.compile(r"@data\.values?\.([A-Za-z0-9_.-]+)")

    def replace(match: re.Match[str]) -> str:
        value = _resolve_data_value(values, match.group(1))
        return _ytt_inline_repr(value)

    return pattern.sub(replace, template_text)


def _load_template_data(template_path: Path) -> Any:
    text = template_path.read_text(encoding="utf-8")
    suffix = template_path.suffix.lower()
    if suffix == ".json":
        return json.loads(text)
    if suffix in {".yml", ".yaml"}:
        return yaml.safe_load(text)
    return text


def render_with_ytt(
    values: dict[str, Any] | None, template_ref: str, category: str | None = None
) -> str:
    """Render a YAML/JSON template by applying ytt-style `@data.values` expressions."""

    template_path = resolve_template_path(template_ref, category=category)
    if template_path is None:
        raise FileNotFoundError(f"Template not found: {template_ref}")

    raw_text = template_path.read_text(encoding="utf-8")
    rendered_text = _render_ytt_expressions(raw_text, values or {})

    # Remove ytt-specific directive lines (e.g. @load("@ytt:data", "data"),
    # #@overlay/..., #@data/values) so the resulting text is valid YAML/JSON.
    cleaned_text = "\n".join(
        line
        for line in rendered_text.splitlines()
        if not line.lstrip().startswith("@") and not line.lstrip().startswith("#@")
    )

    if template_path.suffix.lower() == ".json":
        parsed = yaml.safe_load(cleaned_text)
        return json.dumps(parsed, indent=4, ensure_ascii=False)

    parsed = yaml.safe_load(cleaned_text)
    if isinstance(parsed, str):
        return parsed
    return yaml.safe_dump(parsed, sort_keys=False, allow_unicode=True)
