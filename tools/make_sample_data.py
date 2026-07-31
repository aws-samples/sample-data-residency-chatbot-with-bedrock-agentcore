"""Generate SYNTHETIC, COPY-ready sample CSVs for the four curated tables.

WHY this exists
---------------
Real subsidy-program datasets are large and contain beneficiary PII (bank
account numbers, PFMS ids, gender, category). They are NOT shipped in this
repo. Instead this script generates a small, fully synthetic, deterministic
sample (seeded) with the SAME curated schema, so a partner gets a working demo
out of the box with zero real data.

The output CSVs are already curated + COPY-ready (header = CURATED_COLUMNS,
NULL = empty string, booleans as true/false, dates ISO yyyy-mm-dd, numerics
plain) — exactly what the loader Lambda's ``COPY ... WITH (FORMAT csv, HEADER
true, NULL '')`` expects. So no prep/coercion step is needed for the sample.

The four tables are lifecycle snapshots of the SAME record, so they SHARE the
same application_id set and base attributes; each table just surfaces its own
lifecycle columns.

Run:  uv run python tools/make_sample_data.py [--rows 400] [--out data]
"""
from __future__ import annotations

import argparse
import csv
import datetime as _dt
import os
import random
import sys

# Make ``common.schema`` importable.
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(_ROOT, "src"))

from common.schema import CURATED_COLUMNS, TABLES  # noqa: E402

SEED = 20240710  # deterministic output

# Realistic-looking reference values (align with the UI's illustrative charts).
STATES = [
    ("Maharashtra", ["Pune", "Nagpur", "Nashik", "Thane"]),
    ("Andhra Pradesh", ["Guntur", "Visakhapatnam", "Krishna"]),
    ("West Bengal", ["Howrah", "Nadia", "Hooghly"]),
    ("Gujarat", ["Surat", "Rajkot", "Vadodara"]),
    ("Odisha", ["Cuttack", "Khordha"]),
    ("Telangana", ["Hyderabad", "Warangal"]),
    ("Rajasthan", ["Jaipur", "Jodhpur"]),
    ("Karnataka", ["Bengaluru", "Mysuru"]),
]
# Weight Maharashtra heavily so "top states" leaderboards look realistic.
STATE_WEIGHTS = [40, 12, 8, 7, 6, 5, 5, 4]

DISCOMS = ["MSEDCL", "APSPDCL", "WBSEDCL", "DGVCL", "TPCODL", "TSSPDCL", "JVVNL", "BESCOM"]
RURAL_URBAN = ["Rural", "Urban"]
CATEGORIES = ["General", "OBC", "SC", "ST"]
CONSUMER_CATEGORIES = ["Residential", "Group Housing Society", "Residential Welfare Assoc."]
GENDERS = ["Male", "Female"]
SCHEMES = ["Rooftop Solar Subsidy"]
VENDORS = [
    ("SunTech Solar Pvt Ltd", "V001"),
    ("GreenVolt Energy", "V002"),
    ("Suryodaya Installers", "V003"),
    ("Prakash Solar Solutions", "V004"),
    ("Aditya Rooftop Co", "V005"),
]
BANKS = [
    "State Bank of India",
    "Bank of Baroda",
    "Canara Bank",
    "Punjab National Bank",
    "Union Bank of India",
]
# Bank disbursement weights (SBI dominant, matching the illustrative chart).
BANK_WEIGHTS = [55, 12, 12, 11, 10]

STAGES = ["Vendor Selection", "Subsidy Request", "Feasibility Approval", "Application Submitted"]
STAGE_WEIGHTS = [80, 8, 8, 4]
CURRENT_STATUS = ["In Progress", "Completed", "Pending"]
FEASIBILITY_STATUS = ["Feasible", "Under Review", "Not Feasible"]
INSTALLATION_STATUS = ["Installed", "Installation Pending", "In Progress"]
INSPECTION_STATUS = ["Approved", "Returned for correction", "Scheduled"]

_BASE_DATE = _dt.date(2024, 4, 1)


def _fmt_date(d: _dt.date | None) -> str:
    return d.isoformat() if d else ""


def _fmt_num(n) -> str:
    return "" if n is None else f"{n:.2f}"


def _fmt_bool(b: bool | None) -> str:
    return "" if b is None else ("true" if b else "false")


