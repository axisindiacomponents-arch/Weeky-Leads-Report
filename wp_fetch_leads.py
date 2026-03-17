#!/usr/bin/env python3
"""
wp_fetch_leads.py — WordPress / Flamingo lead fetcher for Axis Electricals.

Connects to the WordPress REST API, fetches Flamingo inbound messages for the
given reporting week, and prints the weekly lead report in the same
"Total(India)" format used by report_generator.py.

Authentication
--------------
Uses WordPress Application Passwords.
  Username : set in config.yaml  (wordpress.auth_username)
  Password : read from env var   WORDPRESS_APP_PASSWORD  (never hard-coded)

API discovery order
-------------------
1. GET /wp-json/wp/v2/flamingo_inbound   — standard WP REST (works if Flamingo
                                            registers its CPT with rest_base)
2. GET /wp-json/flamingo/v1/inbound-messages — Flamingo's own namespace (if
                                            the plugin ships one)
3. GET /wp-json/axis/v1/leads            — custom endpoint from
                                            flamingo-rest-api.php (fallback
                                            plugin in this repo)

Usage
-----
    export WORDPRESS_APP_PASSWORD="xxxx xxxx xxxx xxxx xxxx xxxx"
    python wp_fetch_leads.py --week-start 2026-03-06 --week-end 2026-03-13

    # Dry-run / connection test only:
    python wp_fetch_leads.py --week-start 2026-03-06 --week-end 2026-03-13 --test-auth
"""

import argparse
import base64
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
import yaml

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

CONFIG_PATH = Path(__file__).parent / "config.yaml"
IST = ZoneInfo("Asia/Kolkata")

