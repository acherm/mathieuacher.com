#!/usr/bin/env python3
"""Local review server: the built site with an edit/annotate overlay, plus a question page.

    python3 tools/review.py               # build the site, serve it on http://127.0.0.1:8777/
    python3 tools/review.py --data DIR    # where questions, answers and annotations live
    python3 tools/review.py --report      # what is still to apply (open notes, new answers)
    python3 tools/review.py --resolve ID "commit abc123"   # record that it was applied
    python3 tools/review.py --base REF    # "what changed" compares with REF (default: HEAD)

On every page, a toolbar lets you edit text in place or attach comments. Simple text edits
can be written back to the source file (content/*.toml|md, data/acher.bib, templates/) and the
site rebuilt; everything else stays recorded for later. `/__review/questions` lists the
questions of DIR/questions.json and saves the answers. `/__review/changes` (and the toolbar's
Changes button) shows what differs from the base commit, uncommitted edits included.

Stored in DIR (default ../review, outside the repository): annotations.json, answers.json,
and review-log.jsonl (append-only history of every change). Nothing here is deployed:
the overlay exists only in pages served by this script. Standard library only.
"""

import argparse
import html
import json
import mimetypes
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from collections import Counter
from datetime import datetime
from difflib import SequenceMatcher
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

ROOT = Path(__file__).resolve().parent.parent
UI = Path(__file__).resolve().parent / "review"
SITE = ROOT / "_site"
SOURCES = ["content/*.toml", "content/*.md", "data/acher.bib", "templates/*.html", "templates/md/*"]
BUILD_INPUTS = ["build.py", "sitegen/*.py", "content/*", "data/*", "templates/**/*", "static/**/*"]
CACHE = ROOT / "tools" / ".cache" / "review-base"  # builds of earlier commits, to compare with
KINDS = {"edit", "comment", "question", "wrong", "remove", "missing"}
STATUSES = {"open", "applied", "wontfix"}
ANSWER_STATES = {"answered", "later", "unknown"}
LOCK = threading.Lock()
BUILD = threading.RLock()  # held while _site/ is rebuilt, and while a file of it is read


def now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


# --- storage ---------------------------------------------------------------


class Store:
    """JSON files in the data directory, written atomically, plus an append-only log."""

    def __init__(self, data: Path):
        self.data = data
        data.mkdir(parents=True, exist_ok=True)

    def load(self, name: str, default):
        p = self.data / name
        if not p.exists():
            return default
        return json.loads(p.read_text(encoding="utf-8"))

    def save(self, name: str, obj) -> None:
        p = self.data / name
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, p)

    def log(self, event: str, **fields) -> None:
        line = json.dumps({"at": now(), "event": event, **fields}, ensure_ascii=False)
        with open(self.data / "review-log.jsonl", "a", encoding="utf-8") as f:
            f.write(line + "\n")

    def annotations(self) -> list[dict]:
        return self.load("annotations.json", {"annotations": []})["annotations"]

    def save_annotations(self, items: list[dict]) -> None:
        self.save("annotations.json", {"version": 1, "annotations": items})


# --- finding the source of a rendered text ---------------------------------

# Rendered text went through Markdown with "smarty": map typographic characters back
# to what the source may contain.
TYPO = {
    "’": "(?:’|')", "‘": "(?:‘|')", "“": '(?:“|")', "”": '(?:”|")',
    "–": "(?:–|--)", "—": "(?:—|---)", "…": r"(?:…|\.\.\.)",
    "«": "(?:«|<<)", "»": "(?:»|>>)", " ": r"(?: |\s|&nbsp;)",
}


def source_files() -> list[Path]:
    files: list[Path] = []
    for pattern in SOURCES:
        files.extend(sorted(ROOT.glob(pattern)))
    return [f for f in files if f.is_file()]


def norm(s: str) -> str:
    """Text without Markdown/HTML/BibTeX markup, lower-case, single spaces."""
    s = html.unescape(s)
    s = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", s)
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"[*_`{}\\]", "", s)
    for k, v in {"’": "'", "‘": "'", "“": '"', "”": '"', "–": "-", "—": "-", " ": " "}.items():
        s = s.replace(k, v)
    s = s.replace("--", "-")
    return re.sub(r"\s+", " ", s).strip().lower()


