from __future__ import annotations

import csv
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time
from typing import Dict, List, Optional

import pdf2image
import pytesseract
from PIL import Image, ImageChops, ImageOps
from pytesseract import Output


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class Trip:
    date:             date
    day:              str
    destination:      str
    group:            str
    depart_time:      time
    return_time:      time
    buses_remaining:  int
    buses_total:      int
    # Position of this trip in the PDF, top-to-bottom across pages (0-based). Trips
    # are assigned in this order within a day, NOT by departure time, because the
    # operator works the pull sheet in the order the trips are printed on it.
    pdf_order:        int = 0
    assigned_drivers: List[Driver] = field(default_factory=list)


@dataclass
class Driver:
    first_name:        str
    last_name:         str
    seniority_id:      int
    trip_selections:   List[Trip]     = field(default_factory=list)
    # The single first-choice trip relevant to the pull this Driver belongs to.
    # On the raw extraction record it stays None; split_into_pulls sets it on each
    # per-pull copy from first_choice_trips below.
    first_choice_trip: Optional[Trip] = None
    # Every trip this driver marked as a first choice, across all days. A driver can
    # name a first choice in each pull (weekday/Saturday/Sunday), so first choice is
    # collected as a list here and narrowed to one per pull when the pulls are built.
    first_choice_trips: List[Trip]    = field(default_factory=list)
    assigned_trips:    List[Trip]     = field(default_factory=list)


@dataclass
class Pull:
    pull_type:          str
    starting_seniority: int          = 0
    trips:              List[Trip]   = field(default_factory=list)
    drivers:            List[Driver] = field(default_factory=list)
    # Chronological record of every assignment made by run_pull, in the order it
    # happened. Used for the informational/debug log at the top of the report.
    assignment_log:     List[AssignmentEvent] = field(default_factory=list)
    # Sign-ups whose seniority number OCR dropped: each is auto-matched to a driver
    # by name where possible, else left for the operator to resolve before the pull.
    dropped_seniorities: List[DroppedSeniority] = field(default_factory=list)
    # Sign-ups read from the PDF whose name matched no one in an uploaded seniority
    # roster (roster mode only). Surfaced for the operator to correct or supply.
    unmatched_signups:   List[UnmatchedSignup] = field(default_factory=list)


@dataclass
class AssignmentEvent:
    """One driver-gets-a-trip event, logged in the order assignments are made.

    priority is the driver's 1-based rank in the seniority rotation (rank 1 picks
    first), which is what decides who gets a trip when buses are scarce.
    is_first_choice records whether the trip was the driver's first-choice pick
    (True) or a fallback selection (False).
    """
    priority:        int
    driver:          Driver
    trip:            Trip
    is_first_choice: bool = False


@dataclass
class DroppedSeniority:
    """A sign-up whose seniority number OCR failed to read on the row it appeared on.

    The driver is named but un-numbered, so they can't be keyed during extraction.
    matched_id is the number recovered afterward by matching the name against the
    rest of the sheet; it stays None when the name appears nowhere else, in which
    case the operator is asked to supply the number before the pull is run. manual
    records that the number came from the operator rather than from a name match,
    so validation knows it needs no further confirmation.
    """
    first_name:         str
    last_name:          str
    trips:              List[Trip]    = field(default_factory=list)
    first_choice_trips: List[Trip]    = field(default_factory=list)
    matched_id:         Optional[int] = None
    manual:             bool          = False

    @property
    def name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()


@dataclass
class UnmatchedSignup:
    """A PDF sign-up whose name matched no one in an uploaded seniority roster.

    In roster mode the authoritative seniority number comes from the uploaded list,
    not the PDF, so each sign-up is placed by matching its (OCR-read) name to a roster
    driver. When the name matches nobody — a roster spelling gap, an OCR misread, or
    someone simply absent from the roster — the sign-up can't be placed and is held
    here. resolved records that the operator has since supplied the correct number, so
    validation knows not to flag it.
    """
    first_name:         str
    last_name:          str
    trips:              List[Trip] = field(default_factory=list)
    first_choice_trips: List[Trip] = field(default_factory=list)
    resolved:           bool       = False

    @property
    def name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()


@dataclass
class ValidationIssue:
    """A single problem found in extracted data, written for a non-technical reader.

    severity is "error" (the output cannot be trusted until checked) or "warning"
    (probably fine, but worth a glance against the PDF).
    """
    severity: str
    message:  str


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_WEEKDAYS = {"Monday", "Tuesday", "Wednesday", "Thursday", "Friday"}
_ALL_DAYS  = _WEEKDAYS | {"Saturday", "Sunday"}

# The three independent weekly pulls. Each is assigned separately, walking its own
# days' trips from its own seniority start point until those trips are exhausted.
_PULL_CATEGORIES = (
    ("weekday",  _WEEKDAYS),
    ("saturday", {"Saturday"}),
    ("sunday",   {"Sunday"}),
)

_DATE_RE          = re.compile(r'^\d{1,2}/\d{1,2}/\d{4}$')
_DRIVER_HEADER_RE = re.compile(r'Driver\s*First\s*Name', re.IGNORECASE)
_BOOL_RE          = re.compile(r'^(True|False)$', re.IGNORECASE)
_TIME_HHMM_RE     = re.compile(r'^\d{1,2}:\d{2}$')
_TIME_PATTERN_RE  = re.compile(r'\d{1,2}:\d{2}')
_RUN_DATE_RE      = re.compile(r'Post\s+Selections?\s+Runs?\s+for\s+(\d{1,2}/\d{1,2}/\d{4})', re.IGNORECASE)
_DEST_HDR_RE      = re.compile(r'\bdestination\b', re.IGNORECASE)
_BUSES_HDR_RE     = re.compile(r'^buses', re.IGNORECASE)
_RETURN_HDR_RE    = re.compile(r'^return$', re.IGNORECASE)
# Last-resort fraction of page width for the bus column, used only if neither a
# 'Buses' nor a 'Return' header can be located on the page to anchor it.
_BUS_COL_WIDTH_FRAC = 0.80


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _clean(row: List[Optional[str]]) -> List[str]:
    return [c.strip() if c else "" for c in row]


