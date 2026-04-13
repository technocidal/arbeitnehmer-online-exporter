#!/usr/bin/env python3
"""
DATEV Arbeitnehmer Online - Payslip Downloader
https://apps.datev.de/ano/en/

Auth flow (DATEV-Konto with authenticator app):
  1. Open ANO portal — a login modal appears on the page itself
  2. Script clicks "DATEV-Konto with authenticator app"
  3. Script enters email + password (from env / .env file / 1Password)
  4. Script generates the TOTP code automatically
  5. Script downloads all payslip PDFs to ./payslips/ (git-ignored)
  6. Extracts and totals all "ESPP net deduction" line items

Credentials are resolved from environment variables (or a .env file).
If a value looks like a 1Password secret reference (op://vault/item/field)
it is resolved via the `op` CLI automatically, preserving the existing
1Password workflow without any code changes.

  DATEV_USERNAME  — login e-mail
  DATEV_PASSWORD  — login password
  DATEV_OTP_REF   — 1Password item name or secret reference for the TOTP
                    (e.g. "DATEV" or "op://VAULT/DATEV/one-time password")

Defaults (used when the env vars are absent) fall back to the original
1Password references so existing setups continue to work unchanged.
"""

import asyncio
import os
import subprocess
import sys
import re
from pathlib import Path

