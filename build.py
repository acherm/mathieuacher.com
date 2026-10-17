# /// script
# requires-python = ">=3.11"
# dependencies = ["jinja2>=3.1", "markdown>=3.5"]
# ///
"""Build mathieuacher.com into _site/.

    uv run build.py            # build
    uv run build.py --serve    # build, then serve on http://localhost:8000

Content lives in content/ (TOML + Markdown), publications in data/acher.bib,
templates in templates/, assets in static/. Files under legacy/ are copied
verbatim so that old URLs (teaching material, PhD manuscript, ...) keep working.
"""

from __future__ import annotations

import argparse
import datetime as dt
import functools
import http.server
import json
import os
import re
import shutil
import socketserver
import tomllib
from collections import Counter
from pathlib import Path

import markdown as mdlib
from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup

from sitegen import bib

ROOT = Path(__file__).parent
CONTENT = ROOT / "content"
OUT = ROOT / "_site"


# --------------------------------------------------------------------------
# Content


def load_toml(name: str) -> dict:
    with open(CONTENT / name, "rb") as fh:
        return tomllib.load(fh)


def load_md(name: str) -> str:
    return (CONTENT / name).read_text(encoding="utf-8").strip()


def load_content() -> dict:
    return {
        "site": load_toml("site.toml"),
        "about": load_md("about.md"),
        "news": load_toml("news.toml")["news"],
        "research": load_toml("research.toml"),
        "software": load_toml("software.toml")["projects"],
        "teaching": load_md("teaching.md"),
        "theses": load_toml("theses.toml"),
        "service": load_toml("service.toml"),
        "chess": load_md("chess.md"),
    }


# --------------------------------------------------------------------------
# Jinja filters

_md = mdlib.Markdown(extensions=["extra", "sane_lists", "smarty"])


def md_block(text: str) -> Markup:
    _md.reset()
    return Markup(_md.convert(text or ""))


def md_inline(text: str) -> Markup:
    html = md_block(text)
    if html.startswith("<p>") and html.endswith("</p>") and html.count("<p>") == 1:
        html = Markup(html[3:-4])
    return html


def month_year(value: str) -> str:
    parts = str(value).split("-")
    if len(parts) >= 2:
        return dt.date(int(parts[0]), int(parts[1]), 1).strftime("%b %Y")
    return str(value)


def md_escape(text: str) -> str:
    return str(text).replace("[", "\\[").replace("]", "\\]")


def make_env(templates: Path, autoescape: bool) -> Environment:
    env = Environment(
        loader=FileSystemLoader(templates),
        autoescape=select_autoescape(["html"]) if autoescape else False,
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
    )
    env.filters.update(md=md_block, mdi=md_inline, month_year=month_year, md_escape=md_escape,
                       tojson_ld=lambda d: Markup(json.dumps(d, ensure_ascii=False, indent=1)))
    return env


# --------------------------------------------------------------------------
# Structured data (schema.org JSON-LD)


def person_ld(site: dict, pubs_count: int) -> dict:
    p = site["person"]
    url = site["site"]["url"]
    return {
        "@context": "https://schema.org",
        "@type": "Person",
        "@id": f"{url}/#person",
        "name": p["name"],
        "givenName": p["given_name"],
        "familyName": p["family_name"],
        "jobTitle": p["job_title"],
        "description": site["site"]["description"],
        "email": f"mailto:{p['email']}",
        "url": url + "/",
        "image": f"{url}/{p['photo']}",
        "affiliation": [{"@type": a.get("type", "Organization"), "name": a["name"], "url": a["url"]}
                        for a in p["affiliations"]],
        "memberOf": [{"@type": "Organization", "name": m["name"], "url": m["url"]} for m in p.get("member_of", [])],
        "alumniOf": [{"@type": "CollegeOrUniversity", "name": a["name"]} for a in p.get("alumni", [])],
        "knowsAbout": p["knows_about"],
        "award": p.get("awards", []),
        "sameAs": [pr["url"] for pr in site["profiles"] if pr.get("same_as", True)],
    }


def publications_ld(site: dict, pubs: list[bib.Publication]) -> dict:
    url = site["site"]["url"]
    graph = []
    for p in pubs:
        item = {
            "@type": "ScholarlyArticle",
            "@id": f"{url}/publications/#{p.key}",
            "headline": p.title[:110],
            "name": p.title,
            "author": [{"@type": "Person", "name": a["name"]} for a in p.authors],
            "datePublished": str(p.year),
        }
        if p.venue:
            item["isPartOf"] = p.venue
        if p.main_url:
            item["url"] = p.main_url
        if p.doi:
            item["sameAs"] = f"https://doi.org/{p.doi}"
        graph.append(item)
    return {"@context": "https://schema.org", "@graph": graph}


# --------------------------------------------------------------------------
# Build


