#!/usr/bin/env python3
"""
PayMob UAE Portal2 - Transactions Report Download + S3 Upload

Flow:
  1. Login to uae.paymob.com/portal2 (phone + password)
  2. Navigate to Reports & Statements
  3. Select Report Type: Transactions
  4. Set date range (default: last 10 days)
  5. Click Generate and download the report
  6. Upload to s3://payout-recon/paymob/Weekly/raw/

Usage:
    python paymob_uae_transactions.py
    python paymob_uae_transactions.py --start_date 2026-05-11 --end_date 2026-05-21
"""

import argparse
import asyncio
import os
import sys
import boto3
from botocore.exceptions import ClientError, NoCredentialsError
from datetime import datetime, timedelta
from pathlib import Path
from playwright.async_api import async_playwright

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# ---------------------------------------------------------------------------
# Load .env
# ---------------------------------------------------------------------------
_ENV_FILE = Path(__file__).parent / ".env"
if _ENV_FILE.exists():
    for _line in _ENV_FILE.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))

# ---------------------------------------------------------------------------
# CLI args
# ---------------------------------------------------------------------------
_parser = argparse.ArgumentParser()
_parser.add_argument("--start_date", type=str, default=None)
_parser.add_argument("--end_date",   type=str, default=None)
_args = _parser.parse_args()

today      = datetime.now()
END_DATE   = _args.end_date   or today.strftime("%Y-%m-%d")
START_DATE = _args.start_date or (today - timedelta(days=10)).strftime("%Y-%m-%d")

START_DT = datetime.strptime(START_DATE, "%Y-%m-%d")
END_DT   = datetime.strptime(END_DATE,   "%Y-%m-%d")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
USERNAME = os.environ.get("PAYMOB_UAE2_USERNAME", "")
PASSWORD = os.environ.get("PAYMOB_UAE2_PASSWORD", "")

S3_BUCKET  = os.environ.get("S3_BUCKET", "payout-recon")
S3_PREFIX  = os.environ.get("S3_PAYMOB_UAE_PREFIX", "paymob/Weekly/raw/")
S3_REGION  = os.environ.get("AWS_DEFAULT_REGION", "ap-southeast-1")
S3_ENABLED = os.environ.get("PAYMOB_UAE_S3_ENABLED", "true").lower() == "true"

LOGIN_URL    = "https://uae.paymob.com/portal2/en/login"
DOWNLOAD_DIR = Path("downloads")

async def ss(page, name: str) -> None:
    path = f"paymob_uae2_{name}.png"
    await page.screenshot(path=path, full_page=False)
    print(f"  [screenshot] {path}")

# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------
async def do_login(page) -> None:
    print("[login] Navigating to login page ...")
    await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=30_000)
    await page.wait_for_timeout(3_000)
    await ss(page, "00_login_page")

    print("[login] Filling credentials ...")
    phone_field = page.get_by_role("textbox", name="Phone number")
    await phone_field.click()
    await phone_field.press_sequentially(USERNAME)

    await page.get_by_role("textbox", name="Password").fill(PASSWORD)
    await ss(page, "01_credentials_filled")

    await page.get_by_role("button", name="Sign in").click()
    await page.wait_for_timeout(4_000)
    await ss(page, "02_after_signin")

    if "paymob.com" not in page.url or "/login" in page.url:
        raise RuntimeError(f"[login] Login failed — current URL: {page.url}")

    print(f"[login] Logged in. URL: {page.url}")

# ---------------------------------------------------------------------------
# Navigate to Reports & Statements
# ---------------------------------------------------------------------------
async def navigate_to_reports(page) -> None:
    print("[nav] Navigating to Reports & Statements ...")
    await ss(page, "10_home")

    base = page.url.split("/home")[0]
    for path in ["/reports", "/reports-statements", "/reports/statements"]:
        url = base + path
        print(f"[nav] Trying {url} ...")
        await page.goto(url, wait_until="domcontentloaded", timeout=15_000)
        await page.wait_for_timeout(1_500)
        if "/login" not in page.url and base in page.url:
            await ss(page, "11_reports_direct")
            print(f"[nav] Landed on: {page.url}")
            return

    print("[nav] Direct URL failed — clicking sidebar link ...")
    await page.goto(base + "/home/", wait_until="domcontentloaded", timeout=15_000)
    await page.wait_for_timeout(2_000)

    for label in ["Reports & Statements", "Reports", "Statements"]:
        loc = page.locator(f'a:has-text("{label}"), button:has-text("{label}"), span:has-text("{label}")')
        if await loc.count() > 0:
            await loc.first.click()
            await page.wait_for_timeout(2_000)
            await ss(page, "12_reports_clicked")
            print(f"[nav] Clicked '{label}'. URL: {page.url}")
            return

    raise RuntimeError(f"[nav] Could not find Reports & Statements. URL: {page.url}")

