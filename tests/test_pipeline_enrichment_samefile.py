import os
import shutil
import sys
import types

import pandas as pd

import pipeline_runner


def test_run_enrichment_avoids_samefile_copy(monkeypatch, tmp_path) -> None:
    raw_csv = tmp_path / "raw.csv"
    enriched_csv = tmp_path / "enriched.csv"
    pd.DataFrame([{"Artist Name": "Test Artist", "Email": "test@example.com"}]).to_csv(raw_csv, index=False)

    dummy_origin_validator = types.SimpleNamespace(
        run_auto_validate=lambda raw_csv_path, output_path, **_: (shutil.copyfile(raw_csv_path, output_path) or str(output_path))
    )
    dummy_final_checker = types.SimpleNamespace(run_final_checker=lambda path: path)

    monkeypatch.setitem(sys.modules, "origin_validator", dummy_origin_validator)
    monkeypatch.setitem(sys.modules, "final_checker", dummy_final_checker)

    result_path = pipeline_runner.run_enrichment(str(raw_csv), str(enriched_csv))

    assert os.path.exists(result_path)
    assert os.path.exists(enriched_csv)
