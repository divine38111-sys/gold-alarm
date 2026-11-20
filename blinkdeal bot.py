#!/usr/bin/env python3
"""
Myntra Blink Deal gold tracker
==============================
Polls Myntra's gold-coin listing every few minutes, detects when a
BLINKDEAL coupon (BLINKDEAL, BLINKDEAL6/7/8/10 ...) is live, logs prices,
and fires a loud push alarm to your phone via ntfy (and optionally Telegram).

Run:   python3 blinkdeal_bot.py              # start tracking
       python3 blinkdeal_bot.py --once       # single check, then exit
       python3 blinkdeal_bot.py --test-alert # send a test alarm to your phone

Only the Python standard library is required. If Myntra blocks plain
requests from your network, install Playwright and the bot falls back to a
real headless browser automatically:
       pip install playwright && playwright install chromium
"""

import argparse
import csv
import json
import os
import random
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime

# ---------------------------------------------------------------- settings --
CONFIG = {
    # ntfy topic: pick something long and unguessable; subscribe to the
    # same topic in the ntfy app on your phone.
    "NTFY_TOPIC": os.getenv("NTFY_TOPIC", "rishi-myntra-gold-CHANGE-ME-7x91"),
    "NTFY_SERVER": os.getenv("NTFY_SERVER", "https://ntfy.sh"),
    # Optional Telegram (leave blank to skip)
    "TELEGRAM_BOT_TOKEN": os.getenv("TELEGRAM_BOT_TOKEN", ""),
    "TELEGRAM_CHAT_ID": os.getenv("TELEGRAM_CHAT_ID", ""),
    # How often to check (seconds). Deal often lasts only 30-45 min.
    "CHECK_EVERY_SEC": int(os.getenv("CHECK_EVERY_SEC", "120")),
    # While the deal stays live, re-ring the alarm this often (minutes).
    "REMIND_EVERY_MIN": int(os.getenv("REMIND_EVERY_MIN", "10")),
    # Optional: only alarm if the best 24K price/gram after coupon is at or
    # below this (0 = alarm whenever the deal is live).
    "MAX_PRICE_PER_GRAM": float(os.getenv("MAX_PRICE_PER_GRAM", "0")),
    # Coupon codes to probe directly (the listing scan also catches new ones).
    "CODES": os.getenv(
        "CODES", "BLINKDEAL6,BLINKDEAL,BLINKDEAL7,BLINKDEAL8,BLINKDEAL10"
    ).split(","),
    "LOG_FILE": os.getenv("LOG_FILE", "gold_prices.csv"),
}

LISTING_URL = "https://www.myntra.com/gold-coin"
COUPON_URL = "https://www.myntra.com/gold-coin?f=Coupons%3A{code}"
CODE_RE = re.compile(r"BLINK\s?DEAL\s?\d{0,2}", re.I)
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)


def log(msg):
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


# ------------------------------------------------------------------ fetch --
def fetch_plain(url):
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
            "Accept-Language": "en-IN,en;q=0.9",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", "replace")


_browser = None


def fetch_browser(url):
    """Headless Chromium fallback (needs `pip install playwright`)."""
    global _browser
    from playwright.sync_api import sync_playwright  # noqa: lazy import

    if _browser is None:
        pw = sync_playwright().start()
        _browser = pw.chromium.launch(headless=True)
    page = _browser.new_page(user_agent=UA, locale="en-IN")
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(3000)
        return page.content()
    finally:
        page.close()


_use_browser = False


def fetch(url):
    """Plain HTTP first; switch to the browser permanently if blocked."""
    global _use_browser
    if not _use_browser:
        try:
            html = fetch_plain(url)
            if "__myx" in html:
                return html
            log("Plain request returned no product data (likely bot wall).")
        except Exception as e:  # 403s, timeouts...
            log(f"Plain request failed: {e}")
        try:
            import playwright  # noqa: F401
            log("Switching to headless browser mode.")
            _use_browser = True
        except ImportError:
            log("Install Playwright for the browser fallback: "
                "pip install playwright && playwright install chromium")
            return ""
    try:
        return fetch_browser(url)
    except Exception as e:
        log(f"Browser fetch failed: {e}")
        return ""


