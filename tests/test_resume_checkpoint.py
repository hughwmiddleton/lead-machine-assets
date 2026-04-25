import pytest

from pipeline_runner import ResumeCheckpointError, build_resume_checkpoint


def test_resume_checkpoint_loads_last_complete_row(tmp_path):
    logs = []
    config = {"enrichment_mode": "row_linear", "enabled_sources": ["facebook"]}

    checkpoint = build_resume_checkpoint("input.csv", tmp_path.as_posix(), 4, config, logger=logs.append)
    checkpoint.append_completed(0, logger=logs.append)
    checkpoint.append_completed(1, logger=logs.append)

    loaded = build_resume_checkpoint("input.csv", tmp_path.as_posix(), 4, config, logger=logs.append)

    assert loaded.last_completed_row_index == 1
    assert loaded.resume_row_index == 2


def test_resume_checkpoint_ignores_final_incomplete_line(tmp_path):
    config = {"enrichment_mode": "row_linear", "enabled_sources": ["facebook"]}
    checkpoint = build_resume_checkpoint("input.csv", tmp_path.as_posix(), 4, config)
    checkpoint.append_completed(0)
    checkpoint.append_completed(1)
    with open(checkpoint.path, "a", encoding="utf-8") as handle:
        handle.write("2")

    loaded = build_resume_checkpoint("input.csv", tmp_path.as_posix(), 4, config)

    assert loaded.last_completed_row_index == 1


def test_resume_checkpoint_rejects_non_monotonic_sequence(tmp_path):
    config = {"enrichment_mode": "row_linear", "enabled_sources": ["facebook"]}
    checkpoint = build_resume_checkpoint("input.csv", tmp_path.as_posix(), 4, config)
    with open(checkpoint.path, "w", encoding="utf-8") as handle:
        handle.write("0\n2\n1\n")

    with pytest.raises(ResumeCheckpointError) as excinfo:
        build_resume_checkpoint("input.csv", tmp_path.as_posix(), 4, config)

    assert excinfo.value.reason == "non_monotonic_sequence"


def test_resume_checkpoint_rejects_row_count_mismatch(tmp_path):
    config = {"enrichment_mode": "row_linear", "enabled_sources": ["facebook"]}
    checkpoint = build_resume_checkpoint("input.csv", tmp_path.as_posix(), 4, config)
    checkpoint.append_completed(0)

    with pytest.raises(ResumeCheckpointError) as excinfo:
        build_resume_checkpoint("input.csv", tmp_path.as_posix(), 5, config)

    assert excinfo.value.reason == "row_count_mismatch"