# Report column display order
COLUMN_ORDER = [
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

# Internal key → report column label
KEY_TO_LABEL: dict[str, str] = {
    "catalogue": "Catalogue downloads",
    "sales":     "Sales Inquiry",
    "footer":    "Footer form",
    "prices":    "Get Prices now",
    "popup":     "Pop up",
    "chatbot":   "Chat Bot",
}


def load_config(path: Path = CONFIG_PATH) -> dict:
    with open(path) as fh:
        return yaml.safe_load(fh)


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------

def build_auth_header(username: str, password: str) -> dict[str, str]:
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def get_password() -> str:
    """Read Application Password from environment; exit with clear message if missing."""
    pw = os.environ.get("WORDPRESS_APP_PASSWORD", "").strip()
    if not pw:
        sys.exit(
            "Error: WORDPRESS_APP_PASSWORD environment variable is not set.\n"
            "Set it before running this script:\n"
            "  export WORDPRESS_APP_PASSWORD='xxxx xxxx xxxx xxxx xxxx xxxx'"
        )
    return pw


# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------

def week_boundaries_ist(week_start_date: datetime, week_end_date: datetime):
    """
    Return (start_utc, end_utc) as ISO-8601 strings suitable for WP REST
    date_query parameters (WordPress stores dates in UTC internally).
    """
    start_ist = datetime(
        week_start_date.year, week_start_date.month, week_start_date.day,
        0, 0, 0, tzinfo=IST,
    )
    end_ist = datetime(
        week_end_date.year, week_end_date.month, week_end_date.day,
        23, 59, 59, tzinfo=IST,
    )
    # Convert to UTC for WP REST after/before params
    start_utc = start_ist.astimezone(timezone.utc)
    end_utc   = end_ist.astimezone(timezone.utc)
    return start_utc, end_utc


def parse_wp_date(date_str: str) -> datetime | None:
    """Parse WordPress date string (UTC naive 'YYYY-MM-DDTHH:MM:SS') → aware UTC."""
    if not date_str:
        return None
    try:
        dt = datetime.fromisoformat(date_str.rstrip("Z"))
        return dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# WordPress API helpers
# ---------------------------------------------------------------------------

class WordPressAPIError(Exception):
    pass


def wp_get(session: requests.Session, url: str, params: dict | None = None) -> dict | list:
    """GET a WP REST endpoint; raise WordPressAPIError on failure."""
    resp = session.get(url, params=params, timeout=20)
    if resp.status_code == 401:
        raise WordPressAPIError(
            f"HTTP 401 Unauthorized — check WORDPRESS_APP_PASSWORD and username."
        )
    if resp.status_code == 404:
        raise WordPressAPIError(f"HTTP 404 — endpoint not found: {url}")
    if not resp.ok:
        raise WordPressAPIError(
            f"HTTP {resp.status_code} from {url}: {resp.text[:300]}"
        )
    return resp.json()


def test_auth(session: requests.Session, base_url: str) -> dict:
    """Verify credentials by fetching /wp/v2/users/me. Returns user dict."""
    url = f"{base_url}/wp/v2/users/me"
    print(f"Testing auth → {url}")
    result = wp_get(session, url)
    print(f"Authenticated as: {result.get('name')} (id={result.get('id')})")
    return result


# ---------------------------------------------------------------------------
# Flamingo fetching — three strategies
# ---------------------------------------------------------------------------

def fetch_all_pages(session, url, params):
    """
    Fetch all pages from a paginated WP REST endpoint.
    Returns list of all items across pages.
    """
    items = []
    page = 1
    per_page = 100
    while True:
        p = {**params, "per_page": per_page, "page": page}
        resp = session.get(url, params=p, timeout=30)
        if resp.status_code == 400:
            # WP returns 400 when page exceeds total pages
            break
        if not resp.ok:
            raise WordPressAPIError(
                f"HTTP {resp.status_code} on page {page}: {resp.text[:300]}"
            )
        batch = resp.json()
        if not batch:
            break
        items.extend(batch)
        # Check X-WP-TotalPages header
        total_pages = int(resp.headers.get("X-WP-TotalPages", 1))
        if page >= total_pages:
            break
        page += 1
    return items


def strategy_wp_v2_cpt(session, base_url, start_utc, end_utc) -> list[dict] | None:
    """
    Strategy 1: /wp/v2/flamingo_inbound
    Flamingo may register its CPT with `show_in_rest = true`.
    """
    url = f"{base_url}/wp/v2/flamingo_inbound"
    params = {
        "after":  start_utc.isoformat(),
        "before": end_utc.isoformat(),
        "status": "any",
    }
    try:
        items = fetch_all_pages(session, url, params)
        print(f"Strategy 1 (wp/v2/flamingo_inbound): {len(items)} items")
        return items
    except WordPressAPIError as e:
        print(f"Strategy 1 failed: {e}")
        return None


def strategy_flamingo_v1(session, base_url, start_utc, end_utc) -> list[dict] | None:
    """
    Strategy 2: /flamingo/v1/inbound-messages
    Flamingo's own namespace (not in current public versions, but worth trying).
    """
    url = f"{base_url}/flamingo/v1/inbound-messages"
    params = {
        "after":  start_utc.isoformat(),
        "before": end_utc.isoformat(),
    }
    try:
        items = fetch_all_pages(session, url, params)
        print(f"Strategy 2 (flamingo/v1): {len(items)} items")
        return items
    except WordPressAPIError as e:
        print(f"Strategy 2 failed: {e}")
        return None


def strategy_axis_custom(session, base_url, start_utc, end_utc) -> list[dict] | None:
    """
    Strategy 3: /axis/v1/leads  (custom endpoint from flamingo-rest-api.php)
    """
    url = f"{base_url}/axis/v1/leads"
    params = {
        "after":  start_utc.isoformat(),
        "before": end_utc.isoformat(),
    }
    try:
        items = fetch_all_pages(session, url, params)
        print(f"Strategy 3 (axis/v1/leads): {len(items)} items")
        return items
    except WordPressAPIError as e:
        print(f"Strategy 3 failed: {e}")
        return None


def fetch_flamingo_messages(
    session: requests.Session,
    base_url: str,
    start_utc: datetime,
    end_utc: datetime,
) -> list[dict]:
    """
    Try all three strategies in order; raise if all fail.
    Returns a list of raw API item dicts.
    """
    for strategy in (strategy_wp_v2_cpt, strategy_flamingo_v1, strategy_axis_custom):
        result = strategy(session, base_url, start_utc, end_utc)
        if result is not None:
            return result

    raise WordPressAPIError(
        "All three API strategies failed.\n"
        "Options:\n"
        "  A) Install the flamingo-rest-api.php plugin (see README.md)\n"
        "  B) Export CSVs manually and use report_generator.py instead"
    )


# ---------------------------------------------------------------------------
# Field extraction — normalise across API strategies
# ---------------------------------------------------------------------------

def extract_field(item: dict, *keys: str, default: str = "") -> str:
    """Try multiple keys (for different API shapes) and return first non-empty value."""
    for key in keys:
        val = item.get(key)
        if val and isinstance(val, str):
            return val.strip()
        # WP REST renders some fields as {"rendered": "..."} objects
        if isinstance(val, dict):
            rendered = val.get("rendered", "")
            if rendered:
                return rendered.strip()
    return default


def get_channel(item: dict) -> str:
    """Return the Flamingo channel name for this submission."""
    return extract_field(item, "channel", "meta_channel", "subject")


def get_location(item: dict) -> str:
    """
    Return the Location/country field.
    Flamingo stores CF7 field values inside `fields` (strategy 3) or
    inside meta (strategies 1/2).
    """
    # Custom endpoint format: {"fields": {"location": "India", ...}}
    fields = item.get("fields", {})
    if isinstance(fields, dict):
        for key in ("location", "Location", "country", "Country"):
            val = fields.get(key, "")
            if val:
                return str(val).strip()

    # WP v2 CPT meta format: meta is a dict of lists
    meta = item.get("meta", {})
    if isinstance(meta, dict):
        for key in ("_field_location", "location", "field_location"):
            val = meta.get(key)
            if val:
                v = val[0] if isinstance(val, list) else val
                return str(v).strip()

    # Fallback: scan all top-level string fields
    for key in ("location", "Location", "country", "Country"):
        val = item.get(key, "")
        if val and isinstance(val, str):
            return val.strip()

    return ""


def get_pagetitle(item: dict) -> str:
    """Return the page/catalogue title — used for ebook categorisation."""
    fields = item.get("fields", {})
    if isinstance(fields, dict):
        for key in ("pagetitle", "page_title", "catalogue", "subject"):
            val = fields.get(key, "")
            if val:
                return str(val).strip()

    return extract_field(item, "subject", "title", "page_title")


def get_date(item: dict) -> datetime | None:
    """Return submission datetime (UTC-aware)."""
    for key in ("date", "date_gmt", "submitted", "date_created"):
        val = item.get(key, "")
        if val:
            return parse_wp_date(str(val))
    return None


# ---------------------------------------------------------------------------
# Counting / grouping
# ---------------------------------------------------------------------------

def group_and_count(
    items: list[dict],
    channel_map: dict[str, str],
    ebook_keywords: dict[str, list[str]],
    india_value: str,
    start_utc: datetime,
    end_utc: datetime,
) -> dict:
    """
    Group raw Flamingo items into report buckets and count total + India leads.

    Returns a dict:
      {
        "catalogue": {"rows": [...], "total": N, "india": N},
        "sales":     {...},
        ...
        "ebooks":    {"by_category": {"Ebook Arch": (N, N), ...}, "total": N, "india": N},
      }
    """
    buckets: dict[str, list[dict]] = {
        "catalogue": [],
        "sales":     [],
        "footer":    [],
        "prices":    [],
        "popup":     [],
        "chatbot":   [],
        "ebooks":    [],
        "unknown":   [],
    }

    for item in items:
        # Secondary date filter (API date params may be ignored by strategy 1)
        dt = get_date(item)
        if dt and not (start_utc <= dt <= end_utc):
            continue

        channel = get_channel(item)
        bucket_key = None
        for ch_name, key in channel_map.items():
            if ch_name.lower() in channel.lower():
                bucket_key = key
                break
        if bucket_key is None:
            bucket_key = "unknown"

        buckets[bucket_key].append(item)

    # Build counts
    result: dict[str, dict] = {}

    for key in ("catalogue", "sales", "footer", "prices", "popup", "chatbot"):
        rows = buckets[key]
        total = len(rows)
        india = sum(
            1 for r in rows
            if get_location(r).lower() == india_value.lower()
        )
        result[key] = {"total": total, "india": india}

    # Ebooks — split by keyword
    ebook_rows = buckets["ebooks"]
    ebook_total = len(ebook_rows)
    ebook_india = sum(
        1 for r in ebook_rows
        if get_location(r).lower() == india_value.lower()
    )
    by_cat: dict[str, tuple[int, int]] = {}
    for cat, keywords in ebook_keywords.items():
        pattern_rows = [
            r for r in ebook_rows
            if any(kw.lower() in get_pagetitle(r).lower() for kw in keywords)
        ]
        cat_total = len(pattern_rows)
        cat_india = sum(
            1 for r in pattern_rows
            if get_location(r).lower() == india_value.lower()
        )
        by_cat[cat] = (cat_total, cat_india)

    result["ebooks"] = {
        "total": ebook_total,
        "india": ebook_india,
        "by_category": by_cat,
    }

    if buckets["unknown"]:
        print(
            f"Warning: {len(buckets['unknown'])} submission(s) had unrecognised "
            f"channels and were not counted. Channels seen: "
            + ", ".join({get_channel(r) for r in buckets["unknown"]})
        )

    return result


# ---------------------------------------------------------------------------
# Output formatting (mirrors report_generator.py)
# ---------------------------------------------------------------------------

def fmt(total: int, india: int) -> str:
    return f"{total}({india})"


def format_ebook_breakdown(by_category: dict[str, tuple[int, int]]) -> str:
    lines = []
    for cat, (total, india) in by_category.items():
        if total == 0:
            lines.append(f"{cat} - 0")
        elif india == 0:
            lines.append(f"{cat} - {total}")
        else:
            lines.append(f"{cat} -{total}({india})")
    return "\n".join(lines)


def build_report(counts: dict) -> dict[str, str | int]:
    report: dict[str, str | int] = {}
    grand_total = 0

    for key, label in KEY_TO_LABEL.items():
        total = counts[key]["total"]
        india = counts[key]["india"]
        report[label] = fmt(total, india)
        grand_total += total

    ebook_data = counts["ebooks"]
    grand_total += ebook_data["total"]
    report["Ebooks"] = format_ebook_breakdown(ebook_data["by_category"])

    # Manually tracked columns — left blank for human entry
    report["HST Leads (All)"] = 0
    report["Emailers"] = ""
    report["Others"] = ""
    report["Total"] = grand_total

    return report


def print_report(week_start: datetime, week_end: datetime, report: dict) -> None:
    week_label = (
        f"{week_start.strftime('%d %b')} – {week_end.strftime('%d %b %Y')}"
    )
    print()
    print("=" * 60)
    print(f"Weekly Lead Report  |  {week_label}")
    print("=" * 60)

    for key in COLUMN_ORDER:
        value = report.get(key, "—")
        if key == "Ebooks" and isinstance(value, str) and "\n" in value:
            print(f"\n{key}:")
            for line in value.splitlines():
                print(f"  {line}")
        else:
            print(f"{key:<25} {value}")

    print("=" * 60)
    print()
    print("Note: 'HST Leads', 'Emailers', and 'Others' require manual entry.")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description=(
            "Fetch Flamingo leads from WordPress and generate the weekly "
            "lead report without needing CSV exports."
        )
    )
    p.add_argument(
        "--week-start",
        required=True,
        metavar="YYYY-MM-DD",
        help="Monday of the reporting week (IST date).",
    )
    p.add_argument(
        "--week-end",
        required=True,
        metavar="YYYY-MM-DD",
        help="Sunday of the reporting week (IST date).",
    )
    p.add_argument(
        "--test-auth",
        action="store_true",
        help="Only verify credentials, then exit.",
    )
    p.add_argument(
        "--config",
        default=str(CONFIG_PATH),
        metavar="YAML",
        help="Path to config.yaml.",
    )
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)

    week_start = datetime.strptime(args.week_start, "%Y-%m-%d")
    week_end   = datetime.strptime(args.week_end,   "%Y-%m-%d")

    if week_start.weekday() != 0:
        print(f"Warning: --week-start {args.week_start} is not a Monday.", file=sys.stderr)
    if week_end.weekday() != 6:
        print(f"Warning: --week-end {args.week_end} is not a Sunday.", file=sys.stderr)

    config     = load_config(Path(args.config))
    wp_cfg     = config["wordpress"]
    base_url   = wp_cfg["base_url"]
    username   = wp_cfg["auth_username"]
    password   = get_password()

    session = requests.Session()
    session.headers.update(build_auth_header(username, password))

    # --- Auth test ---
    try:
        test_auth(session, base_url)
    except WordPressAPIError as e:
        sys.exit(f"Auth failed: {e}")
    except requests.exceptions.ConnectionError as e:
        sys.exit(f"Connection error: {e}")

    if args.test_auth:
        print("Auth OK. Exiting (--test-auth).")
        return

    # --- Fetch ---
    start_utc, end_utc = week_boundaries_ist(week_start, week_end)
    print(f"\nFetching leads {start_utc.isoformat()} → {end_utc.isoformat()} UTC")

    try:
        items = fetch_flamingo_messages(session, base_url, start_utc, end_utc)
    except WordPressAPIError as e:
        sys.exit(str(e))
    except requests.exceptions.ConnectionError as e:
        sys.exit(f"Connection error while fetching leads: {e}")

    print(f"Total raw items fetched: {len(items)}")

    # --- Group & count ---
    channel_map   = wp_cfg["flamingo_channels"]
    ebook_keywords = wp_cfg["ebook_keywords"]
    india_value   = config["report"]["india_location_value"]

    counts = group_and_count(
        items, channel_map, ebook_keywords, india_value, start_utc, end_utc
    )

    report = build_report(counts)
    print_report(week_start, week_end, report)


if __name__ == "__main__":
    main()
