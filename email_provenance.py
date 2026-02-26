from typing import Any, MutableMapping
import pandas as pd


def _set_email_with_provenance(
    target,
    email: str,
    source_url: str = "",
    source_type: str = "",
    method: str = "regex",
) -> None:
    email_clean = (email or "").strip()
    if not email_clean:
        return

    # Dict row
    if isinstance(target, MutableMapping):
        target["Email"] = email_clean

        if not str(target.get("Email_All", "")).strip():
            target["Email_All"] = email_clean

        if source_url and not str(target.get("Email_Source_URL", "")).strip():
            target["Email_Source_URL"] = source_url

        if source_type and not str(target.get("Email_Source_Type", "")).strip():
            target["Email_Source_Type"] = source_type

        if method and not str(target.get("Email_Extract_Method", "")).strip():
            target["Email_Extract_Method"] = method

        return

    # DataFrame row
    if (
        isinstance(target, tuple)
        and len(target) == 2
        and isinstance(target[0], pd.DataFrame)
    ):
        df, idx = target

        if "Email" not in df.columns:
            df["Email"] = ""
        df.at[idx, "Email"] = email_clean

        if "Email_All" not in df.columns:
            df["Email_All"] = ""
        if not str(df.at[idx, "Email_All"]).strip():
            df.at[idx, "Email_All"] = email_clean

        if source_url:
            if "Email_Source_URL" not in df.columns:
                df["Email_Source_URL"] = ""
            if not str(df.at[idx, "Email_Source_URL"]).strip():
                df.at[idx, "Email_Source_URL"] = source_url

        if source_type:
            if "Email_Source_Type" not in df.columns:
                df["Email_Source_Type"] = ""
            if not str(df.at[idx, "Email_Source_Type"]).strip():
                df.at[idx, "Email_Source_Type"] = source_type

        if method:
            if "Email_Extract_Method" not in df.columns:
                df["Email_Extract_Method"] = ""
            if not str(df.at[idx, "Email_Extract_Method"]).strip():
                df.at[idx, "Email_Extract_Method"] = method
