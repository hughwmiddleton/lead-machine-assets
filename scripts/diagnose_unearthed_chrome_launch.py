#!/usr/bin/env python3
"""Standalone probe for bisecting legacy Unearthed Chrome launch options."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence

from selenium import webdriver
from selenium.webdriver.chrome.options import Options


LEGACY_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


@dataclass(frozen=True)
class ProbeCase:
    name: str
    args: tuple[str, ...]
    page_load_strategy: str | None = None


def build_options(args: Sequence[str], page_load_strategy: str | None) -> Options:
    opts = Options()
    for arg in args:
        opts.add_argument(arg)
    if page_load_strategy is not None:
        opts.page_load_strategy = page_load_strategy
    return opts


def format_exception(exc: Exception) -> str:
    message = " ".join(str(exc).split())
    return f"{type(exc).__name__}: {message}" if message else type(exc).__name__


def run_probe_case(case: ProbeCase) -> bool:
    opts = build_options(case.args, case.page_load_strategy)
    strategy_label = case.page_load_strategy or "default"

    print(f"=== {case.name} ===")
    print(f"args: {list(opts.arguments)}")
    print(f"page_load_strategy: {strategy_label}")

    driver = None
    try:
        driver = webdriver.Chrome(options=opts)
        print("result: SUCCESS")
        return True
    except Exception as exc:  # Diagnostic script: keep failure reporting generic.
        print("result: FAILURE")
        print(f"exception: {format_exception(exc)}")
        return False
    finally:
        if driver is not None:
            driver.quit()


def build_staged_cases() -> list[ProbeCase]:
    cases: list[ProbeCase] = []
    args: list[str] = []
    strategy: str | None = None

    def add_case(
        name: str,
        new_args: Iterable[str] = (),
        new_strategy: str | None = None,
    ) -> None:
        nonlocal strategy
        args.extend(new_args)
        if new_strategy is not None:
            strategy = new_strategy
        cases.append(ProbeCase(name=name, args=tuple(args), page_load_strategy=strategy))

    add_case(
        "Stage 0 - known-good baseline",
        ("--no-sandbox", "--disable-dev-shm-usage"),
    )
    add_case(
        "Stage 1 - ordinary benign legacy flags",
        ("--disable-gpu", "--window-size=1440,900", "--disable-extensions"),
    )
    add_case("Stage 2 - headless mode currently used by legacy bootstrap", ("--headless",))
    add_case(
        "Stage 3 - stealth flag only",
        ("--disable-blink-features=AutomationControlled",),
    )
    add_case("Stage 4 - language override", ("--lang=en-US,en",))
    add_case("Stage 5 - page load capability", new_strategy="eager")
    add_case(
        "Stage 6 - stale user-agent override only",
        (f"--user-agent={LEGACY_UA}",),
    )
    return cases


def build_combo_cases() -> list[ProbeCase]:
    baseline = ("--no-sandbox", "--disable-dev-shm-usage")
    headless = ("--headless",)
    stealth = ("--disable-blink-features=AutomationControlled",)
    ua = (f"--user-agent={LEGACY_UA}",)

    full_legacy_bundle = (
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--disable-gpu",
        "--window-size=1440,900",
        "--disable-extensions",
        "--headless",
        "--disable-blink-features=AutomationControlled",
        "--lang=en-US,en",
        f"--user-agent={LEGACY_UA}",
    )

    return [
        ProbeCase(
            name="Combo A - baseline + headless + stealth flag + UA override",
            args=baseline + headless + stealth + ua,
        ),
        ProbeCase(
            name="Combo B - full remaining legacy pre-launch bundle",
            args=full_legacy_bundle,
            page_load_strategy="eager",
        ),
    ]


def main() -> None:
    first_failing_stage: str | None = None

    for case in build_staged_cases():
        success = run_probe_case(case)
        print()
        if not success:
            first_failing_stage = case.name
            break

    for case in build_combo_cases():
        run_probe_case(case)
        print()

    if first_failing_stage is not None:
        print(f"CONCLUSION: first failing stage = {first_failing_stage}")
    else:
        print("CONCLUSION: no isolated launch failure reproduced")


if __name__ == "__main__":
    main()