# ---------------------------------------------------------------------------
# Date range picker helpers
# ---------------------------------------------------------------------------
async def _close_any_open_picker(page) -> None:
    if await page.locator('.rs-picker-popup, .rs-picker-menu').count() > 0:
        cancel = page.locator('button:has-text("Cancel")')
        if await cancel.count() > 0:
            await cancel.first.evaluate("el => el.click()")
        else:
            await page.keyboard.press("Escape")
        await page.wait_for_timeout(400)


async def _set_form_date_range(page) -> None:
    print(f"[date] Closing any open picker ...")
    await _close_any_open_picker(page)
    await page.wait_for_timeout(300)

    print(f"[date] Opening the form date range picker ...")
    await ss(page, "22a_before_form_picker")

    clicked = await page.evaluate("""() => {
        const isInFormArea = (el) => {
            const r = el.getBoundingClientRect();
            return r.top > 200 && r.top < 450 && r.width > 0 && el.offsetParent !== null;
        };
        for (const wrapper of document.querySelectorAll('[class*="date-range-picker"]')) {
            if (!isInFormArea(wrapper)) continue;
            const btn = wrapper.querySelector('button, [role="button"]');
            if (btn) {
                btn.click();
                const r = btn.getBoundingClientRect();
                return { clicked: true, method: 'inner-button', top: r.top, left: r.left };
            }
            wrapper.click();
            const r = wrapper.getBoundingClientRect();
            return { clicked: true, method: 'wrapper', top: r.top, left: r.left };
        }
        for (const el of document.querySelectorAll(
            '.rs-picker-toggle, .rs-picker-daterange-toggle, [class*="rs-picker"]'
        )) {
            if (!isInFormArea(el)) continue;
            el.click();
            const r = el.getBoundingClientRect();
            return { clicked: true, method: 'rsuite-toggle', top: r.top, left: r.left };
        }
        return { clicked: false };
    }""")
    print(f"[date] Form picker click result: {clicked}")

    if not clicked.get("clicked"):
        print("[date] JS not found — mouse click at (810, 316)")
        await page.mouse.click(810, 316)

    await page.wait_for_timeout(1_200)
    await ss(page, "22_datepicker_open")

    panel = page.locator(
        '.rs-picker-popup, .rs-picker-menu, .rs-picker-daterange-panel, '
        '[class*="picker-popup"], [class*="date-picker-popup"], '
        '[class*="datepicker"], [class*="calendar-popup"], .react-datepicker'
    )
    if await panel.count() == 0:
        print("[date] Picker panel not found — check paymob_uae2_22_datepicker_open.png")
        return

    await _pick_rsuite_date_range(page)
    await ss(page, "23_dates_set")


