import json, base64, time, boto3, re, requests
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
    body = {"anthropic_version": "bedrock-2023-05-31", "max_tokens": 64,
        "messages": [{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": img_b64}},
            {"type": "text", "text": "Read this CAPTCHA. Return ONLY the characters, nothing else."},
        ]}]}
    r = bedrock.invoke_model(modelId=BEDROCK_MODEL, contentType="application/json", accept="application/json", body=json.dumps(body))
    return json.loads(r["body"].read())["content"][0]["text"].strip()

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
    ctx = browser.new_context(viewport={"width": 1280, "height": 900})
    page = ctx.new_page()

    page.goto(URL, timeout=60000)
    page.wait_for_load_state("networkidle")
    acct = page.query_selector("input[type='text']")
    acct.fill(ACCOUNT_NUMBER)
    captcha_text = solve_captcha(page)
    print(f"CAPTCHA: {captcha_text}")
    cap_inp = page.query_selector("input[name*='captcha'], input[name*='Captcha']")
    if cap_inp: cap_inp.fill(captcha_text)
    page.click("input[value='Submit']")
    page.wait_for_load_state("networkidle")
    time.sleep(1)

    # Get JS refs from bill details page
    js_refs = page.eval_on_selector_all("script[src]", "els => els.map(e => e.src)")
    print("JS refs:", js_refs)

    # Also get full HTML to search for reqid
    html = page.content()
    reqids = re.findall(r'reqid[^=<>"\']*[=:][^=<>"\']*["\']([A-Za-z0-9_]+)["\']', html)
    print("reqid in HTML:", reqids)

    # Download and inspect each JS file
    cookies = {c["name"]: c["value"] for c in ctx.cookies()}
    for js_url in js_refs:
        if "jquery" in js_url.lower(): continue
        try:
            r = requests.get(js_url, cookies=cookies, timeout=10)
            print(f"\n=== {js_url} ({len(r.text)} bytes) ===")
            # Find reqid patterns
            found = re.findall(r'reqid[^"\'=]*[=][^"\']*["\']([A-Za-z0-9_]+)["\']|["\']reqid["\'][^:]*:[^"\']*["\']([A-Za-z0-9_]+)["\']', r.text)
            print("reqid values:", [x for pair in found for x in pair if x])
            # Print relevant sections
            for m in re.finditer(r'.{0,100}(receipt|history|payment|History|Receipt|Payment).{0,100}', r.text, re.I):
                print(" ", m.group(0)[:200])
        except Exception as e:
            print(f"Failed {js_url}: {e}")

    browser.close()