# ------------------------------------------------------------------ parse --
def extract_state(html):
    """Myntra embeds page data as `window.__myx = {...}` in a script tag."""
    m = re.search(r"window\.__myx\s*=\s*(\{.*?\})\s*</script>", html, re.S)
    if not m:
        return {}
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return {}


def find_products(obj, out=None):
    """Walk the JSON and collect anything that looks like a product card."""
    if out is None:
        out = []
    if isinstance(obj, dict):
        if "productId" in obj and ("price" in obj or "mrp" in obj):
            out.append(obj)
        else:
            for v in obj.values():
                find_products(v, out)
    elif isinstance(obj, list):
        for v in obj:
            find_products(v, out)
    return out


GRAM_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:g|gm|gms|gram|grams)\b", re.I)


def product_rows(products, code):
    pct = int(re.sub(r"\D", "", code) or 0)  # BLINKDEAL6 -> 6
    rows = []
    for p in products:
        name = f"{p.get('brand', '')} {p.get('productName') or p.get('product', '')}".strip()
        price = p.get("price") or p.get("discountedPrice") or p.get("mrp")
        try:
            price = float(price)
        except (TypeError, ValueError):
            continue
        g = GRAM_RE.search(name)
        grams = float(g.group(1)) if g else None
        after = round(price * (1 - pct / 100)) if pct else None
        per_g = round((after or price) / grams) if grams else None
        url = p.get("landingPageUrl", "")
        if url and not url.startswith("http"):
            url = "https://www.myntra.com/" + url.lstrip("/")
        rows.append({
            "name": name, "grams": grams, "price": price,
            "after_coupon": after, "per_gram": per_g,
            "is_24k": bool(re.search(r"24\s?k|999", name, re.I)), "url": url,
        })
    return rows


# ------------------------------------------------------------------ check --
def check():
    """Returns (live_codes, rows). rows are coupon-eligible products."""
    live, rows = set(), []

    # 1) Scan the main gold-coin listing for any BLINKDEAL code (catches
    #    new variants like BLINKDEAL9 automatically).
    html = fetch(LISTING_URL)
    for m in CODE_RE.findall(html):
        live.add(re.sub(r"\s", "", m).upper())
    rows_all = product_rows(find_products(extract_state(html)), "") if html else []
    if rows_all:
        cheapest = sorted([r for r in rows_all if r["per_gram"]], key=lambda r: r["per_gram"])
        log(f"HEALTH OK: Myntra listing loaded, {len(rows_all)} gold coins seen."
            + (f" Cheapest now Rs {cheapest[0]['per_gram']:,}/g ({cheapest[0]['name'][:50]})"
               if cheapest else ""))
        log_prices(rows_all, "none")
    else:
        title = re.search(r"<title>(.*?)</title>", html or "", re.S | re.I)
        log("HEALTH WARNING: Myntra page had NO gold coins - probably blocked. "
            f"Page title: {title.group(1).strip()[:80] if title else 'none'} "
            f"| page size: {len(html or '')} chars")

    # 2) Probe each coupon filter; products present => coupon is live.
    for code in dict.fromkeys(CONFIG["CODES"] + sorted(live)):
        code = code.strip().upper()
        if not code:
            continue
        h = fetch(COUPON_URL.format(code=code))
        prods = find_products(extract_state(h))
        if prods:
            live.add(code)
            r = product_rows(prods, code)
            for x in r:
                x["code"] = code
            rows += r
            log_prices(r, code)
        time.sleep(random.uniform(2, 5))

    return sorted(live), rows


def log_prices(rows, code):
    if not rows:
        return
    new = not os.path.exists(CONFIG["LOG_FILE"])
    with open(CONFIG["LOG_FILE"], "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["time", "coupon", "product", "grams", "price",
                        "after_coupon", "per_gram", "url"])
        ts = datetime.now().isoformat(timespec="seconds")
        for r in rows:
            w.writerow([ts, code, r["name"], r["grams"], r["price"],
                        r["after_coupon"], r["per_gram"], r["url"]])