def _build_records(rows: int, rng: random.Random) -> list[dict]:
    """Build ``rows`` synthetic base records (shared across the 4 tables)."""
    records: list[dict] = []
    # A small pool of shared bank accounts to create duplicates + repeat
    # beneficiaries, so the fraud-theme demo questions return non-zero results.
    dup_accounts = [f"ACCT{rng.randint(10**9, 10**10 - 1)}" for _ in range(6)]
    dup_beneficiaries = [f"PFMS{rng.randint(10**6, 10**7 - 1)}" for _ in range(8)]

    for i in range(rows):
        state, districts = _weighted(rng, STATES, STATE_WEIGHTS)
        district = rng.choice(districts)
        stage = _weighted(rng, STAGES, STAGE_WEIGHTS)
        bank = _weighted(rng, BANKS, BANK_WEIGHTS)
        vendor_name, vendor_id = rng.choice(VENDORS)

        # ~8% share a bank account (duplicate flag); ~10% are repeat beneficiaries.
        is_dup = rng.random() < 0.08
        bank_acct = rng.choice(dup_accounts) if is_dup else f"ACCT{rng.randint(10**9, 10**10 - 1)}"
        beneficiary = (
            rng.choice(dup_beneficiaries) if rng.random() < 0.10
            else f"PFMS{rng.randint(10**6, 10**7 - 1)}"
        )

        # Money: eligible >= sanctioned >= tranche1 (+ maybe tranche2).
        capacity = round(rng.uniform(1.0, 10.0), 3)
        eligible = round(capacity * rng.uniform(14000, 18000), 2)
        sanctioned = round(eligible * rng.uniform(0.85, 1.0), 2)
        redeemed = rng.random() < 0.6
        t1 = round(sanctioned * rng.uniform(0.5, 0.7), 2) if redeemed else None
        t2 = round(sanctioned - t1, 2) if (redeemed and rng.random() < 0.5 and t1) else None

        # Dates: registration -> sanction -> net metering -> redeemed.
        reg = _BASE_DATE + _dt.timedelta(days=rng.randint(0, 180))
        sanctioned_date = reg + _dt.timedelta(days=rng.randint(7, 60)) if redeemed or rng.random() < 0.8 else None
        net_metering = (
            (sanctioned_date + _dt.timedelta(days=rng.randint(5, 45)))
            if sanctioned_date and rng.random() < 0.7 else None
        )
        redeemed_date = (
            (net_metering + _dt.timedelta(days=rng.randint(3, 30)))
            if (redeemed and net_metering) else None
        )

        records.append({
            "application_id": f"APP{100000 + i}",
            "application_number": f"RSS-{2024}-{100000 + i}",
            "state": state,
            "district": district,
            "discom": rng.choice(DISCOMS),
            "rural_urban": rng.choice(RURAL_URBAN),
            "category": rng.choice(CATEGORIES),
            "consumer_category": rng.choice(CONSUMER_CATEGORIES),
            "gender": rng.choice(GENDERS),
            "scheme": SCHEMES[0],
            "vendor_name": vendor_name,
            "vendor_id": vendor_id,
            "name_of_bank": bank,
            "bank_account_number": bank_acct,
            "benefiaicry_unique_id_by_pfms": beneficiary,
            "duplicate_bank_account_number": is_dup,
            "current_stage": stage,
            "current_status": rng.choice(CURRENT_STATUS),
            "feasibility_status": rng.choice(FEASIBILITY_STATUS),
            "installation_status": _weighted(
                rng, INSTALLATION_STATUS, [70, 20, 10]
            ),
            "inspection_status": _weighted(
                rng, INSPECTION_STATUS, [90, 3, 7]
            ),
            "eligible_subsidy_amount": eligible,
            "sanctioned_amount_inr": sanctioned,
            "disbursement_tranche_1_amount_inr": t1,
            "disbursement_tranche_2_amount_inr": t2,
            "installed_capacity_in_kw": capacity,
            "subsidy_redeemed": redeemed,
            "registration_date": reg,
            "sanctioned_date": sanctioned_date,
            "net_metering_date": net_metering,
            "subsidy_redeemed_date": redeemed_date,
        })
    return records


def _weighted(rng: random.Random, items, weights):
    """Weighted choice. ``items`` may be (value, extra) tuples or plain values."""
    chosen = rng.choices(items, weights=weights, k=1)[0]
    return chosen


def _serialize_cell(col: str, value) -> str:
    if isinstance(value, bool):
        return _fmt_bool(value)
    if isinstance(value, _dt.date):
        return _fmt_date(value)
    if isinstance(value, (int, float)):
        return _fmt_num(value)
    return "" if value is None else str(value)


def _write_table(out_dir: str, table: str, records: list[dict]) -> int:
    path = os.path.join(out_dir, f"{table}.csv")
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f, quoting=csv.QUOTE_MINIMAL)
        writer.writerow(CURATED_COLUMNS)
        for rec in records:
            writer.writerow([_serialize_cell(c, rec.get(c)) for c in CURATED_COLUMNS])
    return len(records)


def main() -> None:
    ap = argparse.ArgumentParser(description="Generate synthetic sample CSVs")
    ap.add_argument("--rows", type=int, default=400, help="rows per table (default 400)")
    ap.add_argument("--out", default=os.path.join(_ROOT, "data"),
                    help="output directory (default: <repo>/data)")
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)
    rng = random.Random(SEED)
    records = _build_records(args.rows, rng)

    print(f"Generating {args.rows} synthetic rows/table -> {args.out}\n")
    for table in TABLES:
        # All four tables share the same records (lifecycle snapshots of one row).
        n = _write_table(args.out, table, records)
        print(f"  {table:13s} {n} rows -> {os.path.join(args.out, table + '.csv')}")
    print("\nDone. Synthetic sample data (no real PII).")


if __name__ == "__main__":
    main()
