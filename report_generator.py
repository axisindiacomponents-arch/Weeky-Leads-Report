#!/usr/bin/env python3
"""
Weekly Lead Report Generator — Axis Electrical Components (I) Pvt. Ltd.
Processes WordPress form CSV exports and produces the weekly lead summary
in the "Total(India)" format used in the LEADS DATA master sheet.

Usage:
    python report_generator.py \
        --week-start 2026-03-09 \
        --week-end   2026-03-15 \
        --catalogue  path/to/Pdf__57_.csv \
        [--sales     path/to/sales_inquiry.csv] \
        [--footer    path/to/footer_form.csv] \
        [--prices    path/to/get_prices.csv] \
        [--chatbot   path/to/chatbot.csv] \
        [--ebooks    path/to/ebook_downloads.csv]
"""

import argparse
import sys
from datetime import datetime, time
from pathlib import Path

import pandas as pd
import yaml


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

CONFIG_PATH = Path(__file__).parent / "config.yaml"


def load_config(path: Path = CONFIG_PATH) -> dict:
    with open(path, "r") as fh:
        return yaml.safe_load(fh)


# ---------------------------------------------------------------------------
# CSV loading & date filtering
# ---------------------------------------------------------------------------

def load_csv(filepath: str) -> pd.DataFrame:
    """Load a WordPress form export CSV into a DataFrame."""
    df = pd.read_csv(filepath, dtype=str)
    df.columns = [c.strip() for c in df.columns]
    return df


def filter_by_week(
    df: pd.DataFrame,
    week_start: datetime,
    week_end: datetime,
    date_col: str = "Date",
    timezone: str = "Asia/Kolkata",
) -> pd.DataFrame:
    """
    Return only rows whose Date falls within [week_start, week_end] (inclusive).
    The CSV Date column is assumed to be in IST (Asia/Kolkata).
    week_start and week_end should be naive datetime objects representing IST dates.
    """
    df = df.copy()
    df[date_col] = pd.to_datetime(df[date_col], errors="coerce")

    # week_start = Monday 00:00:00, week_end = Sunday 23:59:59
    start = pd.Timestamp(week_start).floor("D")
    end = pd.Timestamp(week_end).replace(hour=23, minute=59, second=59)

    mask = (df[date_col] >= start) & (df[date_col] <= end)
    return df[mask].copy()


# ---------------------------------------------------------------------------
# Counting helpers
# ---------------------------------------------------------------------------

def count_total_india(df: pd.DataFrame, india_value: str = "India") -> tuple[int, int]:
    """Return (total, india_count) for a filtered DataFrame."""
    total = len(df)
    india = int((df["Location"].str.strip().str.lower() == india_value.lower()).sum())
    return total, india


def format_output(total: int, india: int) -> str:
    """Format as 'Total(India)', e.g. '21(11)'."""
    return f"{total}({india})"


# ---------------------------------------------------------------------------
# Ebook sub-breakdown
# ---------------------------------------------------------------------------

def count_ebooks(df: pd.DataFrame, ebook_keywords: dict) -> dict[str, tuple[int, int]]:
    """
    For each ebook category, count rows where the Pagetitle field contains
    any of the configured keywords (case-insensitive).

    Returns a dict: { category_name: (total, india_count) }
    """
    results: dict[str, tuple[int, int]] = {}
    pagetitle_col = "Pagetitle" if "Pagetitle" in df.columns else None

    for category, keywords in ebook_keywords.items():
        if pagetitle_col is None or df.empty:
            results[category] = (0, 0)
            continue

        pattern = "|".join(keywords)
        mask = df[pagetitle_col].str.contains(pattern, case=False, na=False)
        subset = df[mask]
        total, india = count_total_india(subset)
        results[category] = (total, india)

    return results


def format_ebook_breakdown(ebook_counts: dict[str, tuple[int, int]]) -> str:
    """
    Produce the multi-line ebook breakdown string used in column I, e.g.:
        Ebook Arch -9(3)
        HST - 0
        ESE - 1
        Substation -0
        LP Stds - 1
    If total == india, only total is shown (simpler single-number format).
    If india > 0 and total != india, use Total(India) format.
    """
    lines = []
    for category, (total, india) in ebook_counts.items():
        if total == 0:
            lines.append(f"{category} - 0")
        elif india == 0:
            lines.append(f"{category} - {total}")
        else:
            lines.append(f"{category} -{total}({india})")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main report builder
# ---------------------------------------------------------------------------