# ----------------------------------------------------------------- alerts --
def send_ntfy(title, body, url, urgent=True):
    if not CONFIG["NTFY_TOPIC"]:
        return
    try:
        req = urllib.request.Request(
            f"{CONFIG['NTFY_SERVER']}/{CONFIG['NTFY_TOPIC']}",
            data=body.encode(),
            headers={
                "Title": title.encode("ascii", "ignore").decode(),
                "Priority": "urgent" if urgent else "default",
                "Tags": "rotating_light,moneybag" if urgent else "white_check_mark",
                "Click": url,
            },
        )
        urllib.request.urlopen(req, timeout=15)
    except Exception as e:
        log(f"ntfy failed: {e}")


def send_telegram(text):
    tok, chat = CONFIG["TELEGRAM_BOT_TOKEN"], CONFIG["TELEGRAM_CHAT_ID"]
    if not (tok and chat):
        return
    try:
        data = urllib.parse.urlencode({"chat_id": chat, "text": text}).encode()
        urllib.request.urlopen(
            f"https://api.telegram.org/bot{tok}/sendMessage", data=data, timeout=15)
    except Exception as e:
        log(f"Telegram failed: {e}")


def alert(title, body, url, urgent=True):
    log(f"ALERT: {title} | {body}")
    print("\a", end="", flush=True)  # terminal bell
    send_ntfy(title, body, url, urgent)
    send_telegram(f"{title}\n{body}\n{url}")


def summarize(codes, rows):
    best = sorted([r for r in rows if r["is_24k"] and r["per_gram"]],
                  key=lambda r: r["per_gram"])[:3]
    lines = [f"Code: {', '.join(codes)}"]
    for r in best:
        lines.append(f"Rs {r['per_gram']:,}/g - {r['name'][:60]} "
                     f"(Rs {r['after_coupon'] or r['price']:,.0f})")
    if not best:
        lines.append("Open Myntra to see eligible coins.")
    return "\n".join(lines), (best[0]["per_gram"] if best else None), (
        best[0]["url"] if best and best[0]["url"] else
        COUPON_URL.format(code=codes[0]))


# ------------------------------------------------------------------- main --
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--test-alert", action="store_true")
    a = ap.parse_args()

    if a.test_alert:
        alert("TEST: Myntra Blink Deal alarm",
              "If your phone rang, alerts are working.", LISTING_URL)
        return

    log(f"Tracking Myntra gold every {CONFIG['CHECK_EVERY_SEC']}s. "
        f"Alerts -> ntfy topic '{CONFIG['NTFY_TOPIC']}'")
    was_live, last_ring = False, 0.0

    while True:
        try:
            codes, rows = check()
        except Exception as e:
            log(f"Check failed: {e}")
            codes, rows = [], []

        if codes:
            body, best_pg, url = summarize(codes, rows)
            cap = CONFIG["MAX_PRICE_PER_GRAM"]
            ok_price = not cap or (best_pg and best_pg <= cap)
            due = (time.time() - last_ring) >= CONFIG["REMIND_EVERY_MIN"] * 60
            if ok_price and (not was_live or due):
                alert("BLINK DEAL LIVE on Myntra gold!" if not was_live
                      else "Blink Deal still live", body, url)
                last_ring = time.time()
            else:
                log(f"Live ({', '.join(codes)}), best Rs {best_pg}/g - no ring")
            was_live = True
        else:
            if was_live:
                alert("Blink Deal ended", "Coupon no longer showing.",
                      LISTING_URL, urgent=False)
            was_live = False
            log("No Blink Deal right now.")

        if a.once:
            break
        time.sleep(CONFIG["CHECK_EVERY_SEC"] + random.uniform(-20, 20))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("Stopped.")
        sys.exit(0)
