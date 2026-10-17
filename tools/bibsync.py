#!/usr/bin/env python3
"""bibsync.py -- keep data/acher.bib in sync with DBLP, HAL and ORCID.

Usage (from the project root; Python 3 standard library only):

  python3 tools/bibsync.py                 list online works missing from data/acher.bib
  python3 tools/bibsync.py --since 2025    ... only works published in 2025 or later
  python3 tools/bibsync.py --add           append BibTeX entries for the missing works
  python3 tools/bibsync.py --refresh       re-download everything (ignore the cache)
  python3 tools/bibsync.py --check         validate the bib (syntax, keys, required fields,
                                           per-year counts); add --check-dois to resolve DOIs
Other options: --bib PATH, --cache DIR, --include-skipped (also add filtered candidates),
               --ignore FILE (default tools/bibsync-ignore.txt: one source id per line, e.g.
               'dblp:conf/icse/RandrianainaKZA22', 'hal:hal-01234567', 'orcid:198184414' or
               'doi:10.1101/2025.11.26.25341061', '#' starts a comment), --verbose.

How it works
  * Fetches DBLP (pid 57/2898; via the SPARQL endpoint sparql.dblp.org because the
    dblp.org web API now sits behind a bot check), HAL (idHal mathieu-acher, plus his
    ORCID) and ORCID (0000-0003-1483-3858). Every response is cached in tools/.cache/.
    Downloaded data is only parsed (json/xml/regex), never executed.
  * Works are clustered across sources (DOI, arXiv id, near-identical title) and matched
    against data/acher.bib by normalized DOI first, then by fuzzy title (difflib).
  * Missing works are listed with their sources. Obvious non-papers (front matter,
    prefaces, errata, software deposits...) and CoRR preprints superseded by a published
    version are listed as "skipped" with the reason.
  * --add builds entries from DBLP metadata when available (else HAL, else Crossref),
    adds hal_id / hal_version / pdf from HAL, the arXiv id (eprint) when a preprint
    exists, and an abstract from HAL -> OpenAlex -> Crossref -> Semantic Scholar -> arXiv.
    New keys follow <firstauthorlastname><year><firsttitleword> (suffix b, c... if taken).
    New entries are appended at the end of the file: review them, then re-sort with
    --check --sort (sorts by year desc, month desc, unknown month last).
"""

import argparse
import difflib
import hashlib
import html
import json
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import OrderedDict

# --------------------------------------------------------------------------- config
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_BIB = os.path.join(ROOT, 'data', 'acher.bib')
DEFAULT_CACHE = os.path.join(ROOT, 'tools', '.cache')
DEFAULT_IGNORE = os.path.join(ROOT, 'tools', 'bibsync-ignore.txt')

ORCID_ID = '0000-0003-1483-3858'
HAL_IDHAL = 'mathieu-acher'
DBLP_NAME = 'Mathieu Acher'
DBLP_PID = '57/2898'          # resolved via the DBLP author search if this one fails
AUTHOR_LASTNAME = 'acher'

USER_AGENT = 'bibsync/1.0 (+https://mathieuacher.com; publication list maintenance)'
MIN_INTERVAL = {               # seconds between two requests to the same host
    'dblp.org': 3.0, 'sparql.dblp.org': 1.0, 'api.semanticscholar.org': 3.5,
    'export.arxiv.org': 3.0, 'api.crossref.org': 0.3, 'api.openalex.org': 0.2,
    'pub.orcid.org': 0.5, 'api.archives-ouvertes.fr': 0.3, 'doi.org': 0.2,
}
HAL_FIELDS = ('halId_s,uri_s,title_s,en_title_s,abstract_s,en_abstract_s,doiIdentifier_s,'
              'docType_s,producedDateY_i,producedDateM_i,publicationDateY_i,publicationDateM_i,'
              'conferenceStartDateY_i,conferenceStartDateM_i,authFullName_s,authLastName_s,'
              'journalTitle_s,conferenceTitle_s,bookTitle_s,fileMain_s,arxivId_s,version_i,'
              'audience_s,keyword_s,comment_s,label_bibtex,city_s,country_s,page_s,volume_s,'
              'issue_s,publisher_s,language_s,submitType_s')