def build(bib_path: Path) -> None:
    c = load_content()
    site = c["site"]
    pubs = bib.load(str(bib_path))
    years = sorted({p.year for p in pubs}, reverse=True)
    counts = Counter(p.category for p in pubs)
    selected = [p for p in pubs if p.selected or p.award]
    now = dt.datetime.now(dt.timezone.utc)

    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir()

    ctx = dict(
        c,
        pubs=pubs,
        years=years,
        selected=selected,
        counts=counts,
        categories=bib.CATEGORIES,
        awards=[p for p in pubs if p.award],
        built=now.strftime("%Y-%m-%d"),
        built_iso=now.isoformat(timespec="seconds"),
        person_ld=person_ld(site, len(pubs)),
        base=site["site"]["url"],
    )

    html = make_env(ROOT / "templates", autoescape=True)
    text = make_env(ROOT / "templates", autoescape=False)

    pages = {
        "index.html": ("index.html", {"page": "home", "md_url": "/index.md"}),
        "publications/index.html": ("publications.html", {
            "page": "publications", "md_url": "/publications.md",
            "pubs_ld": publications_ld(site, pubs)}),
        "service/index.html": ("service.html", {"page": "service", "md_url": "/service.md"}),
        "acher_bib.html": ("bibtex.html", {"page": "bibtex"}),
        "404.html": ("404.html", {"page": "404"}),
        "services.html": ("redirect.html", {"page": "redirect", "target": "/service/"}),
    }
    for out, (tpl, extra) in pages.items():
        write(out, html.get_template(tpl).render(**ctx, **extra))

    md_pages = {
        "index.md": "md/index.md",
        "publications.md": "md/publications.md",
        "service.md": "md/service.md",
        "llms.txt": "md/llms.txt",
    }
    rendered = {out: tidy_md(text.get_template(tpl).render(**ctx)) for out, tpl in md_pages.items()}
    for out, body in rendered.items():
        write(out, body)
    write("llms-full.txt", "\n\n---\n\n".join(
        rendered[k] for k in ("index.md", "service.md", "publications.md")))

    write("acher.bib", bib_path.read_text(encoding="utf-8"))
    write("sitemap.xml", text.get_template("sitemap.xml").render(**ctx))
    write("robots.txt", text.get_template("robots.txt").render(**ctx))
    write("CNAME", site["site"]["domain"] + "\n")
    write(".nojekyll", "")

    shutil.copytree(ROOT / "static", OUT, dirs_exist_ok=True)
    copy_legacy(ROOT / "legacy", OUT)

    print(f"Built {len(pubs)} publications ({dict(counts)}) into {OUT.relative_to(ROOT)}/")


def tidy_md(body: str) -> str:
    return re.sub(r"\n{3,}", "\n\n", body).strip() + "\n"


def write(rel: str, body: str) -> None:
    path = OUT / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def copy_legacy(src: Path, dst: Path) -> None:
    """Copy legacy files without overwriting generated pages, and give
    directories without an index.html a plain listing (as Apache did)."""
    if not src.exists():
        return
    for path in sorted(src.rglob("*")):
        rel = path.relative_to(src)
        if any(part.startswith(".") for part in rel.parts):
            continue
        target = dst / rel
        if path.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        elif not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
    for d in [src, *[p for p in src.rglob("*") if p.is_dir()]]:
        rel = d.relative_to(src)
        if any(part.startswith(".") for part in rel.parts) or (d / "index.html").exists() or d == src:
            continue
        entries = sorted(p for p in d.iterdir() if not p.name.startswith("."))
        items = "\n".join(
            f'<li><a href="{p.name}{"/" if p.is_dir() else ""}">{p.name}{"/" if p.is_dir() else ""}</a></li>'
            for p in entries)
        (dst / rel / "index.html").write_text(
            f'<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            f"<title>Index of /{rel}/</title><style>body{{font:16px/1.6 system-ui,sans-serif;margin:2rem auto;"
            f"max-width:46rem;padding:0 1rem}}</style><h1>Index of /{rel}/</h1>"
            f'<p><a href="../">Parent directory</a> · <a href="/">mathieuacher.com</a></p><ul>{items}</ul>\n',
            encoding="utf-8")


def serve(port: int) -> None:
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(OUT))
    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer(("127.0.0.1", port), handler) as httpd:
        print(f"Serving on http://localhost:{port}/ (Ctrl+C to stop)")
        httpd.serve_forever()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--serve", action="store_true", help="serve _site/ after building")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--bib", type=Path, default=ROOT / "data" / "acher.bib", help="BibTeX file (default: data/acher.bib)")
    args = ap.parse_args()
    bib_path = args.bib.resolve()
    os.chdir(ROOT)
    build(bib_path)
    if args.serve:
        serve(args.port)
