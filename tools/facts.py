#!/usr/bin/env python3
"""Check and reuse the structured facts of the site (stdlib only).

    python3 tools/facts.py check              # validate content/*.toml, compare with theses.fr, HAL, the bib
    python3 tools/facts.py check --offline    # validation only, no network
    python3 tools/facts.py tables             # Markdown tables for CVs and application forms
    python3 tools/facts.py tables --private ../private/facts-private.toml --lang fr

Public facts live in content/people.toml, projects.toml, juries.toml, talks.toml, media.toml
and data/acher.bib. Private facts (supervision rates, budgets, contracts) live outside the repo,
in a TOML file passed with --private (default: ../private/facts-private.toml if it exists);
`tables` merges them in, the website never reads them.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
import tomllib
import unicodedata
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONTENT = ROOT / "content"
DEFAULT_PRIVATE = ROOT.parent / "private" / "facts-private.toml"
sys.path.insert(0, str(ROOT))
from sitegen import bib  # noqa: E402

UA = {"User-Agent": "mathieuacher.com-facts/1.0"}
DATE = re.compile(r"^\d{4}(-\d{2}(-\d{2})?)?$")
SCHEMA = {
    "people.toml": ("person", {"kind": {"phd", "associated", "postdoc"},
                               "status": {"ongoing", "defended", "not-defended", None}}),
    "projects.toml": ("project", {}),
    "juries.toml": ("jury", {"degree": {"PhD", "HDR"}, "role": {"reviewer", "examiner", "president", "member"}}),
    "talks.toml": ("talk", {"kind": {"keynote", "invited", "lecture", "outreach", "industry"}}),
    "media.toml": ("item", {"kind": {"exhibition", "video", "talk", "channel", "book", "press", "blog"}}),
}
DATE_FIELDS = ("date", "start", "end", "defense")


def load(name: str) -> dict:
    with open(CONTENT / name, "rb") as fh:
        return tomllib.load(fh)


def norm(name: str) -> str:
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z]+", " ", s).strip()


def year(d) -> int | None:
    return int(str(d)[:4]) if d else None


def get_json(url: str) -> dict:
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30) as r:
        return json.load(r)


# --------------------------------------------------------------------------
# check


def check_schema() -> list[str]:
    problems = []
    for fname, (table, enums) in SCHEMA.items():
        for i, row in enumerate(load(fname).get(table, []), 1):
            label = f"{fname} #{i} ({row.get('name') or row.get('title') or row.get('event')})"
            for field in DATE_FIELDS:
                if field in row and not DATE.match(str(row[field])):
                    problems.append(f"{label}: bad {field} {row[field]!r} (use YYYY, YYYY-MM or YYYY-MM-DD)")
            for field, allowed in enums.items():
                if row.get(field) not in allowed:
                    problems.append(f"{label}: {field} = {row.get(field)!r}, expected one of {sorted(a for a in allowed if a)}")
            if fname == "people.toml":
                if row["kind"] != "postdoc" and not row.get("status"):
                    problems.append(f"{label}: PhD entries need a status")
                if row.get("status") == "defended" and not row.get("defense"):
                    problems.append(f"{label}: defended but no defense date")
    return problems


def check_bib_links(pubs) -> list[str]:
    keys = {p.key for p in pubs}
    out = [f"talks.toml: bib key {t['bib']!r} not in data/acher.bib"
           for t in load("talks.toml")["talk"] if t.get("bib") and t["bib"] not in keys]
    authors = {norm(a["name"]): a["name"] for p in pubs for a in p.authors}
    for person in load("people.toml")["person"]:
        n = norm(person["name"])
        if n not in authors:
            near = [v for k, v in authors.items() if n.split()[-1] in k.split()]
            if near:
                out.append(f"people.toml: {person['name']!r} spelled differently in the bib: {sorted(set(near))}")
    return out


def check_thesesfr() -> list[str]:
    """Compare people.toml with the theses.fr records that list Acher as director."""
    q = urllib.parse.urlencode({"q": "Mathieu Acher", "nombre": 100})
    records = [t for t in get_json(f"https://theses.fr/api/v1/theses/recherche/?{q}").get("theses", [])
               if any(d.get("nom") == "Acher" and d.get("prenom", "").startswith("Mathieu")
                      for d in t.get("directeurs", []))]
    people = load("people.toml")["person"]
    by_id = {p["thesesfr"]: p for p in people if p.get("thesesfr")}
    by_name = {norm(p["name"]): p for p in people if p["kind"] != "postdoc"}
    out = []
    for t in records:
        author = " ".join(f"{a.get('prenom', '')} {a.get('nom', '')}" for a in t.get("auteurs", []))
        p = by_id.get(t["id"]) or by_name.get(norm(author))
        if not p:
            out.append(f"theses.fr {t['id']}: {author} ({t.get('titrePrincipal', '')[:60]}) is not in people.toml")
            continue
        enrolled = t.get("datePremiereInscriptionDoctorat")
        if enrolled and year(p.get("start")) != int(enrolled[-4:]):
            out.append(f"{p['name']}: start {p.get('start')} but theses.fr first enrolment {enrolled}")
        defended = t.get("dateSoutenance")
        if defended:
            d, m, y = defended.split("/")
            if p.get("defense") and not f"{y}-{m}-{d}".startswith(str(p["defense"])):
                out.append(f"{p['name']}: defense {p.get('defense')} but theses.fr says {defended}")
            if p.get("status") != "defended":
                out.append(f"{p['name']}: theses.fr says defended on {defended}")
        if not p.get("thesesfr") and t["id"].startswith("s"):
            out.append(f"{p['name']}: add thesesfr = \"{t['id']}\"")
    return out


def check_hal_awards(pubs) -> list[str]:
    """HAL comments often carry the award ("Distinguished Paper Award"); compare with the bib."""
    with_hal = {p.fields["hal_id"]: p for p in pubs if p.fields.get("hal_id")}
    out, ids = [], sorted(with_hal)
    for i in range(0, len(ids), 40):
        q = urllib.parse.urlencode({"q": f"halId_s:({' OR '.join(ids[i:i + 40])})", "fl": "halId_s,comment_s", "rows": 100})
        for doc in get_json(f"https://api.archives-ouvertes.fr/search/?{q}")["response"]["docs"]:
            comment = doc.get("comment_s") or ""
            p = with_hal[doc["halId_s"]]
            if re.search(r"award|prize|prix", comment, re.I) and (p.award or "").lower() not in comment.lower():
                out.append(f"{p.key}: HAL comment {comment[:80]!r} vs bib award {p.award!r}")
    return out


def cmd_check(args) -> int:
    pubs = bib.load(str(ROOT / "data" / "acher.bib"))
    sections = [("schema", check_schema()), ("bib", check_bib_links(pubs))]
    if not args.offline:
        for name, fn in (("theses.fr", check_thesesfr), ("HAL awards", lambda: check_hal_awards(pubs))):
            try:
                sections.append((name, fn()))
            except OSError as e:
                sections.append((name, [f"could not reach the service: {e}"]))
    total = 0
    for name, problems in sections:
        print(f"## {name}: {len(problems) or 'ok'}")
        for p in problems:
            print(f"- {p}")
        total += len(problems)
    return 1 if total else 0


# --------------------------------------------------------------------------
# tables


def load_private(path: Path | None) -> dict:
    if path and path.exists():
        with open(path, "rb") as fh:
            return tomllib.load(fh)
    return {}


def windows(now: int) -> list[tuple[str, int]]:
    return [("career", 0), ("last 10 years", now - 9), ("last 5 years", now - 4)]


def cmd_tables(args) -> int:
    now = args.now or dt.date.today().year
    private = load_private(args.private)
    prv_people, prv_projects = private.get("people", {}), private.get("projects", {})
    people = load("people.toml")["person"]
    fr = args.lang == "fr"

    def rate(p):
        r = prv_people.get(p["name"], {})
        key = "postdoc_rate" if p["kind"] == "postdoc" else "rate"
        return f"{r[key]} %" if key in r else ""

    phd = [p for p in people if p["kind"] == "phd"]
    defended = sorted((p for p in phd if p["status"] == "defended"), key=lambda p: str(p["defense"]))
    ongoing = sorted((p for p in phd if p["status"] == "ongoing"), key=lambda p: str(p.get("start")))
    print(f"## {'Thèses soutenues' if fr else 'Defended PhD theses'} ({len(defended)}; "
          f"{sum(year(p['defense']) >= now - 4 for p in defended)} {'depuis' if fr else 'since'} {now - 4})\n")
    print("| Name | Period | Defense | Rate | With | Funding | Topic |\n|---|---|---|---|---|---|---|")
    for p in defended:
        print(f"| {p['name']} | {year(p.get('start'))}–{year(p['defense'])} | {p['defense']} | {rate(p)} | "
              f"{p.get('with', '')} | {p.get('funding', '')} | {re.sub(r'\[([^]]+)\]\([^)]+\)', r'\1', p.get('topic', ''))} |")
    print(f"\n## {'Thèses en cours' if fr else 'Ongoing PhD theses'} ({len(ongoing)})\n")
    print("| Name | Start | Rate | With | Funding | Topic |\n|---|---|---|---|---|---|")
    for p in ongoing:
        print(f"| {p['name']} | {p.get('start', '')} | {rate(p)} | {p.get('with', '')} | {p.get('funding', '')} | "
              f"{re.sub(r'\[([^]]+)\]\([^)]+\)', r'\1', p.get('topic', ''))} |")
    not_def = [p["name"] for p in phd if p["status"] == "not-defended"]
    assoc = [p["name"] for p in people if p["kind"] == "associated"]
    if not_def or assoc:
        print(f"\nNot counted: {', '.join(not_def)} (not defended); {', '.join(assoc)} (associated, not supervised).")

    posts = sorted((p for p in people if p["kind"] == "postdoc"), key=lambda p: str(p.get("start")), reverse=True)
    print(f"\n## {'Post-doctorants' if fr else 'Postdocs'} ({len(posts)}; "
          f"{sum((year(p.get('end')) or now) >= now - 4 for p in posts)} {'depuis' if fr else 'since'} {now - 4})\n")
    for p in posts:
        bits = [x for x in (p.get("funding"), rate(p), f"now {p['now']}" if p.get("now") else "") if x]
        print(f"- {p['name']} ({year(p.get('start'))}–{year(p.get('end')) or ''}){': ' + ', '.join(bits) if bits else ''}")

    print(f"\n## {'Projets' if fr else 'Projects'}\n")
    for pr in load("projects.toml")["project"]:
        extra = prv_projects.get(pr["name"], {})
        bits = [x for x in (pr.get("role"), extra.get("budget"), extra.get("note")) if x]
        span = f"{year(pr.get('start'))}–{year(pr.get('end')) or ''}" if pr.get("start") else ""
        print(f"- {pr['name']}{f' ({span})' if span else ''}{': ' + '; '.join(bits) if bits else ''}")

    talks = [t for t in load("talks.toml")["talk"] if t["kind"] in ("keynote", "invited", "lecture")]
    counts = " / ".join(f"{sum(year(t['date']) >= since for t in talks)} ({label})" for label, since in windows(now))
    print(f"\n## {'Conférences invitées' if fr else 'Invited talks'}: {counts}\n")
    for t in sorted(talks, key=lambda t: str(t["date"]), reverse=True):
        print(f"- {t['date']}, {t['kind']}, {t['event']}{' (' + t['place'] + ')' if t.get('place') else ''}"
              f"{': ' + t['title'] if t.get('title') else ''}")

    juries = load("juries.toml")["jury"]
    print(f"\n## {'Jurys' if fr else 'Juries'} ({sum(j['degree'] == 'PhD' for j in juries)} PhD, "
          f"{sum(j['degree'] == 'HDR' for j in juries)} HDR)\n")
    for j in sorted(juries, key=lambda j: j.get("year") or 0, reverse=True):
        print(f"- {j['degree']} {j['role']}: {j['name']} ({', '.join(str(x) for x in (j.get('institution'), j.get('year')) if x)})")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check", help="validate and cross-check the facts")
    c.add_argument("--offline", action="store_true", help="skip theses.fr and HAL")
    t = sub.add_parser("tables", help="print Markdown tables for CVs and application forms")
    t.add_argument("--private", type=Path, default=DEFAULT_PRIVATE if DEFAULT_PRIVATE.exists() else None)
    t.add_argument("--lang", choices=["en", "fr"], default="en")
    t.add_argument("--now", type=int, help="reference year for the 5- and 10-year windows")
    args = ap.parse_args()
    sys.exit(cmd_check(args) if args.cmd == "check" else cmd_tables(args))
