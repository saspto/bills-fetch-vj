"""
Retrieve payment receipt from BillDesk CPDCL portal.
- Navigates to the URL
- Enters account number
- Reads CAPTCHA image via Claude vision
- Submits the form and screenshots the latest receipt
"""
import base64
import json
import re
import time
import boto3
from playwright.sync_api import sync_playwright

ACCOUNT_NUMBER = "6423244002992"
URL = "https://payments.billdesk.com/MercOnline/CPDCLAPPGController"
OUTPUT_PATH = "/workshop/bills-vja/receipt_screenshot.png"
BEDROCK_MODEL = "global.anthropic.claude-sonnet-4-6"
AWS_REGION = "us-west-2"


def solve_captcha_with_claude(page) -> str:
    """Take a screenshot of the CAPTCHA element and ask Claude (via Bedrock) to read it."""
    bedrock = boto3.client("bedrock-runtime", region_name=AWS_REGION)

    captcha_selectors = [
        "img[src*='captcha']",
        "img[id*='captcha']",
        "img[id*='Captcha']",
        "#captchaImage",
        ".captcha img",
        "img[src*='Captcha']",
    ]

    captcha_img = None
    for sel in captcha_selectors:
        try:
            el = page.query_selector(sel)
            if el:
                captcha_img = el
                print(f"Found captcha with selector: {sel}")
                break
        except Exception:
            pass

    if captcha_img:
        img_bytes = captcha_img.screenshot()
    else:
        print("Captcha element not found by selector, using full page screenshot")
        img_bytes = page.screenshot()

    img_b64 = base64.standard_b64encode(img_bytes).decode()

    body = {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": 64,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": img_b64,
                        },
                    },
                    {
                        "type": "text",
                        "text": (
                            "This image contains a CAPTCHA. "
                            "Please read the text/characters in the CAPTCHA carefully and return ONLY those characters, "
                            "nothing else. No explanation, no punctuation — just the raw CAPTCHA text."
                        ),
                    },
                ],
            }
        ],
    }

    response = bedrock.invoke_model(
        modelId=BEDROCK_MODEL,
        contentType="application/json",
        accept="application/json",
        body=json.dumps(body),
    )
    result = json.loads(response["body"].read())
    captcha_text = result["content"][0]["text"].strip()
    print(f"Claude read CAPTCHA as: {captcha_text!r}")
    return captcha_text


