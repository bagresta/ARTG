# ARTG Explorer

A fast, filterable view of the Australian Register of Therapeutic Goods. It's a static website: the page
(`site/index.html`) loads one compact data file (`site/data/artg.json.gz`, about 2 MB) and does all
searching and filtering in the browser.

```
site/               what gets published (Cloudflare serves this folder)
  index.html
  data/artg.json.gz   the register
  _headers            caching and no-index headers
tools/              run on your own computer
  artg_extract.py     pull the newest goods from tga.gov.au (new + changed vs your CSV)
  artg_check.py       slower full check, also finds removed goods
  merge_csv.py        fold the new goods into your full CSV
  build_data.py       turn the full CSV into site/data/artg.json.gz
```

## 1. Put it on GitHub (public repo)

1. On github.com: **New repository**, name it (for example `artg-explorer`), set it to **Public**, create it.
2. Upload this folder's contents (**Add file > Upload files**, then drag in `site`, `tools`, `README.md`
   and `.gitignore`). Don't upload any `.csv` file: the full file is too big and is not needed.

## 2. Publish it with Cloudflare Pages

1. In the Cloudflare dashboard: **Workers & Pages > Create > Pages > Connect to Git**, and choose the repo.
   (The wording moves around from time to time; you want a Pages project connected to GitHub.)
2. Settings: framework **None**, build command **leave empty**, build output directory **`site`**. Deploy.
3. Your site appears at `https://<project-name>.pages.dev`. From now on, every commit to the repo redeploys it
   within a minute or two (the free plan allows 500 builds a month; a single file may be up to 25 MiB).

## 3. Updating the data

Run these on your computer (Python 3 with `pip install requests beautifulsoup4` for the first one):

```
python tools/artg_extract.py --pages 110 --compare artg_full.csv        # new + changed goods from the TGA
python tools/merge_csv.py --base artg_full.csv --new artg_out/artg_extract.csv --out artg_full.csv
python tools/build_data.py artg_full.csv                                 # writes site/data/artg.json.gz
```

Then commit `site/data/artg.json.gz` to GitHub (**Add file > Upload files** replaces it, or `git push`).
Cloudflare redeploys and the site shows the new snapshot date. If the TGA blocks the extract script, use the
TGA's own **Export data** button instead and give that file to `merge_csv.py` as `--new`.

To drop goods that have left the register, also pass `--remove out/removed_entries.csv`
(produced by `artg_check.py`).

## Good to know

- **No login.** The repo is public, so the data file is public too; a password on the page would protect
  nothing. The data is public TGA information. To put a login in front of the *website*, use Cloudflare
  Zero Trust > Access > Applications > Self-hosted, and add your `pages.dev` address. To keep the data itself
  private, make the repo private (Cloudflare can deploy from private repos).
- **Not indexed by search engines** (`noindex` in the page and headers). Remove both if you want it findable.
- **No in-page "Update data" button** on this version; updates are a commit, as above.
- If you open `index.html` straight from your computer it can't load the data. Run
  `python -m http.server` inside `site/` and visit http://localhost:8000 to test locally.
