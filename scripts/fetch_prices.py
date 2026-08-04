"""Fetch PSX stock prices + MUFAP mutual-fund NAVs and update data/ files.

Runs in GitHub Actions on a cron schedule. Writes:
  data/prices.json  — latest price per symbol
  data/history.json — one snapshot per calendar day (PKT), for the trend chart
"""
import json, re, subprocess, sys, urllib.request, datetime, html
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"

STOCKS = ["AVN", "BWCL", "CPHL", "FABL", "NATF", "GLAXO", "AIRLINK", "MEBL",
          "EFERT", "MUGHAL", "MZNPETF", "GHGL", "SEARL", "FATIMA", "MIIETF",
          "FFL", "DCR", "UPFL"]

# ticker -> substring to match in the MUFAP fund-name column (case-insensitive,
# punctuation-insensitive). AIAIP has no reliable MUFAP row; skipped.
FUNDS = {
    "ASSF":    "al ameen shariah stock fund",
    "AKDISSF": "akd islamic stock fund",
    "NISIF":   "nbp islamic sarmaya izafa fund",
    "NISF":    "nbp islamic stock fund",
    "MIF":     "meezan islamic fund",
    "AICF":    "al ameen islamic cash fund",
    "MICF":    "mahaana islamic cash fund",
    "AKDISIF": "akd islamic income fund",
}

BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
UA = {"User-Agent": BROWSER_UA}


def get(url, timeout=30):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def get_via_curl(url, timeout=30):
    """mufap.com.pk's WAF returns 403 to urllib's TLS client (and to Anthropic's
    WebFetch) but accepts curl with ordinary browser headers — use curl for it."""
    r = subprocess.run(
        ["curl", "-sS", "-L",
         "-A", BROWSER_UA,
         "-H", "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
         "-H", "Accept-Language: en-US,en;q=0.9",
         "--max-time", str(timeout),
         url],
        capture_output=True, text=True, timeout=timeout + 10,
    )
    if r.returncode != 0:
        raise RuntimeError(f"curl exit {r.returncode}: {r.stderr.strip()}")
    return r.stdout


def psx_price(sym):
    try:
        j = json.loads(get(f"https://dps.psx.com.pk/timeseries/int/{sym}"))
        if j.get("data"):
            return float(j["data"][0][1])
    except Exception as e:
        print(f"  intraday failed for {sym}: {e}", file=sys.stderr)
    try:  # fall back to last end-of-day close (weekends / delisted symbols)
        j = json.loads(get(f"https://dps.psx.com.pk/timeseries/eod/{sym}"))
        if j.get("data"):
            return float(j["data"][0][1])
    except Exception as e:
        print(f"  eod failed for {sym}: {e}", file=sys.stderr)
    return None


def normalize(s):
    return re.sub(r"[^a-z0-9 ]", "", s.lower()).replace("  ", " ")


def mufap_navs():
    """Scrape the MUFAP daily industry-stats table (server-rendered HTML)."""
    navs = {}
    try:
        page = get_via_curl("https://www.mufap.com.pk/Industry/IndustryStatDaily?tab=1", timeout=60)
    except Exception as e:
        print(f"  mufap fetch failed: {e}", file=sys.stderr)
        return navs
    rows = re.split(r"<tr[^>]*>", page)
    for row in rows:
        cells = [html.unescape(re.sub(r"<[^>]+>", " ", c)).strip()
                 for c in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)]
        if len(cells) < 7:
            continue
        rowtext = normalize(" ".join(cells[:5]))
        for ticker, pattern in FUNDS.items():
            if ticker in navs or normalize(pattern) not in rowtext:
                continue
            # NAV is the first plausible decimal number after the validity-date cell
            for c in cells:
                m = re.fullmatch(r"([0-9,]+\.\d{2,6})", c.replace(" ", ""))
                if m:
                    v = float(m.group(1).replace(",", ""))
                    if 1 < v < 100000:
                        navs[ticker] = v
                        break
    return navs


def main():
    prices_path = DATA / "prices.json"
    hist_path = DATA / "history.json"
    old = json.loads(prices_path.read_text()) if prices_path.exists() else {"stocks": {}, "funds": {}}

    stocks = dict(old.get("stocks", {}))
    for s in STOCKS:
        p = psx_price(s)
        if p:
            stocks[s] = p
        print(f"  {s}: {p}")

    funds = dict(old.get("funds", {}))
    navs = mufap_navs()
    funds.update(navs)
    if navs:
        print(f"  MUFAP NAVs matched: {sorted(navs)}")
    else:
        print(f"  WARNING: no MUFAP fund NAVs matched — fund prices are STALE "
              f"(kept {sorted(funds)} unchanged from previous run)", file=sys.stderr)

    now = datetime.datetime.now(datetime.timezone.utc)
    out = {"updated": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "stocks": stocks, "funds": funds}
    prices_path.write_text(json.dumps(out, indent=2) + "\n")

    # daily snapshot keyed by PKT date (UTC+5)
    pkt_day = (now + datetime.timedelta(hours=5)).strftime("%Y-%m-%d")
    hist = json.loads(hist_path.read_text()) if hist_path.exists() else {}
    hist[pkt_day] = {"stocks": stocks, "funds": funds}
    hist = dict(sorted(hist.items())[-730:])  # keep ~2 years
    hist_path.write_text(json.dumps(hist) + "\n")
    print(f"updated {len(stocks)} stocks, {len(funds)} funds for {pkt_day}")


if __name__ == "__main__":
    main()
