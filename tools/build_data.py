#!/usr/bin/env python3
"""
build_data.py - turn the full ARTG CSV into the compact data file the website reads.

  python tools/build_data.py artg_full.csv            # writes site/data/artg.json.gz
  python tools/build_data.py artg_full.csv --out somewhere/artg.json.gz

Uses only the Python standard library. The CSV needs the same columns as artg_full.csv.
Then commit site/data/artg.json.gz to GitHub: Cloudflare redeploys the site automatically.
"""
import argparse, csv, gzip, json, re, sys
from datetime import date
from pathlib import Path

PRE = "https://www.ebs.tga.gov.au/ebs/picmi/picmirepository.nsf/pdf?OpenAgent&id="
REQUIRED = ["ARTG ID", "Product name", "ARTG Date", "Registration Type", "Sponsor", "Licence category", "Licence class"]
MONTHS = {m: i for i, m in enumerate(["january", "february", "march", "april", "may", "june", "july",
                                      "august", "september", "october", "november", "december"])}


def day(s):
    m = re.match(r"^\s*(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})\s*$", s or "")
    if not m or m.group(2).lower() not in MONTHS:
        return None
    return (date(int(m.group(3)), MONTHS[m.group(2).lower()] + 1, int(m.group(1))) - date(1970, 1, 1)).days


def dictionary(values):
    uniq = sorted({v for v in values if v}, key=lambda x: (x.lower(), x))
    idx = {v: i for i, v in enumerate(uniq)}
    return uniq, [idx.get(v, -1) for v in values]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("csv")
    ap.add_argument("--out", default=str(Path(__file__).resolve().parent.parent / "site" / "data" / "artg.json.gz"))
    a = ap.parse_args()

    rows = []
    with open(a.csv, newline="", encoding="utf-8-sig") as f:
        rd = csv.DictReader(f)
        missing = [c for c in REQUIRED if c not in (rd.fieldnames or [])]
        if missing:
            sys.exit("This doesn't look like an ARTG export. Missing column(s): " + ", ".join(missing))
        for r in rd:
            r = {k: (v or "").strip() for k, v in r.items()}
            if r["ARTG ID"].isdigit():
                rows.append(r)
    if len(rows) < 1000:
        sys.exit(f"Only {len(rows)} rows found. The full ARTG file has tens of thousands.")

    g = lambda r, c: r.get(c, "")
    names = [g(r, "Product name") or g(r, "ARTG Name") or re.sub(r"\s*\(\d+\)$", "", g(r, "List title")) for r in rows]
    types, type_i = dictionary([g(r, "Registration Type") for r in rows])
    cats, cat_i = dictionary([g(r, "Licence category") for r in rows])
    clss, cls_i = dictionary([g(r, "Licence class") for r in rows])
    spns, spn_i = dictionary([g(r, "Sponsor") for r in rows])
    mfrs, mfr_i = dictionary([g(r, "Manufacturers") for r in rows])

    slug = {}
    for r in rows:
        if r["Sponsor"] and r["Sponsor"] not in slug and g(r, "Sponsor URL"):
            slug[r["Sponsor"]] = r["Sponsor URL"].rstrip("/").rsplit("/", 1)[-1]

    ing_lists = [[p.strip() for p in g(r, "Ingredients").split("|") if p.strip()] for r in rows]
    ings, _ = dictionary([x for l in ing_lists for x in l])
    ing_idx = {v: i for i, v in enumerate(ings)}
    ing_rows = {str(i): [ing_idx[x] for x in l] for i, l in enumerate(ing_lists) if l}

    rel = lambda u: (u[len(PRE):] if u.startswith(PRE) else u) if u else ""
    pi = {str(i): rel(g(r, "Product Information URL")) for i, r in enumerate(rows) if g(r, "Product Information URL")}
    cmi = {str(i): rel(g(r, "Consumer Medicine Information URL")) for i, r in enumerate(rows) if g(r, "Consumer Medicine Information URL")}

    dates = [day(g(r, "ARTG Date")) for r in rows]
    list_days = [d for d in (day(g(r, "List date")) for r in rows) if d is not None]
    newest = max(list_days or [d for d in dates if d is not None] or [0])
    payload = dict(n=len(rows), id=[int(r["ARTG ID"]) for r in rows], name=names, date=dates,
                   type=type_i, cat=cat_i, cls=cls_i, spn=spn_i, mfr=mfr_i,
                   types=types, cats=cats, clss=clss, spns=spns, spnSlug=[slug.get(s, "") for s in spns], mfrs=mfrs,
                   ings=ings, ingRows=ing_rows, pi=pi, cmi=cmi, piPre=PRE,
                   listDate=date.fromordinal(date(1970, 1, 1).toordinal() + newest).isoformat())
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "wb") as f:
        f.write(gzip.compress(raw, 9, mtime=0))
    print(f"{len(rows):,} goods, snapshot {payload['listDate']}")
    print(f"Wrote {out} ({out.stat().st_size / 1e6:.1f} MB; raw {len(raw) / 1e6:.1f} MB)")
    if out.stat().st_size > 20 * 1024 * 1024:
        print("Warning: Cloudflare Pages allows 25 MiB per file; this is getting close.")


if __name__ == "__main__":
    main()