def find_sources(text: str, limit: int = 5) -> list[dict]:
    """Source lines that probably produced `text` (6-word probes against normalized lines)."""
    words = norm(text).split()
    if not words:
        return []
    if len(words) <= 6:
        probes = [" ".join(words)]
    else:
        starts = sorted({0, (len(words) - 6) // 2, len(words) - 6})
        probes = [" ".join(words[i:i + 6]) for i in starts]
    hits: dict[tuple[str, int], dict] = {}
    for f in source_files():
        rel = f.relative_to(ROOT).as_posix()
        for n, line in enumerate(f.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            nl = norm(line)
            score = sum(1 for p in probes if p in nl)
            if score:
                hits[(rel, n)] = {"file": rel, "line": n, "score": score, "snippet": line.strip()[:200]}
    ranked = sorted(hits.values(), key=lambda h: (-h["score"], h["file"].startswith("templates"), h["file"], h["line"]))
    return ranked[:limit]


def to_pattern(s: str) -> str:
    out = []
    for ch in s:
        if ch in TYPO:
            out.append(TYPO[ch])
        elif ch.isspace():
            out.append(r"\s+")
        else:
            out.append(re.escape(ch))
    return "".join(out).replace(r"\s+\s+", r"\s+")


def edit_candidates(original: str, proposed: str) -> dict | None:
    """Where a small text edit can be written back: the changed span with as much of its
    context as still matches the source verbatim. Returns {old_span, new_span, matches}."""
    if original == proposed:
        return None
    p = 0
    while p < min(len(original), len(proposed)) and original[p] == proposed[p]:
        p += 1
    s = 0
    while s < min(len(original), len(proposed)) - p and original[-1 - s] == proposed[-1 - s]:
        s += 1
    old_span, new_span = original[p:len(original) - s], proposed[p:len(proposed) - s]
    texts = {f.relative_to(ROOT).as_posix(): f.read_text(encoding="utf-8", errors="replace") for f in source_files()}
    # The whole text is one value: a quoted TOML string, a BibTeX {field}, or a Markdown line.
    whole = re.compile(r'((?:=\s*"|=\s*\{|^(?:#+ |- )?))(' + to_pattern(original) + r')("|\}|$)', re.M)
    found = [(rel, m) for rel, text in texts.items() for m in whole.finditer(text)]
    if len(found) == 1:
        rel, m = found[0]
        return {"old_span": old_span, "new_span": new_span, "matches": [{
            "file": rel, "line": texts[rel].count("\n", 0, m.start()) + 1,
            "old": m.group(0), "new": m.group(1) + proposed + m.group(3)}]}
    for ctx in (30, 20, 12, 8, 5, 3, 0):
        left = original[max(0, p - ctx):p]
        right = original[len(original) - s:len(original) - s + ctx]
        needle = left + old_span + right
        if len(needle.strip()) < 4:
            continue
        rx = re.compile("(" + to_pattern(left) + ")(" + to_pattern(old_span) + ")(" + to_pattern(right) + ")")
        matches = []
        for rel, text in texts.items():
            found = list(rx.finditer(text))
            if len(found) == 1:
                m = found[0]
                matches.append({"file": rel, "line": text.count("\n", 0, m.start()) + 1,
                                "old": m.group(0), "new": m.group(1) + new_span + m.group(3)})
            elif len(found) > 1:
                matches = []
                break
        if matches and len(matches) <= 3:
            return {"old_span": old_span, "new_span": new_span, "matches": matches}
    return None


def apply_patch(match: dict) -> str | None:
    """Write one edit back; returns an error message, or None when done."""
    path = (ROOT / match["file"]).resolve()
    if ROOT not in path.parents or path not in source_files():
        return f"{match['file']}: not an editable source file"
    text = path.read_text(encoding="utf-8")
    if text.count(match["old"]) != 1:
        return f"{match['file']}: the original text is no longer there exactly once"
    path.write_text(text.replace(match["old"], match["new"], 1), encoding="utf-8")
    return None


def run_build(where: Path) -> dict:
    t = time.time()
    cmd = ["uv", "run", "build.py"] if shutil.which("uv") else [sys.executable, "build.py"]
    r = subprocess.run(cmd, cwd=where, capture_output=True, text=True)
    out = (r.stdout + r.stderr).strip().splitlines()
    return {"ok": r.returncode == 0, "seconds": round(time.time() - t, 1), "output": "\n".join(out[-15:])}


def rebuild() -> dict:
    with BUILD:
        r = run_build(ROOT)
        FRESH.update(stamp=time.time(), error=None if r["ok"] else r["output"])
        return r


FRESH = {"stamp": 0.0, "error": None}


def inputs_mtime() -> float:
    return max((f.stat().st_mtime for pattern in BUILD_INPUTS for f in ROOT.glob(pattern) if f.is_file()), default=0.0)


def ensure_fresh(store: "Store") -> str | None:
    """Rebuild when a source changed since the last build (edited by hand or by another tool).
    Returns the error of the last build, if it failed."""
    newest = inputs_mtime()
    with BUILD:
        index = SITE / "index.html"
        built = index.stat().st_mtime if index.exists() else 0.0
        if newest > max(built, FRESH["stamp"]):
            r = rebuild()
            store.log("rebuild", ok=r["ok"], seconds=r["seconds"], auto=True)
            sys.stderr.write(("rebuilt" if r["ok"] else "BUILD FAILED") + " (sources changed)\n")
        return FRESH["error"]


# --- what changed since a commit -------------------------------------------
#
# The site at the base commit is built into CACHE/<sha>/_site; both builds are cut into the
# text of their block elements, and the blocks that differ are what changed.

BLOCK_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "dt", "dd", "td", "th", "blockquote", "figcaption"}
SKIP_TAGS = {"script", "style", "template", "svg"}


class BlockText(HTMLParser):
    """Text of every block element of a page, nested blocks included, in document order."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.blocks: list[dict] = []
        self.open: list[dict] = []
        self.skip = 0
        self.section = ""
        self.title: list[str] | None = None
        self.page_title = ""

    def handle_starttag(self, tag, attrs):
        if tag in SKIP_TAGS:
            self.skip += 1
        elif tag == "title" and not self.skip:
            self.title = []
        elif tag == "br":
            self.handle_data(" ")
        elif tag in BLOCK_TAGS:
            b = {"tag": tag, "parts": [], "own": [], "kids": False, "i": len(self.blocks), "section": self.section,
                 "parent": self.open[-1]["i"] if self.open else None}
            if self.open:
                self.open[-1]["kids"] = True
            self.blocks.append(b)
            self.open.append(b)

    def handle_endtag(self, tag):
        if tag in SKIP_TAGS:
            self.skip = max(0, self.skip - 1)
        elif tag == "title" and self.title is not None:
            self.page_title, self.title = " ".join("".join(self.title).split()), None
        elif tag in BLOCK_TAGS and any(b["tag"] == tag for b in self.open):
            while self.open:
                b = self.open.pop()
                if b["tag"] in ("h1", "h2", "h3", "h4"):
                    self.section = " ".join("".join(b["parts"]).split())
                if b["tag"] == tag:
                    break

    def handle_data(self, data):
        if self.skip:
            return
        if self.title is not None:
            self.title.append(data)
        for b in self.open:
            b["parts"].append(data)
        if self.open:
            self.open[-1]["own"].append(data)  # text outside nested blocks


PARSED: dict[tuple, tuple[str, list[dict]]] = {}


def page_blocks(path: Path) -> tuple[str, list[dict]]:
    """(title, blocks) of an HTML file, each block with its text and a whitespace-free key."""
    if not path.is_file():
        return "", []
    st = path.stat()
    sig = (str(path), st.st_mtime_ns, st.st_size)
    if sig not in PARSED:
        p = BlockText()
        p.feed(path.read_text(encoding="utf-8", errors="replace"))
        for b in p.blocks:
            text = "".join(b.pop("parts"))
            b["text"], b["key"] = " ".join(text.split()), re.sub(r"\s+", "", text)
            b["own"] = re.sub(r"\s+", "", "".join(b["own"]))
        if len(PARSED) > 64:
            PARSED.clear()
        PARSED[sig] = (p.page_title, [b for b in p.blocks if b["key"]])
    return PARSED[sig]


def flag_extra(blocks: list[dict], other: list[dict]) -> list[dict]:
    """Blocks of `blocks` whose text is not in `other` (as a multiset). Each block gets `nth`,
    its rank among the blocks with the same text, and `anchor`, the text of the last
    unchanged block before it."""
    have = Counter(b["key"] for b in other)
    seen: Counter = Counter()
    flagged, anchor = [], ""
    for b in blocks:
        seen[b["key"]] += 1
        b["nth"] = seen[b["key"]] - 1
        b["anchor"] = anchor
        if seen[b["key"]] > have[b["key"]]:
            flagged.append(b)
        elif not b["kids"]:
            anchor = b["key"]
    return flagged


def innermost(flagged: list[dict], blocks: list[dict], other_flagged: list[dict]) -> list[dict]:
    """Drop the blocks that differ only because a nested block changed, appeared or went away."""
    parents = {b["i"]: b["parent"] for b in blocks}
    outer = set()
    for b in flagged:
        p = b["parent"]
        while p is not None:
            outer.add(p)
            p = parents.get(p)
    other_own = {b["own"] for b in other_flagged if b["kids"]}
    return [b for b in flagged if b["i"] not in outer and not (b["kids"] and (not b["own"] or b["own"] in other_own))]


def compare_pages(old: list[dict], new: list[dict]) -> list[dict]:
    """Changed, new and removed blocks; a removed block is paired with the most similar new one,
    or with the new one that took its place."""
    fa, fr = flag_extra(new, old), flag_extra(old, new)
    added, removed = innermost(fa, new, fr), innermost(fr, old, fa)
    pairs = []
    for a in added:
        for r in removed:
            x, y = r["text"].split(), a["text"].split()
            sm = SequenceMatcher(None, x, y, autojunk=False)
            ratio = sm.ratio()
            # a list item that grew (talks of a year, committees) still counts as changed
            contained = sum(m.size for m in sm.get_matching_blocks()) / max(1, min(len(x), len(y)))
            if ratio >= 0.45 or (contained >= 0.7 and r["section"] == a["section"]):
                pairs.append((ratio + contained, a["i"], r["i"]))
    was: dict[int, dict] = {}
    used = set()
    for ratio, ai, ri in sorted(pairs, reverse=True):
        if ai not in was and ri not in used:
            was[ai] = next(r for r in removed if r["i"] == ri)
            used.add(ri)
    for r in removed:  # rewritten in place: same unchanged block before both
        a = next((a for a in added if a["i"] not in was and a["anchor"] == r["anchor"] and a["section"] == r["section"]), None)
        if a and r["i"] not in used:
            was[a["i"]] = r
            used.add(r["i"])
    def context(b: dict, blocks: dict[int, dict]) -> str:
        """First line of the item a nested block belongs to (a student's name and years)."""
        parent = blocks.get(b["parent"])
        if not parent:
            return ""
        first = next((x for x in blocks.values() if x["parent"] == parent["i"]), parent)
        return "" if first is b else first["text"][:120]

    by_i_new, by_i_old = {b["i"]: b for b in new}, {b["i"]: b for b in old}
    items = [{"type": "changed" if a["i"] in was else "new", "text": a["text"], "was": was[a["i"]]["text"] if a["i"] in was else "",
              "key": a["key"], "nth": a["nth"], "tag": a["tag"], "section": a["section"],
              "context": context(a, by_i_new)} for a in added]
    items += [{"type": "removed", "text": "", "was": r["text"], "key": "", "nth": 0, "tag": r["tag"], "section": r["section"],
               "context": context(r, by_i_old)} for r in removed if r["i"] not in used]
    return items


def git(*args: str) -> str:
    r = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError(r.stderr.strip() or "git " + " ".join(args) + " failed")
    return r.stdout


def commit_info(ref: str) -> dict:
    if not ref or ref.startswith("-"):
        raise RuntimeError(f"not a commit: {ref!r}")
    sha = git("rev-parse", "--verify", "--quiet", ref + "^{commit}").strip()
    short, date, subject = git("log", "-1", "--format=%h%x09%ad%x09%s", "--date=short", sha).rstrip("\n").split("\t", 2)
    return {"ref": ref, "sha": sha, "short": short, "date": date, "subject": subject}


def recent_commits(n: int = 25) -> list[dict]:
    try:
        deployed = git("rev-parse", "--verify", "--quiet", "origin/main").strip()
    except RuntimeError:
        deployed = ""
    out = []
    for line in git("log", f"-{n}", "--format=%H%x09%h%x09%ad%x09%s", "--date=short").splitlines():
        sha, short, date, subject = line.split("\t", 3)
        out.append({"sha": sha, "short": short, "date": date, "subject": subject, "deployed": sha == deployed})
    return out


BASE_LOCK = threading.Lock()


def base_site(sha: str) -> Path:
    """The site as built from commit `sha` (cached)."""
    done = CACHE / sha / "_site"
    with BASE_LOCK:
        if (done / "index.html").exists():
            return done
        tmp = CACHE / (sha + ".tmp")
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True)
        tar = tmp / "src.tar"
        git("archive", "--format=tar", "-o", str(tar), sha)
        subprocess.run(["tar", "-xf", str(tar), "-C", str(tmp)], check=True)
        tar.unlink()
        r = run_build(tmp)
        if not r["ok"]:
            raise RuntimeError("build of " + sha[:7] + " failed:\n" + r["output"])
        shutil.rmtree(CACHE / sha, ignore_errors=True)
        os.replace(tmp, CACHE / sha)
        for old in sorted((d for d in CACHE.iterdir() if d.is_dir()), key=lambda d: d.stat().st_mtime)[:-4]:
            shutil.rmtree(old, ignore_errors=True)
        return done


def page_url(rel: str) -> str:
    return "/" + (rel[: -len("index.html")] if rel == "index.html" or rel.endswith("/index.html") else rel)


def page_file(url_path: str) -> str:
    rel = url_path.lstrip("/")
    return rel + "index.html" if rel == "" or rel.endswith("/") else rel


def site_changes(sha: str, only: str | None = None) -> list[dict]:
    """Per page: what differs between the build of `sha` and the current build."""
    base = base_site(sha)
    if only is not None:
        rels = [page_file(only)]
    else:
        rels = sorted({p.relative_to(root).as_posix() for root in (base, SITE) for p in root.rglob("*.html")})
    pages = []
    for rel in rels:
        old_title, old = page_blocks(base / rel)
        with BUILD:
            title, new = page_blocks(SITE / rel)
        if only is None and not old and not new:
            continue
        items = compare_pages(old, new)
        if items or only is not None:
            state = "new page" if not old and new else "removed page" if old and not new else ""
            pages.append({"page": page_url(rel), "title": title or old_title, "state": state, "items": items})
    return pages


def source_changes(sha: str) -> list[dict]:
    """Files that feed the site and differ from `sha` (working tree, uncommitted edits included)."""
    spec = ["--", ".", ":!tools", ":!README.md", ":!.github"]
    out = []
    for line in git("diff", "--numstat", sha, *spec).splitlines():
        plus, minus, name = line.split("\t", 2)
        diff = git("diff", "-U2", sha, "--", name)
        out.append({"file": name, "plus": plus, "minus": minus, "diff": diff[:200_000]})
    for name in git("ls-files", "--others", "--exclude-standard", "--", "content", "data", "templates", "static", "sitegen").splitlines():
        out.append({"file": name, "plus": "new", "minus": "", "diff": ""})
    return out


# --- HTTP -----------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    server_version = "review/1"
    store: Store
    token: str
    port: int
    base = "HEAD"  # commit that "what changed" compares with

    def log_message(self, fmt, *args):  # quiet: one line per write request only
        if self.command != "GET":
            sys.stderr.write("%s %s\n" % (self.command, self.path))

    # helpers
    def send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def json(self, obj, code: int = 200) -> None:
        self.send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}") if n else {}

    def allowed(self) -> bool:
        host = self.headers.get("Host", "")
        if host not in (f"127.0.0.1:{self.port}", f"localhost:{self.port}"):
            self.send(403, b"forbidden host", "text/plain")
            return False
        if self.command != "GET" and self.headers.get("X-Review-Token") != self.token:
            self.send(403, b"missing token", "text/plain")
            return False
        return True

    # routing
    def do_GET(self):
        if not self.allowed():
            return
        url = urlparse(self.path)
        path = unquote(url.path)
        if path in ("/__review", "/__review/"):
            return self.hub()
        if path == "/__review/questions":
            return self.ui_page("questions.html")
        if path == "/__review/annotations":
            return self.ui_page("annotations.html")
        if path == "/__review/changes":
            return self.ui_page("changes.html")
        if path.startswith("/__review/static/"):
            return self.static(UI, path[len("/__review/static/"):], inject=False)
        if path == "/__review/api/annotations":
            page = parse_qs(url.query).get("page", [None])[0]
            items = [a for a in self.store.annotations() if page is None or a["page"] == page]
            return self.json({"annotations": items})
        if path == "/__review/api/questions":
            q = self.store.load("questions.json", {"sections": [], "questions": []})
            q["answers"] = self.store.load("answers.json", {"answers": {}})["answers"]
            return self.json(q)
        if path == "/__review/api/changes":
            return self.changes(parse_qs(url.query).get("page", [None])[0])
        return self.static(SITE, path.lstrip("/"), inject=True)

    do_HEAD = do_GET

    def do_POST(self):
        if not self.allowed():
            return
        path = urlparse(self.path).path
        try:
            data = self.body()
        except json.JSONDecodeError:
            return self.json({"error": "bad json"}, 400)
        with LOCK:
            if path == "/__review/api/annotations":
                return self.create_annotation(data)
            m = re.fullmatch(r"/__review/api/annotations/([\w-]+)/apply", path)
            if m:
                return self.apply_annotation(m.group(1), data)
            if path == "/__review/api/answers":
                return self.save_answer(data)
            if path == "/__review/api/rebuild":
                r = rebuild()
                self.store.log("rebuild", ok=r["ok"], seconds=r["seconds"])
                return self.json(r)
            if path == "/__review/api/base":
                try:
                    info = commit_info(str(data.get("ref", "")))
                except RuntimeError as e:
                    return self.json({"error": str(e)}, 400)
                Handler.base = info["sha"]
                return self.json(info)
        self.json({"error": "not found"}, 404)

    def do_PUT(self):
        if not self.allowed():
            return
        m = re.fullmatch(r"/__review/api/annotations/([\w-]+)", urlparse(self.path).path)
        if not m:
            return self.json({"error": "not found"}, 404)
        with LOCK:
            self.update_annotation(m.group(1), self.body())

    def do_DELETE(self):
        if not self.allowed():
            return
        m = re.fullmatch(r"/__review/api/annotations/([\w-]+)", urlparse(self.path).path)
        if not m:
            return self.json({"error": "not found"}, 404)
        with LOCK:
            items = self.store.annotations()
            gone = [a for a in items if a["id"] == m.group(1)]
            self.store.save_annotations([a for a in items if a["id"] != m.group(1)])
            if gone:
                self.store.log("annotation-deleted", annotation=gone[0])
            self.json({"ok": bool(gone)})

    # pages
    def static(self, base: Path, rel: str, inject: bool) -> None:
        target = (base / rel).resolve()
        if base.resolve() not in target.parents and target != base.resolve():
            return self.send(404, b"not found", "text/plain")
        if inject and (target.is_dir() or target.suffix in ("", ".html")):
            ensure_fresh(self.store)
        with BUILD:
            if target.is_dir():
                target = target / "index.html"
            code = 200
            if not target.is_file():
                if not inject:
                    return self.send(404, b"not found", "text/plain")
                code, target = 404, SITE / "404.html"
            body = target.read_bytes()
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        if inject and target.suffix == ".html":
            body = self.inject(body)
        self.send(code, body, ctype)

    def inject(self, body: bytes) -> bytes:
        config = json.dumps({"token": self.token, "buildError": FRESH["error"]}).replace("</", "<\\/")
        snippet = (
            '<link rel="stylesheet" href="/__review/static/overlay.css">'
            f"<script>window.REVIEW={config};</script>"
            '<script src="/__review/static/overlay.js" defer></script>'
        ).encode()
        i = body.rfind(b"</body>")
        return body[:i] + snippet + body[i:] if i >= 0 else body + snippet

    def ui_page(self, name: str) -> None:
        body = (UI / name).read_text(encoding="utf-8").replace("__TOKEN__", self.token)
        self.send(200, body.encode("utf-8"), "text/html; charset=utf-8")

    def hub(self) -> None:
        ann = self.store.annotations()
        q = self.store.load("questions.json", {"questions": []})["questions"]
        ans = self.store.load("answers.json", {"answers": {}})["answers"]
        open_ = sum(1 for a in ann if a["status"] == "open")
        answered = sum(1 for x in q if ans.get(x["id"], {}).get("state") == "answered")
        try:
            info = commit_info(self.base)
            pages = site_changes(info["sha"])
            n = sum(len(p["items"]) for p in pages)
            changes = (f"{n} changed text{'s' if n != 1 else ''} on {len(pages)} page{'s' if len(pages) != 1 else ''}"
                       f" since commit {info['short']} ({info['date']}, {html.escape(info['subject'])})")
        except (RuntimeError, OSError, subprocess.CalledProcessError) as e:
            changes = "Not available: " + html.escape(str(e))
        body = (UI / "hub.html").read_text(encoding="utf-8")
        for k, v in {"__ANN_OPEN__": open_, "__ANN_TOTAL__": len(ann), "__Q_DONE__": answered,
                     "__Q_TOTAL__": len(q), "__CHANGES__": changes, "__DATA__": html.escape(str(self.store.data))}.items():
            body = body.replace(k, str(v))
        self.send(200, body.encode("utf-8"), "text/html; charset=utf-8")

    def changes(self, page: str | None) -> None:
        ensure_fresh(self.store)
        try:
            info = commit_info(self.base)
            pages = site_changes(info["sha"], page)
        except (RuntimeError, OSError, subprocess.CalledProcessError) as e:
            return self.json({"error": str(e)})
        if page is not None:
            return self.json({"base": info, **pages[0]})
        self.json({"base": info, "commits": recent_commits(), "pages": pages, "sources": source_changes(info["sha"])})

    # annotations
    def create_annotation(self, d: dict) -> None:
        kind = d.get("kind", "comment")
        if kind not in KINDS or not isinstance(d.get("page"), str):
            return self.json({"error": "bad annotation"}, 400)
        anchor = d.get("anchor") or {}
        original = str(d.get("original", ""))[:5000]
        proposed = str(d.get("proposed", ""))[:5000] if kind == "edit" else ""
        a = {
            "id": "a" + time.strftime("%y%m%d%H%M%S") + secrets.token_hex(2),
            "created": now(), "updated": now(),
            "page": d["page"], "title": str(d.get("title", ""))[:200],
            "kind": kind, "status": "open",
            "anchor": {k: str(anchor.get(k, ""))[:2000] for k in ("selector", "tag", "id", "quote", "prefix", "suffix")},
            "original": original, "proposed": proposed,
            "comment": str(d.get("comment", ""))[:5000],
            "sources": find_sources(anchor.get("quote") or original),
            "patch": edit_candidates(original, proposed) if kind == "edit" else None,
            "resolution": "",
        }
        items = self.store.annotations()
        items.append(a)
        self.store.save_annotations(items)
        self.store.log("annotation-created", annotation=a)
        self.json(a)

    def update_annotation(self, ident: str, d: dict) -> None:
        items = self.store.annotations()
        for a in items:
            if a["id"] != ident:
                continue
            before = dict(a)
            for k in ("comment", "resolution"):
                if k in d:
                    a[k] = str(d[k])[:5000]
            if d.get("status") in STATUSES:
                a["status"] = d["status"]
            if d.get("kind") in KINDS:
                a["kind"] = d["kind"]
            if "proposed" in d and a["kind"] == "edit":
                a["proposed"] = str(d["proposed"])[:5000]
                a["patch"] = edit_candidates(a["original"], a["proposed"])
            a["updated"] = now()
            self.store.save_annotations(items)
            self.store.log("annotation-updated", id=ident, before={k: before[k] for k in d if k in before},
                           after={k: a[k] for k in d if k in a})
            return self.json(a)
        self.json({"error": "not found"}, 404)

    def apply_annotation(self, ident: str, d: dict) -> None:
        items = self.store.annotations()
        a = next((x for x in items if x["id"] == ident), None)
        if not a or not a.get("patch"):
            return self.json({"error": "nothing to apply"}, 400)
        chosen = [m for i, m in enumerate(a["patch"]["matches"]) if i in set(d.get("indexes", range(99)))]
        done, errors = [], []
        for m in chosen:
            err = apply_patch(m)
            if err:
                errors.append(err)
            else:
                done.append(f"{m['file']}:{m['line']}")
        build = rebuild() if done else None
        if done:
            a["status"] = "applied"
            a["resolution"] = "written to " + ", ".join(done) + " from the review page"
            a["updated"] = now()
            self.store.save_annotations(items)
        self.store.log("annotation-applied", id=ident, done=done, errors=errors,
                       patches=[m for m in chosen if f"{m['file']}:{m['line']}" in done],
                       build_ok=build and build["ok"])
        self.json({"done": done, "errors": errors, "build": build, "annotation": a})

    # answers
    def save_answer(self, d: dict) -> None:
        qid = d.get("id")
        if not isinstance(qid, str):
            return self.json({"error": "bad answer"}, 400)
        doc = self.store.load("answers.json", {"version": 1, "answers": {}})
        prev = doc["answers"].get(qid, {})
        ans = {**prev, "value": d.get("value"), "other": str(d.get("other", ""))[:5000],
               "note": str(d.get("note", ""))[:10000], "updated": now()}
        if d.get("state") in ANSWER_STATES or d.get("state") is None:
            ans["state"] = d.get("state")
        doc["answers"][qid] = ans
        self.store.save("answers.json", {"version": 1, "answers": doc["answers"]})
        self.store.log("answer", id=qid, answer=ans)
        self.json(ans)


def report(store: Store) -> None:
    """Markdown summary of what is still to apply: open notes, answers not yet applied."""
    ann = [a for a in store.annotations() if a["status"] == "open"]
    print(f"# Open notes ({len(ann)})\n")
    for a in ann:
        where = [f"{m['file']}:{m['line']}" for m in (a.get("patch") or {}).get("matches", [])] or \
                [f"{s['file']}:{s['line']}?" for s in a.get("sources", [])[:3]]
        print(f"- `{a['id']}` {a['page']} **{a['kind']}**" + (f" ({', '.join(where)})" if where else ""))
        if a["kind"] == "edit":
            print(f"  - from: {a['original']}\n  - to:   {a['proposed']}")
        elif a["anchor"].get("quote"):
            print(f"  - on: \"{a['anchor']['quote']}\"")
        else:
            print(f"  - on: {a['original'][:200]}")
        if a["comment"]:
            print(f"  - note: {a['comment']}")
    qs = {q["id"]: q for q in store.load("questions.json", {"questions": []})["questions"]}
    ans = store.load("answers.json", {"answers": {}})["answers"]
    todo = [(k, v) for k, v in ans.items() if v.get("state") and not v.get("applied")]
    print(f"\n# Answers not applied yet ({len(todo)})\n")
    for k, v in sorted(todo, key=lambda kv: kv[1].get("updated", "")):
        q = qs.get(k, {})
        val = v.get("value")
        if isinstance(val, dict):
            val = "; ".join(f"{a}: {b}" for a, b in val.items() if str(b).strip())
        elif isinstance(val, list):
            val = ", ".join(val)
        parts = [p for p in (val, v.get("other"), ("note: " + v["note"]) if v.get("note") else "") if p]
        print(f"- `{k}` [{v['state']}] {q.get('q', '?')}\n  - " + " · ".join(parts or ["(no value)"]))


def resolve_item(store: Store, ident: str, text: str) -> None:
    """Record that a note or an answer has been applied (and how)."""
    items = store.annotations()
    for a in items:
        if a["id"] == ident:
            a.update(status="applied", resolution=text, updated=now())
            store.save_annotations(items)
            store.log("annotation-resolved", id=ident, resolution=text)
            return print(f"note {ident}: applied ({text})")
    doc = store.load("answers.json", {"version": 1, "answers": {}})
    if ident in doc["answers"]:
        doc["answers"][ident]["applied"] = f"{text} ({now()[:10]})"
        store.save("answers.json", {"version": 1, "answers": doc["answers"]})
        store.log("answer-applied", id=ident, applied=text)
        return print(f"answer {ident}: applied ({text})")
    sys.exit(f"no note or answer with id {ident}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=8777)
    ap.add_argument("--data", type=Path, default=ROOT.parent / "review",
                    help="directory for questions.json, answers.json, annotations.json (default: ../review)")
    ap.add_argument("--no-build", action="store_true", help="serve _site/ as it is")
    ap.add_argument("--base", default="HEAD", help="commit that 'what changed' compares with (default: HEAD, the last commit)")
    ap.add_argument("--open", action="store_true", help="open the browser")
    ap.add_argument("--report", action="store_true", help="print open notes and unapplied answers (Markdown), then exit")
    ap.add_argument("--resolve", nargs=2, metavar=("ID", "HOW"), help="mark a note or an answer as applied, then exit")
    args = ap.parse_args()
    if args.report or args.resolve:
        store = Store(args.data.resolve())
        return report(store) if args.report else resolve_item(store, *args.resolve)

    if not args.no_build:
        r = rebuild()
        print(("built" if r["ok"] else "BUILD FAILED") + f" in {r['seconds']} s")
        if not r["ok"]:
            print(r["output"])
    Handler.store = Store(args.data.resolve())
    Handler.token = secrets.token_urlsafe(16)
    Handler.port = args.port
    Handler.base = args.base
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    base = f"http://127.0.0.1:{args.port}"
    print(f"site with review overlay: {base}/\nquestions: {base}/__review/questions\n"
          f"annotations: {base}/__review/annotations\nwhat changed: {base}/__review/changes\n"
          f"data: {Handler.store.data}")
    if args.open:
        webbrowser.open(base + "/__review/")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