def _normalize_dest(s: str) -> str:
    """Collapse a destination string to a comparison key, ignoring OCR punctuation/spacing.

    'Winding River Rink-TR', 'Winding River Rink TR' and 'Winding River Rink- TR'
    all normalize to the same value, so a trip's roster split across a page break
    can be recognized as one trip.
    """
    return re.sub(r'[^a-z0-9]', '', s.lower())


def _strip_pipes(cells: List[str]) -> List[str]:
    """Remove OCR pipe-character artifacts from column borders and drop blank cells."""
    result = []
    for c in cells:
        s = re.sub(r'^[\|\s]+', '', c).strip()
        if s:
            result.append(s)
    return result


def _is_run_date_row(cells: List[str]) -> bool:
    return any(_RUN_DATE_RE.search(c) for c in cells)


def _extract_run_date(cells: List[str]) -> Optional[date]:
    for c in cells:
        m = _RUN_DATE_RE.search(c)
        if m:
            return datetime.strptime(m.group(1), "%m/%d/%Y").date()
    return None


def _is_destination_header(cells: List[str]) -> bool:
    return any(_DEST_HDR_RE.search(c) for c in cells)


def _is_trip_data_row(cells: List[str]) -> bool:
    """A trip data row has a destination + group + two time values."""
    stripped = _strip_pipes(cells)
    return (
        len(stripped) >= 4
        and any(_TIME_PATTERN_RE.search(c) for c in stripped)
        and not _is_driver_header(stripped)
        and not _is_destination_header(stripped)
    )


def _is_driver_header(cells: List[str]) -> bool:
    return any(_DRIVER_HEADER_RE.search(c) for c in cells)


def _is_driver_data(cells: List[str]) -> bool:
    # Last cell = true/false; second-to-last = date selected (MM/DD/YYYY).
    # Seniority ID (third-from-last) may occasionally be dropped by OCR;
    # the date-selected pattern is a stronger anchor.
    return (
        len(cells) >= 4
        and bool(_BOOL_RE.match(cells[-1]))
        and bool(_DATE_RE.match(cells[-2]))
    )


def _parse_time(raw: str) -> time:
    # The HH:MM and its AM/PM are matched independently, so OCR noise wedged between
    # them (e.g. '6:00) PM') can't hide the meridiem and flip PM back to AM.
    tm = re.search(r'(\d{1,2}):(\d{2})', raw)
    if not tm:
        raise ValueError(f"Cannot parse time: {raw!r}")
    hour, minute = int(tm.group(1)), int(tm.group(2))

    meridiem = re.search(r'([AaPp])[Mm]', raw)
    if meridiem:
        if meridiem.group(1).upper() == "P" and hour != 12:
            hour += 12
        elif meridiem.group(1).upper() == "A" and hour == 12:
            hour = 0
    # No meridiem at all → take the clock value as written (already 24-hour).
    return time(hour % 24, minute % 60)


def _trips_overlap(a: Trip, b: Trip) -> bool:
    """Two trips conflict only if they share a date and their time windows intersect.

    Half-open interval test: [depart, return) of one must intersect the other's.
    Trips on different dates never overlap, so a driver may hold several in a week.
    """
    if a.date != b.date:
        return False
    return a.depart_time < b.return_time and b.depart_time < a.return_time


def _driver_rotation(drivers: List[Driver], starting_seniority: int) -> List[Driver]:
    """Order drivers ascending by seniority, beginning at the starting number.

    Drivers are walked in ascending seniority order starting at the first driver
    whose number is >= starting_seniority; once the highest is reached the walk
    wraps around to the lowest-numbered driver and continues up to (but not past)
    the start. Seniority numbers are non-contiguous, so the start need not name a
    real driver — we simply begin at the next driver at or above it.
    """
    ordered = sorted(drivers, key=lambda d: d.seniority_id)
    start_idx = next(
        (i for i, d in enumerate(ordered) if d.seniority_id >= starting_seniority),
        0,  # start is above every driver — wrap fully, i.e. begin at the lowest
    )
    return ordered[start_idx:] + ordered[:start_idx]


def _name_tokens(first_name: str, last_name: str) -> List[str]:
    """Lower-cased word tokens of a name, used for tolerant name matching."""
    return [w for w in re.split(r'\s+', f"{first_name} {last_name}".strip().lower()) if w]


def _match_dropped_name(first_name: str, last_name: str,
                        drivers: List[Driver]) -> Optional[Driver]:
    """Find the driver a seniority-less sign-up belongs to, matching by name.

    OCR often appends junk tokens to a name on the row where it drops the seniority
    number ('Bethany Anderson a5)'). A driver matches when their full name tokens are
    a leading prefix of the dropped name's tokens, so trailing noise is ignored; the
    longest such prefix wins. An exact name is just the full-length case. When two
    different drivers tie for the longest prefix the match is ambiguous and we give up
    (the operator is asked instead) rather than guess. A name misread in its own right
    ('Jilt' for 'Jill') won't prefix-match and is also left for the operator.
    """
    target = _name_tokens(first_name, last_name)
    if not target:
        return None
    best: Optional[Driver] = None
    best_len = 0
    ambiguous = False
    for d in drivers:
        cand = _name_tokens(d.first_name, d.last_name)
        if not cand or len(cand) > len(target) or target[:len(cand)] != cand:
            continue
        if len(cand) > best_len:
            best, best_len, ambiguous = d, len(cand), False
        elif len(cand) == best_len and d is not best:
            ambiguous = True
    return None if ambiguous else best


def _derive_pull_type(trips: List[Trip]) -> str:
    days = {t.day for t in trips}
    if "Sunday" in days:
        return "sunday"
    if "Saturday" in days:
        return "saturday"
    return "weekday"


def _merge_time_tokens(cells: List[str]) -> List[str]:
    """Merge OCR-split time tokens: ['6:00', 'PM'] → ['6:00 PM']."""
    result: List[str] = []
    i = 0
    while i < len(cells):
        if (i + 1 < len(cells)
                and _TIME_HHMM_RE.match(cells[i])
                and cells[i + 1].upper() in ("AM", "PM")):
            result.append(f"{cells[i]} {cells[i + 1]}")
            i += 2
        else:
            result.append(cells[i])
            i += 1
    return result


_AMPM_ONLY_RE = re.compile(r'^[|\s]*([AaPp][Mm])[|\s]*$')
_AMPM_ANY_RE  = re.compile(r'[AaPp][Mm]')


