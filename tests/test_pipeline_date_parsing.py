import warnings

import pipeline_runner

def test_normalize_date_string_handles_iso_and_au_without_warning():
    with warnings.catch_warnings(record=True) as record:
        warnings.simplefilter("always")
        iso = pipeline_runner._normalize_date_string("2026-02-24")
        au = pipeline_runner._normalize_date_string("24/02/2026")
    assert iso == "2026-02-24"
    assert au == "2026-02-24"
    assert not record