MONTHS = ['jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'oct', 'nov', 'dec']

# Output field order; unknown fields go between 'important' and 'abstract'.
FIELD_ORDER = [
    'author', 'editor', 'title', 'chapter', 'booktitle', 'journal', 'series', 'volume',
    'number', 'pages', 'articleno', 'numpages', 'edition', 'publisher', 'organization',
    'institution', 'school', 'address', 'location', 'type', 'howpublished', 'year', 'month',
    'isbn', 'issn', 'doi', 'eprint', 'archiveprefix', 'eprinttype', 'primaryclass', 'url',
    'hal_id', 'hal_version', 'pdf', 'slides', 'youtube_id', 'twitter_id', 'keywords',
    'lang', 'language', 'audience', 'workshop', 'other', 'sorte', 'tutorial', 'ar',
    'award', 'note', 'important',
]
LAST_FIELDS = ['abstract']

FRONT_MATTER_RE = re.compile(
    r'^\s*(front\s*matter|back\s*matter|preface|foreword|message from|welcome|editorial\b|'
    r'guest editorial|table of contents|organizing committee|program committee|committees?\b|'
    r'author index|erratum|errata|correction to|corrigendum|retraction|title page|copyright)',
    re.I)
HAL_SKIP_DOCTYPES = {'SOFTWARE', 'VIDEO', 'IMG', 'SON', 'MAP', 'PATENT', 'NOTE', 'BLOG',
                     'TRAD', 'NOTICE', 'MEMLIC', 'ETABTHESE'}
HAL_OTHER_DOCTYPES = {'UNDEFINED', 'PREPRINT', 'REPORT', 'POSTER', 'PRESCONF', 'LECTURE',
                      'OTHER', 'OTHERREPORT', 'REPACT', 'CREPORT', 'SYNTHESE', 'MEM', 'ISSUE',
                      'PROCEEDINGS'}

# --------------------------------------------------------------------------- HTTP + cache


class Http:
    """Tiny cached, throttled HTTP client (GET/POST); caches 200s and 404s to disk."""

    def __init__(self, cache_dir, refresh=False, verbose=False):
        self.cache_dir = cache_dir
        self.refresh = refresh
        self.verbose = verbose
        self.last = {}
        os.makedirs(cache_dir, exist_ok=True)

    def _path(self, name):
        safe = re.sub(r'[^A-Za-z0-9._-]+', '_', name)
        if len(safe) > 120:
            safe = safe[:100] + '_' + hashlib.md5(name.encode()).hexdigest()[:10]
        return os.path.join(self.cache_dir, safe)

    def _throttle(self, url):
        host = urllib.parse.urlparse(url).hostname or ''
        wait = MIN_INTERVAL.get(host, 0.5)
        dt = time.time() - self.last.get(host, 0)
        if dt < wait:
            time.sleep(wait - dt)
        self.last[host] = time.time()

    def get(self, url, cache_name, headers=None, data=None, retries=3):
        """Return (status, text). status 200 / 404 / 'blocked' / other int / None."""
        path = self._path(cache_name)
        if not self.refresh:
            if os.path.exists(path):
                with open(path, encoding='utf-8') as fh:
                    return 200, fh.read()
            if os.path.exists(path + '.404'):
                return 404, ''
        hdrs = {'User-Agent': USER_AGENT}
        hdrs.update(headers or {})
        body = urllib.parse.urlencode(data).encode() if isinstance(data, dict) else data
        for attempt in range(retries):
            self._throttle(url)
            if self.verbose:
                print('  GET', url[:150], file=sys.stderr)
            req = urllib.request.Request(url, data=body, headers=hdrs)
            try:
                with urllib.request.urlopen(req, timeout=90) as resp:
                    raw = resp.read()
                    text = raw.decode(resp.headers.get_content_charset() or 'utf-8', 'replace')
                    status = resp.status
            except urllib.error.HTTPError as err:
                status, text = err.code, ''
                if err.code == 404 or err.code == 410:
                    open(path + '.404', 'w').close()
                    return 404, ''
                if err.code in (429, 500, 502, 503, 504) and attempt < retries - 1:
                    time.sleep(5 * (attempt + 1))
                    continue
                return err.code, ''
            except Exception as err:  # network error
                if attempt < retries - 1:
                    time.sleep(3 * (attempt + 1))
                    continue
                print('  ! network error for %s: %s' % (url[:120], err), file=sys.stderr)
                return None, ''
            if status == 202 or 'Making sure you&#39;re not a bot' in text[:2000]:
                return 'blocked', ''
            with open(path, 'w', encoding='utf-8') as fh:
                fh.write(text)
            return status, text
        return None, ''

    def json(self, url, cache_name, headers=None, data=None):
        h = {'Accept': 'application/json'}
        h.update(headers or {})
        status, text = self.get(url, cache_name, h, data)
        if status != 200 or not text:
            return None
        try:
            return json.loads(text)
        except ValueError:
            return None

    def doi_status(self, doi):
        """Ask doi.org itself whether a DOI is registered: 'ok', 'missing' or 'unknown'."""
        url = 'https://doi.org/api/handles/' + urllib.parse.quote(doi, safe='/')
        status, text = self.get(url, 'doi/' + doi)
        if status == 200:
            try:
                return 'ok' if json.loads(text).get('responseCode') == 1 else 'missing'
            except ValueError:
                return 'unknown'
        if status == 404:
            return 'missing'
        return 'unknown'


# --------------------------------------------------------------------------- BibTeX model


class Entry:
    def __init__(self, etype, key):
        self.type = etype.lower()
        self.key = key
        self.fields = OrderedDict()   # lower-case name -> value (without outer delimiters)
        self.bare = set()             # names whose value was a bare token (macro / number)
        self.dups = []                # (name, value) of repeated fields (first one is kept)
        self.notes = []               # human-readable change log (used by reports)

    def get(self, name, default=''):
        return self.fields.get(name.lower(), default)

    def has(self, name):
        return bool(self.fields.get(name.lower(), '').strip().strip('{}').strip())

    def set(self, name, value, bare=False):
        name = name.lower()
        self.fields[name] = value
        if bare:
            self.bare.add(name)
        else:
            self.bare.discard(name)

    def add_parsed(self, name, value, bare):
        name = name.lower()
        if name in self.fields:
            self.dups.append((name, value, bare))
            return
        self.set(name, value, bare)

    def remove(self, name):
        self.fields.pop(name.lower(), None)
        self.bare.discard(name.lower())

    def year(self):
        m = re.search(r'\d{4}', self.get('year'))
        return int(m.group(0)) if m else 0

    def month(self):
        v = self.get('month').strip('{} ').lower()[:3]
        return MONTHS.index(v) + 1 if v in MONTHS else (int(v) if v.isdigit() else 0)


class BibError(Exception):
    pass


def _parse_braced(text, i):
    """text[i] == '{'; return (inner, index after closing brace)."""
    depth, j = 0, i
    n = len(text)
    while j < n:
        c = text[j]
        if c == '\\':
            j += 2
            continue
        if c == '{':
            depth += 1
        elif c == '}':
            depth -= 1
            if depth == 0:
                return text[i + 1:j], j + 1
        j += 1
    raise BibError('unbalanced braces starting at offset %d (line %d)'
                   % (i, text.count('\n', 0, i) + 1))


def _parse_value(text, j, close_ch):
    parts = []
    n = len(text)
    while True:
        while j < n and text[j].isspace():
            j += 1
        if j >= n:
            raise BibError('unexpected end of file')
        c = text[j]
        if c == '{':
            inner, j = _parse_braced(text, j)
            parts.append((inner, False))
        elif c == '"':
            k, depth = j + 1, 0
            while k < n:
                if text[k] == '\\':
                    k += 2
                    continue
                if text[k] == '{':
                    depth += 1
                elif text[k] == '}':
                    depth -= 1
                elif text[k] == '"' and depth == 0:
                    break
                k += 1
            parts.append((text[j + 1:k], False))
            j = k + 1
        else:
            m = re.compile(r'[^\s,#{}()"]+').match(text, j)
            if not m:
                raise BibError('bad value at line %d' % (text.count('\n', 0, j) + 1))
            parts.append((m.group(0), True))
            j = m.end()
        while j < n and text[j].isspace():
            j += 1
        if j < n and text[j] == '#':
            j += 1
            continue
        break
    if len(parts) == 1:
        return parts[0][0], parts[0][1], j
    return ''.join(p for p, _ in parts), False, j


def parse_bib(text):
    """Parse BibTeX text -> (entries, junk). junk = non-blank text found outside entries."""
    entries, junk = [], []
    i, n = 0, len(text)
    head_re = re.compile(r'@\s*([A-Za-z]+)\s*([{(])')
    key_re = re.compile(r'\s*([^,\s{}()]+)\s*,')
    name_re = re.compile(r'\s*([A-Za-z0-9_:.+\-]+)\s*=')
    while i < n:
        at = text.find('@', i)
        if at < 0:
            junk.append(text[i:])
            break
        junk.append(text[i:at])
        m = head_re.match(text, at)
        if not m:
            junk.append('@')
            i = at + 1
            continue
        etype, open_ch = m.group(1).lower(), m.group(2)
        close_ch = '}' if open_ch == '{' else ')'
        j = m.end()
        if etype in ('comment', 'preamble', 'string'):
            if open_ch == '{':
                _, i = _parse_braced(text, m.end() - 1)
            else:
                i = text.index(')', j) + 1
            continue
        km = key_re.match(text, j)
        if not km:
            raise BibError('cannot read key at line %d' % (text.count('\n', 0, j) + 1))
        entry = Entry(etype, km.group(1))
        j = km.end()
        while True:
            while j < n and (text[j].isspace() or text[j] == ','):
                j += 1
            if j >= n:
                raise BibError('unterminated entry %s' % entry.key)
            if text[j] == close_ch:
                j += 1
                break
            nm = name_re.match(text, j)
            if not nm:
                raise BibError('bad field in entry %s at line %d'
                               % (entry.key, text.count('\n', 0, j) + 1))
            value, bare, j = _parse_value(text, nm.end(), close_ch)
            entry.add_parsed(nm.group(1), value, bare)
        entries.append(entry)
        i = j
    junk = [s.strip() for s in junk if s.strip()]
    return entries, junk


def _norm_space(v):
    return ' '.join(v.split())


def format_value(name, value, bare):
    if bare:
        if value.isdigit():
            return '{%s}' % value
        if name == 'month' and value.lower()[:3] in MONTHS:
            return value.lower()[:3]
        return value
    if name == 'month' and value.strip().lower()[:3] in MONTHS and len(value.strip()) <= 9:
        return value.strip().lower()[:3]
    if name == 'abstract':
        v = value.strip()
        if v.startswith('{') and v.endswith('}'):
            try:
                inner, end = _parse_braced(v, 0)
                if end == len(v):
                    v = '{' + inner.strip() + '}'
            except BibError:
                pass
        v = '\n'.join(line.rstrip() for line in v.split('\n'))
    else:
        v = _norm_space(value)
    return '{%s}' % v


def ordered_fields(entry):
    names = list(entry.fields)
    first = [f for f in FIELD_ORDER if f in entry.fields]
    last = [f for f in LAST_FIELDS if f in entry.fields]
    middle = [f for f in names if f not in first and f not in last]
    imp = first.index('important') + 1 if 'important' in first else len(first)
    return first[:imp] + middle + first[imp:] + last


def format_entry(entry):
    lines = ['@%s{%s,' % (entry.type, entry.key)]
    names = ordered_fields(entry)
    for idx, name in enumerate(names):
        val = format_value(name, entry.fields[name], name in entry.bare)
        lines.append('  %s = %s%s' % (name, val, ',' if idx < len(names) - 1 else ''))
    lines.append('}')
    return '\n'.join(lines)


def sort_entries(entries):
    def k(item):
        pos, e = item
        m = e.month()
        return (-e.year(), 0 if m else 1, -m, pos)
    return [e for _, e in sorted(enumerate(entries), key=k)]


def write_bib(path, entries, header=None):
    with open(path, 'w', encoding='utf-8') as fh:
        if header:
            fh.write(header.rstrip() + '\n\n')
        fh.write('\n\n'.join(format_entry(e) for e in entries) + '\n')


# --------------------------------------------------------------------------- normalisation

_ACCENTS = {"'": '\u0301', '`': '\u0300', '^': '\u0302', '"': '\u0308', '~': '\u0303',
            '=': '\u0304', '.': '\u0307', 'c': '\u0327', 'v': '\u030c', 'u': '\u0306',
            'H': '\u030b', 'k': '\u0328', 'r': '\u030a'}
_SPECIAL = {'aa': 'å', 'AA': 'Å', 'o': 'ø', 'O': 'Ø', 'ss': 'ß', 'ae': 'æ', 'AE': 'Æ',
            'oe': 'œ', 'OE': 'Œ', 'l': 'ł', 'L': 'Ł', 'i': 'ı', 'j': 'ȷ'}


def latex_to_text(s):
    """Best-effort LaTeX -> Unicode (accents, a few symbols); drops remaining braces."""
    if not s:
        return ''
    s = re.sub(r'\\([\'`^"~=.])\s*\{?\s*(\\?[A-Za-z])\s*\}?',
               lambda m: (m.group(2).lstrip('\\').replace('i', 'i') + _ACCENTS[m.group(1)]), s)
    s = re.sub(r'\\([cvuHkr])\s*\{\s*(\\?[A-Za-z])\s*\}',
               lambda m: m.group(2).lstrip('\\') + _ACCENTS[m.group(1)], s)
    s = re.sub(r'\\(aa|AA|ss|ae|AE|oe|OE|o|O|l|L|i|j)\b\s*',
               lambda m: _SPECIAL[m.group(1)], s)
    s = s.replace('\\&', '&').replace('\\%', '%').replace('\\_', '_').replace('\\#', '#')
    s = s.replace('\\$', '$').replace('---', '\u2014').replace('--', '\u2013').replace('~', ' ')
    s = re.sub(r'\\[A-Za-z]+\s*', '', s)
    s = s.replace('{', '').replace('}', '')
    return unicodedata.normalize('NFC', s)


def ascii_fold(s):
    s = unicodedata.normalize('NFKD', latex_to_text(s))
    return ''.join(c for c in s if not unicodedata.combining(c))


def norm_title(s):
    s = ascii_fold(html.unescape(s or '')).lower()
    s = re.sub(r'\((submitted|preprint|draft|under review|working paper)\)', ' ', s)
    s = re.sub(r'[^a-z0-9]+', ' ', s)
    return ' '.join(s.split())


def norm_doi(s):
    if not s:
        return ''
    s = latex_to_text(s.strip()).strip().strip('{}').strip()
    s = s.replace('\\_', '_')
    s = re.sub(r'^(https?://)?(dx\.)?doi\.org/', '', s, flags=re.I)
    s = re.sub(r'^doi:\s*', '', s, flags=re.I)
    return s.strip().rstrip('.').strip()


def plausible_doi(s):
    """Reject placeholders such as 10.1145/nnnnnnn.nnnnnnn or truncated DOIs."""
    d = norm_doi(s)
    return bool(re.match(r'^10\.\d{4,9}/\S{3,}$', d)) and not re.search(r'n{4,}|x{4,}', d, re.I)


def doi_key(s):
    return norm_doi(s).lower()


def norm_arxiv(s):
    if not s:
        return ''
    s = s.strip().strip('{}')
    s = re.sub(r'^(https?://)?(dx\.)?doi\.org/', '', s, flags=re.I)
    s = re.sub(r'^(arxiv:|https?://arxiv\.org/(abs|pdf)/)', '', s, flags=re.I)
    s = re.sub(r'^10\.48550/arxiv\.', '', s, flags=re.I)
    s = re.sub(r'(\.pdf)$', '', s)
    s = re.sub(r'v\d+$', '', s)
    return s.lower()


_ORDINALS = {'first': '1', 'second': '2', 'third': '3', 'fourth': '4', 'fifth': '5',
             'sixth': '6', 'seventh': '7', 'eighth': '8', 'eight': '8', 'ninth': '9',
             'tenth': '10', 'eleventh': '11', 'twelfth': '12'}


def number_signature(title):
    """Numbers / ordinals in a title ('REVE 2017', 'Fifth ...' -> {'2017', '5'})."""
    sig = set()
    for tok in norm_title(title).split():
        if tok in _ORDINALS:
            sig.add(_ORDINALS[tok])
        else:
            m = re.match(r'^(\d+)(st|nd|rd|th)?$', tok)
            if m:
                sig.add(m.group(1))
    return sig


def title_ratio(a, b):
    """Similarity in [0, 1]; 0 when the titles carry different numbers (editions, years)."""
    na, nb = norm_title(a), norm_title(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    if number_signature(na) != number_signature(nb):
        return 0.0
    sm = difflib.SequenceMatcher(None, na, nb)
    if sm.real_quick_ratio() < 0.6 or sm.quick_ratio() < 0.6:
        return 0.0
    return sm.ratio()


def token_containment(a, b):
    """Share of the shorter title's words found in the longer one (numbers must agree)."""
    ta = {_ORDINALS.get(t, re.sub(r'(st|nd|rd|th)$', '', t) if t[:1].isdigit() else t)
          for t in norm_title(a).split()}
    tb = {_ORDINALS.get(t, re.sub(r'(st|nd|rd|th)$', '', t) if t[:1].isdigit() else t)
          for t in norm_title(b).split()}
    if len(ta) > len(tb):
        ta, tb = tb, ta
    sig = number_signature(a)
    if len(ta) < 8 or (not sig and len(ta) < 10) or sig != number_signature(b):
        return 0.0
    return len(ta & tb) / float(len(ta))


def split_name(name):
    """'Last, First' or 'First Last' -> (first, last)."""
    name = _norm_space(name)
    if ',' in name:
        last, first = [p.strip() for p in name.split(',', 1)]
        return first, last
    toks = name.split(' ')
    particles = {'de', 'da', 'del', 'della', 'di', 'do', 'dos', 'du', 'le', 'la', 'van',
                 'von', 'der', 'den', 'ter', 'ten', 'el', 'al', 'bin', 'ben', 'st.'}
    k = len(toks) - 1
    for i in range(1, len(toks) - 1):
        if toks[i].lower() in particles:
            k = i
            break
    return ' '.join(toks[:k]), ' '.join(toks[k:])


def entry_authors(entry):
    raw = entry.get('author') or entry.get('editor')
    return [a.strip() for a in re.split(r'\s+and\s+', _norm_space(raw)) if a.strip()]


TITLE_STOPWORDS = set('a an the on of in for to and at by with from towards toward is are '
                      'do does how what when why can should we our you your it its as into '
                      'via vs versus not or le la les un une des du de et poster'.split())


def _key_words(title):
    words = []
    for w in re.findall(r"[a-z0-9]+(?:-[a-z0-9]+)*", ascii_fold(latex_to_text(title)).lower()):
        parts = w.split('-')
        # 'Re-evaluating' -> reevaluating, 'Co-Evolution' -> coevolution, 'Mutation-Guided' -> mutation
        words.append(''.join(parts) if len(parts[0]) <= 3 and len(parts) > 1 else parts[0])
    return words


def make_key(first_author, year, title, taken, lastname=None):
    last = lastname or split_name(latex_to_text(first_author))[1]
    last = re.sub(r'[^a-z]', '', ascii_fold(last).lower()) or 'anon'
    words = _key_words(title)
    word = next((w for w in words if w not in TITLE_STOPWORDS and not w.isdigit()),
                words[0] if words else 'untitled')
    base = '%s%s%s' % (last, year, word)
    key, suffix = base, 'b'
    while key.lower() in taken:
        key = base + suffix
        suffix = chr(ord(suffix) + 1)
    taken.add(key.lower())
    return key


def protect_title(title):
    """DBLP-style: brace words carrying inner capitals (acronyms, CamelCase)."""
    out = []
    for tok in re.split(r'(\s+)', title):
        core = tok.strip('.,:;!?()"\'')
        if core and not core.startswith('{') and re.search(r'.[A-Z]', core) \
                and not core.startswith('\\'):
            tok = tok.replace(core, '{' + core + '}', 1)
        out.append(tok)
    return ''.join(out)


_MATH = {r'\times': '\u00d7', r'\pm': '\u00b1', r'\approx': '\u2248', r'\leq': '\u2264',
         r'\geq': '\u2265', r'\le': '\u2264', r'\ge': '\u2265', r'\sim': '~',
         r'\%': '%', r'\,': ' '}


def _simple_math(m):
    """Inline TeX math from arXiv abstracts -> plain text ('$F_1$' -> 'F1')."""
    for k, v in _MATH.items():
        m = m.replace(k, v)
    m = re.sub(r'\^\{([^}]*)\}', r'^\1', m)
    m = re.sub(r'_\{?([A-Za-z0-9]+)\}?', r'\1', m)
    return m.replace('{', '').replace('}', '').strip()


def bib_escape(s):
    """Make free text (titles, abstracts) safe for BibTeX/LaTeX."""
    s = html.unescape(s or '')
    s = re.sub(r'<[^>]+>', ' ', s)                      # JATS / HTML tags
    s = re.sub(r'\$([^$]{1,80})\$', lambda m: _simple_math(m.group(1)), s)
    s = re.sub(r'(?<!\\)([%&#])', r'\\\1', s)
    s = re.sub(r'(?<![\\$])_', r'\\_', s)
    depth, out = 0, []
    for c in s:                                          # drop unbalanced braces
        if c == '{':
            depth += 1
        elif c == '}':
            if depth == 0:
                continue
            depth -= 1
        out.append(c)
    s = ''.join(out) + '}' * depth
    return ' '.join(s.split())


# --------------------------------------------------------------------------- sources

class Work(dict):
    """A publication record from one source. Keys: source, id, title, year, doi, arxiv,
    authors, kind, venue, is_preprint, raw."""


def _sparql(http, query, name):
    data = http.json('https://sparql.dblp.org/sparql', 'dblp/' + name,
                     headers={'Accept': 'application/sparql-results+json'},
                     data={'query': query})
    if not data:
        return None
    return [{k: v['value'] for k, v in b.items()} for b in data['results']['bindings']]


def resolve_dblp_pid(http):
    rows = _sparql(http, 'PREFIX dblp: <https://dblp.org/rdf/schema#> SELECT ?p WHERE '
                         '{ ?p dblp:primaryCreatorName "%s" . }' % DBLP_NAME, 'pid.json')
    if rows:
        return rows[0]['p'].split('/pid/')[-1]
    return DBLP_PID


def fetch_dblp(http):
    """All DBLP records of the author (as Work) via SPARQL, with BibTeX-ready fields."""
    pid = DBLP_PID
    me = '<https://dblp.org/pid/%s>' % pid
    pre = 'PREFIX dblp: <https://dblp.org/rdf/schema#> '
    sel = '{ ?pub dblp:authoredBy %s } UNION { ?pub dblp:editedBy %s }' % (me, me)
    q1 = pre + ('SELECT ?pub ?type ?title ?year ?doi ?pages ?venue ?journal ?volume ?number '
                '?parent ?ee ?s ?stream WHERE { %s ?pub dblp:bibtexType ?type ; dblp:title ?title . '
                'OPTIONAL { ?pub dblp:yearOfPublication ?year } OPTIONAL { ?pub dblp:doi ?doi } '
                'OPTIONAL { ?pub dblp:pagination ?pages } OPTIONAL { ?pub dblp:publishedIn ?venue } '
                'OPTIONAL { ?pub dblp:publishedInJournal ?journal } '
                'OPTIONAL { ?pub dblp:publishedInJournalVolume ?volume } '
                'OPTIONAL { ?pub dblp:publishedInJournalVolumeIssue ?number } '
                'OPTIONAL { ?pub dblp:publishedAsPartOf ?parent } '
                'OPTIONAL { ?pub dblp:primaryDocumentPage ?ee } '
                'OPTIONAL { ?pub dblp:publishedInStream ?s . ?s dblp:primaryStreamTitle ?stream } '
                '} LIMIT 20000' % sel)
    q2 = pre + ('SELECT ?pub ?ord ?name ?kind WHERE { %s ?pub dblp:hasSignature ?sig . '
                '?sig dblp:signatureOrdinal ?ord ; dblp:signatureDblpName ?name ; a ?kind . '
                'FILTER(?kind != dblp:Signature) } LIMIT 50000' % sel)
    q3 = pre + ('SELECT ?parent ?ptitle ?publisher ?series ?svol ?isbn WHERE { %s '
                '?pub dblp:publishedAsPartOf ?parent . ?parent dblp:title ?ptitle . '
                'OPTIONAL { ?parent dblp:publishedBy ?publisher } '
                'OPTIONAL { ?parent dblp:publishedInSeries ?series } '
                'OPTIONAL { ?parent dblp:publishedInSeriesVolume ?svol } '
                'OPTIONAL { ?parent dblp:isbn ?isbn } } LIMIT 20000' % sel)
    q4 = pre + ('SELECT ?pub ?publisher ?series ?svol ?isbn ?doc WHERE { %s '
                'OPTIONAL { ?pub dblp:publishedBy ?publisher } '
                'OPTIONAL { ?pub dblp:publishedInSeries ?series } '
                'OPTIONAL { ?pub dblp:publishedInSeriesVolume ?svol } '
                'OPTIONAL { ?pub dblp:isbn ?isbn } OPTIONAL { ?pub dblp:documentPage ?doc } '
                '} LIMIT 20000' % sel)
    rows1 = _sparql(http, q1, 'pubs_v2.json')
    if rows1 is None:
        print('! DBLP SPARQL endpoint unavailable; trying https://dblp.org/pid/%s.xml' % pid,
              file=sys.stderr)
        return fetch_dblp_xml(http, pid)
    rows2 = _sparql(http, q2, 'signatures.json') or []
    rows3 = _sparql(http, q3, 'parents.json') or []
    rows4 = _sparql(http, q4, 'extra.json') or []
    parents = {}
    for r in rows3:
        p = parents.setdefault(r['parent'], {})
        for k in ('ptitle', 'publisher', 'series', 'svol', 'isbn'):
            if r.get(k) and k not in p:
                p[k] = r[k]
    recs = OrderedDict()
    for r in rows1:
        rec = recs.setdefault(r['pub'], {'dois': []})
        for k in ('type', 'title', 'year', 'pages', 'venue', 'journal', 'volume', 'number',
                  'parent', 'ee', 'stream'):
            if r.get(k) and k not in rec:
                rec[k] = r[k]
        if r.get('stream') and '/streams/journals/' in r.get('s', ''):
            rec['stream'] = r['stream']     # a JOT issue can also belong to a conf stream
        if r.get('doi') and r['doi'] not in rec['dois']:
            rec['dois'].append(r['doi'])
    for r in rows4:
        rec = recs.get(r['pub'])
        if rec is None:
            continue
        for k in ('publisher', 'series', 'svol', 'isbn'):
            if r.get(k) and k not in rec:
                rec[k] = r[k]
        if r.get('doc'):
            rec.setdefault('docs', set()).add(r['doc'])
    sigs = {}
    for r in rows2:
        kind = 'editor' if r['kind'].endswith('EditorSignature') else 'author'
        sigs.setdefault(r['pub'], {}).setdefault(kind, {})[int(r['ord'])] = r['name']
    works = []
    for uri, rec in recs.items():
        s = sigs.get(uri, {})
        authors = [clean_dblp_name(s['author'][k]) for k in sorted(s.get('author', {}))]
        editors = [clean_dblp_name(s['editor'][k]) for k in sorted(s.get('editor', {}))]
        key = uri.split('/rec/')[-1]
        par = parents.get(rec.get('parent'), {})
        btype = rec['type'].split('#')[-1].lower()
        title = rec['title'].rstrip()
        if title.endswith('.') and not title.endswith('..'):
            title = title[:-1]
        if '[email' in title:      # e-mail obfuscation artefact in DBLP's RDF dump
            title = title.replace('[email\u00a0protected]', '{EMAIL}').replace(
                '[email protected]', '{EMAIL}')
        doi = ''
        for d in rec['dois']:
            doi = norm_doi(d)
            if not doi.lower().startswith('10.48550/'):
                break
        arxiv = ''
        for d in list(rec['dois']) + sorted(rec.get('docs', [])):
            if 'arxiv' in d.lower():
                arxiv = norm_arxiv(d.split('/')[-1] if 'arxiv.org' in d else d)
                break
        is_corr = (rec.get('journal') == 'CoRR')
        if key.startswith('data/'):
            btype = 'data'
        if is_corr and not arxiv and rec.get('volume', '').startswith('abs/'):
            arxiv = rec['volume'][4:]
        works.append(Work(source='dblp', id=key, title=title, year=int(rec.get('year', 0) or 0),
                          doi=doi, arxiv=arxiv, authors=authors, editors=editors, kind=btype,
                          venue=rec.get('journal') or par.get('ptitle') or rec.get('venue', ''),
                          is_preprint=is_corr, raw=dict(rec, parent_info=par)))
    return works


def clean_dblp_name(name):
    return re.sub(r'\s+\d{4}$', '', name.strip())


def fetch_dblp_xml(http, pid):
    status, text = http.get('https://dblp.org/pid/%s.xml' % pid, 'dblp/pid.xml')
    if status != 200:
        print('! DBLP unavailable (%s); continuing without it' % status, file=sys.stderr)
        return []
    works = []
    root = ET.fromstring(text)
    for r in root.iter('r'):
        for rec in r:
            title = ''.join(rec.find('title').itertext()) if rec.find('title') is not None else ''
            title = title.rstrip('.')
            dois = [e.text for e in rec.findall('ee') if e.text and 'doi.org' in e.text]
            doi = norm_doi(dois[0]) if dois else ''
            journal = rec.findtext('journal') or ''
            vol = rec.findtext('volume') or ''
            works.append(Work(source='dblp', id=rec.get('key'), title=title,
                              year=int(rec.findtext('year') or 0), doi=doi,
                              arxiv=vol[4:] if journal == 'CoRR' else '',
                              authors=[clean_dblp_name(a.text) for a in rec.findall('author')],
                              editors=[clean_dblp_name(a.text) for a in rec.findall('editor')],
                              kind=rec.tag, venue=journal or rec.findtext('booktitle') or '',
                              is_preprint=(journal == 'CoRR'),
                              raw={'type': rec.tag, 'title': title, 'year': rec.findtext('year'),
                                   'pages': rec.findtext('pages'), 'journal': journal,
                                   'volume': vol, 'number': rec.findtext('number'),
                                   'parent_info': {'ptitle': rec.findtext('booktitle')},
                                   'publisher': rec.findtext('publisher'),
                                   'ee': (rec.findall('ee')[0].text if rec.findall('ee') else ''),
                                   'dois': dois}))
    return works


def fetch_hal(http):
    """HAL documents where the author is identified by idHal or ORCID."""
    docs = OrderedDict()
    for q, name in (('authIdHal_s:%s' % HAL_IDHAL, 'hal/idhal.json'),
                    ('authORCIDIdExt_s:"%s"' % ORCID_ID, 'hal/orcid.json'),
                    ('authFullName_t:"%s" -authIdHal_s:%s' % (DBLP_NAME, HAL_IDHAL),
                     'hal/name.json')):
        url = ('https://api.archives-ouvertes.fr/search/?q=%s&rows=2000&wt=json&fl=%s'
               % (urllib.parse.quote(q), HAL_FIELDS))
        data = http.json(url, name)
        if not data:
            print('! HAL query failed: %s' % q, file=sys.stderr)
            continue
        for d in data['response']['docs']:
            docs.setdefault(d['halId_s'], d)
    _record_hal_surnames(docs.values())
    works = []
    for hid, d in docs.items():
        title = (d.get('en_title_s') or d.get('title_s') or [''])[0]
        year = d.get('publicationDateY_i') or d.get('producedDateY_i') or 0
        doc_type = d.get('docType_s', '')
        if not d.get('doiIdentifier_s'):
            m = re.search(r'\bDOI\s*=\s*\{([^}]+)\}', d.get('label_bibtex', ''), re.I)
            if m:
                d['doiIdentifier_s'] = norm_doi(m.group(1))
        works.append(Work(source='hal', id=hid, title=title, year=int(year),
                          doi=(norm_doi(d.get('doiIdentifier_s', ''))
                               if plausible_doi(d.get('doiIdentifier_s', '')) else ''),
                          arxiv=norm_arxiv(d.get('arxivId_s', '')),
                          authors=d.get('authFullName_s', []), kind=doc_type,
                          venue=(d.get('journalTitle_s') or d.get('conferenceTitle_s')
                                 or d.get('bookTitle_s') or ''),
                          is_preprint=doc_type in ('UNDEFINED', 'PREPRINT'), raw=d))
    return works


HAL_SURNAMES = {}


def _record_hal_surnames(docs):
    for d in docs:
        for full, last in zip(d.get('authFullName_s', []), d.get('authLastName_s', [])):
            k = norm_title(full)
            HAL_SURNAMES.setdefault(k, {})
            HAL_SURNAMES[k][last] = HAL_SURNAMES[k].get(last, 0) + 1


def hal_surname(fullname):
    """Most frequent surname HAL records for this full name (None if unknown)."""
    c = HAL_SURNAMES.get(norm_title(latex_to_text(fullname)))
    return max(sorted(c), key=lambda k: c[k]) if c else None


def fetch_orcid(http):
    data = http.json('https://pub.orcid.org/v3.0/%s/works' % ORCID_ID, 'orcid/works.json')
    if not data:
        print('! ORCID query failed', file=sys.stderr)
        return []
    works = []
    for g in data.get('group', []):
        summ = g['work-summary'][0]
        title = ((summ.get('title') or {}).get('title') or {}).get('value', '')
        pd = summ.get('publication-date') or {}
        year = int(((pd.get('year') or {}).get('value')) or 0)
        doi, arxiv = '', ''
        for x in (g.get('external-ids') or {}).get('external-id', []):
            t, v = x.get('external-id-type'), x.get('external-id-value', '')
            if t == 'doi' and not doi and plausible_doi(v):
                doi = norm_doi(v)
            elif t == 'arxiv' and not arxiv:
                arxiv = norm_arxiv(v)
        if doi.lower().startswith('10.48550/arxiv.'):
            arxiv = arxiv or norm_arxiv(doi)
        works.append(Work(source='orcid', id=str(summ.get('put-code')), title=title, year=year,
                          doi=doi, arxiv=arxiv, authors=[], kind=summ.get('type', ''),
                          venue=((summ.get('journal-title') or {}).get('value') or ''),
                          is_preprint=(summ.get('type') == 'preprint'
                                       or doi.lower().startswith('10.48550/')),
                          raw=summ))
    return works


# --- abstract providers --------------------------------------------------------------

def abstract_openalex(http, doi):
    d = http.json('https://api.openalex.org/works/doi:' + urllib.parse.quote(doi, safe='/'),
                  'openalex/' + doi)
    inv = (d or {}).get('abstract_inverted_index')
    if not inv:
        return ''
    pos = {}
    for word, idxs in inv.items():
        for i in idxs:
            pos[i] = word
    return ' '.join(pos[i] for i in sorted(pos))


def crossref(http, doi):
    d = http.json('https://api.crossref.org/works/' + urllib.parse.quote(doi, safe='/'),
                  'crossref/' + doi)
    return (d or {}).get('message') or {}


def abstract_crossref(http, doi):
    a = crossref(http, doi).get('abstract', '')
    a = re.sub(r'<jats:title>.*?</jats:title>', '', a, flags=re.S)
    return re.sub(r'<[^>]+>', ' ', a).strip()


def abstract_s2(http, doi=None, arxiv=None):
    ident = ('DOI:' + doi) if doi else ('ARXIV:' + arxiv)
    d = http.json('https://api.semanticscholar.org/graph/v1/paper/%s?fields=title,abstract'
                  % urllib.parse.quote(ident, safe='/:'), 's2/' + ident)
    return (d or {}).get('abstract') or ''


def abstract_arxiv(http, arxiv):
    status, text = http.get('https://export.arxiv.org/api/query?id_list=' + arxiv,
                            'arxiv/' + arxiv + '.xml')
    if status != 200:
        return ''
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return ''
    ns = {'a': 'http://www.w3.org/2005/Atom'}
    s = root.find('a:entry/a:summary', ns)
    return ' '.join((s.text or '').split()) if s is not None else ''


def hal_abstract(doc):
    for k in ('en_abstract_s', 'abstract_s'):
        v = doc.get(k)
        if v:
            return v[0]
    return ''


def find_abstract(http, doi='', arxiv='', hal_doc=None):
    """HAL -> OpenAlex -> Crossref -> Semantic Scholar -> arXiv. Returns (text, source)."""
    if hal_doc:
        a = hal_abstract(hal_doc)
        if len(a) > 80:
            return a, 'HAL'
    if doi:
        for fn, src in ((abstract_openalex, 'OpenAlex'), (abstract_crossref, 'Crossref'),
                        (abstract_s2, 'Semantic Scholar')):
            a = fn(http, doi)
            if len(a) > 80:
                return a, src
    if arxiv:
        a = abstract_arxiv(http, arxiv)
        if len(a) > 80:
            return a, 'arXiv'
        a = abstract_s2(http, arxiv=arxiv)
        if len(a) > 80:
            return a, 'Semantic Scholar'
    return '', ''


# --------------------------------------------------------------------------- matching

class LocalIndex:
    def __init__(self, entries):
        self.entries = entries
        self.by_doi, self.by_arxiv, self.by_hal = {}, {}, {}
        self.titles = []
        for e in entries:
            for f in ('doi', 'url'):
                v = e.get(f)
                if f == 'url' and 'doi.org/' not in v:
                    continue
                d = doi_key(v)
                if d.startswith('10.'):
                    self.by_doi.setdefault(d, e)
            ax = norm_arxiv(e.get('eprint'))
            if not ax and 'arxiv.org/abs/' in e.get('url'):
                ax = norm_arxiv(e.get('url'))
            if not ax and doi_key(e.get('doi')).startswith('10.48550/arxiv.'):
                ax = norm_arxiv(e.get('doi'))
            if ax:
                self.by_arxiv.setdefault(ax, e)
            h = e.get('hal_id').strip('{} ')
            if h:
                self.by_hal.setdefault(h.lower(), e)
            t = e.get('title') or e.get('chapter')
            self.titles.append((norm_title(t), e))
            if e.get('chapter'):
                self.titles.append((norm_title(e.get('chapter')), e))

    def match(self, work, threshold=0.9):
        d = doi_key(work.get('doi'))
        if d and d in self.by_doi:
            return self.by_doi[d], 'doi'
        if work.get('arxiv') and work['arxiv'] in self.by_arxiv:
            return self.by_arxiv[work['arxiv']], 'arxiv'
        if work.get('source') == 'hal' and work['id'].lower() in self.by_hal:
            return self.by_hal[work['id'].lower()], 'hal_id'
        best, best_r = None, 0.0
        nt = norm_title(work.get('title'))
        for t, e in self.titles:
            r = title_ratio(nt, t) if t else 0
            if r > best_r:
                best, best_r = e, r
        if best is not None:
            same_year = abs(best.year() - (work.get('year') or best.year())) <= 1
            if best_r >= threshold + 0.05 or (best_r >= threshold and same_year):
                return best, 'title %.2f' % best_r
        for t, e in self.titles:
            if t and abs(e.year() - (work.get('year') or 0)) <= 1 \
                    and token_containment(nt, t) >= 0.99:
                return e, 'title-words'
        return None, ''


def is_local_preprint(e):
    j = latex_to_text(e.get('journal')).lower()
    return (j == 'corr' or e.type in ('techreport', 'misc', 'unpublished')
            or 'arxiv' in j or doi_key(e.get('doi')).startswith('10.48550/'))


def cluster_works(works):
    """Group records of the same work across sources (DOI, arXiv id, near-same title)."""
    clusters = []
    by_doi, by_ax = {}, {}
    for w in works:
        target = None
        d = doi_key(w.get('doi'))
        if d and not d.startswith('10.48550/') and d in by_doi:
            target = by_doi[d]
        elif w.get('arxiv') and w['arxiv'] in by_ax and w.get('is_preprint'):
            target = by_ax[w['arxiv']]
        else:
            for c in clusters:
                if w['source'] == 'dblp' and not w.get('is_preprint') and any(
                        o['source'] == 'dblp' and not o.get('is_preprint') for o in c):
                    continue    # two DBLP records are two publications (e.g. journal-first)
                if any((title_ratio(w['title'], o['title']) >= 0.95
                        or token_containment(w['title'], o['title']) >= 0.99)
                       and abs((w['year'] or 0) - (o['year'] or 0)) <= 1
                       and w.get('is_preprint') == o.get('is_preprint')
                       and w.get('kind') != 'data' and o.get('kind') != 'data'
                       for o in c):
                    target = c
                    break
        if target is None:
            target = []
            clusters.append(target)
        target.append(w)
        if d and not d.startswith('10.48550/'):
            by_doi.setdefault(d, target)
        if w.get('arxiv'):
            by_ax.setdefault(w['arxiv'], target)
    return clusters


def primary(cluster):
    rank = {'dblp': 0, 'hal': 1, 'orcid': 2}
    return sorted(cluster, key=lambda w: (w.get('is_preprint', False), rank[w['source']]))[0]


def author_overlap(a, b):
    la = {re.sub(r'[^a-z]', '', ascii_fold(split_name(x)[1]).lower()) for x in a}
    lb = {re.sub(r'[^a-z]', '', ascii_fold(split_name(x)[1]).lower()) for x in b}
    if not la or not lb:
        return 0.0
    return len(la & lb) / float(min(len(la), len(lb)))


def skip_reason(cluster, all_clusters, local):
    """Return a reason string if the cluster should not be added automatically."""
    p = primary(cluster)
    title = p['title']
    if all(w.get('kind') == 'data' for w in cluster):
        return 'research dataset / artifact deposit (not a publication)'
    if FRONT_MATTER_RE.search(title):
        return 'front matter / editorial / erratum'
    hal_types = {w['kind'] for w in cluster if w['source'] == 'hal'}
    if hal_types and hal_types <= HAL_SKIP_DOCTYPES:
        return 'HAL deposit of type %s (not a publication)' % '/'.join(sorted(hal_types))
    if all(w.get('is_preprint') for w in cluster):
        for other in all_clusters:
            if other is cluster:
                continue
            q = primary(other)
            if q.get('is_preprint'):
                continue
            r = title_ratio(title, q['title'])
            ov = author_overlap(p.get('authors') or [], q.get('authors') or [])
            if r >= 0.92 or (r >= 0.75 and ov >= 0.6):
                for w in cluster:          # let the published version carry the arXiv id
                    if w not in other:
                        other.append(dict(w, superseded=True))
                return 'preprint superseded by published version "%s" (%s)' % (q['title'], q['year'])
        for e in local.entries:
            if is_local_preprint(e):
                continue
            r = title_ratio(title, e.get('title'))
            if r >= 0.85:
                return 'preprint of existing entry %s' % e.key
    return ''


# --------------------------------------------------------------------------- entry builder

def _bib_names(names):
    return ' and '.join(_norm_space(n) for n in names)


def entry_from_dblp(w):
    raw = w['raw']
    par = raw.get('parent_info') or {}
    kind = w['kind']
    etype = {'inproceedings': 'inproceedings', 'article': 'article', 'proceedings': 'proceedings',
             'incollection': 'incollection', 'book': 'book', 'phdthesis': 'phdthesis',
             'mastersthesis': 'mastersthesis', 'misc': 'misc'}.get(kind, 'misc')
    e = Entry(etype, 'TMP')
    if w.get('authors'):
        e.set('author', _bib_names(w['authors']))
    if w.get('editors') and (etype in ('proceedings', 'book') or not w.get('authors')):
        e.set('editor', _bib_names(w['editors']))
    e.set('title', protect_title(bib_escape(w['title'])))
    if etype in ('inproceedings', 'incollection'):
        if par.get('ptitle'):
            e.set('booktitle', protect_title(bib_escape(par['ptitle'].rstrip('.'))))
        else:
            e.set('booktitle', bib_escape(raw.get('venue', '')))
        if par.get('series'):
            e.set('series', bib_escape(par['series']))
        if par.get('svol'):
            e.set('volume', par['svol'])
        if par.get('publisher'):
            e.set('publisher', '{%s}' % bib_escape(par['publisher']))
    elif etype == 'article':
        j = raw.get('stream') or raw.get('journal') or ''
        if raw.get('journal') == 'CoRR':
            j = 'CoRR'
        e.set('journal', protect_title(bib_escape(j)))
        if raw.get('volume'):
            e.set('volume', raw['volume'])
        if raw.get('number'):
            e.set('number', raw['number'])
    else:
        if raw.get('publisher'):
            e.set('publisher', '{%s}' % bib_escape(raw['publisher']))
        if raw.get('series'):
            e.set('series', bib_escape(raw['series']))
        if raw.get('svol'):
            e.set('volume', raw['svol'])
        if raw.get('isbn'):
            e.set('isbn', raw['isbn'].replace('urn:isbn:', ''))
    if raw.get('pages'):
        e.set('pages', raw['pages'].replace('-', '--'))
    e.set('year', str(w['year']))
    if w.get('doi'):
        d = w['doi']
        if d.lower().startswith('10.48550/arxiv.'):
            d = '10.48550/arXiv.' + norm_arxiv(d)
        e.set('doi', d)
        e.set('url', 'https://doi.org/' + d)
    elif raw.get('ee'):
        e.set('url', raw['ee'])
    if w.get('is_preprint') and w.get('arxiv'):
        e.set('eprint', w['arxiv'])
        e.set('archiveprefix', 'arXiv')
        e.set('url', 'https://arxiv.org/abs/' + w['arxiv'])
    return e


def entry_from_hal(doc):
    """Build an entry from HAL's own BibTeX export (label_bibtex)."""
    try:
        parsed, _ = parse_bib(doc.get('label_bibtex', ''))
    except BibError:
        parsed = []
    if parsed:
        src = parsed[0]
        e = Entry(src.type, 'TMP')
        for k, v in src.fields.items():
            if k in ('hal_local_reference', 'note') and not v.strip():
                continue
            e.set(k, v, k in src.bare)
    else:
        e = Entry('misc', 'TMP')
        e.set('title', bib_escape(hal_title(doc)))
        e.set('author', ' and '.join(doc.get('authFullName_s', [])))
        e.set('year', str(doc.get('producedDateY_i', '')))
    if hal_pdf(doc):
        e.set('pdf', hal_pdf(doc))
    e.set('hal_id', doc['halId_s'])
    if doc.get('version_i'):
        e.set('hal_version', 'v%s' % doc['version_i'])
    e.set('url', doc.get('uri_s', 'https://hal.science/' + doc['halId_s']))
    return e


def hal_pdf(doc):
    """Main file of a HAL deposit: explicit path from HAL's BibTeX, else fileMain_s."""
    m = re.search(r'\bPDF\s*=\s*\{([^}]+)\}', doc.get('label_bibtex', ''))
    if m and doc.get('fileMain_s'):
        return m.group(1).strip()
    return doc.get('fileMain_s') or ''


def hal_title(doc):
    return (doc.get('en_title_s') or doc.get('title_s') or [''])[0]


def entry_from_crossref(msg, doi):
    typ = msg.get('type', '')
    etype = {'journal-article': 'article', 'proceedings-article': 'inproceedings',
             'book-chapter': 'incollection', 'book': 'book', 'edited-book': 'book',
             'proceedings': 'proceedings', 'posted-content': 'misc'}.get(typ, 'misc')
    e = Entry(etype, 'TMP')
    names = [('%s, %s' % (a['family'], a['given'])) if a.get('given') else a.get('family', '')
             for a in msg.get('author', []) if a.get('family')]
    if names:
        e.set('author', ' and '.join(bib_escape(n) for n in names))
    e.set('title', protect_title(bib_escape((msg.get('title') or [''])[0])))
    conts = msg.get('container-title') or ['']
    cont = conts[-1]
    if etype == 'article':
        e.set('journal', bib_escape(conts[0]))
    elif cont:
        if typ == 'book-chapter' and len(conts) > 1:
            etype = e.type = 'inproceedings' if re.search(r'lecture notes|communications in',
                                                          conts[0], re.I) else etype
            e.set('series', bib_escape(conts[0]))
        e.set('booktitle', bib_escape(cont))
    for src, dst in (('volume', 'volume'), ('issue', 'number'), ('page', 'pages'),
                     ('publisher', 'publisher')):
        if msg.get(src):
            e.set(dst, bib_escape(msg[src]).replace('-', '--') if dst == 'pages'
                  else bib_escape(msg[src]))
    parts = (msg.get('issued') or {}).get('date-parts') or [[]]
    if parts[0]:
        e.set('year', str(parts[0][0]))
        if len(parts[0]) > 1 and parts[0][1]:
            e.set('month', MONTHS[int(parts[0][1]) - 1], bare=True)
    e.set('doi', doi)
    e.set('url', 'https://doi.org/' + doi)
    return e


def build_entry(http, cluster, taken):
    """Return (Entry, provenance dict) for a cluster of records describing one work."""
    p = primary(cluster)
    live = [w for w in cluster if not w.get('superseded')]
    hal = [w for w in live if w['source'] == 'hal']
    dblp = [w for w in live if w['source'] == 'dblp' and not w.get('is_preprint')]
    if not dblp and all(w.get('is_preprint') for w in live):
        dblp = [w for w in live if w['source'] == 'dblp']      # preprint-only work: CoRR record
    prov = {'metadata': '', 'abstract': ''}
    cluster = [w for w in cluster if not w.get('superseded')] + \
        [w for w in cluster if w.get('superseded') and w.get('arxiv')]
    doi = next((w['doi'] for w in cluster if w.get('doi')
                and not w['doi'].lower().startswith('10.48550/')), '')
    if doi:
        doi = crossref(http, doi).get('DOI') or doi     # registrant's case (DBLP upper-cases)
    arxiv = next((w['arxiv'] for w in cluster if w.get('arxiv')), '')
    if dblp:
        e = entry_from_dblp(dblp[0])
        if doi and e.has('doi'):
            e.set('doi', doi)
            e.set('url', 'https://doi.org/' + doi)
        prov['metadata'] = 'DBLP ' + dblp[0]['id']
        if '{EMAIL}' in dblp[0]['title'] and doi:
            ct = (crossref(http, doi).get('title') or [''])[0]
            if ct:
                e.set('title', protect_title(bib_escape(ct)))
    elif hal:
        e = entry_from_hal(hal[0]['raw'])
        prov['metadata'] = 'HAL ' + hal[0]['id']
        if doi:
            cr = crossref(http, doi)
            for src, dst in (('volume', 'volume'), ('issue', 'number'), ('page', 'pages')):
                if cr.get(src) and not e.has(dst):
                    e.set(dst, bib_escape(cr[src]).replace('-', '--'))
            if cr.get('page') and re.match(r'^\d+-\d+$', e.get('pages')):
                e.set('pages', cr['page'].replace('-', '--'))
            e.set('url', 'https://doi.org/' + doi)
    elif doi:
        e = entry_from_crossref(crossref(http, doi), doi)
        prov['metadata'] = 'Crossref (ORCID-listed) ' + doi
    else:
        e = Entry('misc', 'TMP')
        e.set('title', bib_escape(p['title']))
        e.set('year', str(p['year']))
        prov['metadata'] = 'ORCID ' + p['id']
    if doi and not e.has('doi'):
        e.set('doi', doi)
    if not e.has('url') and doi:
        e.set('url', 'https://doi.org/' + doi)
    if hal:
        d = hal[0]['raw']
        e.set('hal_id', d['halId_s'])
        if d.get('version_i'):
            e.set('hal_version', 'v%s' % d['version_i'])
        if hal_pdf(d):
            e.set('pdf', hal_pdf(d))
        if not e.has('url'):
            e.set('url', d.get('uri_s'))
        m = (d.get('conferenceStartDateM_i') or d.get('publicationDateM_i')
             or d.get('producedDateM_i'))
        hy = d.get('conferenceStartDateY_i') or d.get('publicationDateY_i') or \
            d.get('producedDateY_i')
        if m and not e.has('month') and hy == e.year():
            e.set('month', MONTHS[int(m) - 1], bare=True)
        if d.get('audience_s') == '3':
            e.set('audience', 'nationale')
        if d.get('docType_s') in ('THESE', 'HDR'):
            e.set('sorte', 'these')
        if d.get('keyword_s') and not e.has('keywords'):
            e.set('keywords', bib_escape(' ; '.join(d['keyword_s'])))
        award = next((t for t in [e.get('note')] + list(d.get('comment_s') or [])
                      if re.search(r'\baward\b', latex_to_text(t), re.I)), '')
        if award and len(award) < 120:      # e.g. HAL comment "Distinguished Paper Award"
            award = re.sub(r'<[^>]+>', '', latex_to_text(award)).strip()
            e.set('award', award)
            e.set('note', '<b>%s</b>' % award)
    if arxiv and not e.has('eprint'):
        e.set('eprint', arxiv)
        e.set('archiveprefix', 'arXiv')
    if not e.has('month') and doi:
        parts = (crossref(http, doi).get('issued') or {}).get('date-parts') or [[]]
        if parts[0] and len(parts[0]) > 1 and parts[0][1] and parts[0][0] == e.year():
            e.set('month', MONTHS[int(parts[0][1]) - 1], bare=True)
    is_pre = all(w.get('is_preprint') for w in cluster) or e.type in ('techreport', 'unpublished')
    hal_kinds = {w['kind'] for w in hal}
    if is_pre or (hal_kinds and hal_kinds <= HAL_OTHER_DOCTYPES and not dblp):
        e.set('other', '1')
    venue = latex_to_text(e.get('booktitle') + ' ' + e.get('journal')).lower()
    if re.search(r'\bworkshops?\b', venue) and e.type == 'inproceedings':
        e.set('workshop', '1')
    if not e.has('abstract'):
        a, src = find_abstract(http, doi, arxiv, hal[0]['raw'] if hal else None)
        if a:
            e.set('abstract', bib_escape(a))
            prov['abstract'] = src
    else:
        prov['abstract'] = 'HAL'
    first = (entry_authors(e) or ['anon'])[0]
    last = hal_surname(first) if ',' not in first else None
    e.key = make_key(first, e.year() or p['year'], latex_to_text(e.get('title')), taken, last)
    return e, prov


# --------------------------------------------------------------------------- validation

def validate(path, http=None, check_dois=False):
    text = open(path, encoding='utf-8').read()
    problems = []
    try:
        entries, junk = parse_bib(text)
    except BibError as err:
        return None, ['PARSE ERROR: %s' % err]
    if junk:
        problems.append('text outside entries: %r' % junk[:3])
    seen = {}
    for e in entries:
        if e.key.lower() in seen:
            problems.append('duplicate key %s' % e.key)
        seen[e.key.lower()] = e
        if e.dups:
            problems.append('%s: repeated fields %s' % (e.key, [d[0] for d in e.dups]))
        if not e.has('title'):
            problems.append('%s: no title' % e.key)
        if not e.year():
            problems.append('%s: no year' % e.key)
        if not (e.has('author') or e.has('editor')):
            problems.append('%s: no author/editor' % e.key)
        for k, v in e.fields.items():
            if v.count('{') != v.count('}'):
                problems.append('%s: unbalanced braces in %s' % (e.key, k))
        d = e.get('doi')
        if d and d != norm_doi(d):
            problems.append('%s: doi not normalised: %s' % (e.key, d))
        if check_dois and http and d:
            st = http.doi_status(norm_doi(d))
            if st != 'ok':
                problems.append('%s: DOI %s -> %s' % (e.key, d, st))
    return entries, problems


def year_counts(entries):
    counts = OrderedDict()
    for e in sorted(entries, key=lambda x: -x.year()):
        counts[e.year()] = counts.get(e.year(), 0) + 1
    return counts


# --------------------------------------------------------------------------- main

def collect(http):
    dblp = fetch_dblp(http)
    hal = fetch_hal(http)
    orcid = fetch_orcid(http)
    print('fetched: DBLP %d, HAL %d, ORCID %d records' % (len(dblp), len(hal), len(orcid)),
          file=sys.stderr)
    return dblp + hal + orcid


def find_missing(entries, works, since=0):
    local = LocalIndex(entries)
    clusters = cluster_works(works)
    missing, skipped, present = [], [], []
    for c in clusters:
        p = primary(c)
        if since and (p['year'] or 0) < since:
            continue
        hits = [(w, local.match(w)) for w in c]
        found = [(w, m) for w, m in hits if m[0] is not None]
        if found:
            entry, how = found[0][1]
            pdoi = doi_key(p.get('doi'))
            if is_local_preprint(entry) and not p.get('is_preprint') and pdoi \
                    and not pdoi.startswith('10.48550/') and not pdoi.startswith('10.5281/') \
                    and not any(how2 in ('doi', 'hal_id') for _, (_, how2) in found):
                missing.append((c, 'published version of local preprint %s' % entry.key))
            elif how.startswith('title') and pdoi and not pdoi.startswith(('10.48550/', '10.5281/')) \
                    and doi_key(entry.get('doi')) and doi_key(entry.get('doi')) != pdoi \
                    and not p.get('is_preprint'):
                missing.append((c, 'same title as existing %s but another DOI (%s): companion / '
                                   'journal-first publication?' % (entry.key, entry.get('doi'))))
            else:
                present.append((c, entry, how))
            continue
        reason = skip_reason(c, clusters, local)
        if reason:
            skipped.append((c, reason))
        else:
            missing.append((c, ''))
    return missing, skipped, present


def describe(cluster):
    p = primary(cluster)
    srcs = ', '.join(sorted({'%s:%s' % (w['source'], w['id']) for w in cluster
                             if not w.get('superseded')}))
    return '%s  %s  [%s]%s' % (p['year'], p['title'], srcs,
                               ('  doi:' + p['doi']) if p.get('doi') else '')


def load_ignore(path):
    ids = set()
    if path and os.path.exists(path):
        for line in open(path, encoding='utf-8'):
            line = line.split('#', 1)[0].strip()
            if line:
                ids.add(('doi:' + doi_key(line[4:])) if line.lower().startswith('doi:') else line)
    return ids


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('--bib', default=DEFAULT_BIB)
    ap.add_argument('--cache', default=DEFAULT_CACHE)
    ap.add_argument('--since', type=int, default=0)
    ap.add_argument('--add', action='store_true')
    ap.add_argument('--include-skipped', action='store_true')
    ap.add_argument('--refresh', action='store_true')
    ap.add_argument('--check', action='store_true')
    ap.add_argument('--check-dois', action='store_true')
    ap.add_argument('--sort', action='store_true', help='with --check: rewrite sorted/formatted')
    ap.add_argument('--ignore', default=DEFAULT_IGNORE)
    ap.add_argument('--verbose', action='store_true')
    args = ap.parse_args(argv)
    http = Http(args.cache, refresh=args.refresh, verbose=args.verbose)

    if args.check or args.check_dois:
        entries, problems = validate(args.bib, http, args.check_dois)
        if entries is None:
            print('\n'.join(problems))
            return 1
        print('%d entries' % len(entries))
        for y, n in year_counts(entries).items():
            print('  %s: %d' % (y or 'n/a', n))
        print('\n'.join(problems) if problems else 'no problems found')
        if args.sort and entries:
            write_bib(args.bib, sort_entries(entries))
            print('rewrote %s (sorted, formatted)' % args.bib)
        return 1 if problems else 0

    with open(args.bib, encoding='utf-8') as fh:
        entries, _ = parse_bib(fh.read())
    works = collect(http)
    missing, skipped, present = find_missing(entries, works, args.since)
    ignored = load_ignore(args.ignore)
    n_ign = 0
    if ignored:
        def is_ignored(c):
            ids = {'%s:%s' % (w['source'], w['id']) for w in c} | \
                {'doi:' + doi_key(w['doi']) for w in c if w.get('doi')}
            return bool(ids & ignored)
        before = len(missing) + len(skipped)
        missing = [(c, y) for c, y in missing if not is_ignored(c)]
        skipped = [(c, y) for c, y in skipped if not is_ignored(c)]
        n_ign = before - len(missing) - len(skipped)
    print('%d works already in %s; %d ignored (%s)' % (len(present), os.path.relpath(args.bib),
                                                       n_ign, os.path.relpath(args.ignore)))
    print('\nMISSING (%d):' % len(missing))
    for c, why in missing:
        print('  + ' + describe(c) + (('   <- ' + why) if why else ''))
    print('\nSKIPPED (%d):' % len(skipped))
    for c, why in skipped:
        print('  - ' + describe(c) + '   <- ' + why)
    if args.add:
        todo = [c for c, _ in missing] + ([c for c, _ in skipped] if args.include_skipped else [])
        taken = {e.key.lower() for e in entries}
        new = []
        for c in todo:
            e, prov = build_entry(http, c, taken)
            new.append(e)
            print('  added %s (%s; abstract: %s)' % (e.key, prov['metadata'],
                                                     prov['abstract'] or 'none'))
        if new:
            with open(args.bib, 'a', encoding='utf-8') as fh:
                fh.write('\n\n' + '\n\n'.join(format_entry(e) for e in new) + '\n')
            print('appended %d entries to %s -- review them, then run --check --sort'
                  % (len(new), args.bib))
    return 0


if __name__ == '__main__':
    sys.exit(main())