def _attach_vertical_ampm(words: List[dict]) -> List[dict]:
    """Merge an AM/PM token sitting directly *below* a time token into that time.

    The source PDF wraps a time cell across two lines ('3:00' over 'PM'); OCR then
    reports them as separate words on separate rows, so the meridiem is lost and the
    time is later misread as 24-hour (3:00 PM → 03:00, flipping afternoon to morning).
    Before rows are formed, each lone AM/PM word is attached to the nearest time word
    above it whose horizontal span it overlaps. A same-row AM/PM (to the time's right,
    not below) is left untouched for _merge_time_tokens to handle.
    """
    times = [w for w in words
             if _TIME_PATTERN_RE.search(w["text"]) and not _AMPM_ANY_RE.search(w["text"])]
    kept: List[dict] = []
    for w in words:
        m = _AMPM_ONLY_RE.match(w["text"])
        if not m:
            kept.append(w)
            continue
        meridiem = m.group(1).upper()
        a_left, a_right = w["left"], w["left"] + w["width"]
        best, best_dy = None, None
        for tw in times:
            t_left, t_right = tw["left"], tw["left"] + tw["width"]
            dy = w["top"] - tw["top"]                      # positive ⇒ below the time
            if 15 < dy <= 80 and a_left < t_right and t_left < a_right:
                if best_dy is None or dy < best_dy:
                    best, best_dy = tw, dy
        if best is not None and not _AMPM_ANY_RE.search(best["text"]):
            best["text"] = f"{best['text']} {meridiem}"    # fold meridiem onto the time
        else:
            kept.append(w)                                 # no time to attach to — leave it
    return kept


def _preprocess_for_ocr(image):
    """Make white-on-colored-background text OCR-readable.

    Deeply saturated regions (the green trip-header rows) get binarized at a
    mid-point threshold then inverted, yielding clean black-on-white text.
    Lightly tinted cells and normal black-on-white areas are left untouched.
    """
    rgb = image.convert("RGB")
    r, g, b = rgb.split()
    max_ch = ImageChops.lighter(ImageChops.lighter(r, g), b)
    min_ch = ImageChops.darker(ImageChops.darker(r, g), b)
    saturation = ImageChops.subtract(max_ch, min_ch)

    gray = image.convert("L")

    # Threshold inside colored regions: white text (>180) → white, bg → black;
    # then invert so we get black text on white — ideal for Tesseract.
    binarized = gray.point(lambda p: 255 if p > 180 else 0)
    ready      = ImageOps.invert(binarized)

    # Only apply to deeply saturated pixels (threshold 80 avoids light tints)
    mask = saturation.point(lambda p: 255 if p > 80 else 0)
    return Image.composite(ready, gray, mask)


def _ocr_page_to_rows(image, row_tol: int = 12, col_gap: int = 30) -> List[List[str]]:
    """
    OCR a page image and return rows of column-clustered text.

    row_tol  – max vertical pixel difference to consider two words on the same row
    col_gap  – min horizontal gap (px) between words to treat them as separate columns
    """
    image = _preprocess_for_ocr(image)
    raw = pytesseract.image_to_data(image, output_type=Output.DICT)

    words = []
    for i, text in enumerate(raw["text"]):
        text = text.strip()
        if not text or int(raw["conf"][i]) < 0:
            continue
        words.append({
            "text":  text,
            "left":  raw["left"][i],
            "top":   raw["top"][i],
            "width": raw["width"][i],
        })

    if not words:
        return []

    words = _attach_vertical_ampm(words)
    words.sort(key=lambda w: (w["top"], w["left"]))

    # Cluster words into rows by vertical proximity
    row_groups: List[List[dict]] = []
    current_group = [words[0]]
    current_top   = words[0]["top"]

    for word in words[1:]:
        if abs(word["top"] - current_top) <= row_tol:
            current_group.append(word)
        else:
            row_groups.append(current_group)
            current_group = [word]
            current_top   = word["top"]
    row_groups.append(current_group)

    # Within each row, cluster into columns by horizontal gap
    result: List[List[str]] = []
    for group in row_groups:
        sorted_words = sorted(group, key=lambda w: w["left"])
        cells: List[str] = []
        current_cell = [sorted_words[0]]

        for word in sorted_words[1:]:
            prev = current_cell[-1]
            gap  = word["left"] - (prev["left"] + prev["width"])
            if gap > col_gap:
                cells.append(" ".join(w["text"] for w in current_cell))
                current_cell = [word]
            else:
                current_cell.append(word)
        cells.append(" ".join(w["text"] for w in current_cell))
        result.append(_merge_time_tokens(cells))

    return result


def _read_bus_count(image, bx: int, by: int, bw: int, bh: int) -> Optional[int]:
    """OCR the single bus-count digit in the teal box just below a 'Buses' label.

    The general-purpose row OCR reads this glyph unreliably (a thin '1' merges with
    the cell border into noise like 'ji I'). Here we crop the cell beneath the label
    and read it grayscale and *un-thresholded*: hard thresholding thickens a '1' into
    a jagged shape Tesseract calls '7' or '4', whereas the antialiased glyph reads
    cleanly. psm 10 (single char) is tried first, then 8/7 as fallbacks.
    """
    crop = image.crop((bx - 20, by + bh + 25, bx + bw + 40, by + bh + 120)).convert("L")
    crop = ImageOps.autocontrast(crop).resize((crop.width * 6, crop.height * 6))
    for psm in (10, 8, 7):
        digits = re.sub(r'\D', '', pytesseract.image_to_string(
            crop, config=f'--psm {psm} -c tessedit_char_whitelist=0123456789'))
        if digits:
            return int(digits)
    return None


