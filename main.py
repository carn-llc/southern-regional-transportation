#!/usr/bin/env python3
"""Assign bus drivers to weekly trips from a pull PDF.

This is the program the transportation manager runs each week. It reads the pull
PDF, checks that the data came out cleanly, assigns drivers to trips by seniority,
and writes the results to a text file you can open and print.

There are three separate pulls each week — the weekdays (Monday–Friday), Saturday,
and Sunday — and each one starts from its own seniority number. The program asks
you for all three starting numbers up front, then reads the PDF once and assigns
each pull on its own.

Run it either way:

    python main.py "Blank Pull TEST.pdf"   # PDF path on the command line
    python main.py                         # asks you for the PDF path too

By default the seniority numbers are read off the PDF. If you'd rather use an exact
list, give a seniority CSV and those numbers are used instead (drivers are matched to
it by name, so a dropped or misread number on the sheet no longer matters):

    python main.py "Blank Pull TEST.pdf" -s seniorities.csv

The CSV needs a header row, e.g.:  First Name, Last Name, Seniority

If the data does not read cleanly, the program stops and tells you exactly what to
check in the PDF — it will not produce a schedule from numbers it could not read.
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import date
from itertools import groupby
from typing import Dict, List

from trip_assignment import (
    Driver,
    Pull,
    format_validation,
    load_seniority_roster,
    pre_pull_data_extraction,
    resolve_dropped_seniority,
    resolve_unmatched_signup,
    run_pull,
    split_into_pulls,
    validate_extraction,
)


# ---------------------------------------------------------------------------
# Gathering the operator's inputs
# ---------------------------------------------------------------------------

# Each weekly pull and the friendly label shown when asking for its start number.
_PULL_PROMPTS = (
    ("weekday",  "Weekdays (Monday–Friday)"),
    ("saturday", "Saturday"),
    ("sunday",   "Sunday"),
)


def _prompt_seniority(label: str) -> int:
    """Ask for one pull's starting seniority number, re-asking until it's valid."""
    while True:
        raw = input(f"  Starting seniority number for {label}: ").strip()
        try:
            value = int(raw)
        except ValueError:
            print(f"    '{raw}' is not a whole number — please type a number like 27.")
            continue
        if value < 1:
            print("    The starting seniority number must be 1 or greater.")
            continue
        return value


def _resolve_dropped_seniorities(raw: Pull) -> None:
    """Ask the operator for the number of any sign-up whose seniority OCR dropped.

    Only drops that could not be matched to a driver by name reach here — without a
    number they can't be placed in the seniority rotation. Each driver's sign-ups are
    shown and a number requested; the operator may press Enter to skip, leaving that
    sign-up to fall to substitutes. Auto-matched drops are left for the data check to
    surface for confirmation, not re-asked here.
    """
    unresolved = [d for d in raw.dropped_seniorities if d.matched_id is None]
    if not unresolved:
        return

    print("\n" + "-" * 70)
    print("SENIORITY NUMBERS THAT DID NOT READ")
    print("-" * 70)
    print(f"{len(unresolved)} driver(s) signed up but their seniority number could not "
          f"be read,\nand the name appears nowhere else on the sheet. Enter each "
          f"driver's number\nso they can be placed by seniority (or press Enter to skip).")

    for ds in unresolved:
        print(f"\n  Driver: {ds.name or '(no name read)'}")
        for t in ds.trips:
            when = t.date.strftime("%A") if t.date else t.day
            print(f"    signed up: {when} – {t.destination} @ {_fmt_time(t.depart_time)}")
        while True:
            raw_in = input(f"  Seniority number for {ds.name or 'this driver'} "
                           f"(Enter to skip): ").strip()
            if not raw_in:
                print("    Skipped — this sign-up will go to substitutes.")
                break
            try:
                value = int(raw_in)
            except ValueError:
                print(f"    '{raw_in}' is not a whole number — try again, or press "
                      f"Enter to skip.")
                continue
            if value < 1:
                print("    The seniority number must be 1 or greater.")
                continue
            resolve_dropped_seniority(raw, ds, value)
            print(f"    Recorded {ds.name} as #{value}.")
            break


def _resolve_unmatched_signups(raw: Pull, roster: List[Driver]) -> None:
    """Ask the operator for the number of any sign-up whose name isn't on the roster.

    Roster mode only. Each sign-up read from the PDF is placed by matching its name to
    the uploaded list; the ones that matched nobody land here. The driver's sign-ups
    are shown and a number requested; the operator may press Enter to skip, leaving
    that sign-up to fall to substitutes.
    """
    unresolved = [u for u in raw.unmatched_signups if not u.resolved]
    if not unresolved:
        return

    print("\n" + "-" * 70)
    print("SIGN-UPS NOT ON THE SENIORITY LIST")
    print("-" * 70)
    print(f"{len(unresolved)} sign-up name(s) on the PDF did not match anyone on the "
          f"uploaded\nseniority list (a spelling difference or a name the OCR misread). "
          f"Enter\neach driver's number so they can be placed (or press Enter to skip).")

    for us in unresolved:
        print(f"\n  Sign-up name (from PDF): {us.name or '(no name read)'}")
        for t in us.trips:
            when = t.date.strftime("%A") if t.date else t.day
            print(f"    signed up: {when} – {t.destination} @ {_fmt_time(t.depart_time)}")
        while True:
            raw_in = input(f"  Seniority number for {us.name or 'this driver'} "
                           f"(Enter to skip): ").strip()
            if not raw_in:
                print("    Skipped — this sign-up will go to substitutes.")
                break
            try:
                value = int(raw_in)
            except ValueError:
                print(f"    '{raw_in}' is not a whole number — try again, or press "
                      f"Enter to skip.")
                continue
            if value < 1:
                print("    The seniority number must be 1 or greater.")
                continue
            driver = resolve_unmatched_signup(raw, us, value, roster)
            print(f"    Recorded as #{value} "
                  f"({driver.first_name} {driver.last_name}).".replace("  ", " "))
            break


def _get_inputs() -> tuple[str, Dict[str, int], str | None, str | None]:
    """Read the PDF path, the seniority roster (if any), and the three start numbers.

    The PDF path and an optional seniority CSV may come from the command line; the
    three seniority start numbers are always asked for here, up front, before the
    (slow) PDF read begins.
    """
    parser = argparse.ArgumentParser(
        description="Assign bus drivers to weekly trips from a pull PDF.")
    parser.add_argument("pdf", nargs="?", help="Path to the pull PDF")
    parser.add_argument("-s", "--seniority",
                        help="Path to a seniority list CSV to use instead of the "
                             "numbers printed on the PDF")
    parser.add_argument("-o", "--output",
                        help="Where to save the results (default: next to the PDF)")
    args = parser.parse_args()

    pdf = args.pdf or input("Path to the pull PDF: ").strip().strip('"').strip("'")
    if not pdf:
        sys.exit("No PDF given. Nothing to do.")
    if not os.path.isfile(pdf):
        sys.exit(f"Could not find a file at: {pdf}\nCheck the path and try again.")

    seniority = args.seniority
    if seniority is None:
        seniority = input(
            "Path to a seniority list CSV (or press Enter to read the seniority "
            "numbers from the PDF): ").strip().strip('"').strip("'") or None
    if seniority and not os.path.isfile(seniority):
        sys.exit(f"Could not find a file at: {seniority}\nCheck the path and try again.")

    print("\nEach pull starts from its own seniority number:")
    starting_seniorities = {
        pull_type: _prompt_seniority(label) for pull_type, label in _PULL_PROMPTS
    }

    return pdf, starting_seniorities, seniority, args.output


# ---------------------------------------------------------------------------
# Formatting the results for a human
# ---------------------------------------------------------------------------

def _fmt_time(t) -> str:
    return t.strftime("%I:%M %p").lstrip("0")


def _week_range(pull: Pull) -> str:
    dates: List[date] = [t.date for t in pull.trips if t.date]
    if not dates:
        return "unknown dates"
    lo, hi = min(dates), max(dates)
    return f"{lo:%A, %B %d} through {hi:%A, %B %d, %Y}"


def _driver_name(driver) -> str:
    name = f"{driver.first_name} {driver.last_name}".strip()
    return f"{name} (#{driver.seniority_id})"


def _format_assignment_log(pull: Pull) -> List[str]:
    """Raw, in-order log of every assignment as it was made (debug/informational).

    One row per assignment in the exact order run_pull made them: the driver's
    seniority number, the driver, whether it was their first choice, then the
    trip they got (day, name, depart time).
    """
    lines: List[str] = []
    lines.append("-" * 70)
    lines.append("ASSIGNMENT LOG (in order assigned)")
    lines.append("-" * 70)
    lines.append(f"  {'SENIORITY':<9}  {'DRIVER':<28}  {'FC':<9}  TRIP")
    for e in pull.assignment_log:
        trip = f"{e.trip.day} – {e.trip.destination} @ {_fmt_time(e.trip.depart_time)}"
        fc = f"FC: {e.is_first_choice}"
        lines.append(f"  {e.driver.seniority_id:<9}  {_driver_name(e.driver):<28}  {fc:<9}  {trip}")
    if not pull.assignment_log:
        lines.append("  (no assignments were made)")
    lines.append("")
    return lines


def format_assignments(pull: Pull) -> str:
    """Render the finished assignments as a printable report."""
    lines: List[str] = _format_assignment_log(pull)
    lines.append("=" * 70)
    lines.append(f"TRIP ASSIGNMENTS — {pull.pull_type} pull, "
                 f"starting at seniority #{pull.starting_seniority}")
    lines.append(f"Week of {_week_range(pull)}")
    lines.append("=" * 70)

    # Trips grouped by day (pull.trips is already in chronological order).
    for day, trips in groupby(pull.trips, key=lambda t: (t.date, t.day)):
        trip_date, day_name = day
        header = f"{day_name}, {trip_date:%B %d}" if trip_date else day_name
        lines.append("")
        lines.append(header)
        for t in trips:
            assigned = ", ".join(_driver_name(d) for d in t.assigned_drivers)
            filled, total = len(t.assigned_drivers), t.buses_total
            short = total - filled
            flag = f"   ** {short} bus(es) UNFILLED — needs substitute **" if short > 0 else ""
            lines.append(f"  {_fmt_time(t.depart_time)} – {_fmt_time(t.return_time)}  "
                         f"{t.destination} ({t.group})")
            lines.append(f"      buses {filled}/{total} filled: "
                         f"{assigned or '(no one assigned)'}{flag}")

    # Trips that still need substitutes, collected in one place.
    short_trips = [t for t in pull.trips if t.buses_total - len(t.assigned_drivers) > 0]
    lines.append("")
    lines.append("-" * 70)
    lines.append("TRIPS NEEDING SUBSTITUTES")
    if short_trips:
        for t in short_trips:
            short = t.buses_total - len(t.assigned_drivers)
            lines.append(f"  {t.date} {t.destination}: "
                         f"{short} of {t.buses_total} bus(es) unfilled")
    else:
        lines.append("  None — every bus was filled.")

    # Every driver and what they got (including those with nothing this week).
    lines.append("")
    lines.append("-" * 70)
    lines.append("BY DRIVER")
    for d in pull.drivers:
        got = [f"{t.day[:3]} {t.destination} ({_fmt_time(t.depart_time)})"
               for t in d.assigned_trips]
        lines.append(f"  #{d.seniority_id:<3} {d.first_name} {d.last_name}: "
                     f"{', '.join(got) if got else '— none —'}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main flow
# ---------------------------------------------------------------------------

def main() -> None:
    pdf, starting_seniorities, seniority, output = _get_inputs()

    # Optional: an uploaded seniority list. When given, its numbers are used instead of
    # the ones printed on the PDF (drivers are matched to it by name).
    roster: List[Driver] | None = None
    if seniority:
        try:
            roster = load_seniority_roster(seniority)
        except (ValueError, OSError) as e:
            sys.exit(f"Could not read the seniority list: {e}")
        print(f"\nUsing seniority list {os.path.basename(seniority)} "
              f"({len(roster)} drivers).")

    print(f"\nReading {os.path.basename(pdf)} … (this can take a minute)")
    raw = pre_pull_data_extraction(pdf, roster=roster)
    print(f"Read {len(raw.trips)} trips and {len(raw.drivers)} drivers.")

    # Recover sign-ups that couldn't be placed, by asking the operator before anything
    # is assigned. Done before validation so the data check reflects what was resolved.
    # In roster mode that's names absent from the list; otherwise it's OCR-dropped
    # seniority numbers that couldn't be matched to a driver by name.
    if roster is not None:
        _resolve_unmatched_signups(raw, roster)
    else:
        _resolve_dropped_seniorities(raw)

    # Safety gate: never assign from data that did not read cleanly. Validate the
    # whole extraction once, before it is split into the three pulls.
    issues = validate_extraction(raw)
    print("\n" + "-" * 70)
    print("DATA CHECK")
    print("-" * 70)
    print(format_validation(issues))

    if any(i.severity == "error" for i in issues):
        print("\nStopping: the problem(s) above must be fixed before a schedule can "
              "be trusted.\nCheck those trips in the PDF, then run this again.")
        sys.exit(1)

    # Three separate pulls — weekdays, Saturday, Sunday — each assigned on its own
    # from its own seniority start point.
    pulls = split_into_pulls(raw, starting_seniorities)
    sections = []
    for pull in pulls:
        run_pull(pull)
        sections.append(format_assignments(pull))
    report = ("\n\n" + "#" * 70 + "\n\n").join(sections)

    print("\n" + report)

    out_path = output or (os.path.splitext(pdf)[0] + " - assignments.txt")
    with open(out_path, "w") as f:
        f.write(report + "\n")
    print(f"\nSaved results to: {out_path}")


if __name__ == "__main__":
    main()
