#!/usr/bin/env python3
"""
artg_extract.py - pull the first N pages of the TGA ARTG listing (default 31 pages = 775 goods)
and write them in the SAME 21-column layout as your artg_full.csv.

How it works
  1. Reads listing pages  https://www.tga.gov.au/resources/artg?page=0 ... page=N-1  (25 goods each)
  2. Opens each good's own page and reads the labelled fields (ARTG ID, ARTG Name, Product name,
     ARTG Date, Registration Type, Therapeutic good type, Sponsor, Manufacturers, Ingredients,
     Licence category, Licence status, Licence class) plus the Sponsor / Summary / Product
     Information / Consumer Medicine Information links.
  3. Writes artg_extract.csv (same columns as your file) and artg_extract_warnings.csv.
  4. With --compare artg_full.csv it also reports, for these goods, which are NEW and which have
     CHANGED fields compared with your CSV (artg_new.csv, artg_changed_fields.csv).

Faster than artg_check.py: it only touches the pages you ask for, runs 6 requests at a time
(about 6 per second overall), and retries politely on errors.

Setup:   pip install requests beautifulsoup4
Usage:   python artg_extract.py
         python artg_extract.py --compare artg_full.csv
         python artg_extract.py --pages 10 --workers 4 --rate 4     # gentler / smaller
Columns not shown on the web pages are filled like this: List title = "ARTG Name (ID)",
List description = the standard TGA sentence, List date = the date shown on the listing, Other fields = blank.
"""
import argparse, csv, json, re, sys, threading, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib import robotparser
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

CONNECT_HELP = """Couldn't get a reply from tga.gov.au.
 - Open https://www.tga.gov.au/resources/artg in your normal browser. If that works, your network is
   probably sending browser traffic through a proxy: re-run with  --proxy http://your-proxy:port
 - If it only fails from Python, the site may be refusing automated requests. Don't try to get round
   that: use the TGA's own 'Export data' button in the ARTG search visualisation tool instead.
 - Otherwise wait a few minutes and try again with a lower rate:  --rate 0.5 --workers 1"""
COLUMNS = ["ARTG ID", "List title", "List date", "List description", "URL", "ARTG Name", "Product name",
           "ARTG Date", "Registration Type", "Therapeutic good type", "Sponsor", "Manufacturers",
           "Ingredients", "Licence category", "Licence status", "Licence class", "Sponsor URL",
           "Summary URL", "Product Information URL", "Consumer Medicine Information URL", "Other fields"]
COMPARE_COLS = ["ARTG Name", "Product name", "ARTG Date", "Registration Type", "Therapeutic good type",
                "Sponsor", "Manufacturers", "Ingredients", "Licence category", "Licence status", "Licence class"]
LABELS = ["ARTG ID", "ARTG Name", "Product name", "ARTG Date", "Registration Type", "Therapeutic good type",
          "Sponsor", "Manufacturers", "Ingredients", "Licence category", "Licence status", "Licence class", "Summary", "Product Information",
          "Consumer Medicine Information"]
MULTI = {"Manufacturers", "Ingredients"}          # values that may span several text items
JUNK = ("skip to main", "help us improve", "we’re experiencing", "we're experiencing", "sponsor content", "back to top")
LABEL_LOWER = {l.lower(): l for l in LABELS}
SUMMARY_FMT = ("https://www.ebs.tga.gov.au/servlet/xmlmillr6?dbid=ebs/PublicHTML/pdfStore.nsf"
               "&docid={id}&agid=(PrintDetailsPublic)&actionid=1")


def norm(s):
    return re.sub(r"\s+", " ", (s or "")).strip()


class RateLimiter:
    def __init__(self, per_second):
        self.gap, self.lock, self.next = 1.0 / per_second, threading.Lock(), 0.0

    def wait(self):
        with self.lock:
            now = time.monotonic()
            if now < self.next:
                time.sleep(self.next - now)
            self.next = max(now, self.next) + self.gap