async def _pick_rsuite_date_range(page) -> None:
    MONTHS = ['January','February','March','April','May','June',
              'July','August','September','October','November','December']

    async def get_calendar_month_year(cal_index: int = 0):
        return await page.evaluate(f"""() => {{
            const MONTHS = {MONTHS};
            const panels = document.querySelectorAll('.rs-calendar');
            const cal = panels[{cal_index}] || panels[0];
            if (!cal) return null;
            const title = cal.querySelector('.rs-calendar-header-title-date, .rs-calendar-header-title');
            if (!title) return null;
            const txt = title.textContent || '';
            for (let i = 0; i < 12; i++) {{
                if (txt.includes(MONTHS[i])) {{
                    const m = txt.match(/\\b(20\\d{{2}})\\b/);
                    if (m) return [i + 1, parseInt(m[1])];
                }}
            }}
            return null;
        }}""")

    async def nav_calendar_to(target_month: int, target_year: int, cal_index: int = 0) -> None:
        for _ in range(24):
            cur = await get_calendar_month_year(cal_index)
            if cur and cur[0] == target_month and cur[1] == target_year:
                return
            if cur:
                diff = (cur[1] - target_year) * 12 + (cur[0] - target_month)
                btn_cls = '.rs-calendar-header-backward' if diff > 0 else '.rs-calendar-header-forward'
                panels = page.locator('.rs-calendar')
                cal = panels.nth(cal_index) if await panels.count() > cal_index else panels.first
                btn = cal.locator(btn_cls)
                if await btn.count() == 0:
                    btn = cal.locator('button[title*="previous" i]' if diff > 0 else 'button[title*="next" i]')
            else:
                btn = page.locator('.rs-calendar-header-backward').first
            if await btn.count() > 0:
                await btn.first.click()
                await page.wait_for_timeout(250)
            else:
                break

    async def click_rsuite_day(day: int, cal_index: int = 0) -> bool:
        day_str = str(day)
        panels = page.locator('.rs-calendar')
        cal = panels.nth(cal_index) if await panels.count() > cal_index else panels.first
        cells = cal.locator('td.rs-calendar-table-cell:not(.rs-calendar-table-cell-disabled) .rs-calendar-table-cell-day')
        for i in range(await cells.count()):
            cell = cells.nth(i)
            if (await cell.text_content() or "").strip() == day_str:
                await cell.click(timeout=3_000)
                return True
        result = await cal.evaluate(f"""(cal) => {{
            for (const el of cal.querySelectorAll('span, div, td')) {{
                if ((el.textContent || '').trim() === '{day_str}' && el.offsetParent) {{
                    el.click(); return true;
                }}
            }}
            return false;
        }}""")
        return bool(result)

    print(f"[date] Navigating left calendar to {START_DT.month}/{START_DT.year} ...")
    await nav_calendar_to(START_DT.month, START_DT.year, cal_index=0)
    ok = await click_rsuite_day(START_DT.day, cal_index=0)
    print(f"[date] Clicked start day {START_DT.day}: {ok}")
    await page.wait_for_timeout(600)

    cal_count = await page.locator('.rs-calendar').count()
    end_cal_idx = 1 if cal_count > 1 else 0
    print(f"[date] Navigating calendar {end_cal_idx} to {END_DT.month}/{END_DT.year} for end date ...")
    await nav_calendar_to(END_DT.month, END_DT.year, cal_index=end_cal_idx)
    ok = await click_rsuite_day(END_DT.day, cal_index=end_cal_idx)
    print(f"[date] Clicked end day {END_DT.day}: {ok}")
    await page.wait_for_timeout(600)

    panel_open = await page.locator('.rs-picker-popup, .rs-picker-menu').count()
    if panel_open:
        applied = False
        for label in ["Apply", "OK", "Done", "Confirm"]:
            btn = page.locator(f'button:has-text("{label}")')
            if await btn.count() > 0:
                await btn.last.evaluate("el => el.click()")
                print(f"[date] Clicked '{label}' to confirm dates")
                applied = True
                await page.wait_for_timeout(600)
                break
        if not applied:
            await page.keyboard.press("Escape")
            await page.wait_for_timeout(400)
    else:
        print("[date] Panel auto-closed — dates applied")