def _page_bus_counts(image) -> List[Optional[int]]:
    """Bus count for each trip on a page, ordered top-to-bottom (None if unreadable).

    Every coordinate here is located on the page itself, so the reader survives a PDF
    that renders at a different scale or offset (no hard-coded pixel positions):

      * Anchored on the 'Drivers' header (black-on-white, reliable) rather than the
        white-on-teal 'Buses' label, which sometimes fails to OCR entirely. There is
        exactly one 'Drivers' header per trip section (continuation pages included),
        so the result aligns one-to-one with the page's trips for FIFO consumption.
      * Each 'Drivers' header is paired with its nearest 'Buses' label (the label can
        sit a few pixels above or below it), which pins the count box exactly.
      * When a trip's 'Buses' label didn't OCR, the count box's column is taken from
        the page's 'Return' header (the box sits directly beneath that column); only
        if neither header exists do we fall back to a fraction of the page width.

    A None is kept for any box whose digit won't read, preserving the alignment.
    """
    prep = _preprocess_for_ocr(image)
    data = pytesseract.image_to_data(prep, output_type=Output.DICT)
    width, _ = image.size
    drivers_hdrs, buses_hdrs, return_hdrs = [], [], []
    for i, text in enumerate(data["text"]):
        s = text.strip()
        box = (data["top"][i], data["left"][i], data["width"][i], data["height"][i])
        if s.lower() == "drivers":
            drivers_hdrs.append(box)
        elif _BUSES_HDR_RE.match(s):
            buses_hdrs.append(box)
        elif _RETURN_HDR_RE.match(s):
            return_hdrs.append(box)
    drivers_hdrs.sort()

    # Self-located fallback column (used only when a trip's own 'Buses' label is gone).
    anchors = return_hdrs or buses_hdrs
    if anchors:
        col_left  = min(b[1] for b in anchors)
        col_width = max(b[2] for b in anchors)
    else:
        col_left, col_width = int(_BUS_COL_WIDTH_FRAC * width), 90

    counts: List[Optional[int]] = []
    for dtop, dleft, dwidth, dheight in drivers_hdrs:
        match = min(buses_hdrs, key=lambda b: abs(b[0] - dtop), default=None)
        if match is not None and abs(match[0] - dtop) <= 120:
            btop, bleft, bwidth, bheight = match
        else:
            btop, bleft, bwidth, bheight = dtop, col_left, col_width, dheight
        counts.append(_read_bus_count(image, bleft, btop, bwidth, bheight))
    return counts


# ---------------------------------------------------------------------------
# Seniority roster (optional upload, used instead of the PDF's seniority numbers)
# ---------------------------------------------------------------------------

def load_seniority_roster(csv_path: str) -> List[Driver]:
    """Load the master seniority list from a CSV into Driver records.

    The CSV must start with a header row; the name and seniority columns are found by
    their header text, so column order doesn't matter. Either separate 'First Name' /
    'Last Name' columns or a single 'Name' column ('First Last') is accepted, plus a
    'Seniority' (or 'Number'/'#') column. A trailing OCR-style suffix on the number is
    tolerated ('35)' → 35). Rather than silently dropping a bad line, a non-numeric
    seniority or a duplicate number raises with the row number so it can be fixed.

    Returns one Driver per roster line, with no trip selections yet — those are
    attached later by matching the PDF sign-ups to these drivers by name.
    """
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        rows = [r for r in csv.reader(f) if any((c or "").strip() for c in r)]
    if not rows:
        raise ValueError("The seniority file is empty.")

    header = [(c or "").strip().lower() for c in rows[0]]

    def col(*needles: str) -> Optional[int]:
        return next((i for i, h in enumerate(header)
                     if any(n in h for n in needles)), None)

    si, fi, li, ni = col("senior", "number", "#"), col("first"), col("last"), col("name")
    if si is None:
        raise ValueError(
            "Could not find a seniority column. The first row should be a header like "
            "'First Name, Last Name, Seniority'.")
    if fi is None and ni is None:
        raise ValueError(
            "Could not find a name column. The first row should be a header like "
            "'First Name, Last Name, Seniority'.")

    def cell(row: List[str], i: Optional[int]) -> str:
        return row[i].strip() if i is not None and i < len(row) else ""

    drivers: List[Driver] = []
    seen_ids: Dict[int, int] = {}
    for n, row in enumerate(rows[1:], start=2):
        raw_sen = cell(row, si)
        if not raw_sen:
            continue  # blank seniority — an empty trailing line, skip it
        m = re.match(r'^\d+', raw_sen)
        if not m:
            raise ValueError(
                f"Row {n}: '{raw_sen}' is not a seniority number. Each driver needs a "
                f"whole number like 27.")
        seniority_id = int(m.group())
        if seniority_id in seen_ids:
            raise ValueError(
                f"Row {n}: seniority number {seniority_id} also appears on row "
                f"{seen_ids[seniority_id]}. Each number must be unique.")
        seen_ids[seniority_id] = n

        if fi is not None:
            first, last = cell(row, fi), cell(row, li)
        else:
            parts = cell(row, ni).split()
            first = parts[0] if parts else ""
            last  = " ".join(parts[1:])
        drivers.append(Driver(first_name=first, last_name=last, seniority_id=seniority_id))

    if not drivers:
        raise ValueError("No drivers were found in the seniority file.")
    return drivers


def _build_roster_pull(trips: List[Trip], roster: List[Driver],
                       signups: List[tuple]) -> Pull:
    """Assemble a raw Pull in roster mode: place each PDF sign-up onto a roster driver.

    The uploaded roster is the authoritative source of seniority numbers, so every
    sign-up read from the PDF is matched to a roster driver by name (tolerant of OCR
    noise via _match_dropped_name). Sign-ups for the same OCR-read name are grouped
    first so each name is matched once. Any name that matches no roster driver is held
    as an UnmatchedSignup for the operator to resolve; it is not silently dropped.
    """
    grouped: Dict[tuple, dict] = {}
    for first, last, trip, first_choice in signups:
        key = (first.lower(), last.lower())
        g = grouped.get(key)
        if g is None:
            g = {"first": first, "last": last, "trips": [], "fc": []}
            grouped[key] = g
        if all(t is not trip for t in g["trips"]):
            g["trips"].append(trip)
        if first_choice and all(t is not trip for t in g["fc"]):
            g["fc"].append(trip)

    unmatched: List[UnmatchedSignup] = []
    for g in grouped.values():
        drv = _match_dropped_name(g["first"], g["last"], roster)
        if drv is None:
            unmatched.append(UnmatchedSignup(
                first_name=g["first"], last_name=g["last"],
                trips=g["trips"], first_choice_trips=g["fc"]))
            continue
        for t in g["trips"]:
            if all(x is not t for x in drv.trip_selections):
                drv.trip_selections.append(t)
        for t in g["fc"]:
            if all(x is not t for x in drv.first_choice_trips):
                drv.first_choice_trips.append(t)

    trips.sort(key=lambda t: (t.date, t.pdf_order))
    drivers = [d for d in roster if d.trip_selections]
    for d in drivers:
        d.trip_selections.sort(key=lambda t: (t.date, t.pdf_order))
    drivers.sort(key=lambda d: d.seniority_id)

    return Pull(
        pull_type=_derive_pull_type(trips),
        trips=trips,
        drivers=drivers,
        unmatched_signups=unmatched,
    )


