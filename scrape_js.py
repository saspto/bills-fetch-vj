"""Download and search JS files from the portal for reqid values."""
import requests, re

BASE = "https://payments.billdesk.com/MercOnline"
JS_URLS = [
    f"{BASE}/cpdclappg/js/CPDCLAPPGController.js",
    f"{BASE}/js/jquery-3.7.1.min.js",
    f"{BASE}/cpdclappg/js/app.js",
    f"{BASE}/cpdclappg/js/main.js",
]

session = requests.Session()
# First get the main page to set cookies
r = session.get(f"{BASE}/CPDCLAPPGController", timeout=15)
print("Main page status:", r.status_code)

# Try to find JS references in the page
js_refs = re.findall(r'src=["\']([^"\']*\.js[^"\']*)["\']', r.text)
print("JS refs in page:", js_refs)

# Also look for reqid values in the page HTML
reqids = re.findall(r'reqid["\'\s]*[=:]["\'\s]*([A-Za-z0-9_]+)', r.text)
print("reqid values in HTML:", reqids)

# Try downloading each JS file
for url in JS_URLS + [f"{BASE}/{j}" for j in js_refs]:
    if not url.startswith("http"):
        url = f"https://payments.billdesk.com{url}" if url.startswith("/") else f"{BASE}/{url}"
    try:
        jr = session.get(url, timeout=10)
        if jr.status_code == 200 and len(jr.text) > 100:
            print(f"\n=== {url} ({len(jr.text)} bytes) ===")
            # Search for reqid patterns
            matches = re.findall(r'reqid[^=]*=[^"\']*["\']([A-Za-z0-9_]+)["\']', jr.text)
            print("reqid values:", matches)
            # Also search for history/receipt/payment keywords
            keywords = re.findall(r'["\']([^"\']*(?:history|receipt|payment|History|Receipt|Payment)[^"\']*)["\']', jr.text)
            print("keywords:", keywords[:20])
    except Exception as e:
        print(f"Failed {url}: {e}")
