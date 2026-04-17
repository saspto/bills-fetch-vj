"""
Fetch bill details page for each account number from CPDCL BillDesk portal,
screenshot each one, then collate all 3 into a single A4-sized printable image.
"""
import base64, json, time, boto3
from pathlib import Path
from playwright.sync_api import sync_playwright
from PIL import Image

URL = "https://payments.billdesk.com/MercOnline/CPDCLAPPGController"
AWS_REGION = "us-west-2"
BEDROCK_MODEL = "global.anthropic.claude-sonnet-4-6"
OUT_DIR = Path("/workshop/bills-vja")
ACCOUNTS = ["6423244002992", "6423244145358", "6423244217704"]


def solve_captcha(page) -> str:
    bedrock = boto3.client("bedrock-runtime", region_name=AWS_REGION)
    el = page.query_selector("img[id*='Captcha']") or page.query_selector("img[src*='captcha']")
    img_bytes = el.screenshot() if el else page.screenshot()
    img_b64 = base64.standard_b64encode(img_bytes).decode()
    body = {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": 64,
        "messages": [{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": img_b64}},
            {"type": "text", "text": "Read this CAPTCHA carefully. Return ONLY the exact characters shown, nothing else."},
        ]}],
    }
    r = bedrock.invoke_model(modelId=BEDROCK_MODEL, contentType="application/json",
                             accept="application/json", body=json.dumps(body))
    text = json.loads(r["body"].read())["content"][0]["text"].strip()
    print(f"  CAPTCHA solved: {text!r}")
    return text


def fetch_bill_screenshot(account: str, out_path: Path, max_retries: int = 3) -> bool:
    """Navigate the portal, solve CAPTCHA, and screenshot the bill details page."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        ctx = browser.new_context(viewport={"width": 1100, "height": 900})
        page = ctx.new_page()

        for attempt in range(1, max_retries + 1):
            print(f"  Attempt {attempt}/{max_retries} for account {account}")
            try:
                page.goto(URL, timeout=60000)
                page.wait_for_load_state("networkidle", timeout=30000)

                # Fill account number
                acct_inp = page.query_selector("input[name='txtCustomerID'], input[type='text']")
                if not acct_inp:
                    print("  Could not find account input")
                    continue
                acct_inp.fill(account)

                # Solve and fill captcha
                captcha_text = solve_captcha(page)
                # Use specific id known from page inspection: jcaptchaVal
                cap_inp = page.query_selector("input#jcaptchaVal")
                if not cap_inp:
                    cap_inp = page.query_selector(
                        "input[type='text'][name*='captcha'], input[type='text'][name*='Captcha'], "
                        "input[type='text'][id*='captcha'], input[type='text'][id*='Captcha']"
                    )
                if not cap_inp:
                    # Fallback: second text-type input
                    inputs = page.query_selector_all("input[type='text']")
                    cap_inp = inputs[1] if len(inputs) >= 2 else None
                if cap_inp:
                    cap_inp.fill(captcha_text)
                else:
                    print("  Could not find captcha input")
                    continue

                # Submit
                page.click("input[value='Submit']")
                page.wait_for_load_state("networkidle", timeout=30000)
                time.sleep(1.5)

                # Verify we're on bill details (not error page)
                body_text = page.inner_text("body").lower()
                if "sorry" in body_text or "error" in body_text:
                    print(f"  Error page — likely wrong captcha, retrying...")
                    page.goto(URL, timeout=30000)
                    page.wait_for_load_state("networkidle")
                    continue

                # Screenshot the bill details page
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
    """Crop to just the bill details table, trimming header/footer noise."""
    # Remove bottom transaction-fee section — keep top 780px of the original
    crop_h = min(img.height, 780)
    img = img.crop((0, 0, img.width, crop_h))
    # Trim blue side margins (content sits in the middle ~80% of width)
    left = int(img.width * 0.18)
    right = int(img.width * 0.82)
    img = img.crop((left, 0, right, img.height))
    return img


def collate_images(image_paths: list[Path], output_path: Path):
    """
    Layout: 2 images side-by-side on the top row, 1 centred on the bottom row.
    Canvas: A4 at 150 dpi = 1240 × 1754 px.
    """
    A4_W, A4_H = 1240, 1754
    PAD = 24          # outer & inter-cell padding

    # Load and crop all images
    imgs = [crop_bill(Image.open(p).convert("RGB")) for p in image_paths]

    # ── Top row: two images side by side ────────────────────────────────────
    cell_w = (A4_W - PAD * 3) // 2          # width available per top cell
    row_h  = (A4_H - PAD * 3) // 2          # height available per row

    def fit(img: Image.Image, max_w: int, max_h: int) -> Image.Image:
        scale = min(max_w / img.width, max_h / img.height)
        return img.resize((int(img.width * scale), int(img.height * scale)), Image.LANCZOS)

    top_left  = fit(imgs[0], cell_w, row_h)
    top_right = fit(imgs[1], cell_w, row_h)

    # ── Bottom row: one image centred ────────────────────────────────────────
    bot_w = A4_W - PAD * 2
    bottom = fit(imgs[2], bot_w, row_h)

    # ── Paste onto canvas ────────────────────────────────────────────────────
    canvas = Image.new("RGB", (A4_W, A4_H), "white")

    # Top-left cell: align to top of cell
    canvas.paste(top_left,  (PAD, PAD))
    canvas.paste(top_right, (PAD * 2 + cell_w, PAD))

    # Bottom cell: horizontally centred
    bot_x = (A4_W - bottom.width) // 2
    bot_y = PAD * 2 + row_h
    canvas.paste(bottom, (bot_x, bot_y))

    canvas.save(str(output_path), "PNG", dpi=(150, 150))
    print(f"Collated image saved: {output_path} ({A4_W}x{A4_H} px, 150 dpi)")


def main():
    screenshots = []
    for account in ACCOUNTS:
        print(f"\nFetching bill for account: {account}")
        out = OUT_DIR / f"bill_{account}.png"
        ok = fetch_bill_screenshot(account, out)
        if ok:
            screenshots.append(out)
        else:
            print(f"  FAILED to fetch bill for {account}")

    if screenshots:
        print(f"\nCollating {len(screenshots)} screenshot(s) into A4 page...")
        collate_images(screenshots, OUT_DIR / "receipt_screenshot.png")
    else:
        print("No screenshots to collate.")


if __name__ == "__main__":
    main()