# ---------------------------------------------------------------------------
# Public function
# ---------------------------------------------------------------------------

def pre_pull_data_extraction(pdf_path: str,
                             roster: Optional[List[Driver]] = None) -> Pull:
    """Read every trip and driver from the PDF into a single raw Pull.

    This is the slow OCR pass and is run once. The returned Pull holds all days'
    trips and all drivers together; split_into_pulls then partitions it into the
    three weekly pulls, each with its own seniority start point.

    When roster is given (from load_seniority_roster) the PDF's seniority numbers are
    ignored entirely: each sign-up is placed onto a roster driver by name instead, and
    the OCR-number recovery machinery (dropped numbers, digit-drop merges) is skipped.
    """
    trips: List[Trip] = []
    driver_dict: Dict[int, Driver] = {}
    current_trip: Optional[Trip] = None
    run_date: Optional[date] = None
    collecting_drivers = False
    awaiting_trip_data = False
    pending: List[str] = []  # accumulates cells across split trip-data rows
    # Sign-ups whose seniority number was dropped by OCR: the driver is named but the
    # number is gone, so they can't be keyed yet. Held as (first, last, trip, is_fc)
    # and reconciled to the same-named driver once every page (and ID) has been read.
    pending_nameonly: List[tuple] = []
    # Roster mode only: every sign-up read from the PDF, held as
    # (first, last, trip, is_first_choice) and matched to a roster driver by name
    # after the whole sheet is read. Stays empty when no roster was uploaded.
    roster_signups: List[tuple] = []
    # Maps (run_date, normalized_destination) → Trip, so a roster that overflows
    # onto the next page is merged back into one trip instead of duplicated.
    trip_index: Dict[tuple, Trip] = {}
    # Per-name tally of every seniority ID observed, used afterward to reconcile
    # drivers split by OCR digit-drops (e.g. '12' misread as '2').
    name_obs: Dict[tuple, Counter] = defaultdict(Counter)

    # Column-header words that sometimes leak into trip-data rows as OCR splits.
    # A cell is excluded when every alphabetic word in it is one of these.
    _HDR_WORDS = frozenset({'destination', 'group', 'depart', 'return',
                            'date', 'day', 'buses', 'drivers'})

    images = pdf2image.convert_from_path(pdf_path, dpi=200)

    for image in images:
        # Bus counts are read with a dedicated digit OCR (see _page_bus_counts) and
        # consumed FIFO as each trip on the page is parsed top-to-bottom.
        bus_counts = _page_bus_counts(image)
        bus_idx = 0
        for cells in _ocr_page_to_rows(image):
            cells = _clean(cells)

            if _is_run_date_row(cells):
                run_date = _extract_run_date(cells)

            elif _is_destination_header(cells):
                awaiting_trip_data = True
                pending = []

            elif awaiting_trip_data:
                if _is_driver_header(cells):
                    # Hit the driver section with no parseable trip — give up on this trip
                    collecting_drivers = True
                    awaiting_trip_data = False
                else:
                    # Accumulate cells, filtering noise and column-header leakage
                    for raw_c in _strip_pipes(cells):
                        # Strip leading non-alphanumeric characters (OCR underscore/dash noise)
                        c = re.sub(r'^[^a-zA-Z0-9]+', '', raw_c).strip()
                        if not c:
                            continue
                        # Require at least 2 alphanumeric characters (filters 'i}', '-', etc.)
                        if len(re.sub(r'[^a-zA-Z0-9]', '', c)) < 2:
                            continue
                        # Exclude cells whose every alphabetic word is a column-header word
                        words = [w for w in re.split(r'[^a-zA-Z]+', c.lower()) if w]
                        if words and all(w in _HDR_WORDS for w in words):
                            continue
                        pending.append(c)

                    non_times = [c for c in pending if not _TIME_PATTERN_RE.search(c)]
                    times     = [c for c in pending if _TIME_PATTERN_RE.search(c)]

                    if len(non_times) >= 2 and len(times) >= 2:
                        # All but the last non-time cell form the destination;
                        # the last is the group (handles multi-row destination splits)
                        destination  = " ".join(non_times[:-1])
                        group        = non_times[-1]
                        merged_times = _merge_time_tokens(times)
                        depart_raw   = merged_times[0] if merged_times else ""
                        return_raw   = merged_times[1] if len(merged_times) > 1 else ""
                        trip_date   = run_date
                        day = (datetime.combine(trip_date, time()).strftime("%A")
                               if trip_date else "Unknown")
                        key = (trip_date, _normalize_dest(destination))
                        existing = trip_index.get(key)
                        if existing is not None:
                            # Same trip continued on a later page — reuse it so the
                            # rosters from both pages accumulate onto one Trip.
                            current_trip = existing
                            if not existing.group and group:
                                existing.group = group
                        else:
                            current_trip = Trip(
                                date=trip_date,
                                day=day,
                                destination=destination,
                                group=group,
                                depart_time=_parse_time(depart_raw) if depart_raw else time(0, 0),
                                return_time=_parse_time(return_raw) if return_raw else time(0, 0),
                                buses_remaining=0,
                                buses_total=0,
                                pdf_order=len(trips),  # 0-based slot it will occupy
                            )
                            trips.append(current_trip)
                            trip_index[key] = current_trip
                        # Take this trip's bus count from the page's FIFO of readings.
                        # max() guards a continuation page (count re-read, or unreadable
                        # None→0) from clobbering a good value on the original page.
                        if bus_idx < len(bus_counts):
                            cnt = bus_counts[bus_idx] or 0
                            bus_idx += 1
                            current_trip.buses_remaining = max(current_trip.buses_remaining, cnt)
                            current_trip.buses_total     = max(current_trip.buses_total, cnt)
                        awaiting_trip_data = False
                        collecting_drivers = False
                        pending = []

            elif _is_driver_header(cells):
                collecting_drivers = True

            elif collecting_drivers and _is_driver_data(cells) and current_trip is not None:
                first_choice   = cells[-1].lower() == "true"
                name_seniority = cells[:-2]  # everything before [date_selected, first_choice]
                if not name_seniority:
                    continue  # no name and no seniority — nothing to identify

                # Seniority may carry trailing OCR noise (e.g. '35)' or '37.') or be
                # dropped entirely. When the last cell starts with a digit it is the
                # seniority number; otherwise OCR lost it and the whole run is the name,
                # to be matched back to its driver by name after every page is read.
                seniority_m  = re.match(r'^(\d+)', name_seniority[-1])
                name_parts   = name_seniority[:-1] if seniority_m else name_seniority

                first_name = name_parts[0] if name_parts else ""
                last_name  = " ".join(name_parts[1:]) if len(name_parts) > 1 else ""

                if roster is not None:
                    # Authoritative seniority comes from the uploaded roster — ignore the
                    # OCR number and reconcile this sign-up to a roster driver by name
                    # after every page has been read.
                    roster_signups.append(
                        (first_name, last_name, current_trip, first_choice))
                    continue

                if not seniority_m:
                    # Seniority dropped by OCR — defer this sign-up and reconcile it to
                    # the same-named driver once every page (and ID) has been read.
                    pending_nameonly.append(
                        (first_name, last_name, current_trip, first_choice))
                    continue
                seniority_id = int(seniority_m.group(1))

                name_obs[(first_name.lower(), last_name.lower())][seniority_id] += 1

                if seniority_id not in driver_dict:
                    driver_dict[seniority_id] = Driver(
                        first_name=first_name,
                        last_name=last_name,
                        seniority_id=seniority_id,
                    )

                driver = driver_dict[seniority_id]
                # Identity check (not `in`) avoids recursive dataclass __eq__ on the
                # Trip<->Driver cycle, and stops a continuation page double-listing a trip.
                if all(t is not current_trip for t in driver.trip_selections):
                    driver.trip_selections.append(current_trip)
                if first_choice and all(t is not current_trip
                                        for t in driver.first_choice_trips):
                    driver.first_choice_trips.append(current_trip)

    # Roster mode: the PDF's seniority numbers were never read; place each sign-up
    # onto a roster driver by name instead and return, skipping the OCR-number
    # recovery below (which exists only to repair numbers read from the sheet).
    if roster is not None:
        return _build_roster_pull(trips, roster, roster_signups)

    # Reconcile drivers split by OCR digit-drops: the same person read once as
    # '12' and once as '2'. Group by name and fold a record into another only when
    # its ID is a leading-digit drop of the other (the shorter ID is a suffix of the
    # longer). This gate avoids merging two genuinely different same-name drivers.
    by_name: Dict[tuple, List[Driver]] = defaultdict(list)
    for drv in driver_dict.values():
        by_name[(drv.first_name.lower(), drv.last_name.lower())].append(drv)

    for name_key, group in by_name.items():
        if len(group) < 2:
            continue
        counts = name_obs[name_key]
        # True ID has the most digits (drops only remove them); break ties by how
        # often it was observed, then by the larger value.
        canonical = max(group, key=lambda d: (len(str(d.seniority_id)),
                                              counts[d.seniority_id],
                                              d.seniority_id))
        canon_id = str(canonical.seniority_id)
        for drv in group:
            if drv is canonical:
                continue
            drv_id = str(drv.seniority_id)
            if len(drv_id) < len(canon_id) and canon_id.endswith(drv_id):
                # Identity check avoids the recursive Trip<->Driver __eq__ and skips
                # any trip the canonical record already holds.
                for t in drv.trip_selections:
                    if all(x is not t for x in canonical.trip_selections):
                        canonical.trip_selections.append(t)
                for t in drv.first_choice_trips:
                    if all(x is not t for x in canonical.first_choice_trips):
                        canonical.first_choice_trips.append(t)
                del driver_dict[drv.seniority_id]

    # Re-home sign-ups whose seniority number was dropped by OCR (collected name-only
    # above). Group them by name, then for each name attach the sign-ups to the driver
    # of that name if one was read elsewhere on the sheet (the digit-drop merge already
    # treats a name as identifying). Every such case is recorded as a DroppedSeniority:
    # auto-matched ones carry the recovered id for the operator to confirm; names that
    # appear nowhere else are left with matched_id=None for the operator to resolve.
    known_drivers = sorted(driver_dict.values(), key=lambda d: d.seniority_id)

    grouped: Dict[tuple, DroppedSeniority] = {}
    for first_name, last_name, trip, first_choice in pending_nameonly:
        key = (first_name.lower(), last_name.lower())
        ds = grouped.get(key)
        if ds is None:
            ds = DroppedSeniority(first_name=first_name, last_name=last_name)
            grouped[key] = ds
        if all(t is not trip for t in ds.trips):
            ds.trips.append(trip)
        if first_choice and all(t is not trip for t in ds.first_choice_trips):
            ds.first_choice_trips.append(trip)

    dropped_seniorities: List[DroppedSeniority] = []
    for key, ds in grouped.items():
        drv = _match_dropped_name(ds.first_name, ds.last_name, known_drivers)
        if drv is not None:
            for t in ds.trips:
                if all(x is not t for x in drv.trip_selections):
                    drv.trip_selections.append(t)
            for t in ds.first_choice_trips:
                if all(x is not t for x in drv.first_choice_trips):
                    drv.first_choice_trips.append(t)
            ds.matched_id = drv.seniority_id
        dropped_seniorities.append(ds)

    # Order by day, then by where each trip sits on the pull sheet (top-to-bottom),
    # not by departure time — this is the order trips get assigned and listed.
    trips.sort(key=lambda t: (t.date, t.pdf_order))

    for driver in driver_dict.values():
        driver.trip_selections.sort(key=lambda t: (t.date, t.pdf_order))

    drivers = sorted(driver_dict.values(), key=lambda d: d.seniority_id)

    return Pull(
        pull_type=_derive_pull_type(trips),
        trips=trips,
        drivers=drivers,
        dropped_seniorities=dropped_seniorities,
    )


