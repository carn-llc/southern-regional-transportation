from __future__ import annotations

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
    assigned_drivers: List[Driver] = field(default_factory=list)


@dataclass
class Driver:
    first_name:        str
    last_name:         str
    seniority_id:      int
    trip_selections:   List[Trip]     = field(default_factory=list)
    first_choice_trip: Optional[Trip] = None
    assigned_trips:    List[Trip]     = field(default_factory=list)


@dataclass
class Pull:
    pull_type:          str
    starting_seniority: int
    trips:              List[Trip]   = field(default_factory=list)
    drivers:            List[Driver] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_WEEKDAYS = {"Monday", "Tuesday", "Wednesday", "Thursday", "Friday"}
_ALL_DAYS  = _WEEKDAYS | {"Saturday", "Sunday"}

_DATE_RE          = re.compile(r'^\d{1,2}/\d{1,2}/\d{4}$')
_DRIVER_HEADER_RE = re.compile(r'Driver\s*First\s*Name', re.IGNORECASE)
_BOOL_RE          = re.compile(r'^(True|False)$', re.IGNORECASE)
_TIME_HHMM_RE     = re.compile(r'^\d{1,2}:\d{2}$')
_TIME_PATTERN_RE  = re.compile(r'\d{1,2}:\d{2}')
_RUN_DATE_RE      = re.compile(r'Post\s+Selections?\s+Runs?\s+for\s+(\d{1,2}/\d{1,2}/\d{4})', re.IGNORECASE)
_DEST_HDR_RE      = re.compile(r'\bdestination\b', re.IGNORECASE)


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
    # Extract the time token (HH:MM + optional AM/PM) from surrounding OCR noise
    m = re.search(r'\d{1,2}:\d{2}(?:\s*[AaPp][Mm])?', raw)
    if not m:
        raise ValueError(f"Cannot parse time: {raw!r}")
    cleaned = m.group(0).strip()
    for fmt in ("%I:%M %p", "%I:%M%p", "%H:%M"):
        try:
            return datetime.strptime(cleaned, fmt).time()
        except ValueError:
            continue
    raise ValueError(f"Cannot parse time: {raw!r}")


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


# ---------------------------------------------------------------------------
# Public function
# ---------------------------------------------------------------------------

def pre_pull_data_extraction(pdf_path: str, starting_seniority: int) -> Pull:
    trips: List[Trip] = []
    driver_dict: Dict[int, Driver] = {}
    current_trip: Optional[Trip] = None
    run_date: Optional[date] = None
    collecting_drivers = False
    awaiting_trip_data = False
    awaiting_bus_count = False
    pending: List[str] = []  # accumulates cells across split trip-data rows
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
        for cells in _ocr_page_to_rows(image):
            cells = _clean(cells)

            if _is_run_date_row(cells):
                run_date = _extract_run_date(cells)

            elif _is_destination_header(cells):
                awaiting_trip_data = True
                awaiting_bus_count = False
                pending = []

            elif awaiting_trip_data:
                if _is_driver_header(cells):
                    # Hit the driver section with no parseable trip — give up on this trip
                    collecting_drivers = True
                    awaiting_trip_data = False
                    awaiting_bus_count = False
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
                            )
                            trips.append(current_trip)
                            trip_index[key] = current_trip
                        awaiting_trip_data = False
                        collecting_drivers = False
                        awaiting_bus_count = True
                        pending = []

            elif _is_driver_header(cells):
                collecting_drivers = True
                awaiting_bus_count = False

            elif awaiting_bus_count and current_trip is not None:
                m = re.search(r"\b(\d+)\b", " ".join(cells))
                if m:
                    count = int(m.group(1))
                    if count <= 99:  # reject zip codes and other large OCR numbers
                        # max() so a continuation page reading 0 can't clobber a real count
                        current_trip.buses_remaining = max(current_trip.buses_remaining, count)
                        current_trip.buses_total     = max(current_trip.buses_total, count)
                        awaiting_bus_count = False

            elif collecting_drivers and _is_driver_data(cells) and current_trip is not None:
                first_choice   = cells[-1].lower() == "true"
                name_seniority = cells[:-2]  # everything before [date_selected, first_choice]

                # Seniority may have trailing OCR noise (e.g. '35)' or '37.')
                seniority_m = re.match(r'^(\d+)', name_seniority[-1]) if name_seniority else None
                if not seniority_m:
                    continue  # seniority missing — cannot identify driver
                seniority_id = int(seniority_m.group(1))
                name_parts   = name_seniority[:-1]

                first_name = name_parts[0] if name_parts else ""
                last_name  = " ".join(name_parts[1:]) if len(name_parts) > 1 else ""

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
                if first_choice:
                    driver.first_choice_trip = current_trip

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
                if canonical.first_choice_trip is None and drv.first_choice_trip is not None:
                    canonical.first_choice_trip = drv.first_choice_trip
                del driver_dict[drv.seniority_id]

    trips.sort(key=lambda t: (t.date, t.depart_time))

    for driver in driver_dict.values():
        driver.trip_selections.sort(key=lambda t: (t.date, t.depart_time))

    drivers = sorted(driver_dict.values(), key=lambda d: d.seniority_id)

    return Pull(
        pull_type=_derive_pull_type(trips),
        starting_seniority=starting_seniority,
        trips=trips,
        drivers=drivers,
    )
