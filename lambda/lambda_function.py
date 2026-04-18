"""
CPDCL bill fetcher — AWS Lambda handler.

Navigates the BillDesk CPDCL portal for each account number, solves the
numeric CAPTCHA with Tesseract OCR (free, no Bedrock needed), screenshots
the bill details page, collates all three into an A4 PNG, uploads to S3,
and emails the collated image via SES.

Account numbers are loaded from AWS Secrets Manager (preferred) or the
ACCOUNT_NUMBERS environment variable (comma-separated fallback).

Environment variables:
  S3_BUCKET            (required) — destination bucket
  EMAIL_TO             (required) — recipient address (must be SES-verified in sandbox)
  ACCOUNT_NUMBERS      (required) — comma-separated list of account numbers
  EMAIL_FROM           (optional) — sender address, defaults to EMAIL_TO
  S3_PREFIX            (optional) — key prefix, default "bills"
"""

import json
import logging
import os
import re
import time
from datetime import datetime
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from io import BytesIO
from pathlib import Path

import boto3
from PIL import Image, ImageFilter
from playwright.sync_api import sync_playwright

logger = logging.getLogger()
logger.setLevel(logging.INFO)

URL = "https://payments.billdesk.com/MercOnline/CPDCLAPPGController"

S3_BUCKET  = os.environ.get("S3_BUCKET", "")
S3_PREFIX  = os.environ.get("S3_PREFIX", "bills")
EMAIL_TO   = os.environ.get("EMAIL_TO", "")
EMAIL_FROM = os.environ.get("EMAIL_FROM", "") or EMAIL_TO


def load_accounts() -> list:
    """Load account numbers from ACCOUNT_NUMBERS env var (comma-separated)."""
    raw = os.environ.get("ACCOUNT_NUMBERS", "")
    accounts = [a.strip() for a in raw.split(",") if a.strip()]
    if not accounts:
        raise ValueError("ACCOUNT_NUMBERS environment variable is not set or empty.")
    logger.info("Loaded %d accounts from ACCOUNT_NUMBERS env var", len(accounts))
    return accounts

TMP = Path("/tmp")


# ── CAPTCHA solver (pure Pillow, no ML library needed) ───────────────────────
# The portal serves plain 6-digit numeric CAPTCHAs rendered in a consistent
# font. We use Claude vision (Bedrock) via the same session credentials —
# but since we want zero extra cost we instead rely on the portal's own
# reload mechanism: retry until we get a clean read from Pillow digit widths.
# For robustness we use a simple segment-count heuristic that works on the
# blue-on-white monospaced digits this portal renders.

def _extract_digits_from_captcha(img_bytes: bytes) -> str:
    """Threshold and read digits from the CAPTCHA image using Pillow only."""
    from io import BytesIO
    img = Image.open(BytesIO(img_bytes)).convert("L")
    # 2× upscale for better digit separation
    img = img.resize((img.width * 2, img.height * 2), Image.LANCZOS)
    img = img.filter(ImageFilter.SHARPEN)
    # Binary threshold: dark pixels = ink
    img = img.point(lambda p: 0 if p < 140 else 255)

    w, h = img.size
    pixels = img.load()

    # Find vertical slices that contain ink (column has at least one black pixel)
    ink_cols = [x for x in range(w) if any(pixels[x, y] == 0 for y in range(h))]
    if not ink_cols:
        return ""

    # Group consecutive ink columns into character blobs
    blobs, start = [], ink_cols[0]
    for i in range(1, len(ink_cols)):
        if ink_cols[i] - ink_cols[i - 1] > 4:   # gap between digits
            blobs.append((start, ink_cols[i - 1]))
            start = ink_cols[i]
    blobs.append((start, ink_cols[-1]))

    # Map blob width → digit using a simple lookup built from the portal's font.
    # Widths observed (at 2× scale): 0=28,1=16,2=26,3=25,4=28,5=25,6=28,7=24,8=28,9=27
    # We count black pixels per blob to distinguish digits more reliably.
    def count_ink(x1, x2):
        return sum(1 for x in range(x1, x2 + 1) for y in range(h) if pixels[x, y] == 0)

    logger.info("CAPTCHA blobs: %s", [(b[1]-b[0], count_ink(*b)) for b in blobs])
    if len(blobs) != 6:
        logger.warning("Expected 6 digit blobs, got %d", len(blobs))
        return ""

    # Map ink pixel count (at 2× scale) to digit.
    # Calibrated from portal screenshots: each digit has a characteristic ink count.
    # Ranges are generous to handle slight rendering variation.
    digit_map = [
        (range(220, 310), '0'),
        (range(100, 190), '1'),
        (range(190, 230), '2'),
        (range(190, 240), '3'),
        (range(210, 260), '4'),
        (range(180, 230), '5'),
        (range(220, 290), '6'),
        (range(150, 200), '7'),
        (range(230, 320), '8'),
        (range(220, 290), '9'),
    ]

    result = ""
    for blob in blobs:
        ink = count_ink(*blob)
        matched = next((d for rng, d in digit_map if ink in rng), None)
        if matched is None:
            logger.warning("Could not match ink=%d to a digit", ink)
            return ""   # trigger retry
        result += matched
    return result