def split_into_pulls(raw: Pull, starting_seniorities: Dict[str, int]) -> List[Pull]:
    """Partition one raw extraction into the three independent weekly pulls.

    starting_seniorities maps each pull type ('weekday', 'saturday', 'sunday') to
    its own seniority start number. Each pull sees only its own days' trips; a pull
    with no trips this week is skipped. Every driver in a pull is a fresh copy that
    carries only that pull's selections and the one first choice that falls within
    it, so the three pulls share no Driver state and running one never disturbs the
    assignments of another. (Trips are not copied — each trip belongs to exactly one
    pull — so run_pull's mutations stay isolated too.)
    """
    pulls: List[Pull] = []
    for pull_type, days in _PULL_CATEGORIES:
        pull_trips = [t for t in raw.trips if t.day in days]
        if not pull_trips:
            continue
        trip_ids = {id(t) for t in pull_trips}

        pull_drivers: List[Driver] = []
        for d in raw.drivers:
            selections = [t for t in d.trip_selections if id(t) in trip_ids]
            if not selections:
                continue  # this driver signed up for nothing in this pull
            first_choice = next(
                (t for t in d.first_choice_trips if id(t) in trip_ids), None)
            pull_drivers.append(Driver(
                first_name=d.first_name,
                last_name=d.last_name,
                seniority_id=d.seniority_id,
                trip_selections=selections,
                first_choice_trip=first_choice,
            ))

        pulls.append(Pull(
            pull_type=pull_type,
            starting_seniority=starting_seniorities.get(pull_type, 0),
            trips=pull_trips,
            drivers=pull_drivers,
        ))
    return pulls


