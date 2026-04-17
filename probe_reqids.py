"""Probe different reqid values to find payment history page."""
import json, base64, time, boto3
from playwright.sync_api import sync_playwright

ACCOUNT_NUMBER = "6423244002992"
URL = "https://payments.billdesk.com/MercOnline/CPDCLAPPGController"
AWS_REGION = "us-west-2"
BEDROCK_MODEL = "global.anthropic.claude-sonnet-4-6"

def solve_captcha(page):
    bedrock = boto3.client("bedrock-runtime", region_name=AWS_REGION)
    el = page.query_selector("img[id*='Captcha']") or page.query_selector("img[src*='captcha']")
    img_bytes = el.screenshot() if el else page.screenshot()
    img_b64 = base64.standard_b64encode(img_bytes).decode()
    body = {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": 64,
        "messages": [{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": img_b64}},
            {"type": "text", "text": "Read this CAPTCHA. Return ONLY the characters, nothing else."},
        ]}],
    }
    r = bedrock.invoke_model(modelId=BEDROCK_MODEL, contentType="application/json", accept="application/json", body=json.dumps(body))
    return json.loads(r["body"].read())["content"][0]["text"].strip()

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
    ctx = browser.new_context(viewport={"width": 1280, "height": 900})
    page = ctx.new_page()

    # Step 1: load initial form and submit account + captcha
    page.goto(URL, timeout=60000)
    page.wait_for_load_state("networkidle", timeout=30000)

    acct = page.query_selector("input[type='text']")
    acct.fill(ACCOUNT_NUMBER)

    captcha_text = solve_captcha(page)
    print(f"CAPTCHA: {captcha_text}")

    cap_inp = page.query_selector("input[name*='captcha'], input[id*='captcha'], input[name*='Captcha']")
    if cap_inp:
        cap_inp.fill(captcha_text)

    page.click("input[value='Submit']")
    page.wait_for_load_state("networkidle", timeout=30000)
    time.sleep(1)

    # Get the reqtoken from the current page (needed for subsequent requests)
    reqtoken = page.eval_on_selector("input[name='reqtoken']", "el => el.value")
    print(f"reqtoken: {reqtoken}")

    # Print all hidden inputs
    hidden_inputs = page.query_selector_all("input[type='hidden']")
    print("Hidden inputs:")
    for h in hidden_inputs:
        print(f"  name={h.get_attribute('name')!r} value={h.get_attribute('value')!r}")

    # Try injecting reqid=paymentHistory and clicking Submit
    for reqid_val in ["paymentHistory", "getPaymentHistory", "viewHistory", "receiptHistory", "getPastReceipts"]:
        print(f"\nTrying reqid={reqid_val!r} ...")
        page.evaluate(f"""() => {{
            let f = document.forms[0];
            let inp = document.createElement('input');
            inp.type = 'hidden';
            inp.name = 'reqid';
            inp.value = '{reqid_val}';
            f.appendChild(inp);
            f.submit();
        }}""")
        page.wait_for_load_state("networkidle", timeout=15000)
        time.sleep(1)
        title = page.title()
        url_now = page.url
        page.screenshot(path=f"/workshop/bills-vja/debug_reqid_{reqid_val}.png")
        print(f"  title={title!r} url={url_now}")
        # Check for history/receipt content
        content = page.content().lower()
        if "receipt" in content or "history" in content or "transaction" in content:
            print("  ** Found relevant content! **")
            break
        # Go back to account details page for next try
        page.go_back()
        page.wait_for_load_state("networkidle", timeout=15000)

    browser.close()