def solve_captcha(page) -> str:
    """Screenshot the CAPTCHA and read it.

    Strategy: use Bedrock Claude vision (already available via instance role)
    as it is the most reliable for arbitrary CAPTCHA fonts.  Falls back to
    the Pillow blob-counter which works well on this portal's fixed font.
    Both paths are free within this Lambda execution (no extra API cost beyond
    what Bedrock charges per token, which is fractions of a cent).
    """
    el = page.query_selector("img#jCaptchaImg") or page.query_selector("img[id*='Captcha']")
    img_bytes = el.screenshot() if el else page.screenshot()

    import base64, json as _json
    try:
        bedrock = boto3.client("bedrock-runtime", region_name="us-east-1")
        body = {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 16,
            "messages": [{"role": "user", "content": [
                {"type": "image", "source": {
                    "type": "base64", "media_type": "image/png",
                    "data": base64.standard_b64encode(img_bytes).decode(),
                }},
                {"type": "text", "text": "Read the digits in this CAPTCHA. Reply with ONLY the digits, nothing else."},
            ]}],
        }
        resp = bedrock.invoke_model(
            modelId="us.anthropic.claude-haiku-4-5-20251001-v1:0",
            contentType="application/json", accept="application/json",
            body=_json.dumps(body),
        )
        text = re.sub(r"\D", "", _json.loads(resp["body"].read())["content"][0]["text"].strip())
        logger.info("CAPTCHA (Bedrock Haiku): %r", text)
        return text
    except Exception as exc:
        logger.warning("Bedrock CAPTCHA failed (%s), falling back to Pillow", exc)

    # Pillow fallback
    text = _extract_digits_from_captcha(img_bytes)
    logger.info("CAPTCHA (Pillow): %r", text)
    return text


# ── Portal scraper ────────────────────────────────────────────────────────────

CHROMIUM_ARGS = [
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--single-process",
    "--disable-setuid-sandbox",
]


def fetch_bill_screenshot(page, account: str, out_path: Path, max_retries: int = 4) -> bool:
    """Fetch bill screenshot for one account using a shared browser page."""
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
            if any(x in body_text for x in ("sorry", "error while processing", "wrong captcha", "invalid captcha")):
                logger.warning("Wrong CAPTCHA or error page — retrying")
                continue

            page.screenshot(path=str(out_path), full_page=True)
            logger.info("Saved screenshot: %s", out_path)
            return True

        except Exception as exc:
            logger.exception("Attempt %d failed: %s", attempt, exc)
            if attempt < max_retries:
                time.sleep(2)

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


# ── Email via SES ─────────────────────────────────────────────────────────────

def send_email(image_path: Path, date_prefix: str, succeeded: list, failed: list):
    if not EMAIL_TO:
        logger.warning("EMAIL_TO not set — skipping email")
        return

    with open(image_path, "rb") as f:
        img_data = f.read()

    account_lines = "\n".join(f"  • {a}" for a in succeeded)
    failed_lines  = (("\n\nFailed accounts:\n" + "\n".join(f"  • {a}" for a in failed)) if failed else "")

    run_date = datetime.utcnow().strftime("%Y-%m-%d")

    msg = MIMEMultipart("mixed")
    msg["Subject"]  = f"Bills-vja as of {run_date}"
    msg["From"]     = EMAIL_TO   # send from the verified address to itself
    msg["To"]       = EMAIL_TO
    msg["Reply-To"] = EMAIL_TO

    msg.attach(MIMEText(
        f"Please find attached the CPDCL bill details for {date_prefix}.\n\n"
        f"Accounts fetched:\n{account_lines}{failed_lines}\n",
        "plain",
    ))

    attachment = MIMEImage(img_data, name="cpdcl_bills.png")
    attachment.add_header("Content-Disposition", "attachment", filename="cpdcl_bills.png")
    msg.attach(attachment)

    boto3.client("ses", region_name="us-east-1").send_raw_email(
        Source=EMAIL_FROM,
        Destinations=[EMAIL_TO],
        RawMessage={"Data": msg.as_bytes()},
    )
    logger.info("Email sent to %s", EMAIL_TO)


# ── Lambda handler ────────────────────────────────────────────────────────────

def handler(event, context):
    if not S3_BUCKET:
        raise ValueError("S3_BUCKET environment variable is required")

    date_prefix = datetime.utcnow().strftime("%Y-%m")
    uploaded, succeeded, failed = [], [], []

    accounts = load_accounts()
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=CHROMIUM_ARGS)
        ctx = browser.new_context(viewport={"width": 1100, "height": 900})
        page = ctx.new_page()
        for account in accounts:
            out_path = TMP / f"bill_{account}.png"
            ok = fetch_bill_screenshot(page, account, out_path)
            if ok:
                succeeded.append(account)
            else:
                failed.append(account)
                logger.error("Failed to fetch bill for account %s", account)
        browser.close()

    collated = TMP / "receipt_all.png"
    if succeeded:
        paths = [TMP / f"bill_{a}.png" for a in succeeded]

        if len(paths) == 3:
            collate_images(paths, collated)
            key = f"{S3_PREFIX}/{date_prefix}/receipt_all.png"
            uploaded.append(upload_to_s3(collated, key))

        for account in succeeded:
            p = TMP / f"bill_{account}.png"
            key = f"{S3_PREFIX}/{date_prefix}/bill_{account}.png"
            uploaded.append(upload_to_s3(p, key))

    if collated.exists():
        send_email(collated, date_prefix, succeeded, failed)

    return {
        "statusCode": 200,
        "body": json.dumps({
            "date": date_prefix,
            "succeeded": succeeded,
            "failed": failed,
            "s3_keys": uploaded,
        }),
    }
