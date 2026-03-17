# Axis Electricals — Weekly Lead Report Automation

Automated weekly lead report generator for **axis-india.com**.
Pulls WordPress / Flamingo form data, splits India vs International leads,
categorises by source and ebook type, and delivers the summary in the
`Total(India)` format used in the master Excel sheet.

---

## Repository Structure

```
Weeky-Leads-Report/
├── report_generator.py      # CSV-based report (offline / manual export)
├── wp_fetch_leads.py        # Live WordPress API fetcher (no CSV needed)
├── flamingo-rest-api.php    # Fallback WP plugin (install if API is unavailable)
├── config.yaml              # All mappings, keywords, auth username
├── requirements.txt         # Python dependencies
└── README.md
```

---

## Method 1 — Live WordPress API (`wp_fetch_leads.py`)

Fetches Flamingo inbound messages directly from the WordPress REST API.
**No CSV exports needed.**

### Prerequisites

```bash
pip install -r requirements.txt
```

### Authentication
Uses [WordPress Application Passwords](https://make.wordpress.org/core/2020/11/05/application-passwords-integration-guide/).

1. In WordPress Admin → Users → your user profile, scroll to
   **Application Passwords**, create one named `n8n-leads-api` (or similar).
2. Copy the generated password (shown only once).
3. Export it as an environment variable — **never hard-code it**:

```bash
export WORDPRESS_APP_PASSWORD="xxxx xxxx xxxx xxxx xxxx xxxx"
```

The username is read from `config.yaml` → `wordpress.auth_username` (currently `Editor_account`).

### Test the connection first

```bash
python wp_fetch_leads.py \
    --week-start 2026-03-09 \
    --week-end   2026-03-15 \
    --test-auth
```

Expected output:
```
Testing auth → https://axis-india.com/wp-json/wp/v2/users/me
Authenticated as: Editor Account (id=3)
Auth OK. Exiting (--test-auth).
```

### Generate a weekly report

```bash
python wp_fetch_leads.py \
    --week-start 2026-03-09 \
    --week-end   2026-03-15
```

Example output:
```
============================================================
Weekly Lead Report  |  09 Mar – 15 Mar 2026
============================================================
Catalogue downloads       21(11)
Sales Inquiry             23(11)
Footer form               6(5)
Get Prices now            8(5)
Pop up                    17(0)
Chat Bot                  18(11)
HST Leads (All)           0

Ebooks:
  Ebook Arch -9(3)
  HST - 0
  ESE - 1
  Substation -0
  LP Stds - 1

Emailers
Others
Total                     98
============================================================
Note: 'HST Leads', 'Emailers', and 'Others' require manual entry.
```

### How API discovery works

`wp_fetch_leads.py` tries three endpoints in order and uses the first that works:

| # | Endpoint | When it works |
|---|---|---|
| 1 | `GET /wp-json/wp/v2/flamingo_inbound` | If Flamingo registers its CPT with `show_in_rest = true` |
| 2 | `GET /wp-json/flamingo/v1/inbound-messages` | If Flamingo ships its own REST namespace |
| 3 | `GET /wp-json/axis/v1/leads` | After installing `flamingo-rest-api.php` (see Method 1b) |

---

### Method 1b — Install the fallback plugin (if all 3 strategies fail)

If the WordPress site's Flamingo version doesn't expose a REST API, install the
single-file plugin included in this repo.

**Steps:**

1. SSH into the server (or use SFTP / File Manager):
   ```bash
   mkdir -p /var/www/html/wp-content/plugins/axis-flamingo-rest-api/
   cp flamingo-rest-api.php \
      /var/www/html/wp-content/plugins/axis-flamingo-rest-api/axis-flamingo-rest-api.php
   ```

2. Activate in **WordPress Admin → Plugins → Axis Flamingo REST API → Activate**.

3. Verify the endpoint is live:
   ```bash
   curl -u "Editor_account:APP_PASSWORD" \
     "https://axis-india.com/wp-json/axis/v1/leads?after=2026-03-06T00:00:00Z&before=2026-03-13T23:59:59Z"
   ```
   You should see a JSON array of inbound messages.

4. Re-run `wp_fetch_leads.py` — it will automatically pick up strategy 3.

---

## Method 2 — CSV Export (`report_generator.py`)

For manual / offline use when the WordPress API is unavailable.

1. Export CSVs from WordPress Admin → Flamingo → Inbound Messages → Export.
2. Run:

```bash
python report_generator.py \
    --week-start 2026-03-09 \
    --week-end   2026-03-15 \
    --catalogue  exports/Pdf__57_.csv \
    --sales      exports/sales_inquiry.csv \
    --footer     exports/footer_form.csv \
    --prices     exports/get_prices.csv \
    --chatbot    exports/chatbot.csv \
    --ebooks     exports/ebooks.csv
```

All CSV arguments are optional — omit any form you don't have an export for.

---

## Configuration (`config.yaml`)

| Section | Key | Description |
|---|---|---|
| `wordpress.auth_username` | `Editor_account` | WP Application Password username |
| `wordpress.forms` | form entries | Form IDs (fill in `XX` once known) |
| `wordpress.flamingo_channels` | channel → key | Maps Flamingo channel names to report buckets |
| `wordpress.ebook_keywords` | category → keywords | Pagetitle keywords for ebook categorisation |
| `report.india_location_value` | `India` | Case-insensitive match for India leads |
| `report.timezone` | `Asia/Kolkata` | IST — week boundaries Mon 00:00 → Sun 23:59 |
| `report.output_format` | `Total(India)` | e.g. `21(11)` |

To add a new channel mapping, add an entry under `flamingo_channels`:
```yaml
flamingo_channels:
  "New Form Title": chatbot   # maps to the chatbot bucket
```

---

## n8n Integration

In the n8n workflow:

1. **Schedule Trigger** — every Monday 09:00 IST.
2. **Code node** — calculate `week_start` (previous Monday) and `week_end` (previous Sunday).
3. **Execute Command node** — run:
   ```
   WORDPRESS_APP_PASSWORD={{ $env.WORDPRESS_APP_PASSWORD }} \
   python /opt/scripts/wp_fetch_leads.py \
     --week-start {{ $json.week_start }} \
     --week-end   {{ $json.week_end }}
   ```
4. **Email / Slack node** — deliver the output.

Set `WORDPRESS_APP_PASSWORD` as a credential/environment variable in n8n — never store it in the workflow JSON.

---

## Security Notes

- The Application Password is **never stored in code or config files**.
- `flamingo-rest-api.php` requires `edit_posts` capability — it is not publicly accessible.
- Rotate the Application Password if it is ever exposed.
