from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

_TEMPLATE_ALIAS_STEMS: dict[str, tuple[str, ...]] = {
    "base": (
        "base_tnlcm_descriptor",
        "tnlcm_descriptor_base",
        "base",
    ),
    "base_tnlcm_descriptor": (
        "base_tnlcm_descriptor",
        "tnlcm_descriptor_base",
        "base",
    ),
    "tnlcm_descriptor_base": (
        "base_tnlcm_descriptor",
        "tnlcm_descriptor_base",
        "base",
    ),
}

_OVERLAY_SECTION_ALIASES: dict[str, dict[str, str]] = {
    "vnet_sample_tnlcm_descriptor": {"vnet": "network"},
    "vm_kvm_sample_tnlcm_descriptor": {"vm_kvm": "vm"},
    "loadcore_agent_sample_tnlcm_descriptor_open5gs_vm": {"loadcore_agent": "loadcore"},
    "upf_p4_sw_sample_tnlcm_descriptor": {"upf_p4_sw": "upf"},
    "ueransim_both_sample_tnlcm_descriptor_open5gs_vm": {"ueransim_both": "ueransim"},
    "ueransim_split_sample_tnlcm_descriptor_open5gs_vm": {"ueransim_split": "ueransim"},
}


def _normalize_asset_key(value: str) -> str:
    name = Path(value).name.lower()
    for suffix in (".overlay.yaml", ".yaml", ".yml", ".json"):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
            break
    return re.sub(r"[-\s]+", "_", name)


def _asset_match_key(path: Path) -> str:
    name = path.name
    if name.endswith(".overlay.yaml"):
        name = name.removesuffix(".overlay.yaml")
    else:
        name = path.stem
    return _normalize_asset_key(name)


@dataclass(frozen=True)
class OverlaySpec:
    path: Path
    component: str
    dependencies: tuple[str, ...]
    sections: tuple[str, ...]
    defaults: dict[str, Any]


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def templates_root_dir() -> Path:
    return _repo_root() / "templates"


def _candidate_paths(template_ref: str, category: str | None = None) -> list[Path]:
    ref = Path(template_ref)
    candidates: list[Path] = []

    if ref.is_absolute():
        candidates.append(ref)
        return candidates

    if ref.exists():
        candidates.append(ref.resolve())

    root = templates_root_dir()
    if category:
        category_dir = root / category
        candidates.extend(
            [
                category_dir / "templates" / ref,
                category_dir / "templates" / ref.name,
                category_dir / ref,
                category_dir / ref.name,
                category_dir / "legacy" / ref.name,
            ]
        )

    candidates.extend(
        [
            root / ref,
            root / ref.name,
            _repo_root() / ref,
            _repo_root() / "examples" / ref.name,
            _repo_root() / "examples" / ref,
        ]
    )

    alias_stems = _TEMPLATE_ALIAS_STEMS.get(ref.stem, ())
    if alias_stems:
        alias_names: list[str] = []
        for alias_stem in alias_stems:
            alias_names.extend(
                [
                    f"{alias_stem}.yaml",
                    f"{alias_stem}.yml",
                    f"{alias_stem}",
                    f"TNLCM/{alias_stem}.yaml",
                    f"TNLCM/{alias_stem}.yml",
                ]
            )
        for alias_name in alias_names:
            alias_ref = Path(alias_name)
            if category:
                category_dir = root / category
                candidates.extend(
                    [
                        category_dir / "templates" / alias_ref,
                        category_dir / "templates" / alias_ref.name,
                        category_dir / alias_ref,
                        category_dir / alias_ref.name,
                        category_dir / "legacy" / alias_ref.name,
                    ]
                )
            candidates.extend(
                [
                    root / alias_ref,
                    root / alias_ref.name,
                    _repo_root() / alias_ref,
                    _repo_root() / "examples" / alias_ref.name,
                    _repo_root() / "examples" / alias_ref,
                ]
            )

    seen: set[str] = set()
    unique_candidates: list[Path] = []
    for candidate in candidates:
        key = str(candidate.resolve() if candidate.exists() else candidate)
        if key in seen:
            continue
        seen.add(key)
        unique_candidates.append(candidate)
    return unique_candidates


def resolve_template_path(template_ref: str, category: str | None = None) -> Path | None:
    for candidate in _candidate_paths(template_ref, category=category):
        if candidate.exists() and candidate.is_file() and not candidate.name.endswith(".overlay.yaml"):
            return candidate.resolve()

    if category:
        root = templates_root_dir()
        category_dir = root / category
        if category_dir.exists():
            ref_norm = _normalize_asset_key(Path(template_ref).name or Path(template_ref).stem)
            search_dirs = [category_dir]
            templates_dir = category_dir / "templates"
            if templates_dir.exists():
                search_dirs.insert(0, templates_dir)
            legacy_dir = category_dir / "legacy"
            if legacy_dir.exists():
                search_dirs.append(legacy_dir)

            matches: list[Path] = []
            for search_dir in search_dirs:
                for path in search_dir.rglob("*"):
                    if not path.is_file():
                        continue
                    if path.name.endswith(".overlay.yaml"):
                        continue
                    if path.suffix.lower() not in {".yaml", ".yml", ".json"}:
                        continue
                    candidate_norm = _asset_match_key(path)
                    if candidate_norm == ref_norm or candidate_norm.startswith(ref_norm) or ref_norm.startswith(candidate_norm):
                        matches.append(path.resolve())

            if matches:
                matches.sort(key=lambda item: (len(item.name), str(item)))
                return matches[0]

    return None


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
    if name.endswith("_new"):
        return name.removesuffix("_new")
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
        if not stripped or stripped.startswith("@data/values") or stripped.startswith("#@data/values"):
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
        component = _parse_overlay_comment_value(text, "Component") or _infer_overlay_component_name(overlay_path)
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
        component = _parse_overlay_comment_value(text, "Component") or _infer_overlay_component_name(overlay_path)
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


def overlay_editable_fields_for_template(template_ref: str, category: str | None = None) -> dict[str, set]:
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


def render_with_ytt(values: dict[str, Any] | None, template_ref: str, category: str | None = None) -> str:
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