def resolve_dropped_seniority(raw: Pull, dropped: DroppedSeniority,
                              seniority_id: int) -> Driver:
    """Attach an OCR-dropped sign-up to its driver using an operator-supplied number.

    Reuses the existing driver carrying that seniority number, or creates one when the
    number is new, then records the held-back trip selections and first choices onto
    that driver and marks the DroppedSeniority resolved. Must be called on the raw Pull
    before split_into_pulls so the recovered sign-ups flow into the right weekly pull.
    Returns the driver the sign-up was attached to.
    """
    driver = next((d for d in raw.drivers if d.seniority_id == seniority_id), None)
    if driver is None:
        driver = Driver(first_name=dropped.first_name,
                        last_name=dropped.last_name,
                        seniority_id=seniority_id)
        raw.drivers.append(driver)
        raw.drivers.sort(key=lambda d: d.seniority_id)

    for t in dropped.trips:
        if all(x is not t for x in driver.trip_selections):
            driver.trip_selections.append(t)
    driver.trip_selections.sort(key=lambda t: (t.date, t.pdf_order))
    for t in dropped.first_choice_trips:
        if all(x is not t for x in driver.first_choice_trips):
            driver.first_choice_trips.append(t)

    dropped.matched_id = seniority_id
    dropped.manual = True
    return driver


def resolve_unmatched_signup(raw: Pull, unmatched: UnmatchedSignup,
                             seniority_id: int, roster: List[Driver]) -> Driver:
    """Attach a roster-unmatched sign-up to its driver using an operator-supplied number.

    Used in roster mode when a PDF sign-up's name matched nobody on the uploaded list.
    The number is looked up in the roster for the authoritative name; if the roster has
    no such number a driver is created from the sign-up's read name as a last resort.
    The driver is made present in the pull, the held-back selections recorded onto it,
    and the sign-up marked resolved. Call on the raw Pull before split_into_pulls.
    Returns the driver the sign-up was attached to.
    """
    driver = next((d for d in raw.drivers if d.seniority_id == seniority_id), None)
    if driver is None:
        driver = next((d for d in roster if d.seniority_id == seniority_id), None)
        if driver is None:
            driver = Driver(first_name=unmatched.first_name,
                            last_name=unmatched.last_name,
                            seniority_id=seniority_id)
        raw.drivers.append(driver)
        raw.drivers.sort(key=lambda d: d.seniority_id)

    for t in unmatched.trips:
        if all(x is not t for x in driver.trip_selections):
            driver.trip_selections.append(t)
    driver.trip_selections.sort(key=lambda t: (t.date, t.pdf_order))
    for t in unmatched.first_choice_trips:
        if all(x is not t for x in driver.first_choice_trips):
            driver.first_choice_trips.append(t)

    unmatched.resolved = True
    return driver


def _assign_one(driver: Driver) -> Optional[Trip]:
    """Try to assign this driver a single trip on one visit. Returns the trip, or None.

    Implements one branch of the run_pull decision tree: gather the driver's
    still-open selections (buses left, not already theirs, no time conflict with a
    trip they already hold), then take their first-choice trip if it qualifies,
    otherwise the earliest such trip. Identity (`is`) comparisons avoid triggering
    the recursive Trip<->Driver dataclass __eq__ on the object cycle.
    """
    candidates = [
        t for t in driver.trip_selections
        if t.buses_remaining > 0
        and all(t is not a for a in driver.assigned_trips)
        and not any(_trips_overlap(t, a) for a in driver.assigned_trips)
    ]
    if not candidates:
        return None

    fc = driver.first_choice_trip
    if fc is not None and any(fc is c for c in candidates):
        chosen = fc                 # first-choice with buses jumps the queue
    else:
        chosen = candidates[0]      # else the earliest non-overlapping open trip

    chosen.buses_remaining -= 1
    chosen.assigned_drivers.append(driver)
    driver.assigned_trips.append(chosen)
    return chosen


def run_pull(pull: Pull) -> Pull:
    """Assign drivers to trips by seniority rotation. Mutates and returns the pull.

    Walks drivers in the seniority rotation (see _driver_rotation), giving each at
    most one trip per round, and repeats the rounds until either no buses remain or
    a full round assigns nothing (every remaining driver is exhausted/conflicted).
    Resetting at the top makes the call idempotent — safe to re-run on a pull.
    """
    for t in pull.trips:
        t.buses_remaining = t.buses_total
        t.assigned_drivers = []
    for d in pull.drivers:
        d.assigned_trips = []
    pull.assignment_log = []

    rotation = _driver_rotation(pull.drivers, pull.starting_seniority)
    # 1-based priority = position in the seniority rotation (rank 1 picks first).
    priority = {id(d): i + 1 for i, d in enumerate(rotation)}

    while any(t.buses_remaining > 0 for t in pull.trips):
        progressed = False
        for driver in rotation:
            chosen = _assign_one(driver)
            if chosen is not None:
                progressed = True
                # _assign_one takes the first-choice trip whenever it still qualifies,
                # so chosen being that trip means this was a first-choice assignment.
                is_fc = chosen is driver.first_choice_trip
                pull.assignment_log.append(
                    AssignmentEvent(priority[id(driver)], driver, chosen, is_fc))
        if not progressed:
            break  # no driver could take a remaining trip — drivers exhausted

    return pull