def make_session(ua, proxy=None):
    s = requests.Session()
    s.headers.update({"User-Agent": ua,
                      "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                      "Accept-Language": "en-AU,en;q=0.9"})
    if proxy:
        s.proxies.update({"http": proxy, "https": proxy})
    retry = Retry(total=5, backoff_factor=1.5, status_forcelist=(429, 500, 502, 503, 504),
                  allowed_methods=("GET",), respect_retry_after_header=True)
    s.mount("https://", HTTPAdapter(max_retries=retry, pool_maxsize=16))
    s.mount("http://", HTTPAdapter(max_retries=retry, pool_maxsize=16))
    return s


# ------------------------------------------------------------------ listing
DATE_RE = re.compile(r"\b\d{1,2} (?:January|February|March|April|May|June|July|August|September|October|November|December) \d{4}\b")
TITLE_ID = re.compile(r"^(.*?)\s*\((\d{3,})\)\s*$")


def parse_listing(content):
    """One listing page -> [(artg_id, name, list_date), ...].
    Each result is a heading like 'Product name (532559)' (usually a link, sometimes plain text),
    followed by its date ('20 October 2026'). We take the ID from the heading text, so a result
    without a link still counts."""
    soup = BeautifulSoup(content, "html.parser")
    scope = soup.find("main") or soup
    out, seen = [], set()
    for h in scope.find_all(["h2", "h3", "h4"]):
        m = TITLE_ID.match(norm(h.get_text(" ")))
        if not m or m.group(2) in seen:
            continue
        seen.add(m.group(2))
        date = ""
        for s in h.find_all_next(string=True, limit=10):
            d = DATE_RE.search(s)
            if d:
                date = d.group(0)
                break
        out.append((int(m.group(2)), norm(m.group(1)), date))
    if not out:   # fallback: plain links whose text ends in (ID)
        for a in scope.find_all("a", href=True):
            m = TITLE_ID.match(norm(a.get_text(" ")))
            if m and m.group(2) not in seen:
                seen.add(m.group(2))
                out.append((int(m.group(2)), norm(m.group(1)), ""))
    return out


# ------------------------------------------------------------------ detail page
def parse_detail(content, base):
    soup = BeautifulSoup(content, "html.parser")
    h1 = soup.find("h1")                       # the title band sits above <main>, so read it first
    title = norm(h1.get_text(" ")) if h1 else ""
    m = re.search(r"ARTG ID\s+(\d{3,})", soup.get_text(" "))
    page_id = m.group(1) if m else ""
    for tg in soup(["script", "style", "noscript", "header", "footer", "nav"]):
        tg.decompose()
    main = soup.find("main") or soup.body or soup
    strings = [norm(s) for s in main.stripped_strings if norm(s)]

    values, cur = {}, None
    for s in strings:
        key = LABEL_LOWER.get(s.lower())
        if key and key not in values:
            cur = key
            values[cur] = []
            continue
        if cur and not s.lower().startswith(JUNK):
            if cur in MULTI or not values[cur]:      # single-value fields keep only the first item
                values[cur].append(s)
    rec = {k: (" | ".join(v) if k in MULTI else (v[0] if v else "")) for k, v in values.items()}

    links = {"sponsor": "", "summary": "", "pi": "", "cmi": ""}
    sponsor_text = ""
    for a in main.find_all("a", href=True):
        h = urljoin(base, a["href"])
        if "/resources/sponsor/" in h and not links["sponsor"]:
            links["sponsor"], sponsor_text = h, norm(a.get_text(" "))
        elif "PrintDetailsPublic" in h and not links["summary"]:
            links["summary"] = h
        elif "picmirepository" in h and re.search(r"-PI-", h) and not links["pi"]:
            links["pi"] = h
        elif "picmirepository" in h and re.search(r"-CMI-", h) and not links["cmi"]:
            links["cmi"] = h
    rec.setdefault("ARTG ID", page_id)
    return rec, links, sponsor_text, title


def build_row(rec, links, sponsor_text, h1, base, listing_name, list_date, warnings):
    m = re.search(r"\((\d+)\)\s*$", h1)
    artg_id = (m.group(1) if m else "") or rec.get("ARTG ID", "").strip()
    if not artg_id.isdigit():
        raise ValueError("no ARTG ID found on page")
    name = rec.get("ARTG Name") or re.sub(r"\s*\(\d+\)\s*$", "", h1) or listing_name
    product = rec.get("Product name", "")
    sponsor = rec.get("Sponsor") or sponsor_text
    if not sponsor and product and name.endswith(" - " + product):      # devices: "Sponsor - Product"
        sponsor = name[: -len(" - " + product)]
        warnings.append((artg_id, "Sponsor", "taken from the ARTG Name prefix"))
    date = rec.get("ARTG Date", "")
    row = {
        "ARTG ID": artg_id,
        "List title": f"{name} ({artg_id})",
        "List date": list_date or date,
        "List description": f"Australian Register of Therapeutic Goods (ARTG) information for {name}.",
        "URL": f"{base}/resources/artg/{artg_id}",
        "ARTG Name": name, "Product name": product, "ARTG Date": date,
        "Registration Type": rec.get("Registration Type", ""),
        "Therapeutic good type": rec.get("Therapeutic good type", ""),
        "Sponsor": sponsor, "Manufacturers": rec.get("Manufacturers", ""),
        "Ingredients": rec.get("Ingredients", ""),
        "Licence category": rec.get("Licence category", ""),
        "Licence status": rec.get("Licence status", ""),
        "Licence class": rec.get("Licence class", ""),
        "Sponsor URL": links["sponsor"],
        "Summary URL": links["summary"] or SUMMARY_FMT.format(id=artg_id),
        "Product Information URL": links["pi"], "Consumer Medicine Information URL": links["cmi"],
        "Other fields": "",
    }
    for col in ("ARTG Date", "Registration Type", "Sponsor", "Licence category"):
        if not row[col]:
            warnings.append((artg_id, col, "empty: check the page layout"))
    if row["Licence class"] and not re.match(r"^(Class \w+|AIMD)$", row["Licence class"]):
        warnings.append((artg_id, "Licence class", f"unexpected value: {row['Licence class'][:40]}"))
    return row


# ------------------------------------------------------------------ main
def read_csv(path):
    with open(path, newline="", encoding="utf-8-sig") as f:
        return {r["ARTG ID"].strip(): r for r in csv.DictReader(f)}


def write_csv(path, header, rows):
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pages", type=int, default=31, help="how many listing pages to read (default 31)")
    ap.add_argument("--start", type=int, default=0, help="first listing page number (default 0)")
    ap.add_argument("--out", default="artg_out")
    ap.add_argument("--compare", help="your existing CSV, to report new and changed goods")
    ap.add_argument("--base", default="https://www.tga.gov.au")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--rate", type=float, default=6.0, help="max requests per second (default 6)")
    ap.add_argument("--proxy", help="e.g. http://proxy.mycompany.com:8080 (otherwise your system proxy settings are used)")
    ap.add_argument("--user-agent", default="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36 artg-extract/1.0 (personal data check; contact: you@example.com)")
    a = ap.parse_args()

    base = a.base.rstrip("/")
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    sess, rl = make_session(a.user_agent, a.proxy), RateLimiter(a.rate)

    rp = robotparser.RobotFileParser(base + "/robots.txt")
    try:
        rp.read()
        if not rp.can_fetch(a.user_agent, base + "/resources/artg"):
            sys.exit("robots.txt disallows /resources/artg. Stopping.")
    except Exception:
        pass

    def get(url, **kw):
        rl.wait()
        r = sess.get(url, timeout=60, **kw)
        r.raise_for_status()
        return r.content

    try:   # one quick test request so a blocked connection gives a clear message, not a traceback
        get(base + "/resources/artg", params={"page": a.start})
    except requests.exceptions.RequestException as e:
        sys.exit(CONNECT_HELP + f"\n\nTechnical detail: {type(e).__name__}")

    # 1. listing pages (in parallel)
    pages = list(range(a.start, a.start + a.pages))
    print(f"Reading {len(pages)} listing pages...")
    found, page_fail = {}, []
    with ThreadPoolExecutor(a.workers) as ex:
        futs = {ex.submit(get, base + "/resources/artg", params={"page": p}): p for p in pages}
        for f in as_completed(futs):
            try:
                found[futs[f]] = parse_listing(f.result())
            except Exception as e:
                page_fail.append(futs[f]); print(f"  listing page {futs[f]} failed: {e}")
    goods, seen = [], set()
    for p in sorted(found):
        for gid, name, ldate in found[p]:
            if gid not in seen:
                seen.add(gid); goods.append((f"{base}/resources/artg/{gid}", name, ldate))
    if not goods:
        sys.exit("No goods found on the listing pages. The page layout may have changed: check parse_listing().")
    print(f"Found {len(goods)} goods. Fetching each good's page...")

    # 2. detail pages (in parallel)
    rows, warnings, errors = {}, [], []

    def work(item):
        url, name, ldate = item
        rec, links, sp, h1 = parse_detail(get(url), base)
        w = []
        row = build_row(rec, links, sp, h1, base, name, ldate, w)
        return row, w

    with ThreadPoolExecutor(a.workers) as ex:
        futs = {ex.submit(work, g): g for g in goods}
        for n, f in enumerate(as_completed(futs), 1):
            try:
                row, w = f.result()
                rows[row["ARTG ID"]] = row
                warnings += w
            except Exception as e:
                errors.append((futs[f][0], str(e))); print(f"  {futs[f][0]} failed: {e}")
            if n % 50 == 0 or n == len(futs):
                print(f"  {n}/{len(futs)}")

    ordered = [rows[i] for i in sorted(rows, key=int, reverse=True)]
    write_csv(out / "artg_extract.csv", COLUMNS, [[r[c] for c in COLUMNS] for r in ordered])
    write_csv(out / "artg_extract_warnings.csv", ["ARTG ID or URL", "Field", "Note"],
              warnings + [(u, "page", e) for u, e in errors])
    print(f"\nWrote {len(ordered)} goods to {out / 'artg_extract.csv'}"
          f" ({len(warnings)} warnings, {len(errors)} failed pages, {len(page_fail)} failed listing pages)")

    # 3. optional comparison with your CSV
    if a.compare:
        mine = read_csv(a.compare)
        new = [r for r in ordered if r["ARTG ID"] not in mine]
        changed = []
        for r in ordered:
            old = mine.get(r["ARTG ID"])
            if not old:
                continue
            for c in COMPARE_COLS:
                if norm(old.get(c)) != norm(r[c]):
                    changed.append([r["ARTG ID"], r["ARTG Name"], c, norm(old.get(c)), norm(r[c])])
        write_csv(out / "artg_new.csv", COLUMNS, [[r[c] for c in COLUMNS] for r in new])
        write_csv(out / "artg_changed_fields.csv", ["ARTG ID", "ARTG Name", "Field", "In your CSV", "On TGA now"], changed)
        print(f"Compared with {a.compare}: {len(new)} new goods, "
              f"{len({c[0] for c in changed})} goods with changed fields ({len(changed)} field changes).")
        print(f"See {out / 'artg_new.csv'} and {out / 'artg_changed_fields.csv'}")


if __name__ == "__main__":
    main()