def generate_report(
    week_start: datetime,
    week_end: datetime,
    csv_paths: dict[str, str | None],
    config: dict,
) -> dict:
    """
    Build the weekly report dict.

    csv_paths keys: 'catalogue', 'sales', 'footer', 'prices', 'chatbot', 'ebooks'
    """
    india_value = config["report"]["india_location_value"]
    timezone = config["report"]["timezone"]
    ebook_keywords = config["wordpress"]["ebook_keywords"]

    report: dict[str, str | int] = {}

    # --- Standard forms (Total(India) format) ---
    form_map = {
        "catalogue": "Catalogue downloads",
        "sales": "Sales Inquiry",
        "footer": "Footer form",
        "prices": "Get Prices now",
        "chatbot": "Chat Bot",
    }

    totals_for_sum: list[int] = []

    for key, column_label in form_map.items():
        path = csv_paths.get(key)
        if path:
            df = load_csv(path)
            df = filter_by_week(df, week_start, week_end, timezone=timezone)
            total, india = count_total_india(df, india_value)
        else:
            total, india = 0, 0

        report[column_label] = format_output(total, india)
        totals_for_sum.append(total)

    # --- Ebooks sub-breakdown ---
    ebook_path = csv_paths.get("ebooks")
    if ebook_path:
        ebook_df = load_csv(ebook_path)
        ebook_df = filter_by_week(ebook_df, week_start, week_end, timezone=timezone)
        ebook_counts = count_ebooks(ebook_df, ebook_keywords)
    else:
        ebook_counts = {cat: (0, 0) for cat in ebook_keywords}

    report["Ebooks"] = format_ebook_breakdown(ebook_counts)
    ebook_total = sum(t for t, _ in ebook_counts.values())
    totals_for_sum.append(ebook_total)

    # --- Placeholders for manually-tracked columns ---
    report["HST Leads (All)"] = 0          # tracked separately
    report["Pop up"] = 0                    # tracked separately
    report["Emailers"] = ""                 # free-text, filled manually
    report["Others"] = ""                   # free-text, filled manually

    # --- Grand total ---
    report["Total"] = sum(totals_for_sum)   # pop-up / HST / emailers added manually

    return report


# ---------------------------------------------------------------------------
# Output rendering
# ---------------------------------------------------------------------------

def print_report(week_start: datetime, week_end: datetime, report: dict) -> None:
    """Print the report in a human-readable format matching the SOP table."""
    week_label = (
        f"{week_start.strftime('%d %b')} – {week_end.strftime('%d %b %Y')}"
    )
    print("=" * 60)
    print(f"Weekly Lead Report  |  {week_label}")
    print("=" * 60)

    order = [
        "Catalogue downloads",
        "Sales Inquiry",
        "Footer form",
        "Get Prices now",
        "Pop up",
        "Chat Bot",
        "HST Leads (All)",
        "Ebooks",
        "Emailers",
        "Others",
        "Total",
    ]

    for key in order:
        value = report.get(key, "—")
        if key == "Ebooks" and isinstance(value, str) and "\n" in value:
            print(f"\n{key}:")
            for line in value.splitlines():
                print(f"  {line}")
        else:
            print(f"{key:<25} {value}")

    print("=" * 60)
    print()
    print("Note: 'Pop up', 'HST Leads', 'Emailers', and 'Others' are")
    print("      tracked separately and must be entered manually.")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Generate the Axis Electricals weekly lead report from WordPress CSV exports."
    )
    parser.add_argument(
        "--week-start",
        required=True,
        metavar="YYYY-MM-DD",
        help="Monday of the reporting week (IST).",
    )
    parser.add_argument(
        "--week-end",
        required=True,
        metavar="YYYY-MM-DD",
        help="Sunday of the reporting week (IST).",
    )
    parser.add_argument("--catalogue", metavar="CSV", help="Catalogue Download form CSV.")
    parser.add_argument("--sales",     metavar="CSV", help="Sales Inquiry form CSV.")
    parser.add_argument("--footer",    metavar="CSV", help="Footer Form CSV.")
    parser.add_argument("--prices",    metavar="CSV", help="Get Prices Now form CSV.")
    parser.add_argument("--chatbot",   metavar="CSV", help="Chat Bot form CSV.")
    parser.add_argument("--ebooks",    metavar="CSV", help="Ebook Downloads form CSV.")
    parser.add_argument(
        "--config",
        default=str(CONFIG_PATH),
        metavar="YAML",
        help="Path to config.yaml (default: same directory as this script).",
    )
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)

    week_start = datetime.strptime(args.week_start, "%Y-%m-%d")
    week_end   = datetime.strptime(args.week_end,   "%Y-%m-%d")

    if week_start.weekday() != 0:
        print(f"Warning: --week-start {args.week_start} is not a Monday.", file=sys.stderr)
    if week_end.weekday() != 6:
        print(f"Warning: --week-end {args.week_end} is not a Sunday.", file=sys.stderr)

    config = load_config(Path(args.config))

    csv_paths = {
        "catalogue": args.catalogue,
        "sales":     args.sales,
        "footer":    args.footer,
        "prices":    args.prices,
        "chatbot":   args.chatbot,
        "ebooks":    args.ebooks,
    }

    report = generate_report(week_start, week_end, csv_paths, config)
    print_report(week_start, week_end, report)


if __name__ == "__main__":
    main()
