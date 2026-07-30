import zipfile

import pytest

from app.utils.results_bundle import extract_csv_bundle


def test_extract_csv_bundle_keeps_only_csv(tmp_path):
    # ZIP interno con el CSV nombrado con barra inicial, tal como lo genera ELCM
    # (Compress.Zip sin flat -> archiveName = "/csv_query_<id>.csv").
    inner_zip = tmp_path / "dataset_9.zip"
    with zipfile.ZipFile(inner_zip, "w") as zf:
        zf.writestr("/csv_query_9.csv", "time,value\n1,2\n")

    # ZIP externo plano: logs + ZIP interno (como Compress.Zip flat=True).
    outer = tmp_path / "9.zip"
    with zipfile.ZipFile(outer, "w") as zf:
        zf.writestr("PreRun.log", "prerun\n")
        zf.writestr("Executor.log", "exec\n")
        zf.write(inner_zip, "dataset_9.zip")
    inner_zip.unlink()

    dest = tmp_path / "result"
    csv_files = extract_csv_bundle(outer, dest)

    # Solo queda el CSV: logs borrados y ZIP interno extraído + borrado.
    names = sorted(p.name for p in dest.iterdir())
    assert names == ["csv_query_9.csv"]
    assert [p.name for p in csv_files] == ["csv_query_9.csv"]
    assert (dest / "csv_query_9.csv").read_text(encoding="utf-8").startswith("time,value")


def test_extract_csv_bundle_rejects_non_zip(tmp_path):
    not_zip = tmp_path / "nope.zip"
    not_zip.write_text("not a zip", encoding="utf-8")

    with pytest.raises(ValueError):
        extract_csv_bundle(not_zip, tmp_path / "out")


def test_extract_csv_bundle_neutralizes_zip_slip(tmp_path):
    # Una entrada con '..' no debe escribir fuera del destino: zipfile la sanea
    # dejándola DENTRO de dest_dir.
    evil = tmp_path / "evil.zip"
    with zipfile.ZipFile(evil, "w") as zf:
        zf.writestr("../escape.csv", "x")

    dest = tmp_path / "result"
    csv_files = extract_csv_bundle(evil, dest)

    assert not (tmp_path / "escape.csv").exists()
    assert (dest / "escape.csv").exists()
    assert [p.name for p in csv_files] == ["escape.csv"]
