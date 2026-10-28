#!/usr/bin/env python3
"""
artg_check.py - compare your ARTG CSV against the live TGA register.

What it does
  Phase 1 (always): crawls the public ARTG listing (https://www.tga.gov.au/resources/artg?page=N,
            25 goods per page) and compares it with your CSV by ARTG ID:
              NEW      listed on tga.gov.au, not in your CSV
              REMOVED  in your CSV, not in the listing (each one is double-checked by
                       requesting its own ARTG page, because the listing shifts while crawling)
              RENAMED  same ID, different name/title (ignoring symbols, punctuation and spacing)
  Phase 2 (--deep): opens the detail page of every NEW and RENAMED good, extracts the visible
            fields, and keeps a content hash per page in baseline_hashes.json. On later runs a
            different hash flags the good as CHANGED. --recheck-all does this for every good
            (about 97,000 requests: a multi-hour run at a polite pace).

Setup
  pip install requests beautifulsoup4

Usage
  python artg_check.py --csv artg_full.csv --out out --max-pages 5      # quick dry run
  python artg_check.py --csv artg_full.csv --out out                    # full listing comparison
  python artg_check.py --csv artg_full.csv --out out --deep             # + detail pages for new/renamed
  python artg_check.py --csv artg_full.csv --out out --deep --recheck-all

Be polite: default is ~2 requests/second across all workers, with retries and a resumable cache (--resume)
(add --resume to the same command and it carries on where it stopped). Read the TGA site terms before
running it regularly, and consider the "Export data" option in the TGA's ARTG search
visualisation tool, which gives you the same listing as a CSV without crawling.

Outputs (in --out): report.md, new_entries.csv, removed_entries.csv, changed_entries.csv,
                    listing_cache.jsonl, detail_cache.jsonl, baseline_hashes.json
"""
import argparse, csv, hashlib, json, math, re, sys, threading, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib import robotparser
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

PAGE_SIZE = 25
CONNECT_HELP = """Couldn't get a reply from tga.gov.au.
 - Open https://www.tga.gov.au/resources/artg in your normal browser. If that works, your network is
   probably sending browser traffic through a proxy: re-run with  --proxy http://your-proxy:port
   (your IT team or the browser's proxy settings will have the address).
 - If it only fails from Python, the site may be refusing automated requests. Don't try to get round
   that: use the TGA's own 'Export data' button in the ARTG search visualisation tool instead and
   compare the exported file with your CSV.
 - Otherwise wait a few minutes and try again with a lower rate:  --rate 0.5 --workers 1"""
TOTAL_RE = re.compile(r"([\d,]+)\s+result\(s\)\s+found", re.I)


# ---------------------------------------------------------------- http helpers
class RateLimiter:
    def __init__(self, per_second):
        self.gap = 1.0 / per_second
        self.lock = threading.Lock()
        self.next = 0.0

    def wait(self):
        with self.lock:
            now = time.monotonic()
            if now < self.next:
                time.sleep(self.next - now)
            self.next = max(now, self.next) + self.gap


