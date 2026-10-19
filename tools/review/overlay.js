// Review overlay, injected by tools/review.py into every page it serves (never deployed).
// Modes: Browse (normal), Edit (click a text block, change it in place), Comment (click a
// block or select text, leave a note). Everything is saved through /__review/api.
(() => {
  "use strict";
  const TOKEN = (window.REVIEW || {}).token;
  const API = "/__review/api";
  const BLOCKS = "h1,h2,h3,h4,h5,h6,p,li,dt,dd,td,th,blockquote,figcaption";
  const KINDS = {
    edit: "Edit", comment: "Comment", question: "Unclear / question",
    wrong: "Wrong or outdated", missing: "Something missing", remove: "Remove",
  };
  const STATUS = { open: "open", applied: "done", wontfix: "won't fix" };
  const page = location.pathname;
  let mode = "browse";
  let items = [];
  let editing = null;
  let popover = null;

  // --- helpers -------------------------------------------------------------
  function h(tag, attrs, ...kids) {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v == null || v === false) continue;
      if (k === "class") el.className = v;
      else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
      else if (k === "text") el.textContent = v;
      else el.setAttribute(k, v === true ? "" : v);
    }
    for (const kid of kids.flat(Infinity)) if (kid != null) el.append(kid);
    return el;
  }
  async function api(method, url, body) {
    const r = await fetch(API + url, {
      method, headers: { "Content-Type": "application/json", "X-Review-Token": TOKEN },
      body: body ? JSON.stringify(body) : undefined,
    });
    if (!r.ok) throw new Error(method + " " + url + ": " + r.status);
    return r.json();
  }
  const store = {
    get(k) { try { return sessionStorage.getItem(k); } catch { return null; } },
    set(k, v) { try { sessionStorage.setItem(k, v); } catch { /* private mode */ } },
  };
  const textOf = (el) => (el.innerText || el.textContent || "").replace(/\s+/g, " ").trim();
  const isUI = (el) => !!(el && el.closest && el.closest(".rv-ui"));
  function blockOf(node) {
    const el = node && (node.nodeType === 1 ? node : node.parentElement);
    const b = el && el.closest(BLOCKS);
    return b && !isUI(b) && textOf(b) ? b : null;
  }
  const typing = (e) => e.target.closest && e.target.closest("input, textarea, select, [contenteditable=true], [contenteditable=plaintext-only]");
  const short = (s, n = 140) => (s.length > n ? s.slice(0, n - 1) + "…" : s);

  function cssPath(el) {
    const parts = [];
    for (let e = el; e && e.nodeType === 1 && e !== document.body; e = e.parentElement) {
      if (e.id) { parts.unshift("#" + CSS.escape(e.id)); return parts.join(" > "); }
      let i = 1;
      for (let s = e.previousElementSibling; s; s = s.previousElementSibling) if (s.tagName === e.tagName) i++;
      parts.unshift(e.tagName.toLowerCase() + ":nth-of-type(" + i + ")");
    }
    return ["body", ...parts].join(" > ");
  }
  function anchorOf(el, quote) {
    const owner = el.closest("[id]");
    const a = { selector: cssPath(el), tag: el.tagName.toLowerCase(), id: owner ? owner.id : "", quote: "", prefix: "", suffix: "" };
    if (quote) {
      const t = textOf(el), q = quote.replace(/\s+/g, " ").trim(), i = t.indexOf(q);
      a.quote = q;
      if (i >= 0) { a.prefix = t.slice(Math.max(0, i - 40), i); a.suffix = t.slice(i + q.length, i + q.length + 40); }
    }
    return a;
  }
  function resolve(a) {
    const wanted = [a.original, a.proposed].filter(Boolean);
    const ok = (el) => el && wanted.includes(textOf(el));
    let el = null;
    try { el = document.querySelector(a.anchor.selector); } catch { /* bad selector */ }
    if (ok(el)) return el;
    const scope = (a.anchor.id && document.getElementById(a.anchor.id)) || document.body;
    for (const c of scope.querySelectorAll(a.anchor.tag || BLOCKS)) if (ok(c) && !isUI(c)) return c;
    if (a.anchor.quote) {
      for (const c of scope.querySelectorAll(BLOCKS)) if (!isUI(c) && textOf(c).includes(a.anchor.quote)) return c;
    }
    return el && !isUI(el) ? el : null;  // text changed since: best effort
  }

  // --- toolbar -------------------------------------------------------------
  const modeButtons = {};
  const countLabel = h("span", { class: "rv-count", text: "0" });
  const toolbar = h("div", { class: "rv-ui rv-toolbar", role: "toolbar", "aria-label": "Review" },
    h("span", { class: "rv-brand", title: "Local review mode (tools/review.py)", text: "Review" }),
    h("div", { class: "rv-seg" },
      ["browse", "edit", "comment"].map((m) => (modeButtons[m] = h("button", {
        type: "button", "data-mode": m, title: { browse: "Browse normally (b or Esc)", edit: "Click a text to change it (e)", comment: "Click a text or select words to comment (c)" }[m],
        onclick: () => setMode(m), text: { browse: "Browse", edit: "Edit", comment: "Comment" }[m],
      })))),
    h("button", { type: "button", class: "rv-btn", title: "Notes on this page", onclick: () => toggleDrawer() }, "Notes ", countLabel),
    h("a", { class: "rv-btn", href: "/__review/questions", title: "Questions to answer" }, "Questions"),
    h("a", { class: "rv-btn", href: "/__review/annotations", title: "All notes, all pages" }, "All notes"),
    h("button", { type: "button", class: "rv-btn", title: "Rebuild the site from the sources and reload", onclick: rebuild }, "Rebuild"),
  );
  const layer = h("div", { class: "rv-ui rv-layer" });
  const hint = h("div", { class: "rv-ui rv-hint", hidden: true });

  function setMode(m) {
    if (editing) cancelEdit();
    mode = m;
    store.set("rv-mode", m);
    document.documentElement.dataset.rvMode = m;
    for (const [k, b] of Object.entries(modeButtons)) b.setAttribute("aria-pressed", String(k === m));
    hint.hidden = m === "browse";
    hint.textContent = m === "edit"
      ? "Edit mode: click a text, change it, then Save (⌘/Ctrl+Enter). Esc cancels."
      : "Comment mode: click a text, or select a few words, to leave a note.";
    clearHover();
  }

  async function rebuild() {
    toast("Rebuilding…");
    try {
      const r = await api("POST", "/rebuild");
      if (r.ok) location.reload();
      else toast("Build failed:\n" + r.output, 12000);
    } catch (e) { toast(String(e), 6000); }
  }

  function toast(msg, ms = 2500) {
    const t = h("div", { class: "rv-ui rv-toast", text: msg });
    document.body.append(t);
    setTimeout(() => t.remove(), ms);
  }

  // --- hover + clicks --------------------------------------------------------
  let hovered = null;
  function clearHover() { if (hovered) hovered.classList.remove("rv-hover"); hovered = null; }
  document.addEventListener("mouseover", (e) => {
    if (mode === "browse" || editing || isUI(e.target)) return clearHover();
    const b = blockOf(e.target);
    if (b === hovered) return;
    clearHover();
    if (b) { hovered = b; b.classList.add("rv-hover"); }
  });

  document.addEventListener("click", (e) => {
    if (mode === "browse" || isUI(e.target)) return;
    if (e.target.closest("summary")) return;  // let <details> (abstracts, BibTeX) open
    if (editing) {
      if (editing.el.contains(e.target)) { if (e.target.closest("a")) e.preventDefault(); return; }
      e.preventDefault(); e.stopPropagation();
      editing.bar.classList.add("rv-shake"); setTimeout(() => editing && editing.bar.classList.remove("rv-shake"), 400);
      return;
    }
    if (Date.now() - selectedAt < 500) { e.preventDefault(); e.stopPropagation(); return; }  // handled on mouseup
    const b = blockOf(e.target);
    if (!b) return;
    e.preventDefault(); e.stopPropagation();
    clearHover();
    if (mode === "edit") startEdit(b);
    else openComment(b, "");
  }, true);

  // Comment mode: selecting words opens a note on that quote.
  let selectedAt = 0;
  document.addEventListener("mouseup", (e) => {
    if (mode !== "comment" || editing || isUI(e.target)) return;
    const sel = window.getSelection();
    const quote = sel && !sel.isCollapsed ? sel.toString().trim() : "";
    if (!quote) return;
    selectedAt = Date.now();
    const b = blockOf(sel.getRangeAt(0).commonAncestorContainer);
    if (!b) { toast("Select words within a single paragraph or item"); return; }
    clearHover();
    openComment(b, quote);
  }, true);

  document.addEventListener("keydown", (e) => {
    if (editing) {
      if (e.key === "Escape") { e.preventDefault(); cancelEdit(); }
      else if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); saveEdit(); }
      return;
    }
    if (e.key === "Escape" && popover) return closePopover();
    if (typing(e) || e.metaKey || e.ctrlKey || e.altKey) return;
    if (e.key === "e") setMode("edit");
    else if (e.key === "c") setMode("comment");
    else if (e.key === "b" || e.key === "Escape") setMode("browse");
  });

  // --- editing in place --------------------------------------------------------
  function place(box, el) {
    const r = el.getBoundingClientRect();
    box.style.top = window.scrollY + r.bottom + 6 + "px";
    box.style.left = Math.max(8, Math.min(window.scrollX + r.left, window.scrollX + document.documentElement.clientWidth - box.offsetWidth - 8)) + "px";
  }

  function startEdit(el) {
    closePopover();
    const comment = h("input", { type: "text", class: "rv-input", placeholder: "Why? (optional note)" });
    const bar = h("div", { class: "rv-ui rv-editbar" },
      comment,
      h("button", { type: "button", class: "rv-primary", onclick: saveEdit, text: "Save" }),
      h("button", { type: "button", onclick: cancelEdit, text: "Cancel" }),
      h("span", { class: "rv-muted", text: "⌘/Ctrl+Enter · Esc" }));
    editing = { el, bar, comment, original: textOf(el), html: el.innerHTML };
    el.classList.add("rv-editing");
    try { el.contentEditable = "plaintext-only"; } catch { el.contentEditable = "true"; }
    if (el.contentEditable !== "plaintext-only") el.contentEditable = "true";
    document.body.append(bar);
    place(bar, el);
    el.focus();
  }

  function stopEdit() {
    if (!editing) return null;
    const e = editing;
    e.el.removeAttribute("contenteditable");
    e.el.classList.remove("rv-editing");
    e.bar.remove();
    editing = null;
    return e;
  }
  function cancelEdit() {
    const e = stopEdit();
    if (e) e.el.innerHTML = e.html;
  }

  async function saveEdit() {
    if (!editing) return;
    const proposed = textOf(editing.el), note = editing.comment.value.trim();
    const e = stopEdit();
    if (proposed === e.original && !note) return;
    if (proposed === e.original) return save(e.el, { kind: "comment", comment: note });
    e.el.classList.add("rv-edited");
    const a = await save(e.el, { kind: "edit", original: e.original, proposed, comment: note });
    if (a) showAnnotation(a, true);
  }

  // --- comments ----------------------------------------------------------------
  function openComment(el, quote) {
    closePopover();
    const kind = h("select", { class: "rv-input" }, Object.entries(KINDS).filter(([k]) => k !== "edit")
      .map(([k, v]) => h("option", { value: k, text: v })));
    const text = h("textarea", { class: "rv-input", rows: 4, placeholder: "Your note: what is wrong, what is missing, what to check…" });
    const box = popoverBox(el,
      h("div", { class: "rv-pop-head" }, h("strong", { text: "New note" }), closeButton()),
      quote ? h("blockquote", { class: "rv-quote", text: "“" + short(quote, 300) + "”" }) : h("p", { class: "rv-muted", text: short(textOf(el), 200) }),
      kind, text,
      h("div", { class: "rv-actions" },
        h("button", { type: "button", class: "rv-primary", text: "Save", onclick: async () => {
          if (!text.value.trim() && kind.value !== "remove") { text.focus(); return; }
          closePopover();
          await save(el, { kind: kind.value, comment: text.value.trim() }, quote);
        } }),
        h("button", { type: "button", text: "Cancel", onclick: closePopover })));
    text.addEventListener("keydown", (e) => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) box.querySelector(".rv-primary").click(); });
    text.focus();
  }

  async function save(el, fields, quote) {
    try {
      const a = await api("POST", "/annotations", {
        page, title: document.title, original: textOf(el), anchor: anchorOf(el, quote), proposed: "", ...fields,
      });
      items.push(a);
      render();
      toast(fields.kind === "edit" ? "Edit recorded" : "Note saved");
      return a;
    } catch (e) { toast("Not saved: " + e, 6000); return null; }
  }

  // --- popovers ----------------------------------------------------------------
  function closeButton() { return h("button", { type: "button", class: "rv-x", title: "Close (Esc)", text: "×", onclick: closePopover }); }
  function closePopover() { if (popover) popover.remove(); popover = null; }
  function popoverBox(el, ...kids) {
    closePopover();
    popover = h("div", { class: "rv-ui rv-popover" }, ...kids);
    document.body.append(popover);
    if (el) place(popover, el);
    else { popover.style.position = "fixed"; popover.style.top = "4rem"; popover.style.right = "1rem"; }
    return popover;
  }

  function wordDiff(a, b) {
    const x = a.split(/(\s+)/), y = b.split(/(\s+)/);
    if (x.length * y.length > 250000) return [h("del", { text: a }), " ", h("ins", { text: b })];
    const L = Array.from({ length: x.length + 1 }, () => new Uint16Array(y.length + 1));
    for (let i = x.length - 1; i >= 0; i--) for (let j = y.length - 1; j >= 0; j--)
      L[i][j] = x[i] === y[j] ? L[i + 1][j + 1] + 1 : Math.max(L[i + 1][j], L[i][j + 1]);
    const out = [];
    let i = 0, j = 0;
    while (i < x.length || j < y.length) {
      if (i < x.length && j < y.length && x[i] === y[j]) { out.push(x[i]); i++; j++; }
      else if (i < x.length && (j === y.length || L[i + 1][j] >= L[i][j + 1])) out.push(h("del", { text: x[i++] }));
      else out.push(h("ins", { text: y[j++] }));
    }
    return out;
  }

  function showAnnotation(a, fresh) {
    const el = resolve(a);
    const n = items.indexOf(a) + 1;
    const body = [];
    if (a.kind === "edit") body.push(h("p", { class: "rv-diff" }, wordDiff(a.original, a.proposed)));
    else if (a.anchor.quote) body.push(h("blockquote", { class: "rv-quote", text: "“" + short(a.anchor.quote, 300) + "”" }));
    const note = h("textarea", { class: "rv-input", rows: 3, placeholder: "Note" });
    note.value = a.comment || "";
    body.push(note);

    const patch = a.patch && a.status === "open" ? a.patch : null;
    const checks = [];
    if (patch) {
      body.push(h("p", { class: "rv-muted", text: patch.matches.length > 1
        ? "The changed words appear in " + patch.matches.length + " source places; write to the ones you tick:"
        : "Source of this text:" }));
      body.push(h("ul", { class: "rv-sources" }, patch.matches.map((m, i) => {
        const c = h("input", { type: "checkbox", checked: true, "data-i": i });
        checks.push(c);
        return h("li", {}, h("label", {}, c, " ", h("code", { text: m.file + ":" + m.line })));
      })));
    } else if (a.status === "open" && a.kind === "edit") {
      body.push(h("p", { class: "rv-muted", text: "Not mapped to a single place in the sources (markup or several fields in between): recorded for Claude to apply." }));
    }
    if (!patch && a.sources && a.sources.length) {
      body.push(h("details", { class: "rv-src" }, h("summary", { text: "Likely sources" }),
        h("ul", { class: "rv-sources" }, a.sources.map((s) => h("li", {}, h("code", { text: s.file + ":" + s.line }), " ", h("span", { class: "rv-muted", text: short(s.snippet, 90) }))))));
    }
    if (a.resolution) body.push(h("p", { class: "rv-ok", text: a.resolution }));

    const actions = h("div", { class: "rv-actions" });
    if (patch) actions.append(h("button", { type: "button", class: "rv-primary", text: "Write to source & rebuild", onclick: async () => {
      const indexes = checks.filter((c) => c.checked).map((c) => +c.dataset.i);
      if (!indexes.length) return;
      await update(a, { comment: note.value });
      try {
        const r = await api("POST", "/annotations/" + a.id + "/apply", { indexes });
        if (r.errors.length) toast(r.errors.join("\n"), 8000);
        if (r.done.length) { Object.assign(a, r.annotation); toast("Written to " + r.done.join(", ") + (r.build && r.build.ok ? " · rebuilt" : " · BUILD FAILED")); setTimeout(() => location.reload(), 900); }
      } catch (e) { toast(String(e), 6000); }
    } }));
    actions.append(h("button", { type: "button", text: "Save note", onclick: async () => { await update(a, { comment: note.value }); toast("Saved"); } }));
    if (a.status === "open") {
      actions.append(h("button", { type: "button", text: "Done", title: "Mark as handled", onclick: () => update(a, { status: "applied", comment: note.value }).then(closePopover) }));
      actions.append(h("button", { type: "button", text: "Won't fix", onclick: () => update(a, { status: "wontfix", comment: note.value }).then(closePopover) }));
    } else {
      actions.append(h("button", { type: "button", text: "Reopen", onclick: () => update(a, { status: "open" }).then(closePopover) }));
    }
    actions.append(h("button", { type: "button", class: "rv-danger", text: fresh && a.kind === "edit" ? "Undo" : "Delete", onclick: async () => {
      await api("DELETE", "/annotations/" + a.id);
      items = items.filter((x) => x !== a);
      if (a.kind === "edit" && el && a.status === "open") location.reload(); else { render(); closePopover(); }
    } }));

    popoverBox(el,
      h("div", { class: "rv-pop-head" },
        h("strong", { text: "#" + n + " " + KINDS[a.kind] }),
        h("span", { class: "rv-pill rv-" + a.status, text: STATUS[a.status] }),
        h("span", { class: "rv-muted", text: a.created.slice(0, 16).replace("T", " ") }),
        closeButton()),
      body, actions);
    if (el) flash(el);
  }

  async function update(a, fields) {
    try { Object.assign(a, await api("PUT", "/annotations/" + a.id, fields)); render(); }
    catch (e) { toast("Not saved: " + e, 6000); }
  }

  function flash(el) {
    el.scrollIntoView({ block: "center", behavior: "smooth" });
    el.classList.remove("rv-flash"); void el.offsetWidth; el.classList.add("rv-flash");
  }

  // --- badges and the notes drawer ---------------------------------------------
  let drawer = null;
  function toggleDrawer(force) {
    const show = force ?? !drawer;
    if (drawer) { drawer.remove(); drawer = null; }
    if (show) { drawer = h("aside", { class: "rv-ui rv-drawer" }); document.body.append(drawer); renderDrawer(); }
    store.set("rv-drawer", show ? "1" : "");
  }
  function renderDrawer() {
    if (!drawer) return;
    drawer.replaceChildren(
      h("div", { class: "rv-pop-head" }, h("strong", { text: "Notes on this page" }), h("a", { href: "/__review/annotations", text: "all pages →" }),
        h("button", { type: "button", class: "rv-x", text: "×", onclick: () => toggleDrawer(false) })),
      items.length ? h("ol", { class: "rv-list" }, items.map((a, i) => h("li", { class: "rv-" + a.status, onclick: () => showAnnotation(a) },
        h("span", { class: "rv-num", text: i + 1 }),
        h("span", {}, h("strong", { text: KINDS[a.kind] }), " · ", h("span", { class: "rv-muted", text: STATUS[a.status] }), h("br"),
          h("span", { text: short(a.kind === "edit" ? a.proposed : a.comment || a.anchor.quote || a.original, 120) })))))
        : h("p", { class: "rv-muted", text: "No notes yet. Switch to Edit or Comment and click a text." }));
  }

  function render() {
    document.querySelectorAll(".rv-annotated").forEach((el) => el.classList.remove("rv-annotated", "rv-st-open", "rv-st-applied", "rv-st-wontfix"));
    layer.replaceChildren();
    items.forEach((a, i) => {
      const el = resolve(a);
      a._el = el;
      if (!el) return;
      el.classList.add("rv-annotated", "rv-st-" + a.status);
      a._badge = h("button", { type: "button", class: "rv-badge rv-" + a.status, title: KINDS[a.kind] + ": " + short(a.comment || a.proposed || a.anchor.quote || "", 80),
        text: i + 1, onclick: (e) => { e.stopPropagation(); showAnnotation(a); } });
      layer.append(a._badge);
    });
    countLabel.textContent = items.filter((a) => a.status === "open").length + "/" + items.length;
    position();
    renderDrawer();
  }
  function measure() { document.documentElement.style.setProperty("--rv-tb-h", toolbar.offsetHeight + "px"); }
  window.addEventListener("resize", measure);
  function position() {
    for (const a of items) {
      if (!a._badge) continue;
      const r = a._el && a._el.getBoundingClientRect();
      const visible = r && (r.width || r.height);
      a._badge.hidden = !visible;
      if (visible) { a._badge.style.left = window.scrollX + r.right - 4 + "px"; a._badge.style.top = window.scrollY + r.top - 10 + "px"; }
    }
  }
  let raf = 0;
  const schedule = () => { cancelAnimationFrame(raf); raf = requestAnimationFrame(position); };
  window.addEventListener("resize", schedule);
  document.addEventListener("toggle", schedule, true);
  document.addEventListener("click", () => setTimeout(schedule, 60));
  document.addEventListener("input", schedule);
  setInterval(position, 1500);

  // --- start -------------------------------------------------------------------
  async function init() {
    document.body.append(toolbar, layer, hint);
    measure();
    setMode(store.get("rv-mode") || "browse");
    try { items = (await api("GET", "/annotations?page=" + encodeURIComponent(page))).annotations; }
    catch (e) { toast("Review server unreachable: " + e, 6000); }
    render();
    if (store.get("rv-drawer")) toggleDrawer(true);
    const focus = new URLSearchParams(location.search).get("rv");
    const a = focus && items.find((x) => x.id === focus);
    if (a) setTimeout(() => showAnnotation(a), 300);
  }
  init();
})();
