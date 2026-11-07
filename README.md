# DATEV Arbeitnehmer Online — Payslip Downloader

Downloads all payslip PDFs from [apps.datev.de/ano/en](https://apps.datev.de/ano/en/) fully automatically, including:

- Brutto-Netto-Abrechnungen
- Lohnsteuerbescheinigungen
- Meldebescheinigungen zur Sozialversicherung

When multiple versions of the same month exist (corrections/amendments), all are downloaded (UUID suffix appended for duplicates).

## Prerequisites

- macOS with Python 3.10+
- Chromium (installed via Playwright)
- Credentials for your DATEV account with authenticator-app 2FA

## Setup

```bash
pip install -r requirements.txt
playwright install chromium
```

## Credentials

Credentials are read from environment variables. Copy `.env.example` to `.env` and fill in your values:

```bash
cp .env.example .env
```

```ini
# .env
DATEV_USERNAME=you@example.com
DATEV_PASSWORD=your-password
DATEV_OTP_REF=DATEV
```

Any value that starts with `op://` is resolved transparently via the [1Password CLI](https://developer.1password.com/docs/cli/), so the following also works without any other changes:

```ini
DATEV_USERNAME=op://VAULT/DATEV/username
DATEV_PASSWORD=op://VAULT/DATEV/password
DATEV_OTP_REF=op://VAULT/DATEV/one-time password
```

If no `.env` file is present and the env vars are not set, the script falls back to those same 1Password references automatically — so existing setups require no configuration at all.

`DATEV_OTP_REF` can be either a plain 1Password item name (e.g. `DATEV`) or a full `op://` reference. Either way, `op item get <item> --otp` is used to obtain the live 6-digit code.

## Usage

```bash
python3 download_payslips.py
```

PDFs are saved to `./payslips/` (git-ignored). Already-downloaded files are skipped.

```
options:
  --output-dir PATH   Directory to save PDFs (default: ./payslips)
  --headless          Run without a visible browser window
  --env-file PATH     Path to a custom .env file
```

## Output

Downloaded PDFs are saved as e.g. `Brutto-Netto-Abrechnung-Dezember-2025.pdf`. For duplicate
document names, a short UUID suffix is appended (`..._ab12cd34.pdf`).
