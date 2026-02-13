"""Minimal reproducible harness for T0XX/T0XY/T0XZ."""
import time

import pandas as pd

import night_mode_fb
import pipeline_runner
import night_mode_runner


def demo_quarantine():
    df = pd.DataFrame(
        [
            {"Artist Name": "Origin Artist", "Email": "repeat@example.com", "Email_All": "repeat@example.com", "__source_job": "job_a"},
            {"Artist Name": "Quarantined Artist", "Email": "repeat@example.com", "Email_All": "repeat@example.com", "__source_job": "job_b"},
        ]
    )
    result = night_mode_runner.quarantine_repeated_emails(df, min_repeats=1)
    print("== Quarantine result ==")
    print(result[["Artist Name", "Email", "Email_All", "Suspect_Email_All", "Email Source", "Needs_Review"]])


def demo_email_all_logging():
    df = pd.DataFrame(
        [
            {
                "Artist Name": "Logger",
                "Email_All": "a@example.com",
                "Directory_Email": "a@example.com",
                "Spotify_Email": "b@example.com",
            }
        ]
    )
    pipeline_runner._set_email_all(df, 0, ["a@example.com", "b@example.com"], source="harness", logger=print)
    print("== Email_All after merge ==", df.at[0, "Email_All"])


def demo_anchor_wait():
    state = {"count": 0}

    class DummyDriver:
        pass

    def fake_find_first(drv, selector):
        return "container"

    def fake_extract(container):
        state["count"] += 1
        anchors = list(range(0 if state["count"] == 1 else 2))
        return anchors, anchors

    night_mode_fb._find_first = fake_find_first  # type: ignore
    night_mode_fb._extract_anchor_hrefs = fake_extract  # type: ignore

    success, before, after, waited_ms = night_mode_fb._wait_for_anchor_population(
        DummyDriver(), "div[role='main']", min_anchors=1, timeout=0.5, poll_seconds=0.05, logger=print, context={"demo": True}
    )
    print("== Anchor wait ==", success, before, after, waited_ms)


if __name__ == "__main__":
    demo_quarantine()
    demo_email_all_logging()
    demo_anchor_wait()