# ---------------------------------------------------------------------------
# Validation — turn silent extraction errors into plain-English flags
# ---------------------------------------------------------------------------

# A trip rarely needs more than a handful of buses; a larger count is more likely
# an OCR misread (e.g. a stray seniority number) than a real value worth trusting.
_MAX_PLAUSIBLE_BUSES = 10


def _fmt_time(t: time) -> str:
    """Render a time as a friendly 12-hour string, e.g. 3:00 PM."""
    return t.strftime("%I:%M %p").lstrip("0")


def _trip_label(trip: Trip) -> str:
    """A human-findable description of a trip for use in validation messages."""
    when = f"{trip.date} ({trip.day})" if trip.date else trip.day
    return f"{when} – {trip.destination or '(no destination read)'}"


def validate_extraction(pull: Pull) -> List[ValidationIssue]:
    """Check extracted data against known invariants and return any problems found.

    Designed for an operator who can't read the code: the OCR can occasionally
    misread or drop a value, and a wrong-but-plausible number would otherwise flow
    silently into the assignment. Each issue names the trip and date so it can be
    found and confirmed in the source PDF.
    """
    issues: List[ValidationIssue] = []

    def err(msg: str) -> None:
        issues.append(ValidationIssue("error", msg))

    def warn(msg: str) -> None:
        issues.append(ValidationIssue("warning", msg))

    if not pull.trips:
        err("No trips were found in the PDF. The file likely did not read correctly.")
    if not pull.drivers:
        err("No drivers were found in the PDF. The file likely did not read correctly.")

    for trip in pull.trips:
        label = _trip_label(trip)

        # Known invariant from the operator: every trip has at least one bus.
        if trip.buses_total < 1:
            err(f"Could not read the number of buses for {label}. Every trip should "
                f"have at least 1 — open the PDF and check this trip's bus count.")
        elif trip.buses_total > _MAX_PLAUSIBLE_BUSES:
            warn(f"{label} shows {trip.buses_total} buses, which is unusually high "
                 f"— please confirm against the PDF.")

        # A time that parsed to midnight is the parser's failure fallback, not a
        # real departure/return.
        if trip.depart_time == time(0, 0) or trip.return_time == time(0, 0):
            warn(f"Could not read the depart/return time for {label} "
                 f"— please confirm the times.")
        # A return at or before departure usually means a misread AM/PM. It also
        # breaks the overlap check, which could silently double-book a driver.
        elif trip.return_time <= trip.depart_time:
            warn(f"{label} ends ({_fmt_time(trip.return_time)}) at or before it "
                 f"departs ({_fmt_time(trip.depart_time)}) — likely an AM/PM "
                 f"misread; please confirm the times.")

        if not trip.destination:
            warn(f"A trip on {trip.date} ({trip.day}) has no destination text "
                 f"— please confirm.")

        # Legitimate (it goes to substitutes) but worth surfacing so it isn't a
        # surprise that no one was assigned.
        signups = sum(1 for d in pull.drivers
                      if any(trip is sel for sel in d.trip_selections))
        if signups == 0:
            warn(f"No drivers signed up for {label} — it will go to substitutes.")

    for drv in pull.drivers:
        if not drv.first_name and not drv.last_name:
            warn(f"Driver #{drv.seniority_id} has no name — please confirm.")
        elif not drv.last_name:
            warn(f"Driver #{drv.seniority_id} ({drv.first_name}) has no last name "
                 f"— please confirm.")

    # Sign-ups whose seniority number OCR dropped. Auto-matched ones are reported so
    # the operator can confirm the name really is that driver; ones the operator typed
    # in need no confirmation; any still unresolved is flagged (it can't be placed by
    # seniority and will fall to substitutes unless a number is supplied). The operator
    # is given a chance to enter the number interactively before this check runs.
    for ds in pull.dropped_seniorities:
        who   = ds.name or "(unnamed driver)"
        trips = "; ".join(_trip_label(t) for t in ds.trips) or "(no trip recorded)"
        if ds.matched_id is None:
            warn(f"Could not read the seniority number for {who} on: {trips}. The name "
                 f"appears nowhere else on the sheet, so without a number this sign-up "
                 f"cannot be placed by seniority and will go to substitutes.")
        elif not ds.manual:
            warn(f"The seniority number for {who} did not read on: {trips}. It was "
                 f"matched to #{ds.matched_id} by name — confirm that is the same "
                 f"person against the PDF.")

    # Roster mode: sign-ups whose name matched no one on the uploaded seniority list.
    # Each was offered to the operator to resolve; any still unresolved can't be placed
    # by seniority and will fall to substitutes.
    for us in pull.unmatched_signups:
        if us.resolved:
            continue
        who   = us.name or "(unnamed driver)"
        trips = "; ".join(_trip_label(t) for t in us.trips) or "(no trip recorded)"
        warn(f"'{who}' signed up on: {trips}, but that name is not on the uploaded "
             f"seniority list. Check the spelling on the list (or against the PDF) — "
             f"without a match this sign-up cannot be placed and will go to substitutes.")

    return issues


def format_validation(issues: List[ValidationIssue]) -> str:
    """Render validation issues as a plain-English block for the operator to read."""
    if not issues:
        return "✓ All checks passed — the data read cleanly."

    errors   = [i for i in issues if i.severity == "error"]
    warnings = [i for i in issues if i.severity == "warning"]
    lines: List[str] = []

    if errors:
        lines.append(f"✗ {len(errors)} problem(s) that must be checked before "
                     f"trusting the results:")
        lines += [f"   - {i.message}" for i in errors]
    if warnings:
        if errors:
            lines.append("")
        lines.append(f"! {len(warnings)} thing(s) to confirm:")
        lines += [f"   - {i.message}" for i in warnings]

    return "\n".join(lines)