def make_session(user_agent, proxy=None):
    s = requests.Session()
    s.headers.update({"User-Agent": user_agent,
                      "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                      "Accept-Language": "en-AU,en;q=0.9"})
    if proxy:
        s.proxies.update({"http": proxy, "https": proxy})
    retry = Retry(total=5, backoff_factor=1.5, status_forcelist=(429, 500, 502, 503, 504),
                  allowed_methods=("GET",), respect_retry_after_header=True)
    s.mount("http://", HTTPAdapter(max_retries=retry))
    s.mount("https://", HTTPAdapter(max_retries=retry))
    return s


def norm(s):
    return re.sub(r"\s+", " ", (s or "")).strip()


def name_key(s):
    """Loose key so symbols/punctuation/spacing differences don't count as renames."""
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


# ---------------------------------------------------------------- CSV side
def csv_name(row):
    """The CSV's List title looks like 'Name (123456)'; drop the ID suffix."""
    return norm(re.sub(r"\s*\(\d+\)\s*$", "", row.get("List title") or row.get("ARTG Name") or ""))


def load_csv(path):
    out = {}
    with open(path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            try:
                out[int(row["ARTG ID"])] = {"name": csv_name(row), "row": row}
            except (KeyError, ValueError):
                continue
    return out


# ---------------------------------------------------------------- listing crawl
DATE_RE = re.compile(r"\b\d{1,2} (?:January|February|March|April|May|June|July|August|September|October|November|December) \d{4}\b")
TITLE_ID = re.compile(r"^(.*?)\s*\((\d{3,})\)\s*$")


def parse_listing(content):
    """One listing page -> ([(artg_id, name, list_date), ...], total_or_None).
    Each result is a heading like 'Product name (532559)' (usually a link, sometimes plain text),
    followed by its date ('20 October 2026'). We take the ID from the heading text, so a result
    without a link still counts."""
    soup = BeautifulSoup(content, "html.parser")
    total = None
    tm = TOTAL_RE.search(soup.get_text(" ", strip=True))
    if tm:
        total = int(tm.group(1).replace(",", ""))
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
    return out, total


def crawl_listing(sess, base, rl, workers, cache_path, max_pages):
    cache = {}
    if cache_path.exists():
        for line in cache_path.read_text(encoding="utf-8").splitlines():
            d = json.loads(line)
            cache[d["page"]] = d["items"]
    url = urljoin(base, "/resources/artg")
    rl.wait()
    try:
        r = sess.get(url, params={"page": 0}, timeout=60)
        r.raise_for_status()
    except requests.exceptions.RequestException as e:
        sys.exit(CONNECT_HELP + f"\n\nTechnical detail: {type(e).__name__}")
    items0, total = parse_listing(r.content)
    if not items0 or not total:
        sys.exit("Couldn't read the first listing page (no goods or no total found). "
                 "The TGA page layout may have changed: check parse_listing().")
    pages = math.ceil(total / PAGE_SIZE)
    if max_pages:
        pages = min(pages, max_pages)
    print(f"Listing reports {total:,} goods across {math.ceil(total / PAGE_SIZE):,} pages; "
          f"crawling {pages:,}. {len(cache):,} pages already cached.")
    cache[0] = items0
    todo = [p for p in range(pages) if p not in cache]
    lock = threading.Lock()

    def fetch(p):
        rl.wait()
        resp = sess.get(url, params={"page": p}, timeout=60)
        resp.raise_for_status()
        return p, parse_listing(resp.content)[0]

    with open(cache_path, "a", encoding="utf-8") as fh, ThreadPoolExecutor(workers) as ex:
        if 0 in cache and not cache_path.stat().st_size:
            fh.write(json.dumps({"page": 0, "items": items0}) + "\n")
        futs = [ex.submit(fetch, p) for p in todo]
        for n, f in enumerate(as_completed(futs), 1):
            try:
                p, items = f.result()
            except Exception as e:  # keep going; failed pages are reported at the end
                print(f"  page failed: {e}")
                continue
            with lock:
                cache[p] = items
                fh.write(json.dumps({"page": p, "items": items}) + "\n")
                fh.flush()
            if n % 100 == 0 or n == len(futs):
                print(f"  {n:,}/{len(futs):,} pages")
    missing = [p for p in range(pages) if p not in cache]
    live = {}
    for p in sorted(cache):
        for item in cache[p]:
            live.setdefault(item[0], item[1])
    return live, total, missing


# ---------------------------------------------------------------- detail pages
def parse_detail(html):
    """Best-effort field extraction + a hash of the page's visible content."""
    soup = BeautifulSoup(html, "html.parser")
    h1_all = soup.find("h1")
    title = norm(h1_all.get_text(" ")) if h1_all else ""
    for t in soup(["script", "style", "noscript", "header", "footer", "nav"]):
        t.decompose()
    main = soup.find("main") or soup.body or soup
    fields = {}
    if title:
        fields["title"] = title
    for dt in main.find_all("dt"):
        dd = dt.find_next_sibling("dd")
        if dd:
            fields[norm(dt.get_text(" ")).rstrip(":")] = norm(dd.get_text(" "))
    for tr in main.find_all("tr"):
        cells = tr.find_all(["th", "td"])
        if len(cells) == 2:
            fields.setdefault(norm(cells[0].get_text(" ")).rstrip(":"), norm(cells[1].get_text(" ")))
    text = norm(main.get_text(" "))
    return fields, hashlib.sha1(text.encode("utf-8")).hexdigest()


def fetch_detail(sess, base, rl, artg_id):
    rl.wait()
    r = sess.get(urljoin(base, f"/resources/artg/{artg_id}"), timeout=60, allow_redirects=True)
    if r.status_code == 404:
        return {"id": artg_id, "status": 404}
    r.raise_for_status()
    fields, h = parse_detail(r.content)
    return {"id": artg_id, "status": r.status_code, "fields": fields, "hash": h}


def run_details(sess, base, rl, workers, ids, cache_path, label):
    cache = {}
    if cache_path.exists():
        for line in cache_path.read_text(encoding="utf-8").splitlines():
            d = json.loads(line)
            cache[d["id"]] = d
    todo = [i for i in ids if i not in cache]
    print(f"{label}: {len(ids):,} pages wanted, {len(ids) - len(todo):,} cached.")
    with open(cache_path, "a", encoding="utf-8") as fh, ThreadPoolExecutor(workers) as ex:
        futs = {ex.submit(fetch_detail, sess, base, rl, i): i for i in todo}
        for n, f in enumerate(as_completed(futs), 1):
            try:
                d = f.result()
            except Exception as e:
                print(f"  {futs[f]} failed: {e}")
                continue
            cache[d["id"]] = d
            fh.write(json.dumps(d) + "\n")
            fh.flush()
            if n % 200 == 0 or n == len(futs):
                print(f"  {n:,}/{len(futs):,}")
    return {i: cache[i] for i in ids if i in cache}


# ---------------------------------------------------------------- main
def write_csv(path, header, rows):
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--csv", required=True)
    ap.add_argument("--out", default="artg_check_out")
    ap.add_argument("--base", default="https://www.tga.gov.au")
    ap.add_argument("--rate", type=float, default=2.0, help="max requests per second (default 2)")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--max-pages", type=int, default=0, help="only crawl the first N listing pages (dry run)")
    ap.add_argument("--deep", action="store_true", help="fetch detail pages for new and renamed goods")
    ap.add_argument("--recheck-all", action="store_true", help="with --deep: fetch every good (slow)")
    ap.add_argument("--resume", action="store_true", help="reuse caches from an interrupted run (default: start fresh)")
    ap.add_argument("--proxy", help="e.g. http://proxy.mycompany.com:8080 (otherwise your system proxy settings are used)")
    ap.add_argument("--user-agent", default="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36 artg-check/1.0 (personal data check; contact: you@example.com)")
    a = ap.parse_args()

    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    if not a.resume:  # caches only make sense for continuing an interrupted run; baseline_hashes.json is kept
        for name in ("listing_cache.jsonl", "detail_cache.jsonl", "removed_check_cache.jsonl"):
            (out / name).unlink(missing_ok=True)
    sess = make_session(a.user_agent, a.proxy)
    rl = RateLimiter(a.rate)

    rp = robotparser.RobotFileParser(urljoin(a.base, "/robots.txt"))
    try:
        rp.read()
        if not rp.can_fetch(a.user_agent, urljoin(a.base, "/resources/artg")):
            sys.exit("robots.txt disallows crawling /resources/artg. Stopping.")
    except Exception:
        pass  # no robots.txt reachable: carry on

    mine = load_csv(a.csv)
    print(f"CSV: {len(mine):,} goods.")
    live, total, missing_pages = crawl_listing(sess, a.base, rl, a.workers, out / "listing_cache.jsonl", a.max_pages)
    partial = bool(a.max_pages) or bool(missing_pages)
    print(f"Listing: {len(live):,} distinct goods collected.")

    new_ids = sorted(set(live) - set(mine))
    renamed = sorted(i for i in set(live) & set(mine) if live[i] and mine[i]["name"] and
                     name_key(live[i]) != name_key(mine[i]["name"]))
    gone_candidates = [] if partial else sorted(set(mine) - set(live))

    # Double-check removals by asking for each good's own page.
    removed, still_there = [], []
    if gone_candidates:
        res = run_details(sess, a.base, rl, a.workers, gone_candidates, out / "removed_check_cache.jsonl",
                          "Checking possible removals")
        for i in gone_candidates:
            (removed if res.get(i, {}).get("status") == 404 else still_there).append(i)

    details, changed = {}, []
    baseline_path = out / "baseline_hashes.json"
    baseline = json.loads(baseline_path.read_text()) if baseline_path.exists() else {}
    if a.deep:
        want = sorted(live) if a.recheck_all else sorted(set(new_ids) | set(renamed))
        details = run_details(sess, a.base, rl, a.workers, want, out / "detail_cache.jsonl", "Detail pages")
        for i, d in details.items():
            h = d.get("hash")
            if not h:
                continue
            if str(i) in baseline and baseline[str(i)] != h and i not in new_ids:
                changed.append(i)
            baseline[str(i)] = h
        baseline_path.write_text(json.dumps(baseline))

    # ---- write outputs
    write_csv(out / "new_entries.csv", ["ARTG ID", "Name on listing", "URL", "Detail fields (JSON)"],
              [[i, live[i], f"{a.base}/resources/artg/{i}", json.dumps(details.get(i, {}).get("fields", {}))] for i in new_ids])
    write_csv(out / "removed_entries.csv", ["ARTG ID", "Name in CSV", "Sponsor", "Own page status"],
              [[i, mine[i]["name"], mine[i]["row"].get("Sponsor", ""), 404] for i in removed])
    ch_rows = [[i, "renamed", mine[i]["name"], live[i]] for i in renamed]
    ch_rows += [[i, "page content changed", mine.get(i, {}).get("name", live.get(i, "")), live.get(i, "")] for i in changed]
    write_csv(out / "changed_entries.csv", ["ARTG ID", "Change", "Name in CSV", "Name on listing"], ch_rows)

    lines = [
        "# ARTG check", "",
        f"- Your CSV: {len(mine):,} goods",
        f"- TGA listing says: {total:,} goods; collected {len(live):,}",
        f"- New on TGA, not in your CSV: **{len(new_ids):,}**",
        f"- Renamed (same ID, different name): **{len(renamed):,}**",
        f"- Removed (not listed, own page 404): **{len(removed):,}**",
    ]
    if still_there:
        lines.append(f"- Not in listing but own page still loads: {len(still_there):,} (likely listing shifts or cancelled-but-viewable; check manually)")
    if a.deep:
        lines.append(f"- Page content changed since baseline: **{len(changed):,}**" +
                     ("" if any(baseline) and changed else " (first deep run builds the baseline; changes show on the next run)"))
    if partial:
        lines += ["", "> Partial crawl" + (f" (--max-pages {a.max_pages})" if a.max_pages else "") +
                  (f"; {len(missing_pages)} listing pages failed" if missing_pages else "") +
                  ". Removed-entry checks were skipped. Re-run to complete."]
    (out / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n" + "\n".join(lines))
    print(f"\nFiles written to {out.resolve()}")


if __name__ == "__main__":
    main()