import pdfplumber
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Where downloaded PDFs will be saved (relative to this script's directory).
DOWNLOAD_DIR = Path(__file__).parent / "payslips"

# The ANO portal URL.
PORTAL_URL = "https://apps.datev.de/ano/en/"

# ---------------------------------------------------------------------------
# .env loader (no external dependencies)
# ---------------------------------------------------------------------------

def load_dotenv(path: Path | None = None) -> None:
    """
    Load a .env file into os.environ (does NOT override existing variables).
    Supports KEY=value, KEY="value", KEY='value', and inline # comments.
    """
    candidates = [path] if path else [
        Path(__file__).parent / ".env",
        Path.cwd() / ".env",
    ]
    for dotenv in candidates:
        if dotenv and dotenv.exists():
            with dotenv.open() as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, _, raw = line.partition("=")
                    key = key.strip()
                    raw = raw.strip()
                    # Strip inline comment (only outside quotes)
                    raw = re.sub(r'\s+#.*$', '', raw)
                    # Strip surrounding quotes
                    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in ('"', "'"):
                        raw = raw[1:-1]
                    if key and key not in os.environ:
                        os.environ[key] = raw
            print(f"[Config] Loaded .env from {dotenv}")
            return

# ---------------------------------------------------------------------------
# Credential resolution: env var → plain value OR 1Password reference
# ---------------------------------------------------------------------------

_OP_REF_RE = re.compile(r"^op://", re.IGNORECASE)


def resolve_secret(value: str, label: str) -> str:
    """
    If *value* starts with 'op://' treat it as a 1Password secret reference
    and resolve it via `op read`.  Otherwise return the value as-is.
    """
    if _OP_REF_RE.match(value):
        print(f"[1Password] Resolving {label} via op read ...")
        try:
            result = subprocess.run(
                ["op", "read", value],
                capture_output=True, text=True, check=True,
            )
            return result.stdout.strip()
        except subprocess.CalledProcessError as e:
            print(f"[ERROR] Could not resolve 1Password reference for {label}:\n{e.stderr.strip()}")
            print("Make sure you are signed in: run `op signin` first.")
            sys.exit(1)
    return value


def _op_get_totp_for_item(item_ref: str) -> str:
    """
    Given a value that is either a plain 1Password item name (e.g. "DATEV")
    or a secret reference (e.g. "op://VAULT/DATEV/one-time password"),
    derive the item name and fetch the live TOTP via `op item get <name> --otp`.
    Returns the 6-digit code string, or "" on failure.
    """
    if _OP_REF_RE.match(item_ref):
        # Extract item name: op://vault/<item>/...
        parts = item_ref.replace("op://", "").split("/")
        item_name = parts[1] if len(parts) >= 2 else item_ref
    else:
        item_name = item_ref

    try:
        result = subprocess.run(
            ["op", "item", "get", item_name, "--otp"],
            capture_output=True, text=True, check=True,
        )
        code = result.stdout.strip()
        if re.fullmatch(r"\d{6}", code):
            print("[1Password] TOTP code fetched automatically.")
            return code
        print(f"[1Password] Unexpected OTP value: '{code}'")
    except subprocess.CalledProcessError as e:
        print(f"[1Password] Could not fetch TOTP: {e.stderr.strip()}")
    return ""


def get_credentials() -> tuple[str, str]:
    """
    Return (username, password) by reading DATEV_USERNAME / DATEV_PASSWORD
    env vars (populated from .env if present).  Values that look like
    op:// references are resolved transparently via the 1Password CLI.

    Defaults fall back to the original 1Password references so existing
    setups work without a .env file.
    """
    raw_user = os.environ.get("DATEV_USERNAME", "op://VAULT/DATEV/username")
    raw_pass = os.environ.get("DATEV_PASSWORD", "op://VAULT/DATEV/password")

    username = resolve_secret(raw_user, "DATEV_USERNAME")
    password = resolve_secret(raw_pass, "DATEV_PASSWORD")
    print(f"[Config] Credentials loaded for: {username}")
    return username, password


def get_totp() -> str:
    """
    Fetch the current TOTP code.  Reads DATEV_OTP_REF from the environment;
    if it looks like an op:// reference or a plain item name, delegates to
    `op item get --otp`.  Falls back to prompting the user if unavailable.
    """
    otp_ref = os.environ.get("DATEV_OTP_REF", "op://VAULT/DATEV/one-time password")
    return _op_get_totp_for_item(otp_ref)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def run(username: str, password: str, download_dir: Path, headless: bool = False):
    download_dir.mkdir(parents=True, exist_ok=True)
    print(f"[Downloader] PDFs will be saved to: {download_dir}")

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=headless,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = await browser.new_context(
            viewport={"width": 1280, "height": 900},
            accept_downloads=True,
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        )
        page = await context.new_page()

        # Step 1: Open portal
        print(f"[Browser] Opening {PORTAL_URL} ...")
        await page.goto(PORTAL_URL, wait_until="domcontentloaded", timeout=30_000)
        # Angular app needs a moment to render and show the login modal
        await page.wait_for_timeout(3_000)
        print(f"[Browser] After load URL: {page.url}")
        print(f"[Browser] Page title: {await page.title()}")

        # Step 2: Click "Login" button to open the modal (if not already open)
        await open_login_modal(page)
        print(f"[Browser] After modal URL: {page.url}")

        # Step 3: Select "DATEV-Konto with authenticator app"
        await select_datev_konto(page)

        # Step 4: Enter email + password
        await enter_credentials(page, username, password)

        # Step 5: Enter TOTP code
        await enter_totp(page)

        # Step 6: Wait for login to complete
        print("[Browser] Waiting for login to complete...")
        try:
            await page.wait_for_function(
                "() => !document.querySelector('dialog, [role=\"dialog\"], .modal, [class*=\"modal\"], [class*=\"login\"]')",
                timeout=30_000,
            )
        except PlaywrightTimeoutError:
            # Modal may have a different structure; just wait for network idle
            pass
        await page.wait_for_load_state("networkidle", timeout=30_000)
        print(f"[Browser] Post-login URL: {page.url}")

        # Step 7: Navigate to documents
        await navigate_to_documents(page)

        # Step 8: Download everything
        await download_all_payslips(page, context, download_dir)

        print("\n[Done] Finished.")
        await browser.close()


# ---------------------------------------------------------------------------
# Login steps
# ---------------------------------------------------------------------------

async def open_login_modal(page):
    """Click the Login button on the ANO landing page if the modal isn't open yet."""
    # Check if a modal/login dialog is already visible
    modal_indicators = [
        '[role="dialog"]',
        'dialog',
        '[class*="modal"]',
        '[class*="login-modal"]',
        '[class*="dialog"]',
    ]
    for sel in modal_indicators:
        try:
            el = await page.wait_for_selector(sel, timeout=2_000)
            if el and await el.is_visible():
                print("[Login] Login modal already open.")
                return
        except PlaywrightTimeoutError:
            continue

    # Modal not open — click the Login button
    login_btn_selectors = [
        'button:has-text("Login")',
        'button:has-text("Anmelden")',
        'a:has-text("Login")',
        'a:has-text("Anmelden")',
        '[class*="login-btn"]',
        '[class*="login-button"]',
    ]
    for sel in login_btn_selectors:
        try:
            await page.click(sel, timeout=4_000)
            print(f"[Login] Opened login modal via: {sel}")
            await page.wait_for_timeout(1_500)
            return
        except PlaywrightTimeoutError:
            continue

    print("[Login] Could not find Login button. The modal may already be open or auto-triggered.")


async def select_datev_konto(page):
    """Click 'DATEV-Konto with authenticator app' in the login method selection modal."""
    print("[Login] Selecting 'DATEV-Konto with authenticator app'...")

    selectors = [
        # Text-based matches (most reliable for an Angular app)
        ':text("DATEV-Konto with authenticator app")',
        ':text("DATEV-Konto")',
        'button:has-text("DATEV-Konto")',
        'li:has-text("DATEV-Konto")',
        '[class*="method"]:has-text("DATEV-Konto")',
        '[class*="option"]:has-text("DATEV-Konto")',
        # Fallback: first option in the list
        '[role="dialog"] li:first-child',
        '[role="dialog"] button:first-of-type',
    ]

    for sel in selectors:
        try:
            await page.click(sel, timeout=5_000)
            print(f"[Login] Selected DATEV-Konto via: {sel}")
            await page.wait_for_timeout(1_500)
            return
        except PlaywrightTimeoutError:
            continue

    print("[Login] Could not auto-select 'DATEV-Konto with authenticator app'.")
    print("        Please click it manually in the browser window.")
    input("        Press Enter once the email/password fields are visible...")


async def enter_credentials(page, username: str, password: str):
    """Fill in email and password fields."""
    print("[Login] Entering email and password...")

    email_selectors = [
        'input[type="email"]',
        'input[name="email"]',
        'input[name="username"]',
        'input[autocomplete="username"]',
        'input[autocomplete="email"]',
        'input[placeholder*="mail" i]',
        'input[placeholder*="Benutzer" i]',
        'input[id*="email" i]',
        'input[id*="user" i]',
    ]
    filled_email = False
    for sel in email_selectors:
        try:
            await page.fill(sel, username, timeout=4_000)
            print(f"[Login] Filled email via: {sel}")
            filled_email = True
            break
        except PlaywrightTimeoutError:
            continue

    if not filled_email:
        print("[Login] Could not find email field — please fill it manually.")
        input("        Press Enter once email is entered...")

    password_selectors = [
        'input[type="password"]',
        'input[name="password"]',
        'input[autocomplete="current-password"]',
        'input[id*="password" i]',
    ]
    filled_pw = False
    for sel in password_selectors:
        try:
            await page.fill(sel, password, timeout=4_000)
            print(f"[Login] Filled password via: {sel}")
            filled_pw = True
            break
        except PlaywrightTimeoutError:
            continue

    if not filled_pw:
        print("[Login] Could not find password field — please fill it manually.")
        input("        Press Enter once password is entered...")

    # Submit the credentials form
    submit_selectors = [
        'button[type="submit"]',
        'button:has-text("Next")',
        'button:has-text("Weiter")',
        'button:has-text("Login")',
        'button:has-text("Anmelden")',
        'button:has-text("Continue")',
    ]
    for sel in submit_selectors:
        try:
            await page.click(sel, timeout=3_000)
            print(f"[Login] Submitted credentials via: {sel}")
            break
        except PlaywrightTimeoutError:
            continue
    else:
        await page.keyboard.press("Enter")

    await page.wait_for_load_state("domcontentloaded", timeout=15_000)
    await page.wait_for_timeout(2_000)


async def enter_totp(page):
    """
    Enter the TOTP code. Tries 1Password first for a seamless experience;
    falls back to prompting the user to type it.
    """
    # Check if a TOTP input is actually present
    totp_selectors = [
        'input[autocomplete="one-time-code"]',
        'input[inputmode="numeric"]',
        'input[name="otp"]',
        'input[name="totp"]',
        'input[name="tan"]',
        'input[name="code"]',
        'input[placeholder*="code" i]',
        'input[placeholder*="Code" i]',
        'input[placeholder*="TAN" i]',
        'input[maxlength="6"]',
        'input[maxlength="8"]',
    ]

    totp_input = None
    for sel in totp_selectors:
        try:
            totp_input = await page.wait_for_selector(sel, timeout=4_000)
            if totp_input and await totp_input.is_visible():
                print(f"[Login] TOTP input found: {sel}")
                break
            totp_input = None
        except PlaywrightTimeoutError:
            continue

    if totp_input is None:
        print("[Login] No TOTP input detected — assuming login completed without a second factor.")
        return

    # Fetch the current TOTP code automatically
    totp_code = get_totp()

    if not totp_code:
        # Fallback: ask the user to type it
        print()
        print("=" * 55)
        print("  Could not fetch TOTP automatically.")
        print("  Open your authenticator app and enter the 6-digit code.")
        totp_code = input("  TOTP code: ").strip()
        print("=" * 55)
        print()

    await totp_input.fill(totp_code)
    await page.wait_for_timeout(500)

    # Log all visible buttons to help diagnose if none match
    buttons = await page.eval_on_selector_all("button", "els => els.map(e => e.innerText.trim()).filter(t => t)")
    print(f"[Login] Visible buttons after TOTP fill: {buttons}")

    # Submit — try every plausible button label, then fall back to Enter
    submit_selectors = [
        'button:has-text("Continue")',
        'button:has-text("Weiter")',
        'button:has-text("Confirm")',
        'button:has-text("Bestätigen")',
        'button:has-text("Verify")',
        'button:has-text("Next")',
        'button:has-text("Login")',
        'button:has-text("Anmelden")',
        'button[type="submit"]',
    ]
    clicked = False
    for sel in submit_selectors:
        try:
            await page.click(sel, timeout=3_000)
            print(f"[Login] Submitted TOTP via: {sel}")
            clicked = True
            break
        except PlaywrightTimeoutError:
            continue

    if not clicked:
        print("[Login] No submit button found — pressing Enter.")
        await page.keyboard.press("Enter")

    # Wait for the OAuth redirect chain to finish and land back on apps.datev.de/ano
    print("[Login] Waiting for post-TOTP redirect to complete...")
    try:
        await page.wait_for_url("https://apps.datev.de/ano/**", timeout=30_000)
        print(f"[Login] Redirected to: {page.url}")
    except PlaywrightTimeoutError:
        print(f"[Login] Still on: {page.url} — continuing anyway.")
    await page.wait_for_load_state("networkidle", timeout=15_000)


# ---------------------------------------------------------------------------
# Navigate to dashboard
# ---------------------------------------------------------------------------

async def navigate_to_documents(page):
    """
    The payslips live on the ANO dashboard (/ano/en/dashboard).
    Navigate there and wait until Angular has rendered the actual PDF links.
    """
    print("[Browser] Navigating to dashboard...")
    dashboard_url = "https://apps.datev.de/ano/en/dashboard"
    await page.goto(dashboard_url, wait_until="domcontentloaded", timeout=20_000)

    # Wait for Angular to finish rendering the payroll card AND populate the links.
    # We wait for the first real PDF anchor to appear inside the card.
    print("[Browser] Waiting for payroll report links to render...")
    try:
        await page.wait_for_selector(
            'axt-payroll-reports-overview-card a[href*="payroll-reports"]',
            timeout=20_000,
        )
        print("[Browser] Payroll report links are ready.")
    except PlaywrightTimeoutError:
        # Fallback: wait for any .pdf link
        try:
            await page.wait_for_selector('a[href$=".pdf"]', timeout=10_000)
            print("[Browser] PDF links found (fallback selector).")
        except PlaywrightTimeoutError:
            print("[Browser] Could not detect PDF links — proceeding anyway.")
            await page.wait_for_load_state("networkidle", timeout=10_000)


# ---------------------------------------------------------------------------
# Download all payslips
# ---------------------------------------------------------------------------

async def download_all_payslips(page, context, download_dir: Path):
    """
    The ANO dashboard shows payslip PDFs as plain <a href="...pdf"> links.
    Strategy:
      1. Collect all PDF hrefs from the dashboard overview card.
      2. Follow the "View all documents" link to the full listing page.
      3. Collect remaining PDF hrefs there.
      4. Download each unique PDF by fetching its URL directly (authenticated
         session cookies are already set in the browser context).
    """
    print("[Downloader] Collecting payslip links...")
    await page.wait_for_load_state("networkidle", timeout=15_000)

    # Collect "View all" hrefs BEFORE navigating away from the dashboard
    view_all_hrefs = await page.eval_on_selector_all(
        'a:has-text("View all documents"), a:has-text("Alle Dokumente"), a[href*="payroll-reports"][href*="documents"]',
        "els => els.map(e => e.href)"
    )
    print(f"[Downloader] 'View all' links found: {view_all_hrefs}")

    pdf_links = await collect_pdf_links(page)

    # Follow each "View all documents" link to get the full listing
    for href in view_all_hrefs:
        print(f"[Browser] Following 'View all' link: {href}")
        await page.goto(href, wait_until="domcontentloaded", timeout=20_000)
        await page.wait_for_load_state("networkidle", timeout=15_000)
        more_links = await collect_pdf_links(page)
        for lnk in more_links:
            if lnk not in pdf_links:
                pdf_links.append(lnk)

    if not pdf_links:
        print("[Downloader] No PDF links found. Try running without --headless to inspect the page.")
        return

    print(f"[Downloader] Found {len(pdf_links)} PDF(s) total.")

    downloaded = 0
    skipped = 0
    for lnk in pdf_links:
        filename = sanitize_filename(lnk["name"])
        dest = download_dir / filename
        if dest.exists():
            print(f"  [skip] {dest.name}")
            skipped += 1
            continue
        print(f"  Downloading: {dest.name}")
        success = await fetch_pdf(context, lnk["url"], dest)
        if success:
            downloaded += 1
        else:
            print(f"    [WARN] Failed to download {dest.name}")

    print(f"\n[Downloader] {downloaded} downloaded, {skipped} already existed.")

    # Extract and report ESPP net deduction totals
    extract_espp_totals(download_dir)


async def collect_pdf_links(page) -> list[dict]:
    """
    Collect all PDF anchor hrefs on the current page.
    Returns list of {url, name} dicts. Deduplicates by URL.
    The ANO app renders links like:
      <a href="../api/amr/ano-payroll-reports/v1/inbox/<id>/document/<id>/Filename.pdf">
    """
    # Wait for Angular to finish rendering PDF links on this page
    try:
        await page.wait_for_selector(
            'a[href*="ano-payroll-reports"], a[href$=".pdf"]',
            timeout=10_000,
        )
    except PlaywrightTimeoutError:
        pass

    # Also dump all hrefs for debugging
    all_hrefs = await page.eval_on_selector_all("a[href]", "els => els.map(e => e.href)")
    payroll_hrefs = [h for h in all_hrefs if "payroll" in h or h.endswith(".pdf")]
    print(f"[Downloader] All payroll/PDF hrefs on page: {payroll_hrefs}")

    raw = await page.eval_on_selector_all(
        'a[href$=".pdf"], a[href*="ano-payroll-reports"][href*="/document/"]',
        "els => els.map(e => ({url: e.href, text: e.innerText.trim()}))"
    )

    seen_urls = set()
    seen_names: dict[str, int] = {}  # basename -> count, to detect collisions
    results = []
    for item in raw:
        url = item["url"]
        if not url or url in seen_urls:
            continue
        # Derive a filename: prefer the last path segment (which DATEV sets to
        # the human-readable PDF name), fall back to the link text.
        path_parts = url.split("/")
        path_name = path_parts[-1].split("?")[0]
        if path_name.lower().endswith(".pdf"):
            base_name = path_name
        else:
            text = item["text"].strip()
            base_name = (sanitize_filename(text) + ".pdf") if text else "document.pdf"

        # If this base name has appeared before, append the document UUID
        # (the URL segment two levels up: .../document/<uuid>/Filename.pdf)
        # to make each filename unique on disk.
        if base_name in seen_names:
            # Extract document UUID from URL path when available
            try:
                doc_uuid = path_parts[-2]  # segment immediately before filename
            except IndexError:
                doc_uuid = ""
            suffix = doc_uuid[:8] if re.fullmatch(r"[0-9a-f\-]{8,}", doc_uuid) else str(seen_names[base_name])
            stem, ext = base_name.rsplit(".", 1)
            name = f"{stem}_{suffix}.{ext}"
        else:
            name = base_name

        seen_names[base_name] = seen_names.get(base_name, 0) + 1
        seen_urls.add(url)
        results.append({"url": url, "name": name})
        print(f"  [found] {name}")

    return results


async def fetch_pdf(context, url: str, dest: Path) -> bool:
    """
    Download a PDF using the browser context's shared cookie session.
    Uses context.request which sends the same cookies as the logged-in browser.
    """
    try:
        response = await context.request.get(url)
        if response.status == 200:
            body = await response.body()
            if body[:4] == b"%PDF":
                dest.write_bytes(body)
                print(f"    Saved: {dest.name}")
                return True
            else:
                print(f"    [WARN] Response is not a PDF (got {len(body)} bytes, starts: {body[:20]!r})")
        else:
            print(f"    [WARN] HTTP {response.status} for {url}")
    except Exception as e:
        print(f"    [WARN] Error fetching {url}: {e}")
    return False


def parse_pdf_text(pdf_path: Path) -> str:
    """Return the full extracted text of a PDF, or empty string on error."""
    try:
        with pdfplumber.open(pdf_path) as pdf:
            return "\n".join(page.extract_text() or "" for page in pdf.pages)
    except Exception as e:
        print(f"[ESPP] Could not read {pdf_path.name}: {e}")
        return ""


# Matches the issuance date in DATEV payslip headers, e.g.:
#   'Abrechnung der Brutto/Netto-Bezüge für Dezember 2025 23.01.2026 Blatt : 1'
# Capture group 1 = DD.MM.YYYY issuance date (the second date on that line).
_ISSUE_DATE_RE = re.compile(
    r"B\s*r\s*u\s*t\s*t\s*o\s*/\s*N\s*e\s*t\s*t\s*o.*?"   # header marker
    r"(\d{2}\.\d{2}\.\d{4})",                                # issuance date
    re.IGNORECASE | re.DOTALL,
)

_AMOUNT_RE = re.compile(r"ESPP net deduction\s+([\d.,]+)-?", re.IGNORECASE)


def extract_espp_totals(download_dir: Path) -> None:
    """
    Scan all Brutto-Netto PDFs in download_dir for "ESPP net deduction" lines.

    When multiple versions of the same payslip month exist (duplicate base
    filenames with UUID suffixes), only the version with the latest issuance
    date is included in the total — older versions are listed but marked as
    superseded.

    The line format in DATEV payslips looks like:
      '... 9022 ESPP net deduction 445,83-'
    Amounts use German decimal notation (comma = decimal separator).
    """
    from datetime import datetime

    # Collect all PDFs that contain an ESPP deduction, recording their
    # issuance date so we can pick the latest per payslip month.
    # key: (payslip_month_str, base_stem_without_uuid)  e.g. "Dezember 2025"
    # value: list of (issuance_date, filename, amount)
    # We use the base filename stem (strip trailing _XXXXXXXX UUID suffix) as
    # the grouping key so all versions of the same month cluster together.
    _UUID_SUFFIX_RE = re.compile(r"_[0-9a-f]{8}$", re.IGNORECASE)

    # (group_key) -> list of (issue_date, pdf_path, amount)
    groups: dict[str, list[tuple[datetime, Path, float]]] = {}

    for pdf_path in sorted(download_dir.glob("*.pdf")):
        text = parse_pdf_text(pdf_path)
        if not text:
            continue

        matches = _AMOUNT_RE.findall(text)
        if not matches:
            continue

        # Parse amount (use the first match; each payslip has at most one)
        raw = matches[0]
        normalised = raw.replace(".", "").replace(",", ".")
        try:
            amount = float(normalised)
        except ValueError:
            print(f"[ESPP] Could not parse amount '{raw}' in {pdf_path.name}")
            continue

        # Extract issuance date from the PDF header text
        m = _ISSUE_DATE_RE.search(text)
        if m:
            try:
                issue_date = datetime.strptime(m.group(1), "%d.%m.%Y")
            except ValueError:
                issue_date = datetime.min
        else:
            issue_date = datetime.min

        # Group key: base stem without the UUID suffix
        stem = _UUID_SUFFIX_RE.sub("", pdf_path.stem)
        groups.setdefault(stem, []).append((issue_date, pdf_path, amount))

    if not groups:
        print("\n[ESPP] No 'ESPP net deduction' entries found in any PDF.")
        return

    print("\n" + "=" * 70)
    print("  ESPP net deduction amounts  (latest version per payslip month)")
    print("=" * 70)

    total = 0.0
    # Sort groups by the latest issuance date in each group (chronological)
    def group_sort_key(item):
        _, entries = item
        return max(e[0] for e in entries)

    for stem, entries in sorted(groups.items(), key=group_sort_key):
        entries.sort(key=lambda e: e[0])  # oldest first within group
        latest_date, latest_path, latest_amount = entries[-1]

        if len(entries) > 1:
            # Print superseded versions indented and marked
            for issue_date, pdf_path, amount in entries[:-1]:
                label = f"  (superseded) {pdf_path.name}"
                print(f"  {label:<65}  {amount:>8.2f} EUR  [{issue_date.strftime('%d.%m.%Y')}]")

        date_str = latest_date.strftime("%d.%m.%Y") if latest_date.year > 1 else "unknown"
        label = latest_path.name
        print(f"  {label:<65}  {latest_amount:>8.2f} EUR  [{date_str}]")
        total += latest_amount

    print("-" * 70)
    print(f"  {'TOTAL':<65}  {total:>8.2f} EUR")
    print("=" * 70)


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def sanitize_filename(name: str) -> str:
    name = re.sub(r'[\\/*?:"<>|]', "_", name)
    name = re.sub(r'\s+', "_", name.strip()).strip("._")
    return (name[:120] if len(name) > 120 else name) or "document"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Download all payslips from DATEV Arbeitnehmer Online",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--output-dir", default=str(DOWNLOAD_DIR),
        help=f"Directory to save PDFs (default: {DOWNLOAD_DIR})",
    )
    parser.add_argument(
        "--headless", action="store_true", default=False,
        help="Run browser headless (default: visible window)",
    )
    parser.add_argument(
        "--env-file", default=None,
        help="Path to a .env file (default: .env next to script or in cwd)",
    )
    args = parser.parse_args()

    load_dotenv(Path(args.env_file) if args.env_file else None)
    username, password = get_credentials()

    asyncio.run(run(
        username=username,
        password=password,
        download_dir=Path(args.output_dir),
        headless=args.headless,
    ))
