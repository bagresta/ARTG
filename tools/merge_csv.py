#!/usr/bin/env python3
"""
merge_csv.py - fold newly extracted goods into your full ARTG CSV.

  python tools/merge_csv.py --base artg_full.csv --new artg_out/artg_extract.csv --out artg_full_updated.csv
  python tools/merge_csv.py --base artg_full.csv --new artg_out/artg_extract.csv \
         --remove out/removed_entries.csv --out artg_full_updated.csv

 - goods in --new that aren't in --base are added (newest first, at the top)
 - goods in both are replaced by the --new version (so changed fields are picked up)
 - goods listed in --remove (a CSV with an "ARTG ID" column, e.g. removed_entries.csv from artg_check.py)
   are dropped
Column order follows --base. Standard library only.
"""
import argparse, csv, sys


def read(path):
    with open(path, newline="", encoding="utf-8-sig") as f:
        rd = csv.DictReader(f)
        return rd.fieldnames or [], list(rd)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True)
    ap.add_argument("--new", required=True)
    ap.add_argument("--remove")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()

    cols, base = read(a.base)
    ncols, new = read(a.new)
    if "ARTG ID" not in cols or "ARTG ID" not in ncols:
        sys.exit("Both files need an 'ARTG ID' column.")
    extra = [c for c in ncols if c not in cols]
    gone = [c for c in cols if c not in ncols]
    if extra or gone:
        print(f"Note: column differences. Only in new file (ignored): {extra or 'none'}; missing from new file (left blank): {gone or 'none'}")

    new_by = {r["ARTG ID"].strip(): r for r in new}
    drop = set()
    if a.remove:
        _, rem = read(a.remove)
        drop = {r["ARTG ID"].strip() for r in rem}

    out, replaced, removed, seen = [], 0, 0, set()
    for r in base:
        i = r["ARTG ID"].strip()
        seen.add(i)
        if i in drop:
            removed += 1
            continue
        if i in new_by:
            r = new_by[i]
            replaced += 1
        out.append(r)
    added = [r for i, r in new_by.items() if i not in seen and i not in drop]
    added.sort(key=lambda r: int(r["ARTG ID"]), reverse=True)
    out = added + out

    with open(a.out, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore", restval="")
        w.writeheader()
        w.writerows(out)
    print(f"Base {len(base):,} goods -> {len(out):,}: added {len(added):,}, replaced {replaced:,}, removed {removed:,}")
    print(f"Wrote {a.out}")


if __name__ == "__main__":
    main()
