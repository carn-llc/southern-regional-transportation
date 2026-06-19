"""
Run this to validate OCR extraction against the real PDF.
Usage: python debug_parser.py <path_to_pdf>
"""
import sys
import pdf2image
from trip_assignment import pre_pull_data_extraction, _ocr_page_to_rows

if len(sys.argv) < 2:
    print("Usage: python debug_parser.py <path_to_pdf>")
    sys.exit(1)

PDF_PATH = sys.argv[1]

# ── All pages: raw OCR rows ─────────────────────────────────────────────────
images = pdf2image.convert_from_path(PDF_PATH, dpi=200)
for pg_idx, image in enumerate(images):
    rows = _ocr_page_to_rows(image)
    if not rows:
        continue
    print("=" * 60)
    print(f"PAGE {pg_idx + 1} — OCR reconstructed rows (col_gap=30)")
    print("=" * 60)
    for r_idx, row in enumerate(rows):
        print(f"  Row {r_idx:3d}: {row}")

# ── Parsed output ───────────────────────────────────────────────────────────
print("\n" + "=" * 60)
print("PARSED PULL OUTPUT")
print("=" * 60)
pull = pre_pull_data_extraction(PDF_PATH, starting_seniority=1)

print(f"\nPull type:          {pull.pull_type}")
print(f"Starting seniority: {pull.starting_seniority}")
print(f"Trips found:        {len(pull.trips)}")
print(f"Drivers found:      {len(pull.drivers)}")

print("\n--- Trips (chronological) ---")
for t in pull.trips:
    print(f"  {t.date} {t.day:10s} | {t.destination:30s} | {t.group:15s} "
          f"| {t.depart_time} → {t.return_time} | buses: {t.buses_total}")

print("\n--- Drivers (by seniority) ---")
total_signups = 0
for d in pull.drivers:
    fc = d.first_choice_trip.destination if d.first_choice_trip else "none"
    trips = [tr.destination for tr in d.trip_selections]
    n = len(d.trip_selections)
    total_signups += n
    print(f"  #{d.seniority_id:3d} {d.first_name} {d.last_name:20s} "
          f"| signups: {n:2d} | first choice: {fc:25s} | selected: {trips}")

# ── Summary totals ──────────────────────────────────────────────────────────
print("\n" + "=" * 60)
print("SUMMARY")
print("=" * 60)

print("\n--- Sign-ups per trip ---")
for t in pull.trips:
    signed_up = [
        f"{d.first_name} {d.last_name}"
        for d in pull.drivers
        if any(t is sel for sel in d.trip_selections)
    ]
    names = ", ".join(signed_up) if signed_up else "(none)"
    print(f"  {t.date} {t.destination:30s} | {names}")

print()
print(f"Total trips:   {len(pull.trips)}")
print(f"Total drivers: {len(pull.drivers)}")
print(f"Total trip sign-ups (sum across drivers): {total_signups}")
avg = total_signups / len(pull.drivers) if pull.drivers else 0
print(f"Avg sign-ups per driver: {avg:.1f}")