# ---------------------------------------------------------------------------
# Generate report
# ---------------------------------------------------------------------------
async def generate_report(page) -> Path | None:
    print(f"[report] Generating Transactions report {START_DATE} -> {END_DATE} ...")
    await ss(page, "20_reports_page")

    # Step 1: Select Report Type = Transactions
    print("[report] Opening Type dropdown ...")
    type_btn = page.locator('text="Select Report Type"')
    if await type_btn.count() > 0:
        await type_btn.first.click()
        await page.wait_for_timeout(800)
        await ss(page, "21_type_dropdown_open")

        clicked_type = False
        for option in ["Transactions", "All Transaction"]:
            found = await page.evaluate(f"""() => {{
                const els = Array.from(document.querySelectorAll('*'));
                for (const el of els) {{
                    if (el.children.length === 0 || el.tagName === 'SPAN') {{
                        const txt = (el.textContent || '').trim();
                        if (txt === '{option}' && el.offsetParent !== null) {{
                            el.click(); return true;
                        }}
                    }}
                }}
                for (const el of els) {{
                    const txt = (el.textContent || '').trim();
                    if (txt === '{option}' && el.offsetParent !== null) {{
                        el.click(); return true;
                    }}
                }}
                return false;
            }}""")
            if found:
                print(f"[report] Selected type: '{option}'")
                clicked_type = True
                await page.wait_for_timeout(500)
                break

        if not clicked_type:
            print("[report] Could not select report type — check dropdown")

    await ss(page, "21_type_selected")

    # Step 2: Set date range
    print(f"[report] Setting date range {START_DATE} -> {END_DATE} ...")
    await _set_form_date_range(page)

    # Step 3: Click Generate Report
    print("[report] Clicking Generate Report ...")
    await _close_any_open_picker(page)
    await page.wait_for_timeout(500)

    gen_btn = page.locator('button:has-text("Generate Report"), button:has-text("Generate")')
    if await gen_btn.count() == 0:
        raise RuntimeError("[report] Generate Report button not found")

    await gen_btn.first.evaluate("el => el.click()")
    print("[report] Clicked Generate Report — waiting for new row in list ...")
    await page.wait_for_timeout(5_000)
    await ss(page, "24_after_generate")

    # Step 4: Download latest report
    print("[report] Downloading the latest report ...")
    DOWNLOAD_DIR.mkdir(exist_ok=True)
    dest_name = f"PAYMOB_UAE_TXN_{START_DATE}_to_{END_DATE}.xlsx"

    dl_btn = page.locator('button:has-text("Download"), a:has-text("Download")').first
    await dl_btn.wait_for(timeout=15_000)

    async with page.expect_download(timeout=30_000) as dl_info:
        await dl_btn.evaluate("el => el.click()")
    download = await dl_info.value
    dest = DOWNLOAD_DIR / (download.suggested_filename or dest_name)
    await download.save_as(dest)
    print(f"[report] Saved: {dest.resolve()}")
    await ss(page, "25_download_done")
    return dest

# ---------------------------------------------------------------------------
# S3 Upload
# ---------------------------------------------------------------------------
def upload_to_s3(local_path: Path) -> str:
    s3_key = f"{S3_PREFIX}{local_path.name}"
    print(f"[s3] Uploading {local_path.name} -> s3://{S3_BUCKET}/{s3_key} ...")
    try:
        s3 = boto3.client(
            "s3",
            region_name=S3_REGION,
            aws_access_key_id=os.environ.get("AWS_ACCESS_KEY_ID"),
            aws_secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY"),
        )
        content_type = (
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            if local_path.suffix == ".xlsx" else "text/csv"
        )
        s3.upload_file(str(local_path), S3_BUCKET, s3_key, ExtraArgs={"ContentType": content_type})
        uri = f"s3://{S3_BUCKET}/{s3_key}"
        print(f"[s3] Upload complete -> {uri}")
        return uri
    except NoCredentialsError:
        print("[s3] ERROR: AWS credentials not found")
        raise
    except ClientError as e:
        print(f"[s3] ERROR: {e.response['Error']['Code']} — {e.response['Error']['Message']}")
        raise

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
async def main() -> None:
    DOWNLOAD_DIR.mkdir(exist_ok=True)

    print("=" * 60)
    print(f"[*] PayMob UAE Transactions Report")
    print(f"[*] Portal    : {LOGIN_URL}")
    print(f"[*] Username  : {USERNAME}")
    print(f"[*] Date range: {START_DATE}  ->  {END_DATE}")
    print(f"[*] S3 upload : {'enabled -> ' + S3_BUCKET + '/' + S3_PREFIX if S3_ENABLED else 'disabled'}")
    print("=" * 60)

    IS_CI   = os.environ.get("CI", "false").lower() == "true"
    SLOW_MO = 0 if IS_CI else 60

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=IS_CI, slow_mo=SLOW_MO)
        context = await browser.new_context(accept_downloads=True, viewport={"width": 1440, "height": 900})
        page = await context.new_page()

        try:
            await do_login(page)
            await navigate_to_reports(page)
            dest = await generate_report(page)

            if dest and dest.exists():
                print(f"\n[+] Downloaded: {dest.resolve()}")
                if S3_ENABLED:
                    s3_uri = upload_to_s3(dest)
                    print(f"[+] S3: {s3_uri}")
                else:
                    print("[s3] S3_ENABLED=false — skipping upload.")
            else:
                print("\n[!] No file downloaded — check paymob_uae2_2*.png screenshots.")

        except Exception as exc:
            print(f"\n[!] Error: {exc}")
            try:
                await ss(page, "error_final")
            except Exception:
                pass
            raise
        finally:
            await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
