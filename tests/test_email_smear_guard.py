import pandas as pd

from night_mode_runner import _coalesce_emails, _guard_against_email_smear


def test_guard_against_email_smear_clears_cross_job_rows():
    email = "x@example.com"

    job_a_rows = [
        {"Artist Name": f"a{i}", "Email": "", "Email_All": "", "__source_job": "job_a"}
        for i in range(10)
    ]
    job_b_rows = [
        {
            "Artist Name": f"b{i}",
            "Email": email if i == 0 else "",
            "Email_All": email if i == 0 else "",
            "__source_job": "job_b",
        }
        for i in range(6)
    ]

    combined = _coalesce_emails(pd.DataFrame(job_a_rows + job_b_rows))

    # Simulate smear: copy the email onto all rows of job_b and a couple of job_a rows.
    smeared = combined.copy()
    smeared.loc[smeared["__source_job"] == "job_b", ["Email", "Email_All"]] = email
    smeared.loc[smeared.index[:2], ["Email", "Email_All"]] = email

    guarded = _guard_against_email_smear(smeared, logger=None, min_repeats=5)

    job_a_mask = guarded["__source_job"] == "job_a"
    job_b_mask = guarded["__source_job"] == "job_b"

    # Smear should be cleared from job A.
    assert (guarded.loc[job_a_mask, "Email"] == "").all()
    assert (guarded.loc[job_a_mask, "Email_All"] == "").all()

    # Origin job retains the email.
    assert (guarded.loc[job_b_mask, "Email"] == email).all()
    assert (guarded.loc[job_b_mask, "Email_All"] == email).all()

    # Cleared rows keep suspect markers and review flag.
    suspect_rows = guarded.loc[job_a_mask].head(2)
    assert (suspect_rows["Suspect_Email"] == email).all()
    assert suspect_rows["Suspect_Email_All"].str.contains(email).all()
    assert (suspect_rows["Needs_Review"] == "TRUE").all()
    assert (suspect_rows["Email Source"] == "Quarantined (smear guard)").all()
