#!/usr/bin/env python3
"""Local review server: the built site with an edit/annotate overlay, plus a question page.

    python3 tools/review.py               # build the site, serve it on http://127.0.0.1:8777/
    python3 tools/review.py --data DIR    # where questions, answers and annotations live
    python3 tools/review.py --report      # what is still to apply (open notes, new answers)
    python3 tools/review.py --resolve ID "commit abc123"   # record that it was applied

On every page, a toolbar lets you edit text in place or attach comments. Simple text edits
can be written back to the source file (content/*.toml|md, data/acher.bib, templates/) and the
site rebuilt; everything else stays recorded for later. `/__review/questions` lists the
questions of DIR/questions.json and saves the answers.

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
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

ROOT = Path(__file__).resolve().parent.parent
UI = Path(__file__).resolve().parent / "review"
SITE = ROOT / "_site"
SOURCES = ["content/*.toml", "content/*.md", "data/acher.bib", "templates/*.html", "templates/md/*"]
KINDS = {"edit", "comment", "question", "wrong", "remove", "missing"}
STATUSES = {"open", "applied", "wontfix"}
ANSWER_STATES = {"answered", "later", "unknown"}
LOCK = threading.Lock()


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


def rebuild() -> dict:
    t = time.time()
    cmd = ["uv", "run", "build.py"] if shutil.which("uv") else [sys.executable, "build.py"]
    r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
    out = (r.stdout + r.stderr).strip().splitlines()
    return {"ok": r.returncode == 0, "seconds": round(time.time() - t, 1), "output": "\n".join(out[-15:])}


# --- HTTP -----------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    server_version = "review/1"
    store: Store
    token: str
    port: int

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
        snippet = (
            '<link rel="stylesheet" href="/__review/static/overlay.css">'
            f"<script>window.REVIEW={json.dumps({'token': self.token})};</script>"
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
        body = (UI / "hub.html").read_text(encoding="utf-8")
        for k, v in {"__ANN_OPEN__": open_, "__ANN_TOTAL__": len(ann), "__Q_DONE__": answered,
                     "__Q_TOTAL__": len(q), "__DATA__": html.escape(str(self.store.data))}.items():
            body = body.replace(k, str(v))
        self.send(200, body.encode("utf-8"), "text/html; charset=utf-8")

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
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    base = f"http://127.0.0.1:{args.port}"
    print(f"site with review overlay: {base}/\nquestions: {base}/__review/questions\n"
          f"annotations: {base}/__review/annotations\ndata: {Handler.store.data}")
    if args.open:
        webbrowser.open(base + "/__review/")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
