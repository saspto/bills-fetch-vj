"""
CPDCL bill fetcher — AWS Lambda handler.

Navigates the BillDesk CPDCL portal for each account number, solves the
numeric CAPTCHA with Tesseract OCR (free, no Bedrock needed), screenshots
the bill details page, collates all three into an A4 PNG, and uploads
everything to S3.

Environment variables:
  S3_BUCKET        (required) — destination bucket
  ACCOUNT_NUMBERS  (optional) — comma-separated, defaults to the three accounts
  S3_PREFIX        (optional) — key prefix, default "bills"
"""

import base64
import json
import logging
import os
import re
import subprocess
import time
from datetime import datetime
from pathlib import Path

import boto3
import pytesseract
from PIL import Image, ImageFilter, ImageOps
from playwright.sync_api import sync_playwright

logger = logging.getLogger()
logger.setLevel(logging.INFO)

URL = "https://payments.billdesk.com/MercOnline/CPDCLAPPGController"

DEFAULT_ACCOUNTS = ["6423244002992", "6423244145358", "6423244217704"]

S3_BUCKET = os.environ.get("S3_BUCKET", "")
S3_PREFIX = os.environ.get("S3_PREFIX", "bills")
ACCOUNTS = [a.strip() for a in os.environ.get("ACCOUNT_NUMBERS", ",".join(DEFAULT_ACCOUNTS)).split(",") if a.strip()]

TMP = Path("/tmp")

# Tesseract config tuned for simple numeric CAPTCHAs
TESS_CONFIG = "--psm 8 --oem 3 -c tessedit_char_whitelist=0123456789"


# ── CAPTCHA solver (Tesseract, free) ─────────────────────────────────────────

def solve_captcha(page) -> str:
    el = page.query_selector("img#jCaptchaImg") or page.query_selector("img[id*='Captcha']")
    img_bytes = el.screenshot() if el else page.screenshot()

    from io import BytesIO
    img = Image.open(BytesIO(img_bytes)).convert("L")  # greyscale

    # Upscale 2× so Tesseract has more pixels to work with
    img = img.resize((img.width * 2, img.height * 2), Image.LANCZOS)
    # Sharpen then threshold to clean background noise
    img = img.filter(ImageFilter.SHARPEN)
    img = img.point(lambda p: 255 if p > 128 else 0)

    text = pytesseract.image_to_string(img, config=TESS_CONFIG).strip()
    # Keep only digits
    text = re.sub(r"\D", "", text)
    logger.info("CAPTCHA OCR result: %r", text)
    return text


# ── Portal scraper ────────────────────────────────────────────────────────────

CHROMIUM_ARGS = [
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--single-process",
    "--disable-setuid-sandbox",
]


def fetch_bill_screenshot(account: str, out_path: Path, max_retries: int = 4) -> bool:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=CHROMIUM_ARGS)
        ctx = browser.new_context(viewport={"width": 1100, "height": 900})
        page = ctx.new_page()

        for attempt in range(1, max_retries + 1):
            logger.info("Account %s — attempt %d/%d", account, attempt, max_retries)
            try:
                page.goto(URL, timeout=60000)
                page.wait_for_load_state("networkidle", timeout=30000)

                acct_inp = page.query_selector("input[name='txtCustomerID']") or page.query_selector("input[type='text']")
                if not acct_inp:
                    logger.warning("Account input not found")
                    continue
                acct_inp.fill(account)

                captcha_text = solve_captcha(page)
                if len(captcha_text) < 4:
                    logger.warning("Captcha OCR too short (%r), retrying", captcha_text)
                    continue

                cap_inp = page.query_selector("input#jcaptchaVal") or page.query_selector(
                    "input[type='text'][name*='captcha'], input[type='text'][id*='captcha'], "
                    "input[type='text'][name*='Captcha'], input[type='text'][id*='Captcha']"
                )
                if not cap_inp:
                    inputs = page.query_selector_all("input[type='text']")
                    cap_inp = inputs[1] if len(inputs) >= 2 else None
                if not cap_inp:
                    logger.warning("Captcha input field not found")
                    continue

                cap_inp.fill(captcha_text)
                page.click("input[value='Submit']")
                page.wait_for_load_state("networkidle", timeout=30000)
                time.sleep(1.5)

                body_text = page.inner_text("body").lower()
                if "sorry" in body_text or "error while processing" in body_text:
                    logger.warning("Error page detected — wrong captcha, retrying")
                    page.goto(URL, timeout=30000)
                    page.wait_for_load_state("networkidle")
                    continue

                page.screenshot(path=str(out_path), full_page=True)
                logger.info("Saved screenshot: %s", out_path)
                browser.close()
                return True

            except Exception as exc:
                logger.exception("Attempt %d failed: %s", attempt, exc)
                if attempt < max_retries:
                    time.sleep(2)

        browser.close()
        return False


