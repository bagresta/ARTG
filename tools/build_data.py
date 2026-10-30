#!/usr/bin/env python3
"""
build_data.py - turn the full ARTG CSV into the compact data file the website reads.

  python tools/build_data.py artg_full.csv            # writes site/data/artg.json.gz
  python tools/build_data.py artg_full_updated.xlsx   # an Excel file works too (pip install openpyxl)
  python tools/build_data.py artg_full.csv --out somewhere/artg.json.gz

Uses only the Python standard library (plus openpyxl for .xlsx). The file needs the same columns as artg_full.csv.
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


SCHED_NAMES = {"S2": "Pharmacy Medicine", "S3": "Pharmacist Only Medicine",
               "S4": "Prescription Only Medicine / Prescription Animal Remedy", "S5": "Caution",
               "S6": "Poison", "S7": "Dangerous Poison", "S8": "Controlled Drug",
               "S9": "Prohibited Substance", "S10": "Prohibited (so dangerous that sale is banned)",
               "Not scheduled": "Not in the Poisons Standard schedules"}
SCHED_ORDER = ["S2", "S3", "S4", "S5", "S6", "S7", "S8", "S9", "S10", "Not scheduled"]


def sched_code(v):
    """'(S3) Pharmacist Only Medicine' -> 'S3'; 'Not scheduled...' / 'S1 (intentionally blank)' -> 'Not scheduled'."""
    v = (v or "").strip()
    m = re.match(r"^\(?\s*(S\d+[A-Za-z]?)\s*\)?", v)
    if m and m.group(1).upper() != "S1":
        return m.group(1).upper()
    if m or v.lower().startswith("not "):
        return "Not scheduled"
    return v or ""


def row_schedules(r):
    """Poison schedule codes for one good, from a 'Poison schedule' column or the TGA API record."""
    vals = [x for x in (r.get("Poison schedule", "") or "").split("|") if x.strip()]
    if not vals:
        of = r.get("Other fields", "") or ""
        if of.startswith("TGA API record:"):
            try:
                d = json.loads(of[len("TGA API record:"):].strip())
            except ValueError:
                d = {}
            for p in d.get("Products", []):
                for pk in p.get("Packs", []):
                    if (pk.get("PoisonSchedule") or "").strip():
                        vals.append(pk["PoisonSchedule"])
    return sorted({c for c in (sched_code(v) for v in vals) if c},
                  key=lambda c: SCHED_ORDER.index(c) if c in SCHED_ORDER else 99)


def _n(s):
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def load_poisons(path):
    """Poisons Standard Index (tools/data/poisons_standard_index.csv) -> (substance -> schedules, synonym -> targets)."""
    sched_of, syn = {}, {}
    if not path or not Path(path).exists():
        return sched_of, syn
    with open(path, newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            nm = _n(r.get("Substance"))
            ss = {int(m) for m in re.findall(r"Schedule (\d+)", r.get("Schedules", "") or "")}
            if ss:
                sched_of.setdefault(nm, set()).update(ss)
            elif r.get("Cross reference (see)"):
                syn[nm] = [_n(x) for x in re.split(r"[;,]", r["Cross reference (see)"]) if x.strip()]
    return sched_of, syn


def poisons_match(ingredient, sched_of, syn, single):
    """'e' = the ingredient name is in the Index (directly or via a synonym); 'b' = its first word is
    (a salt or form such as 'amlodipine besilate' -> AMLODIPINE; salt/derivative wording not checked)."""
    x = _n(ingredient)
    if x in sched_of:
        return "e", sorted(sched_of[x])
    if x in syn:
        s = set()
        for tgt in syn[x]:
            s |= sched_of.get(tgt, set())
        if s:
            return "e", sorted(s)
    parts = x.split(" ")
    if len(parts) > 1 and parts[0] in single:
        return "b", sorted(sched_of[parts[0]])
    return None


def dictionary(values):
    uniq = sorted({v for v in values if v}, key=lambda x: (x.lower(), x))
    idx = {v: i for i, v in enumerate(uniq)}
    return uniq, [idx.get(v, -1) for v in values]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("csv")
    ap.add_argument("--poisons", default=str(Path(__file__).resolve().parent / "data" / "poisons_standard_index.csv"),
                    help="Poisons Standard Index CSV (default: tools/data/poisons_standard_index.csv)")
    ap.add_argument("--out", default=str(Path(__file__).resolve().parent.parent / "site" / "data" / "artg.json.gz"))
    a = ap.parse_args()

    def clean(v):
        return "" if v is None else str(v).strip()

    rows = []
    if a.csv.lower().endswith((".xlsx", ".xlsm")):
        try:
            import openpyxl
        except ImportError:
            sys.exit("Reading Excel files needs openpyxl:  pip install openpyxl")
        ws = openpyxl.load_workbook(a.csv, read_only=True, data_only=True).worksheets[0]
        it = ws.iter_rows(values_only=True)
        header = [clean(h) for h in next(it)]
        fieldnames = header
        source = ({h: (row[i] if i < len(row) else None) for i, h in enumerate(header)} for row in it)
    else:
        f = open(a.csv, newline="", encoding="utf-8-sig")
        rd = csv.DictReader(f)
        fieldnames = rd.fieldnames or []
        source = rd
    missing = [c for c in REQUIRED if c not in fieldnames]
    if missing:
        sys.exit("This doesn't look like an ARTG export. Missing column(s): " + ", ".join(missing))
    for r in source:
        r = {k: clean(v) for k, v in r.items() if k}
        r = {c: r.get(c, "") for c in fieldnames if c} | r
        if r.get("ARTG ID", "").isdigit():
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

    sched_of, syn = load_poisons(a.poisons)
    single = {n for n in sched_of if " " not in n and len(n) >= 5}
    ing_ps = {}
    for i, name in enumerate(ings):
        m = poisons_match(name, sched_of, syn, single)
        if m:
            ing_ps[str(i)] = {"m": m[0], "s": m[1]}

    sched_lists = [row_schedules(r) for r in rows]
    scheds = sorted({c for l in sched_lists for c in l}, key=lambda c: SCHED_ORDER.index(c) if c in SCHED_ORDER else 99)
    sch_idx = {c: i for i, c in enumerate(scheds)}
    sched_rows = {str(i): [sch_idx[c] for c in l] for i, l in enumerate(sched_lists) if l}

    dates = [day(g(r, "ARTG Date")) for r in rows]
    list_days = [d for d in (day(g(r, "List date")) for r in rows) if d is not None]
    newest = max(list_days or [d for d in dates if d is not None] or [0])
    payload = dict(n=len(rows), id=[int(r["ARTG ID"]) for r in rows], name=names, date=dates,
                   type=type_i, cat=cat_i, cls=cls_i, spn=spn_i, mfr=mfr_i,
                   types=types, cats=cats, clss=clss, spns=spns, spnSlug=[slug.get(s, "") for s in spns], mfrs=mfrs,
                   ings=ings, ingRows=ing_rows, pi=pi, cmi=cmi, piPre=PRE,
                   ingPS=ing_ps,
                   scheds=scheds, schedNames={c: SCHED_NAMES.get(c, "") for c in scheds}, schedRows=sched_rows,
                   listDate=date.fromordinal(date(1970, 1, 1).toordinal() + newest).isoformat())
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "wb") as f:
        f.write(gzip.compress(raw, 9, mtime=0))
    print(f"{len(rows):,} goods, snapshot {payload['listDate']}")
    meds = sum(1 for r in rows if r.get("Registration Type") == "Medicine")
    med_s = sum(1 for r, l in zip(rows, sched_lists) if r.get("Registration Type") == "Medicine" and l)
    print(f"Poisons Standard: {len(sched_of):,} substances read; {len(ing_ps):,} of {len(ings):,} ARTG ingredients matched"
          + ("" if sched_of else " (no Index file found: skipped)"))
    print(f"Poison schedule known for {med_s:,} of {meds:,} medicines ({scheds or 'none'})")
    print(f"Wrote {out} ({out.stat().st_size / 1e6:.1f} MB; raw {len(raw) / 1e6:.1f} MB)")
    if out.stat().st_size > 20 * 1024 * 1024:
        print("Warning: Cloudflare Pages allows 25 MiB per file; this is getting close.")


if __name__ == "__main__":
    main()
