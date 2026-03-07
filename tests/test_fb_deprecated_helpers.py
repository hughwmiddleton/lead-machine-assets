import pytest

import cross_directory_enricher as cde


def test_enrich_row_with_facebook_raises_deprecated_runtime_error():
    with pytest.raises(
        RuntimeError,
        match=r"enrich_row_with_facebook\(\).*_enrich_row_facebook",
    ):
        cde.enrich_row_with_facebook({}, None, None)
