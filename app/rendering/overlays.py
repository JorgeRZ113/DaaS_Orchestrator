"""Registro de overlays: componentes, dependencias y campos editables.

Cada `.overlay.yaml` declara en comentarios su `Component` y sus `Depends on`, y
en el cuerpo los valores por defecto de cada seccion. Este modulo los lee,
resuelve la cadena de dependencias en orden topologico y expone que campos puede
sobrescribir el usuario (`overlay_editable_fields_for_template`).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from app.rendering.paths import resolve_template_path, templates_root_dir

_OVERLAY_SECTION_ALIASES: dict[str, dict[str, str]] = {
    "vnet_sample_tnlcm_descriptor": {"vnet": "network"},
    "vm_kvm_sample_tnlcm_descriptor": {"vm_kvm": "vm"},
    "upf_p4_sw_sample_tnlcm_descriptor": {"upf_p4_sw": "upf"},
}


@dataclass(frozen=True)
class OverlaySpec:
    path: Path
    component: str
    dependencies: tuple[str, ...]
    sections: tuple[str, ...]
    defaults: dict[str, Any]


def _merge_data(base: Any, overlay: Any) -> Any:
    if overlay is None:
        return base
    if base is None:
        return overlay
    if isinstance(base, dict) and isinstance(overlay, dict):
        merged = dict(base)
        for key, value in overlay.items():
            merged[key] = _merge_data(merged.get(key), value)
        return merged
    if isinstance(base, list) and isinstance(overlay, list):
        return overlay if overlay else base
    if isinstance(base, str) and isinstance(overlay, str):
        return overlay
    return base


def _overlay_file_paths(category: str | None = None) -> list[Path]:
    root = templates_root_dir()
    search_dirs = [root]
    if category:
        # Para TNLCM, priorizar templates/TNLCM/overlays/ y luego templates/TNLCM/
        category_dir = root / category
        search_dirs = [category_dir]
        overlays_subdir = category_dir / "overlays"
        if overlays_subdir.exists():
            search_dirs.insert(0, overlays_subdir)
    paths: list[Path] = []
    for search_dir in search_dirs:
        if search_dir.exists():
            paths.extend(sorted(search_dir.glob("*.overlay.yaml")))
    return paths


def _infer_overlay_component_name(overlay_path: Path) -> str:
    name = overlay_path.name.removesuffix(".overlay.yaml")
    if "_sample_tnlcm_descriptor" in name:
        return name.split("_sample_tnlcm_descriptor", 1)[0]
    return name


def _parse_overlay_comment_value(text: str, label: str) -> str | None:
    pattern = re.compile(rf"^#\s*{re.escape(label)}\s*:\s*(.+?)\s*$", re.IGNORECASE)
    for line in text.splitlines():
        match = pattern.match(line.strip())
        if match:
            return match.group(1).strip()
    return None


def _parse_overlay_dependencies(text: str, component: str) -> tuple[str, ...]:
    raw = _parse_overlay_comment_value(text, "Depends on")
    if raw is None:
        fallback = {
            "tnlcm_descriptor_base": ("tn_init", "elcm"),
            "open5gs_vm": ("vnet",),
            "ocf": ("vnet", "oneKE"),
            "oneKE": ("vnet",),
            "upf_p4_sw": ("vnet", "ueransim", "vm_kvm"),
            "int_p4_sw": ("vnet", "vm_kvm"),
            "loadcore_agent": ("vnet", "open5gs_vm"),
            "ueransim": ("vnet", "open5gs_vm"),
        }
        return fallback.get(component, ())

    dependencies: list[str] = []
    for chunk in raw.split(","):
        dep = chunk.strip()
        if not dep:
            continue
        dep = dep.split()[0].strip()
        if dep:
            dependencies.append(dep)
    return tuple(dependencies)


def _load_overlay_defaults(overlay_path: Path) -> dict[str, Any]:
    text = overlay_path.read_text(encoding="utf-8")
    if "---" not in text:
        return {}
    body = text.split("---", 1)[1]
    body_lines = []
    for line in body.splitlines():
        stripped = line.strip()
        if (
            not stripped
            or stripped.startswith("@data/values")
            or stripped.startswith("#@data/values")
        ):
            continue
        body_lines.append(line)
    parsed = yaml.safe_load("\n".join(body_lines).strip())
    defaults = parsed or {}
    if not isinstance(defaults, dict):
        return {}

    aliases = _OVERLAY_SECTION_ALIASES.get(overlay_path.name.removesuffix(".overlay.yaml"), {})
    if not aliases:
        return defaults

    normalized: dict[str, Any] = {}
    for key, value in defaults.items():
        normalized[aliases.get(key, key)] = value
    return normalized


@lru_cache(maxsize=None)
def _overlay_registry(category: str | None = None) -> dict[str, OverlaySpec]:
    registry: dict[str, OverlaySpec] = {}
    for overlay_path in _overlay_file_paths(category):
        text = overlay_path.read_text(encoding="utf-8")
        component = _parse_overlay_comment_value(
            text, "Component"
        ) or _infer_overlay_component_name(overlay_path)
        defaults = _load_overlay_defaults(overlay_path)
        sections = tuple(defaults.keys()) if isinstance(defaults, dict) else ()
        dependencies = _parse_overlay_dependencies(text, component)
        registry[component] = OverlaySpec(
            path=overlay_path.resolve(),
            component=component,
            dependencies=dependencies,
            sections=sections,
            defaults=defaults if isinstance(defaults, dict) else {},
        )
    return registry


def resolve_overlay_chain(template_ref: str, category: str | None = None) -> list[OverlaySpec]:
    template_path = resolve_template_path(template_ref, category=category)
    if template_path is None:
        raise FileNotFoundError(f"Template not found: {template_ref}")

    overlay_path = template_path.with_name(f"{template_path.stem}.overlay.yaml")
    if category:
        category_overlay = templates_root_dir() / category / "overlays" / overlay_path.name
        if category_overlay.exists():
            overlay_path = category_overlay
    if not overlay_path.exists():
        return []

    registry = _overlay_registry(category)
    spec_by_path = {spec.path: spec for spec in registry.values()}
    root_spec = spec_by_path.get(overlay_path.resolve())
    if root_spec is None:
        # Fallback: parse the overlay directly if it is not in the registry cache.
        text = overlay_path.read_text(encoding="utf-8")
        component = _parse_overlay_comment_value(
            text, "Component"
        ) or _infer_overlay_component_name(overlay_path)
        defaults = _load_overlay_defaults(overlay_path)
        root_spec = OverlaySpec(
            path=overlay_path.resolve(),
            component=component,
            dependencies=_parse_overlay_dependencies(text, component),
            sections=tuple(defaults.keys()) if isinstance(defaults, dict) else (),
            defaults=defaults if isinstance(defaults, dict) else {},
        )

    ordered: list[OverlaySpec] = []
    visited: set[str] = set()

    def visit(spec: OverlaySpec) -> None:
        if spec.component in visited:
            return
        visited.add(spec.component)
        for dependency in spec.dependencies:
            dep_spec = registry.get(dependency)
            if dep_spec is not None:
                visit(dep_spec)
        ordered.append(spec)

    visit(root_spec)
    return ordered


def build_tnlcm_values(
    template_ref: str,
    data_values: dict[str, Any] | None = None,
    category: str | None = None,
) -> dict[str, Any]:
    data_values = data_values or {}
    merged: dict[str, Any] = {}

    # Track which top-level keys from data_values have been consumed by overlay sections
    consumed_sections: set[str] = set()

    for spec in resolve_overlay_chain(template_ref, category=category):
        merged = _merge_data(merged, spec.defaults)

        selected_sections: dict[str, Any] = {}
        for section in spec.sections:
            if isinstance(data_values, dict) and section in data_values:
                selected_sections[section] = data_values[section]
                consumed_sections.add(section)
        merged = _merge_data(merged, selected_sections)

    # If the caller provided additional top-level keys that do not match any
    # overlay section, merge them as well so user-provided values are not lost.
    if isinstance(data_values, dict):
        remaining: dict[str, Any] = {
            key: value for key, value in data_values.items() if key not in consumed_sections
        }
        if remaining:
            merged = _merge_data(merged, remaining)

    return merged


def overlay_editable_fields_for_template(
    template_ref: str, category: str | None = None
) -> dict[str, set]:
    """Return a mapping section -> set(field_names) for every field declared in the
    overlay chain, regardless of its default value.

    This helps callers know which keys from a provided `component` are allowed
    to override. A field is editable whether the overlay leaves it empty ("",
    a required field with no default) or ships a real default value (an
    optional field): whether it is actually mandatory or optional is decided
    later by COMPONENT_PARAMETER_MAPPING, not by this function.
    If no overlay is present for the template, returns an empty dict.
    """
    editable: dict[str, set] = {}
    try:
        chain = resolve_overlay_chain(template_ref, category=category)
    except FileNotFoundError:
        return {}

    for spec in chain:
        defaults = spec.defaults or {}
        if not isinstance(defaults, dict):
            continue
        for section, section_defaults in defaults.items():
            if not isinstance(section_defaults, dict):
                continue
            for key in section_defaults:
                editable.setdefault(section, set()).add(key)

    return editable
