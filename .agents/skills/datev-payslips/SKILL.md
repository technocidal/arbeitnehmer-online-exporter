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
playwright install webkit

# Run (credentials from .env or 1Password defaults)
python3 download_payslips.py

# Options
python3 download_payslips.py --headless --output-dir ~/Documents/Payslips
```

## Playwright install failures (workaround)

`playwright install webkit` sometimes fails with a 30-second download timeout
(`Request to ... timed out after 30000ms` / `Download failure, code=1`), even
when the download URL itself is reachable. The browser can then be installed
manually into the Playwright cache:

1. Get the exact URL and target directory from the error message, e.g.
   `Executable doesn't exist at ~/Library/Caches/ms-playwright/webkit-2359/pw_run.sh`
   → download `.../builds/webkit/2359/webkit-mac-26-arm64.zip`
2. Download with curl (no 30s limit, resumes/retries):
   ```bash
   curl -L --retry 3 -o /tmp/webkit.zip "<url-from-error>"
   ```
3. Unzip directly into the missing cache directory:
   ```bash
   mkdir -p ~/Library/Caches/ms-playwright/webkit-2359
   unzip -o /tmp/webkit.zip -d ~/Library/Caches/ms-playwright/webkit-2359
   ```
4. Verify `pw_run.sh` exists in that directory, then launch the script normally.

## Duplicate document handling

DATEV issues amended payslips for the same month with distinct document UUIDs.
- All versions are downloaded (UUID suffix appended to filename for duplicates)

## Important implementation notes

- PDFs must be fetched with `context.request.get(url)`, NOT by opening a new page — a new page loses the authenticated session
- The portal is an Angular SPA; always `wait_for_selector()` before scraping links
- TOTP must come from `op item get --otp`, not `op read` (which returns the raw otpauth URI)
- The "View all documents" link leads to `/ano/en/services/payroll-reports/<inbox-uuid>/documents` which lists all documents (not just recent ones shown on the dashboard)
