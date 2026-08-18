"""Resolucion de rutas de plantillas dentro de `templates/`.

Capa mas baja de `rendering`: solo sabe encontrar ficheros en disco. No parsea
overlays ni invoca a `ytt`, asi que no depende de ningun otro modulo del paquete
y todos los demas pueden apoyarse en el sin riesgo de import circular.
"""

from __future__ import annotations

import re
from pathlib import Path

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


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def templates_root_dir() -> Path:
    return _repo_root() / "templates"


def elcm_testcase_dir() -> Path:
    """Biblioteca de TestCases y UEs de ELCM (`templates/ELCM/TestCase/`).

    Es de donde `testcase_paths`/`ues_paths` resuelven por nombre de fichero.
    Como el resto de `templates/`, cuelga de la raiz del repositorio y no de
    `cwd`: el servicio resuelve igual se arranque desde donde se arranque.
    """
    return templates_root_dir() / "ELCM" / "TestCase"


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
        if (
            candidate.exists()
            and candidate.is_file()
            and not candidate.name.endswith(".overlay.yaml")
        ):
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
                    if (
                        candidate_norm == ref_norm
                        or candidate_norm.startswith(ref_norm)
                        or ref_norm.startswith(candidate_norm)
                    ):
                        matches.append(path.resolve())

            if matches:
                matches.sort(key=lambda item: (len(item.name), str(item)))
                return matches[0]

    return None