# ── Image collation ───────────────────────────────────────────────────────────

def crop_bill(img: Image.Image) -> Image.Image:
    w, h = img.width, img.height
    img = img.crop((0, 0, w, min(h, 780)))
    left, right = int(w * 0.22), int(w * 0.80)
    return img.crop((left, 0, right, img.height))


def fit(img: Image.Image, max_w: int, max_h: int) -> Image.Image:
    scale = min(max_w / img.width, max_h / img.height)
    return img.resize((int(img.width * scale), int(img.height * scale)), Image.LANCZOS)


def collate_images(image_paths: list, output_path: Path):
    A4_W, A4_H = 1240, 1754
    PAD = 20
    cell_w = (A4_W - PAD * 3) // 2
    top_row_h = (A4_H - PAD * 3) // 2

    imgs = [crop_bill(Image.open(p).convert("RGB")) for p in image_paths]

    top_left  = fit(imgs[0], cell_w, top_row_h)
    top_right = fit(imgs[1], cell_w, top_row_h)
    bot_row_h = A4_H - PAD * 3 - top_row_h
    bottom    = fit(imgs[2], int(A4_W * 0.6), bot_row_h)

    canvas = Image.new("RGB", (A4_W, A4_H), "white")
    canvas.paste(top_left,  (PAD, PAD))
    canvas.paste(top_right, (PAD * 2 + cell_w, PAD))
    canvas.paste(bottom,    ((A4_W - bottom.width) // 2, PAD * 2 + top_row_h))
    canvas.save(str(output_path), "PNG", dpi=(150, 150))
    logger.info("Collated image: %s", output_path)


# ── S3 upload ─────────────────────────────────────────────────────────────────

def upload_to_s3(local_path: Path, s3_key: str) -> str:
    boto3.client("s3").upload_file(str(local_path), S3_BUCKET, s3_key)
    logger.info("Uploaded s3://%s/%s", S3_BUCKET, s3_key)
    return s3_key


# ── Lambda handler ────────────────────────────────────────────────────────────

def handler(event, context):
    if not S3_BUCKET:
        raise ValueError("S3_BUCKET environment variable is required")

    date_prefix = datetime.utcnow().strftime("%Y-%m")
    uploaded, succeeded, failed = [], [], []

    for account in ACCOUNTS:
        out_path = TMP / f"bill_{account}.png"
        ok = fetch_bill_screenshot(account, out_path)
        if ok:
            succeeded.append(account)
        else:
            failed.append(account)
            logger.error("Failed to fetch bill for account %s", account)

    if succeeded:
        paths = [TMP / f"bill_{a}.png" for a in succeeded]

        if len(paths) == 3:
            collated = TMP / "receipt_all.png"
            collate_images(paths, collated)
            key = f"{S3_PREFIX}/{date_prefix}/receipt_all.png"
            uploaded.append(upload_to_s3(collated, key))

        for account in succeeded:
            p = TMP / f"bill_{account}.png"
            key = f"{S3_PREFIX}/{date_prefix}/bill_{account}.png"
            uploaded.append(upload_to_s3(p, key))

    return {
        "statusCode": 200,
        "body": json.dumps({
            "date": date_prefix,
            "succeeded": succeeded,
            "failed": failed,
            "s3_keys": uploaded,
        }),
    }
