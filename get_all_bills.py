"""
Fetch bill details page for each account number from CPDCL BillDesk portal,
screenshot each one, then collate all 3 into a single A4-sized printable image.

CAPTCHA is solved with Tesseract OCR (free, no cloud API needed).
"""
import os
import re
import time
from pathlib import Path

import pytesseract
from PIL import Image, ImageFilter
from playwright.sync_api import sync_playwright

URL = "https://payments.billdesk.com/MercOnline/CPDCLAPPGController"
OUT_DIR = Path("/workshop/bills-vja")


def load_accounts() -> list[str]:
    """Load account numbers from ACCOUNT_NUMBERS env var (comma-separated)."""
    raw = os.environ.get("ACCOUNT_NUMBERS", "")
    accounts = [a.strip() for a in raw.split(",") if a.strip()]
    if not accounts:
        raise ValueError("ACCOUNT_NUMBERS environment variable is not set. "
                         "Set it as a comma-separated list, e.g.: "
                         "export ACCOUNT_NUMBERS=1234,5678,9012")
    return accounts

TESS_CONFIG = "--psm 8 --oem 3 -c tessedit_char_whitelist=0123456789"


def solve_captcha(page) -> str:
    from io import BytesIO
    el = page.query_selector("img#jCaptchaImg") or page.query_selector("img[id*='Captcha']")
    img_bytes = el.screenshot() if el else page.screenshot()

    img = Image.open(BytesIO(img_bytes)).convert("L")
    img = img.resize((img.width * 2, img.height * 2), Image.LANCZOS)
    img = img.filter(ImageFilter.SHARPEN)
    img = img.point(lambda p: 255 if p > 128 else 0)

    text = re.sub(r"\D", "", pytesseract.image_to_string(img, config=TESS_CONFIG).strip())
    print(f"  CAPTCHA solved: {text!r}")
    return text


def fetch_bill_screenshot(account: str, out_path: Path, max_retries: int = 4) -> bool:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        ctx = browser.new_context(viewport={"width": 1100, "height": 900})
        page = ctx.new_page()

        for attempt in range(1, max_retries + 1):
            print(f"  Attempt {attempt}/{max_retries} for account {account}")
            try:
                page.goto(URL, timeout=60000)
                page.wait_for_load_state("networkidle", timeout=30000)

                acct_inp = page.query_selector("input[name='txtCustomerID']") or page.query_selector("input[type='text']")
                if not acct_inp:
                    print("  Could not find account input")
                    continue
                acct_inp.fill(account)

                captcha_text = solve_captcha(page)
                if len(captcha_text) < 4:
                    print(f"  Captcha OCR too short ({captcha_text!r}), retrying")
                    continue

                cap_inp = page.query_selector("input#jcaptchaVal") or page.query_selector(
                    "input[type='text'][name*='captcha'], input[type='text'][name*='Captcha'], "
                    "input[type='text'][id*='captcha'], input[type='text'][id*='Captcha']"
                )
                if not cap_inp:
                    inputs = page.query_selector_all("input[type='text']")
                    cap_inp = inputs[1] if len(inputs) >= 2 else None
                if not cap_inp:
                    print("  Could not find captcha input")
                    continue

                cap_inp.fill(captcha_text)
                page.click("input[value='Submit']")
                page.wait_for_load_state("networkidle", timeout=30000)
                time.sleep(1.5)

                body_text = page.inner_text("body").lower()
                if "sorry" in body_text or "error while processing" in body_text:
                    print("  Error page — wrong captcha, retrying")
                    page.goto(URL, timeout=30000)
                    page.wait_for_load_state("networkidle")
                    continue

                page.screenshot(path=str(out_path), full_page=True)
                print(f"  Saved: {out_path}")
                browser.close()
                return True

            except Exception as e:
                print(f"  Exception on attempt {attempt}: {e}")
                if attempt < max_retries:
                    time.sleep(2)

        browser.close()
        return False


def crop_bill(img: Image.Image) -> Image.Image:
    w, h = img.width, img.height
    img = img.crop((0, 0, w, min(h, 780)))
    return img.crop((int(w * 0.22), 0, int(w * 0.80), img.height))


def collate_images(image_paths: list[Path], output_path: Path):
    """2 images side-by-side top row, 1 centred bottom row on A4 at 150 dpi."""
    A4_W, A4_H = 1240, 1754
    PAD = 20
    cell_w    = (A4_W - PAD * 3) // 2
    top_row_h = (A4_H - PAD * 3) // 2

    def fit(img, max_w, max_h):
        scale = min(max_w / img.width, max_h / img.height)
        return img.resize((int(img.width * scale), int(img.height * scale)), Image.LANCZOS)

    imgs      = [crop_bill(Image.open(p).convert("RGB")) for p in image_paths]
    top_left  = fit(imgs[0], cell_w, top_row_h)
    top_right = fit(imgs[1], cell_w, top_row_h)
    bot_row_h = A4_H - PAD * 3 - top_row_h
    bottom    = fit(imgs[2], int(A4_W * 0.6), bot_row_h)

    canvas = Image.new("RGB", (A4_W, A4_H), "white")
    canvas.paste(top_left,  (PAD, PAD))
    canvas.paste(top_right, (PAD * 2 + cell_w, PAD))
    canvas.paste(bottom,    ((A4_W - bottom.width) // 2, PAD * 2 + top_row_h))
    canvas.save(str(output_path), "PNG", dpi=(150, 150))
    print(f"Collated image saved: {output_path} ({A4_W}x{A4_H} px, 150 dpi)")


def main():
    accounts = load_accounts()
    screenshots = []
    for account in accounts:
        print(f"\nFetching bill for account: {account}")
        out = OUT_DIR / f"bill_{account}.png"
        if fetch_bill_screenshot(account, out):
            screenshots.append(out)
        else:
            print(f"  FAILED to fetch bill for {account}")

    if len(screenshots) < len(accounts):
        missing = len(accounts) - len(screenshots)
        print(f"  WARNING: {missing} account(s) failed")

    if screenshots:
        print(f"\nCollating {len(screenshots)} screenshot(s) into A4 page...")
        collate_images(screenshots, OUT_DIR / "receipt_screenshot.png")
    else:
        print("No screenshots to collate.")


if __name__ == "__main__":
    main()
