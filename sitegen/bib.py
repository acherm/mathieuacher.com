"""BibTeX parsing and rendering helpers for the site generator.

Stdlib only. Parses data/acher.bib into Publication objects that carry
everything the templates need: plain-text title/authors/venue, links,
category, award, abstract, and a cleaned BibTeX snippet for citing.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

# Fields kept in the "cite this" BibTeX shown on the site.
CITE_FIELDS = [
    "author", "editor", "title", "booktitle", "journal", "volume", "number",
    "pages", "numpages", "chapter", "series", "year", "month", "publisher",
    "organization", "address", "school", "institution", "type", "howpublished",
    "isbn", "issn", "doi", "url", "eprint", "archiveprefix", "primaryclass",
]


# --------------------------------------------------------------------------
# Parsing


@dataclass
class RawEntry:
    type: str
    key: str
    fields: dict[str, str]
    bare: set[str] = field(default_factory=set)  # fields written without braces (e.g. month = nov)


def parse(text: str) -> list[RawEntry]:
    entries: list[RawEntry] = []
    i, n = 0, len(text)
    while True:
        at = text.find("@", i)
        if at < 0:
            break
        m = re.compile(r"@\s*([A-Za-z]+)\s*([{(])").match(text, at)
        if not m:
            i = at + 1
            continue
        kind = m.group(1).lower()
        close = "}" if m.group(2) == "{" else ")"
        j = m.end()
        if kind in ("comment", "preamble", "string"):
            i = _skip_group(text, m.end() - 1)
            continue
        comma = text.find(",", j)
        key = text[j:comma].strip()
        j = comma + 1
        fields: dict[str, str] = {}
        bare: set[str] = set()
        while True:
            while j < n and text[j] in " \t\r\n,":
                j += 1
            if j >= n:
                raise ValueError(f"unterminated entry {key!r}")
            if text[j] == close:
                j += 1
                break
            fm = re.compile(r"([A-Za-z0-9_\-:.]+)\s*=\s*").match(text, j)
            if not fm:
                raise ValueError(f"bad field in {key!r} near: {text[j:j + 40]!r}")
            name = fm.group(1).lower()
            value, is_bare, j = _read_value(text, fm.end())
            if name not in fields:
                fields[name] = value
                if is_bare:
                    bare.add(name)
        entries.append(RawEntry(kind, key, fields, bare))
        i = j
    return entries


def _skip_group(text: str, start: int) -> int:
    depth = 0
    for k in range(start, len(text)):
        c = text[k]
        if c in "{(":
            depth += 1
        elif c in "})":
            depth -= 1
            if depth == 0:
                return k + 1
    return len(text)


def _read_braced(text: str, start: int) -> tuple[str, int]:
    depth = 0
    for k in range(start, len(text)):
        c = text[k]
        if c == "\\":
            continue
        if c == "{" and text[k - 1] != "\\":
            depth += 1
        elif c == "}" and text[k - 1] != "\\":
            depth -= 1
            if depth == 0:
                return text[start + 1:k], k + 1
    raise ValueError("unbalanced braces")


def _read_value(text: str, j: int) -> tuple[str, bool, int]:
    parts: list[str] = []
    bare = False
    while True:
        c = text[j]
        if c == "{":
            v, j = _read_braced(text, j)
        elif c == '"':
            k = j + 1
            depth = 0
            while not (text[k] == '"' and depth == 0 and text[k - 1] != "\\"):
                if text[k] == "{":
                    depth += 1
                elif text[k] == "}":
                    depth -= 1
                k += 1
            v, j = text[j + 1:k], k + 1
        else:
            m = re.compile(r"[A-Za-z0-9_\-:.]+").match(text, j)
            v, j = m.group(0), m.end()
            bare = True
        parts.append(v)
        while text[j] in " \t\r\n":
            j += 1
        if text[j] == "#":
            j += 1
            while text[j] in " \t\r\n":
                j += 1
            continue
        return "".join(parts), bare and len(parts) == 1, j


# --------------------------------------------------------------------------
# LaTeX -> Unicode

_ACCENTS = {
    "'": "\u0301", "`": "\u0300", "^": "\u0302", '"': "\u0308", "~": "\u0303",
    "=": "\u0304", ".": "\u0307", "u": "\u0306", "v": "\u030c", "H": "\u030b",
    "c": "\u0327", "k": "\u0328", "r": "\u030a", "b": "\u0331", "d": "\u0323",
}
_SYMBOLS = {
    "ss": "ß", "o": "ø", "O": "Ø", "aa": "å", "AA": "Å", "ae": "æ", "AE": "Æ",
    "oe": "œ", "OE": "Œ", "l": "ł", "L": "Ł", "i": "ı", "j": "ȷ",
    "textendash": "–", "textemdash": "—", "ldots": "…", "dots": "…",
    "textellipsis": "…", "textquoteright": "’", "textquoteleft": "‘",
    "textregistered": "®", "texttrademark": "™", "copyright": "©",
    "textasciitilde": "~", "textbackslash": "\\", "LaTeX": "LaTeX", "TeX": "TeX",
    "times": "×", "approx": "≈", "leq": "≤", "geq": "≥", "le": "≤", "ge": "≥",
    "rightarrow": "→", "to": "→", "sim": "∼", "pm": "±", "infty": "∞",
    "alpha": "α", "beta": "β", "gamma": "γ", "delta": "δ", "epsilon": "ε",
    "lambda": "λ", "mu": "μ", "pi": "π", "sigma": "σ", "tau": "τ",
    "euro": "€", "S": "§", "P": "¶", "%": "%", "&": "&", "_": "_", "#": "#", "$": "$",
}
_KEEP_INNER = r"(?:text(?:it|bf|sc|tt|rm|sf|up|sl|normal|superscript|subscript)|emph|mathrm|mathit|mathbf|mathsf|mathtt|mbox|url|uppercase|lowercase|textsuperscript|NoCaseChange|protect)"


def _accent(match: re.Match) -> str:
    cmd, letter = match.group(1), match.group(2) or match.group(3)
    base = {"\\i": "i", "\\j": "j"}.get(letter, letter)
    return unicodedata.normalize("NFC", base + _ACCENTS[cmd])


def latex_to_text(s: str, *, url: bool = False) -> str:
    if not s:
        return ""
    if url:
        return s.replace("\\_", "_").replace("\\%", "%").replace("\\&", "&").replace("\\#", "#").strip("{} ")
    s = s.replace("\\\\", " ")
    # \'{e} \'e {\'e} \'{\i}
    s = re.sub(r"\\(['`^\"~=.])\s*(?:\{\s*(\\?[A-Za-z])\s*\}|(\\[ij](?![A-Za-z])|[A-Za-z]))", _accent, s)
    # \c{c} \v{s} \c c
    s = re.sub(r"\\([uvHckrbd])(?:\s*\{\s*(\\?[A-Za-z])\s*\}|\s+([A-Za-z]))", _accent, s)
    s = re.sub(r"\\href\s*\{[^{}]*\}\s*\{([^{}]*)\}", r"\1", s)
    for _ in range(3):  # nested formatting commands
        s = re.sub(r"\\" + _KEEP_INNER + r"\s*\{([^{}]*)\}", r"\1", s)
    s = re.sub(r"\\([A-Za-z]+)(?![A-Za-z])\s*(?:\{\})?", lambda m: _SYMBOLS.get(m.group(1), m.group(1)), s)
    s = re.sub(r"\\([%&_#$])", r"\1", s)
    s = s.replace("---", "—").replace("--", "–").replace("``", "“").replace("''", "”")
    s = re.sub(r"(?<!\\)~", "\u00a0", s)
    s = s.replace("$", "").replace("{", "").replace("}", "").replace("\\", "")
    return re.sub(r"\s+", " ", s).strip()


def strip_html(s: str) -> str:
    return re.sub(r"<[^>]+>", "", s or "").strip()


# --------------------------------------------------------------------------
# Names


def split_names(value: str) -> list[str]:
    names, depth, cur = [], 0, []
    tokens = re.split(r"(\{|\}|\s+and\s+)", value, flags=re.I)
    for tok in tokens:
        if tok == "{":
            depth += 1
        elif tok == "}":
            depth -= 1
        if depth == 0 and re.fullmatch(r"\s+and\s+", tok, flags=re.I):
            names.append("".join(cur))
            cur = []
        else:
            cur.append(tok)
    names.append("".join(cur))
    return [n.strip() for n in names if n.strip()]


def display_name(name: str) -> str:
    if name.strip().lower() == "others":
        return "et al."
    parts = _split_depth0(name, ",")
    if len(parts) == 1:
        out = parts[0]
    elif len(parts) == 2:
        out = f"{parts[1]} {parts[0]}"
    else:
        out = f"{parts[2]} {parts[0]}, {parts[1]}"
    return latex_to_text(out)


def _split_depth0(s: str, sep: str) -> list[str]:
    out, depth, cur = [], 0, ""
    for c in s:
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
        if c == sep and depth == 0:
            out.append(cur.strip())
            cur = ""
        else:
            cur += c
    out.append(cur.strip())
    return [p for p in out if p]


# --------------------------------------------------------------------------
# Publications

CATEGORIES = {
    "journal": "Journal",
    "conference": "Conference",
    "workshop": "Workshop",
    "book": "Book / chapter",
    "thesis": "Thesis",
    "other": "Preprint / other",
}


@dataclass
class Link:
    label: str
    url: str


@dataclass
class Publication:
    key: str
    type: str
    fields: dict[str, str]
    title: str
    authors: list[dict]
    editors: bool
    year: int
    month: int
    venue: str
    category: str
    selected: bool
    award: str
    note: str
    abstract: str
    acceptance: str
    links: list[Link]
    main_url: str
    doi: str
    bibtex: str

    @property
    def category_label(self) -> str:
        return CATEGORIES[self.category]

    @property
    def authors_text(self) -> str:
        names = [a["name"] for a in self.authors]
        if len(names) <= 2:
            return " and ".join(names)
        return ", ".join(names[:-1]) + ", and " + names[-1]


def _month(fields: dict[str, str]) -> int:
    m = fields.get("month", "").strip().lower()[:3]
    if m.isdigit():
        return int(m)
    return MONTHS.get(m, 0)


def _category(e: RawEntry) -> str:
    f = e.fields
    flag = lambda name, val="1": f.get(name, "").strip().lower() == val
    if e.type in ("phdthesis", "mastersthesis") or flag("sorte", "these"):
        return "thesis"
    if e.type in ("book", "proceedings", "incollection", "inbook") or "chapter" in f:
        return "book"
    if e.type == "article":
        journal = latex_to_text(f.get("journal", "")).lower()
        if journal in ("corr", "arxiv", "arxiv preprint") or "arxiv" in journal:
            return "other"
        if flag("other") or flag("audience", "nationale"):
            return "other"
        return "journal"
    if e.type in ("inproceedings", "conference"):
        if flag("other") or flag("audience", "nationale") or flag("tutorial"):
            return "other"
        if flag("workshop"):
            return "workshop"
        return "conference"
    return "other"


def _venue(e: RawEntry) -> str:
    f = {k: latex_to_text(v) for k, v in e.fields.items() if k not in ("url", "pdf", "doi")}
    t = e.type
    if "chapter" in f and t not in ("incollection",):
        # old convention: title = book title, chapter = chapter title
        return f"Chapter in {f.get('title', '')}" + (f", {f['publisher']}" if f.get("publisher") else "")
    if t == "article":
        v = f.get("journal", "")
        if f.get("volume"):
            v += f", {f['volume']}" + (f"({f['number']})" if f.get("number") else "")
        return v
    if t in ("inproceedings", "conference"):
        return f.get("booktitle", "")
    if t in ("incollection", "inbook"):
        return "In " + f.get("booktitle", f.get("title", "")) + (f", {f['publisher']}" if f.get("publisher") else "")
    if t == "book":
        return f.get("publisher", "")
    if t == "proceedings":
        return "Edited volume" + (f", {f['publisher']}" if f.get("publisher") else "")
    if t == "phdthesis":
        kind = f.get("type") or "PhD thesis"
        return ", ".join(x for x in (kind, f.get("school", "")) if x)
    if t == "mastersthesis":
        return ", ".join(x for x in (f.get("type") or "Master's thesis", f.get("school", "")) if x)
    if t == "techreport":
        kind = f.get("type") or "Technical report"
        num = f" {f['number']}" if f.get("number") else ""
        return ", ".join(x for x in (kind + num, f.get("institution", "")) if x)
    for k in ("journal", "booktitle", "howpublished", "publisher", "institution", "school"):
        if f.get(k):
            return f[k]
    if f.get("eprint"):
        return f"arXiv:{f['eprint']}"
    return ""


def _norm_url(u: str) -> str:
    return re.sub(r"^https?://(www\.)?", "", u.strip()).rstrip("/").lower()


def _links(e: RawEntry) -> tuple[list[Link], str, str]:
    f = e.fields
    get = lambda k: latex_to_text(f.get(k, ""), url=True)
    doi = re.sub(r"^https?://(dx\.)?doi\.org/", "", get("doi"), flags=re.I)
    hal = get("hal_id")
    pdf = get("pdf")
    url = get("url")
    if not pdf and url.lower().endswith(".pdf"):
        pdf, url = url, ""
    links: list[Link] = []
    seen: set[str] = set()

    def add(label: str, u: str) -> None:
        if u and _norm_url(u) not in seen:
            seen.add(_norm_url(u))
            links.append(Link(label, u))

    add("PDF", pdf)
    if hal:
        add("HAL", f"https://hal.science/{hal}")
    if doi:
        add("DOI", f"https://doi.org/{doi}")
    eprint = get("eprint")
    if eprint and (get("archiveprefix").lower() == "arxiv" or get("eprinttype").lower() == "arxiv" or re.fullmatch(r"\d{4}\.\d{4,5}(v\d+)?", eprint)):
        add("arXiv", f"https://arxiv.org/abs/{eprint}")
    # a url pointing at HAL or the DOI is redundant
    if url and not re.search(r"hal\.(science|inria\.fr|archives-ouvertes\.fr)/" + re.escape(hal or "@@"), url) and "doi.org/" not in url:
        add("Link", url)
    for k, label in (("slides", "Slides"), ("youtube_id", "Video"), ("video", "Video"),
                     ("code", "Code"), ("artifact", "Artifact"), ("twitter_id", "Thread")):
        add(label, get(k))
    # preprint of a paper that is also in the bib
    if get("published"):
        add("Published version", f"/publications/#{get('published')}")
    main = next((l.url for l in links if l.label == "DOI"), "") or next(
        (l.url for l in links if l.label in ("HAL", "Link", "arXiv", "PDF")), "")
    return links, main, doi


def cite_bibtex(e: RawEntry) -> str:
    lines = [f"@{e.type}{{{e.key},"]
    for name in CITE_FIELDS:
        if name not in e.fields or not e.fields[name].strip():
            continue
        value = e.fields[name]
        if name == "doi":
            value = re.sub(r"^https?://(dx\.)?doi\.org/", "", value.replace("\\_", "_"), flags=re.I)
        if name in e.bare:
            lines.append(f"  {name} = {value},")
        else:
            lines.append(f"  {name} = {{{value}}},")
    lines[-1] = lines[-1].rstrip(",")
    lines.append("}")
    return "\n".join(lines)


def to_publication(e: RawEntry, me: str = "Acher") -> Publication:
    f = e.fields
    people_field = "author" if f.get("author") else "editor"
    authors = []
    for n in split_names(f.get(people_field, "")):
        name = display_name(n)
        authors.append({"name": name, "me": me.lower() in name.lower()})
    title = latex_to_text(f.get("title", ""))
    if "chapter" in f and e.type not in ("incollection",):
        title = latex_to_text(f["chapter"])
    award = latex_to_text(f.get("award", ""))
    note = latex_to_text(strip_html(f.get("note", "")))
    if not award and re.search(r"award", note, re.I):
        award, note = note, ""
    if award and note.lower() == award.lower():
        note = ""
    links, main, doi = _links(e)
    year_digits = re.sub(r"\D", "", f.get("year", ""))[:4]
    return Publication(
        key=e.key,
        type=e.type,
        fields=f,
        title=title.rstrip("."),
        authors=authors,
        editors=people_field == "editor",
        year=int(year_digits) if year_digits else 0,
        month=_month(f),
        venue=_venue(e),
        category=_category(e),
        selected=f.get("important", "").strip() == "1",
        award=award,
        note=note.strip("() ") if note else "",
        abstract=latex_to_text(f.get("abstract", "")),
        acceptance=latex_to_text(f.get("ar", "")),
        links=links,
        main_url=main,
        doi=doi,
        bibtex=cite_bibtex(e),
    )


def load(path: str) -> list[Publication]:
    with open(path, encoding="utf-8") as fh:
        raw = parse(fh.read())
    pubs = [to_publication(e) for e in raw if e.fields.get("author") or e.fields.get("editor")]
    pubs.sort(key=lambda p: (p.year, p.month), reverse=True)
    return pubs
