// Progressive enhancement only: every publication, abstract, and BibTeX entry
// is already in the HTML. This adds search/filters and copy buttons.
(() => {
  // Copy BibTeX
  if (navigator.clipboard) {
    for (const btn of document.querySelectorAll("[data-copy]")) {
      btn.hidden = false;
      btn.addEventListener("click", async () => {
        const pre = btn.parentElement.querySelector("pre");
        await navigator.clipboard.writeText(pre.innerText);
        btn.textContent = "Copied";
        setTimeout(() => (btn.textContent = "Copy"), 1500);
      });
    }
  }

  // Open the abstract of a publication targeted by the URL hash.
  const openTarget = () => {
    const el = location.hash && document.getElementById(decodeURIComponent(location.hash.slice(1)));
    if (el && el.classList.contains("pub")) {
      const abs = el.querySelector(".fold-abstract");
      if (abs) abs.open = true;
    }
  };
  openTarget();
  addEventListener("hashchange", openTarget);

  const form = document.querySelector("form.filters");
  if (!form) return;
  form.hidden = false;

  const fold = (s) => s.normalize("NFD").replace(/[̀-ͯ]/g, "").toLowerCase();
  const pubs = [...document.querySelectorAll(".pubs .pub")].map((el) => ({ el, text: fold(el.textContent) }));
  const groups = [...document.querySelectorAll(".year-group")];
  const input = form.querySelector("input[type=search]");
  const catButtons = [...form.querySelectorAll("[data-cat]")];
  const flagButtons = [...form.querySelectorAll("[data-flag]")];
  const expand = form.querySelector("[data-expand]");
  const counter = form.querySelector(".result-count");
  const state = { q: "", cat: "", flags: new Set() };

  const params = new URLSearchParams(location.search);
  state.q = params.get("q") || "";
  state.cat = params.get("type") || "";
  for (const f of (params.get("only") || "").split(",").filter(Boolean)) state.flags.add(f);
  input.value = state.q;

  const apply = () => {
    const terms = fold(state.q).split(/\s+/).filter(Boolean);
    let shown = 0;
    for (const { el, text } of pubs) {
      const ok =
        (!state.cat || el.dataset.cat === state.cat) &&
        [...state.flags].every((f) => f in el.dataset) &&
        terms.every((t) => text.includes(t));
      el.hidden = !ok;
      if (ok) shown++;
    }
    for (const g of groups) g.hidden = !g.querySelector(".pub:not([hidden])");
    for (const b of catButtons) b.setAttribute("aria-pressed", String(b.dataset.cat === state.cat));
    for (const b of flagButtons) b.setAttribute("aria-pressed", String(state.flags.has(b.dataset.flag)));
    const filtered = terms.length || state.cat || state.flags.size;
    counter.textContent = filtered ? (shown === 1 ? "1 publication matches" : `${shown} publications match`) : "";

    const p = new URLSearchParams();
    if (state.q) p.set("q", state.q);
    if (state.cat) p.set("type", state.cat);
    if (state.flags.size) p.set("only", [...state.flags].join(","));
    const qs = p.toString();
    history.replaceState(null, "", location.pathname + (qs ? "?" + qs : "") + location.hash);
  };

  let timer;
  input.addEventListener("input", () => {
    clearTimeout(timer);
    timer = setTimeout(() => { state.q = input.value; apply(); }, 120);
  });
  form.addEventListener("submit", (e) => e.preventDefault());
  for (const b of catButtons) b.addEventListener("click", () => { state.cat = b.dataset.cat; apply(); });
  for (const b of flagButtons)
    b.addEventListener("click", () => {
      const f = b.dataset.flag;
      state.flags.has(f) ? state.flags.delete(f) : state.flags.add(f);
      apply();
    });
  expand.addEventListener("click", () => {
    const on = expand.getAttribute("aria-pressed") !== "true";
    expand.setAttribute("aria-pressed", String(on));
    expand.textContent = on ? "Hide abstracts" : "Show all abstracts";
    for (const d of document.querySelectorAll(".fold-abstract")) d.open = on;
  });
  apply();
})();
