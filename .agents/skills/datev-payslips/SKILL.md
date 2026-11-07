---
name: datev-payslips
description: Download DATEV Arbeitnehmer Online payslips using download_payslips.py
license: MIT
compatibility: opencode
---

## What this skill does

This repo contains `download_payslips.py` — a Playwright-based script that logs into the DATEV Arbeitnehmer Online portal and downloads all payslip PDFs.

## Project layout

```
download_payslips.py      Main script — all logic lives here
requirements.txt          Python dependencies (playwright)
.env.example              Template for credentials
payslips/                 Downloaded PDFs (git-ignored)
```

## Key functions (download_payslips.py)

| Function | Purpose |
|---|---|
| `load_dotenv()` | Loads `.env` file into `os.environ` without overriding existing vars |
| `resolve_secret(value, label)` | Resolves plain values or `op://` 1Password references |
| `get_credentials()` | Returns `(username, password)` from env / 1Password |
| `get_totp()` | Returns live 6-digit TOTP via `op item get --otp` |
| `run()` | Main async entry point: login → navigate → download |
| `collect_pdf_links(page)` | Scrapes PDF `<a href>` links; appends UUID suffix to deduplicate |
| `fetch_pdf(context, url, dest)` | Downloads PDF using `context.request.get()` (shares auth cookies) |

## Credential resolution order

1. Env var is set to a plain value → use it directly
2. Env var is set to an `op://` reference → resolve via `op read`
3. Env var not set → fall back to the hardcoded `op://VAULT/DATEV/...` defaults

`DATEV_OTP_REF` is always passed to `op item get <item> --otp` (never `op read`) because `op read` returns the raw `otpauth://` URI, not a live code.

## Running the script

```bash
# First-time setup
pip install -r requirements.txt
playwright install chromium

# Run (credentials from .env or 1Password defaults)
python3 download_payslips.py

# Options
python3 download_payslips.py --headless --output-dir ~/Documents/Payslips
```

## Duplicate document handling

DATEV issues amended payslips for the same month with distinct document UUIDs.
- All versions are downloaded (UUID suffix appended to filename for duplicates)

## Important implementation notes

- PDFs must be fetched with `context.request.get(url)`, NOT by opening a new page — a new page loses the authenticated session
- The portal is an Angular SPA; always `wait_for_selector()` before scraping links
- TOTP must come from `op item get --otp`, not `op read` (which returns the raw otpauth URI)
- The "View all documents" link leads to `/ano/en/services/payroll-reports/<inbox-uuid>/documents` which lists all documents (not just recent ones shown on the dashboard)