def run():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        context = browser.new_context(viewport={"width": 1280, "height": 900})
        page = context.new_page()

        print(f"Navigating to {URL} ...")
        page.goto(URL, timeout=60000)
        page.wait_for_load_state("networkidle", timeout=30000)

        # Save initial screenshot for debugging
        page.screenshot(path="/workshop/bills-vja/debug_initial.png")
        print("Saved debug_initial.png")

        # Dump page title and some HTML for inspection
        print("Page title:", page.title())
        print("URL now:", page.url)

        # Find account number input
        acct_selectors = [
            "input[name*='account']",
            "input[id*='account']",
            "input[name*='Account']",
            "input[id*='Account']",
            "input[name*='consumer']",
            "input[id*='consumer']",
            "input[type='text']",
        ]
        acct_input = None
        for sel in acct_selectors:
            try:
                el = page.query_selector(sel)
                if el and el.is_visible():
                    acct_input = el
                    print(f"Account input found with: {sel}")
                    break
            except Exception:
                pass

        if not acct_input:
            # Print all input names to help debug
            inputs = page.query_selector_all("input")
            for inp in inputs:
                print("  input:", inp.get_attribute("name"), inp.get_attribute("id"), inp.get_attribute("type"))
            raise RuntimeError("Could not find account number input field")

        acct_input.fill(ACCOUNT_NUMBER)
        print(f"Entered account number: {ACCOUNT_NUMBER}")

        # Solve CAPTCHA
        captcha_text = solve_captcha_with_claude(page)

        # Find captcha input field
        captcha_input_selectors = [
            "input[name*='captcha']",
            "input[id*='captcha']",
            "input[name*='Captcha']",
            "input[id*='Captcha']",
        ]
        captcha_input = None
        for sel in captcha_input_selectors:
            try:
                el = page.query_selector(sel)
                if el and el.is_visible():
                    captcha_input = el
                    print(f"Captcha input found with: {sel}")
                    break
            except Exception:
                pass

        if not captcha_input:
            # Fallback: second text input
            inputs = page.query_selector_all("input[type='text']")
            if len(inputs) >= 2:
                captcha_input = inputs[1]
                print("Using second text input as captcha field")

        if captcha_input:
            captcha_input.fill(captcha_text)
            print("Filled captcha input")
        else:
            print("WARNING: Could not find captcha input, trying anyway")

        # Screenshot before submit
        page.screenshot(path="/workshop/bills-vja/debug_before_submit.png")
        print("Saved debug_before_submit.png")

        # Submit the form
        submit_selectors = [
            "input[type='submit']",
            "button[type='submit']",
            "input[value*='Submit']",
            "input[value*='Go']",
            "button",
        ]
        submitted = False
        for sel in submit_selectors:
            try:
                el = page.query_selector(sel)
                if el and el.is_visible():
                    el.click()
                    submitted = True
                    print(f"Clicked submit: {sel}")
                    break
            except Exception:
                pass

        if not submitted:
            page.keyboard.press("Enter")
            print("Pressed Enter to submit")

        page.wait_for_load_state("networkidle", timeout=30000)
        time.sleep(2)

        page.screenshot(path="/workshop/bills-vja/debug_after_submit.png")
        print("Saved debug_after_submit.png")
        print("Page title after submit:", page.title())
        print("URL after submit:", page.url)

        # Dump all links on the page for debugging
        all_links = page.query_selector_all("a")
        print(f"Links on page ({len(all_links)}):")
        for lnk in all_links:
            href = lnk.get_attribute("href") or ""
            text = lnk.inner_text().strip()
            print(f"  [{text!r}] -> {href}")

        # Dump all buttons
        all_btns = page.query_selector_all("input[type='submit'], button, input[type='button']")
        print(f"Buttons on page ({len(all_btns)}):")
        for btn in all_btns:
            print(f"  value={btn.get_attribute('value')!r} name={btn.get_attribute('name')!r} text={btn.inner_text().strip()!r}")

        # Print page HTML for inspection
        html = page.content()
        print("--- PAGE HTML (first 5000 chars) ---")
        print(html[:5000])
        print("--- END HTML ---")

        # Look for payment history / receipt navigation
        history_selectors = [
            "a:has-text('Payment History')",
            "a:has-text('History')",
            "a:has-text('Receipt')",
            "a:has-text('receipt')",
            "a:has-text('View Receipt')",
            "a[href*='history']",
            "a[href*='History')",
            "a[href*='receipt']",
            "a[href*='Receipt']",
            "a[href*='print']",
            "a[href*='Print']",
        ]
        history_link = None
        for sel in history_selectors:
            try:
                el = page.query_selector(sel)
                if el and el.is_visible():
                    history_link = el
                    print(f"History/receipt link found: {sel}")
                    break
            except Exception:
                pass

        if history_link:
            history_link.click()
            page.wait_for_load_state("networkidle", timeout=30000)
            time.sleep(1)
            page.screenshot(path="/workshop/bills-vja/debug_history.png", full_page=True)
            print("Saved debug_history.png")

            # Now look for individual receipt links in the history table
            receipt_links = page.query_selector_all(
                "a[href*='receipt'], a[href*='Receipt'], a[href*='print'], a[href*='Print'], a:has-text('View'), a:has-text('Receipt')"
            )
            print(f"Receipt links in history: {len(receipt_links)}")
            if receipt_links:
                # Click first (latest) receipt
                with context.expect_page() as new_page_info:
                    receipt_links[0].click()
                receipt_page = new_page_info.value
                receipt_page.wait_for_load_state("networkidle", timeout=30000)
                time.sleep(1)
                receipt_page.screenshot(path=OUTPUT_PATH, full_page=True)
                print(f"Receipt screenshot saved to: {OUTPUT_PATH}")
            else:
                page.screenshot(path=OUTPUT_PATH, full_page=True)
                print(f"History page saved to: {OUTPUT_PATH}")
        else:
            # Check if there's a "Payment History" button/input on bill details page
            # Some portals use a form POST to navigate to history
            html_snippet = page.content()
            if "history" in html_snippet.lower() or "receipt" in html_snippet.lower():
                print("Found 'history'/'receipt' text in page HTML but no visible link")
                # Look for hidden forms or JS links
                inputs_with_history = page.query_selector_all("[value*='History'], [value*='Receipt'], [onclick*='history'], [onclick*='receipt']")
                print(f"History/receipt inputs: {len(inputs_with_history)}")
                for el in inputs_with_history:
                    print(f"  {el.get_attribute('value')!r} onclick={el.get_attribute('onclick')!r}")

            # Save current account details page as the best we have
            page.screenshot(path=OUTPUT_PATH, full_page=True)
            print(f"Saved account details page to: {OUTPUT_PATH}")

        browser.close()
        print("Done.")


if __name__ == "__main__":
    run()
