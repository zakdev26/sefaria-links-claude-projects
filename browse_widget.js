// browse_widget.js -- the Sefaria Links app's Browse, as one widget in a
// Claude chat. Every level is drawn here in the browser; only the final
// choice (a reference) goes back to Claude, through sendPrompt.
//
// Loaded by a short snippet (see project_instructions_v5.md) into
// <div id="snav" data-start="...">. Everything it shows comes from files
// beside it, made by `build_epub.py browse-data`: browse_toc.json (the
// contents) and browse_books/<id>.json (each book's structure and counts),
// so the widget never has to reach Sefaria itself. Live Sefaria is only a
// fallback for a book with no file.
(function (srcUrl) {
  "use strict";
  const API = "https://www.sefaria.org/api/";
  const BASE = String(srcUrl || "").replace(/[^/]*$/, "");
  const box = document.getElementById("snav");
  if (!box) return;

  // ---- look ---------------------------------------------------------------
  const css = document.createElement("style");
  css.textContent = `
#snav{--bg:#fff;--fg:#1f1f1f;--mut:#6b6b6b;--btn:#f3f1ec;--bd:#d6d2c8;--acc:#7a5c1e;--ok:#2f6b2f;
  font:15px/1.35 system-ui,-apple-system,"Segoe UI",sans-serif;color:var(--fg);background:var(--bg);padding:6px 2px}
@media(prefers-color-scheme:dark){#snav{--bg:#1e1e1e;--fg:#eee;--mut:#a5a5a5;--btn:#2c2b28;--bd:#4a473f;--acc:#d9b36a;--ok:#8fd18f}}
#snav .cr{display:flex;flex-wrap:wrap;gap:4px;align-items:center;margin:0 0 8px;font-size:13px;color:var(--mut)}
#snav .cr a{color:var(--acc);cursor:pointer;text-decoration:none}
#snav .bk{border:1px solid var(--bd);background:var(--btn);color:var(--fg);border-radius:6px;padding:3px 9px;margin-right:4px;cursor:pointer;font:inherit}
#snav .ls,#snav .gd{display:flex;flex-wrap:wrap;gap:6px}
#snav button.o{border:1px solid var(--bd);background:var(--btn);color:var(--fg);border-radius:8px;padding:7px 11px;
  min-height:38px;cursor:pointer;font:inherit;text-align:start}
#snav .gd button.o{min-width:44px;text-align:center;padding:7px 6px}
#snav button.o:hover{border-color:var(--acc)}
#snav button.o .h{display:block;font-size:13px;color:var(--mut);direction:rtl}
#snav button.w{width:100%;margin:0 0 8px;font-weight:600}
#snav .rows{columns:112px;column-gap:10px}
#snav .row{display:flex;align-items:center;gap:6px;margin:0 0 5px;break-inside:avoid}
#snav .row b{min-width:34px;text-align:end;color:var(--mut);font-weight:500}
#snav .nt{font-size:13px;color:var(--mut);margin:0 0 8px}
#snav .sent{color:var(--ok);font-weight:600;margin:8px 0 0}
#snav button.done{border-color:var(--ok);outline:2px solid var(--ok)}`;
  document.head.appendChild(css);

  const el = (tag, cls, txt) => {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (txt != null) e.textContent = txt;
    return e;
  };

  // ---- Sefaria, cached ----------------------------------------------------
  const memo = {};
  const get1 = url => fetch(url).then(r => { if (!r.ok) throw new Error("HTTP " + r.status); return r.json(); });
  function getJSON(url) {     // one retry: a dropped request shouldn't end the walk
    if (!memo[url]) memo[url] = get1(url).catch(() => new Promise(ok => setTimeout(ok, 400)).then(() => get1(url)));
    memo[url].catch(() => { delete memo[url]; });
    return memo[url];
  }
  const enc = encodeURIComponent;

  // The contents tree: [name, he, children] for a category, [title, he] for
  // a book. browse_toc.json first; Sefaria's own index (4 MB) if that fails.
  const isCommCat = c => /commentary$/i.test(c.searchRoot || "") || /commentary/i.test(c.category || "");
  const isCommBook = b => String(b.dependence || "").toLowerCase() === "commentary";
  function trim(nodes, parent) {
    const out = [];
    for (const c of nodes || []) {
      if (c.contents) {
        if (isCommCat(c)) continue;
        const k = trim(c.contents, c.category);
        if (k.length) out.push([c.category, c.heCategory || "", k]);
      } else if (c.title && !isCommBook(c)) out.push([c.title, c.heTitle || ""]);
    }
    if (String(parent).toLowerCase() === "midrash") {  // aggadah above halakhah, as the app
      const ai = out.findIndex(x => /aggad/i.test(x[0])), hi = out.findIndex(x => /hala[ck]h/i.test(x[0]));
      if (ai > -1 && hi > -1 && hi < ai) out.splice(hi, 0, out.splice(ai, 1)[0]);
    }
    return out;
  }
  let TOC = null;
  function loadToc() {
    if (TOC) return Promise.resolve(TOC);
    const live = () => getJSON(API + "index").then(t => trim(t, ""));
    const p = BASE ? getJSON(BASE + "browse_toc.json").catch(live) : live();
    return p.then(t => (TOC = t));
  }

  // ---- pages --------------------------------------------------------------
  // A page: { items: [{label, he, next?, ref?}], whole: [{label, ref}], note }.
  // next() gives the following page; ref ends the walk.
  const nav = (label, he, next) => ({ label, he: he || "", next });
  const fin = (label, he, ref) => ({ label, he: he || "", ref });
  const finalPage = ref => ({ items: [fin(ref, "", ref)] });

  // [name, he, children] is a category; [title, he, id] a book.
  const isCat = k => Array.isArray(k[2]);
  function catPage(kids) {
    return {
      items: kids.map(k => isCat(k)
        ? nav(k[0], k[1], () => catPage(k[2]))
        : nav(k[0], k[1], () => bookPage(k[0], k[2])))
    };
  }

  // A book: { i: its index record (schema, alts, ...), s: shape counts keyed
  // by each leaf's full title }. Counts go into SHAPE for the pages below.
  const SHAPE = {};
  async function liveBook(title) {
    const i = await getJSON(API + "v2/index/" + enc(title)), s = {};
    try {
      const j = await getJSON(API + "shape/" + enc(title));
      for (const f of Array.isArray(j) ? j : [j])
        for (const leaf of f && f.isComplex && Array.isArray(f.chapters) ? f.chapters : [f])
          if (leaf && leaf.title && leaf.chapters != null) s[leaf.title] = leaf.chapters;
    } catch (e) {}
    return { i, s };
  }
  async function getBook(title, id) {
    let bk = null;
    if (id != null && BASE) { try { bk = await getJSON(BASE + "browse_books/" + id + ".json"); } catch (e) {} }
    if (!bk) bk = await liveBook(title);
    Object.assign(SHAPE, bk.s || {});
    return bk.i;
  }

  const nodeEn = n => n.title || n.category || ((n.titles || []).find(x => x.lang === "en" && x.primary) || (n.titles || []).find(x => x.lang === "en") || {}).text || n.key || "section";
  const nodeHe = n => n.heTitle || n.heCategory || ((n.titles || []).find(x => x.lang === "he" && x.primary) || (n.titles || []).find(x => x.lang === "he") || {}).text || "";
  const altStructs = rec => {
    const a = rec && (rec.alt_structs || rec.alts);
    return a && typeof a === "object" ? Object.keys(a).filter(k => a[k] && Array.isArray(a[k].nodes) && a[k].nodes.length).map(k => ({ name: k, nodes: a[k].nodes })) : [];
  };
  const isDafSchema = s => !!(s && !s.nodes && Array.isArray(s.addressTypes) && s.addressTypes[0] === "Talmud");
  const dafFromRef = r => { const m = String(r).match(/\s(\d+[ab])(?::|\s*-|$)/); return m ? m[1] : null; };
  const STRUCT_HE = { parasha: "לפי פרשה", parshiot: "לפי פרשה", daf: "לפי דף", essay: "לפי מאמר", tikkunim: "לפי תיקון", gate: "לפי שער", topic: "לפי נושא", chapters: "לפי פרק" };
  const SECNAME_HE = { chapter: "לפי פרק", perek: "לפי פרק", daf: "לפי דף", siman: "לפי סימן", mishnah: "לפי משנה", halakhah: "לפי הלכה", volume: "לפי כרך", paragraph: "לפי פסקה", verse: "לפי פסוק", section: "לפי חלק" };
  const ALIYOT = ["Rishon", "Sheni", "Shlishi", "Revi'i", "Chamishi", "Shishi", "Shevi'i"];
  const HE_ALIYOT = ["ראשון", "שני", "שלישי", "רביעי", "חמישי", "שישי", "שביעי"];

  async function bookPage(title, id) {
    const rec = await getBook(title, id);
    const schema = rec && rec.schema;
    if (!schema) return finalPage(title);
    const structs = altStructs(rec);
    if (isDafSchema(schema)) {             // a tractate: by perek or by daf
      const ch = structs.find(s => /chapter/i.test(s.name)) || structs[0];
      const items = [];
      if (ch && ch.nodes.every(n => n && Array.isArray(n.refs) && n.refs.length))
        items.push(nav("By perek", "לפי פרק", () => perekList(title, ch.nodes)));
      items.push(nav("By daf", "לפי דף", () => dafPage(title, schema)));
      return { items };
    }
    // The book's own schema plus its alternate structures, as the site shows
    // them: default_struct first, exclude_structs hidden.
    const ex = (rec.exclude_structs || []).map(s => String(s).toLowerCase());
    const rows = [];
    if (ex.indexOf("schema") < 0) {
      const sn = String((schema.sectionNames || [])[0] || "");
      rows.push(nav(sn ? "By " + sn.toLowerCase() : "Contents", SECNAME_HE[sn.toLowerCase()], () => defaultSchema(title, schema)));
    }
    for (const s of structs) rows.push(Object.assign(nav("By " + s.name.toLowerCase(), STRUCT_HE[s.name.toLowerCase()], () => altPage(title, s.nodes, s.name)), { st: s.name }));
    const i = rows.findIndex(r => r.st && r.st === rec.default_struct);
    if (i > 0) rows.unshift(rows.splice(i, 1)[0]);
    return rows.length ? { items: rows } : defaultSchema(title, schema);
  }

  function perekList(title, nodes) {
    return {
      items: nodes.map((n, i) => {
        const en = nodeEn(n);
        return nav(en && en !== "section" ? en : "Chapter " + (i + 1), nodeHe(n), () => perekPage(title, n, i + 1));
      })
    };
  }

  // One chapter: the whole chapter, then its pages. A page shared with the
  // next chapter is Sefaria's partial ref ("Shabbat 20b:1-4").
  function perekPage(title, node, num) {
    const first = dafFromRef(node.refs[0]), last = dafFromRef(node.refs[node.refs.length - 1]);
    const rng = first ? title + " " + first + (last && last !== first ? "-" + last : "") : title;
    return {
      whole: [{ label: "Whole chapter — " + rng, ref: rng + " (chapter " + num + ")" }],
      items: node.refs.map(r => fin(dafFromRef(r) || r, "", r)),
      note: "a is the front of the page, b the back"
    };
  }

  // Every amud that has text, from the shape (index 0 is 1a); the app's
  // 2a-on fallback if there is no shape.
  function dafPage(prefix, node) {
    const ch = SHAPE[prefix];
    let refs = Array.isArray(ch)
      ? ch.map((c, i) => (Array.isArray(c) ? c.length : c) ? prefix + " " + ((i >> 1) + 1) + "ab"[i & 1] : null).filter(Boolean)
      : [];
    if (!refs.length) {
      const n = (node && node.lengths && node.lengths[0]) || 180;
      refs = Array.from({ length: n }, (_, i) => prefix + " " + (2 + (i >> 1)) + "ab"[i & 1]);
    }
    return { items: refs.map(r => fin(r.slice(prefix.length + 1), "", r)), note: "a is the front of the page, b the back" };
  }

  const altEn = n => { if (n.default) return "(main text)"; const t = nodeEn(n); return t && t !== "section" ? t : "(main text)"; };
  const dafLabelFrom = (start, i) => {
    const m = String(start).match(/^(\d+)\s*([ab])$/);
    const s = m ? parseInt(m[1], 10) * 2 + (m[2] === "b" ? 1 : 0) + i : 4 + i;
    return (s >> 1) + "ab"[s & 1];
  };

  function altPage(title, nodes, sname) {          // renderAltNodes
    const def = nodes.find(n => n && n.default && (Array.isArray(n.refs) || n.wholeRef));
    const pg = def ? altLeaf(title, def, sname) : { items: [], whole: [] };
    for (const n of nodes) {
      if (!n || n === def) continue;
      const he = n.default ? "" : nodeHe(n);
      if (Array.isArray(n.nodes) && n.nodes.length) pg.items.push(nav(altEn(n), he, () => altPage(title, n.nodes, sname)));
      else if (n.wholeRef && !(Array.isArray(n.refs) && n.refs.length)) pg.items.push(fin(altEn(n), he, n.wholeRef));  // one section: no page of its own
      else pg.items.push(nav(altEn(n), he, () => altLeaf(title, n, sname)));
    }
    return pg;
  }

  function altLeaf(title, node, sname) {           // appendAltLeaf
    const pg = { items: [], whole: [] };
    if (node.wholeRef) {
      const nm = altEn(node);
      pg.whole.push({ label: "Whole " + (nm === "(main text)" ? String(sname || "section").toLowerCase() : nm) + " — " + node.wholeRef, ref: node.wholeRef });
    }
    const refs = Array.isArray(node.refs) ? node.refs : [];
    const at = Array.isArray(node.addressTypes) ? node.addressTypes : [];
    if (node.startingAddress || at.indexOf("Talmud") >= 0 || at.indexOf("Folio") >= 0) {
      pg.items = refs.map((r, i) => fin(node.startingAddress ? dafLabelFrom(node.startingAddress, i) : (dafFromRef(r) || String(i + 1)), "", r));
      pg.note = "a is the front of the page, b the back";
      return pg;
    }
    const aliyot = refs.length === 7 && /^parash/i.test(String(sname || ""));
    pg.items = refs.map((r, i) => fin(aliyot ? ALIYOT[i] + " · " + r : r, aliyot ? HE_ALIYOT[i] : "", r));
    return pg;
  }

  const depthOf = n => n.depth || (n.sectionNames || []).length || 1;
  const refJoin = (prefix, secs) => prefix + " " + secs.join(":");

  function defaultSchema(title, schema) {
    return schema.nodes ? schemaNodes(title, schema, title) : jagged(title, schema);
  }

  function schemaNodes(title, node, prefix) {      // renderSchemaNodes
    const items = (node.nodes || []).map(ch => {
      const label = ch.default ? "(main text)" : nodeEn(ch), he = ch.default ? "" : nodeHe(ch);
      const cp = ch.default ? prefix : prefix + ", " + nodeEn(ch);
      if (ch.nodes) return nav(label, he, () => schemaNodes(title, ch, cp));
      if (depthOf(ch) === 1) return fin(label, he, cp);
      return nav(label, he, () => jagged(cp, ch));
    });
    return items.length ? { items } : finalPage(prefix);
  }

  // How many sections are under prefix + secs, from the shape: a list is
  // counted, a plain number is the count (shapeLen / fetchCount in the app).
  function countAt(prefix, secs) {
    let v = SHAPE[prefix];
    for (const n of secs) v = Array.isArray(v) ? v[n - 1] : undefined;
    return Array.isArray(v) ? v.length : (typeof v === "number" ? v : null);
  }

  function numberItems(prefix, node, secs, count) {
    const depth = (node.sectionNames || []).length || 1;
    return Array.from({ length: count }, (_, k) => {
      const s = secs.concat(k + 1);
      return s.length >= depth ? fin(String(k + 1), "", refJoin(prefix, s)) : nav(String(k + 1), "", () => pickNumber(prefix, node, s));
    });
  }

  function jagged(prefix, node) {                  // renderJagged
    if (depthOf(node) === 1) return finalPage(prefix);
    if ((node.addressTypes || [])[0] === "Talmud") return dafPage(prefix, node);
    const sname = String((node.sectionNames || [])[0] || "section").toLowerCase();
    const n = countAt(prefix, []) || (node.lengths && node.lengths[0]);
    if (!n) return finalPage(prefix);
    return { items: numberItems(prefix, node, [], n), note: "pick a " + sname };
  }

  function pickNumber(prefix, node, secs) {        // pickNumber
    const cur = refJoin(prefix, secs);
    const nxt = String((node.sectionNames || [])[secs.length] || "part").toLowerCase();
    const c = countAt(prefix, secs);
    return { whole: [{ label: "All of " + cur, ref: cur }], items: c ? numberItems(prefix, node, secs, c) : [], note: "all of it, or narrow to a " + nxt };
  }

  // ---- walking and drawing -------------------------------------------------
  let stack = [];          // [{label, page}]
  let busy = 0;
  let retry = null;        // the step that failed, for "Try again"

  function send(text, btn) {
    if (btn) btn.classList.add("done");
    const old = box.querySelector(".sent");
    if (old) old.remove();
    if (typeof sendPrompt === "function") {
      sendPrompt(text);
      box.appendChild(el("div", "sent", "✓ Sent: " + text));
    } else {
      box.appendChild(el("div", "sent", "Type this in the chat: " + text));
    }
  }

  const chatPath = () => "browse: " + stack.slice(1).map(s => s.label).join(" > ");

  async function go(label, next, silent) {
    const my = ++busy;
    const t = setTimeout(() => { if (my === busy) draw(null, "Loading…"); }, 150);
    let page;
    try { page = await next(); } catch (e) { page = null; }
    clearTimeout(t);
    if (my !== busy) return false;
    if (!page) { retry = () => go(label, next); draw(null, "Couldn’t load this book."); return false; }
    // A level with only one way on is passed through, as Sefaria's site does.
    if (page.items.length === 1 && !(page.whole || []).length && page.items[0].next) {
      stack.push({ label, page, skip: true });
      return go(page.items[0].label, page.items[0].next, silent);
    }
    stack.push({ label, page });
    if (!silent) draw(page);
    return true;
  }

  function back(to) {        // to: index in stack to show
    while (to > 0 && stack[to].skip) to--;
    stack = stack.slice(0, to + 1);
    busy++;
    draw(stack[to].page);
  }

  function draw(page, msg) {
    box.textContent = "";
    const cr = el("div", "cr");
    const shown = stack.map((s, i) => ({ s, i })).filter(x => !x.s.skip);
    if (shown.length > 1) {
      const b = el("button", "bk", "‹ Back");
      b.onclick = () => back(shown[shown.length - 2].i);
      cr.appendChild(b);
    }
    shown.forEach((x, k) => {
      if (k) cr.appendChild(document.createTextNode("›"));
      if (k < shown.length - 1 || msg) { const a = el("a", "", x.s.label); a.onclick = () => back(x.i); cr.appendChild(a); }
      else cr.appendChild(el("span", "", x.s.label));
    });
    box.appendChild(cr);
    if (msg) {
      box.appendChild(el("div", "nt", msg));
      if (/Couldn/.test(msg)) {
        const wrap = el("div", "ls");
        if (retry) { const r = el("button", "o", "Try again"); r.onclick = retry; wrap.appendChild(r); }
        const b = el("button", "o", "Browse step by step in the chat instead");
        b.onclick = () => send(chatPath(), b);
        wrap.appendChild(b);
        box.appendChild(wrap);
      }
      return;
    }
    if (page.note) box.appendChild(el("div", "nt", page.note));
    for (const w of page.whole || []) {
      const b = el("button", "o w", w.label);
      b.onclick = () => send(w.ref, b);
      box.appendChild(b);
    }
    const items = page.items;
    const opt = (it, text) => {
      const b = el("button", "o", text == null ? it.label : text);
      if (it.he && text == null) b.appendChild(el("span", "h", it.he));
      b.onclick = () => it.ref ? send(it.ref, b) : go(it.label, it.next);
      return b;
    };
    // Talmud pages: one row per daf, its a / b buttons beside it.
    const daf = items.length > 1 && items.every(it => it.ref && /^\d+[ab]/.test(it.label) && dafFromRef(it.ref) === it.label.match(/^\d+[ab]/)[0]);
    if (daf) {
      const rows = el("div", "rows");
      box.appendChild(rows);
      let row = null, d = null;
      for (const it of items) {
        const tok = (it.ref.match(/\s(\d+)([ab](?::[\d-]+)?)$/) || [, it.label, ""]);
        if (tok[1] !== d) { d = tok[1]; row = el("div", "row"); row.appendChild(el("b", "", d)); rows.appendChild(row); }
        row.appendChild(opt(it, tok[2] || it.label));
      }
      return;
    }
    const grid = items.every(it => it.label.length <= 4 && !it.he);
    const wrap = el("div", grid ? "gd" : "ls");
    items.forEach(it => wrap.appendChild(opt(it)));
    box.appendChild(wrap);
  }

  // ---- start ----------------------------------------------------------------
  // data-start: a book title ("Shabbat") or a path ("Talmud > Bavli").
  async function start() {
    draw(null, "Loading…");
    let toc;
    try { toc = await loadToc(); } catch (e) { stack = [{ label: "Sefaria" }]; retry = start; draw(null, "Couldn’t load the contents."); return; }
    stack = [];
    const startAt = String(box.getAttribute("data-start") || "").trim();
    let parts = startAt ? startAt.split(">").map(s => s.trim()).filter(Boolean) : [];
    if (parts.length && !toc.some(k => k[0].toLowerCase() === parts[0].toLowerCase())) {
      const want = parts[0].toLowerCase(), path = [];
      const look = (kids, trail) => {
        for (const k of kids) {
          if (isCat(k)) { if (look(k[2], trail.concat(k[0]))) return true; }
          else if (k[0].toLowerCase() === want || k[1] === parts[0]) { path.push(...trail, k[0]); return true; }
        }
        return false;
      };
      if (look(toc, [])) parts = path.concat(parts.slice(1));
    }
    await go("Sefaria", () => catPage(toc), parts.length > 0);
    for (let i = 0; i < parts.length; i++) {
      const pg = stack[stack.length - 1].page;
      const it = pg.items.find(x => x.next && (x.label.toLowerCase() === parts[i].toLowerCase() || x.he === parts[i]));
      if (!it) {          // a level passed through by itself may be named too
        const here = stack[stack.length - 1].label.toLowerCase(), want = parts[i].toLowerCase();
        if (here === want || stack.some(x => x.skip && x.label.toLowerCase() === want)) continue;
        break;
      }
      if (!(await go(it.label, it.next, i < parts.length - 1))) return;
    }
    draw(stack[stack.length - 1].page);
  }
  start();
})(typeof SNAV_URL !== "undefined" ? SNAV_URL : (document.currentScript && document.currentScript.src));
