#!/usr/bin/env python3
"""
build_epub.py -- Sefaria Links EPUB builder (port of the Sefaria Links app, v54)

Builds a Kindle-ready EPUB3 from data gathered with the Sefaria MCP. The output
reproduces the app's book, with one addition: the complete original text is
always the first section, in both groupings. Otherwise it is the app's book: Contents page with collapsed/expanded section
blocks, nav.xhtml + toc.ncx (3 levels, Kindle "Go To"), section navigation
lines (contents / up / source), commentator marker rows under each source
segment, back-links under every piece, page headings for multi-page sources,
English / Hebrew / both headings, AI translations and dated user notes.

USAGE
    python3 build_epub.py OUT.epub part1.json [part2.json ...]

Every part file is a JSON object; they are deep-merged in order (lists are
concatenated, objects merged, plain values: last one wins), so data can be
written in small pieces -- meta.json, source.json, rashi.json, trans.json ...

BUNDLE KEYS (all optional except what the mode needs)
  mode           "links" (default) or "search"
  ref            the reference the book is about (title + file name)
  title          override the book title
  options        { group: "cat"|"seg", headings: "both"|"en"|"he",
                   hebrew: true, english: true, include_source: true,
                   translation_language: "English" }
  he_titles      { "Berakhot": "ברכות", "Rashi on Berakhot": "רש\"י על ברכות" }
  source         one piece or a list of pieces, in reading order:
                   { ref, heRef?, start?, he, en }     he/en: string or (nested) list
                   { segments: [ {ref, heRef?, he, en}, ... ] }
  links          [ { ref, category, anchor, he, en,
                     heRef?, commentator?, commentator_he? } ]
  commentary_sections
                 [ { ref: "Rashi on Berakhot 2a", base: "Berakhot 2a",
                     category: "Commentary", commentator?, commentator_he?,
                     he: [[...]], en: [[...]] } ]   expanded into links
  translations   { ref: "text" }  or  { ref: { text, lang } }
  notes          { "L:<linked ref>": "...", "S:<segment ref>": "...",
                   "__src__": "..." }
  search         { query, sections: [ {ref, heRef?, he, en} ] }   (mode "search")
"""

import difflib
import html
import json
import os
import re
import sys
import time
import urllib.error
import uuid
import zipfile
from datetime import datetime, timezone
from html.parser import HTMLParser
from xml.dom import minidom

# --------------------------------------------------------------------------
# Text helpers
# --------------------------------------------------------------------------
VOID = {"br", "img", "hr", "wbr", "input", "meta", "link", "col", "area", "source"}


class _Stripper(HTMLParser):
    """textContent of an HTML fragment, minus footnote markers and footnotes."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out = []
        self.skip = 0

    def _skippable(self, tag, attrs):
        if tag == "sup":
            return True
        cls = dict(attrs).get("class") or ""
        return "footnote" in cls.split() or "mam-spi-pe" in cls.split()

    def handle_starttag(self, tag, attrs):
        if tag in VOID:
            if tag == "br" and not self.skip:
                self.out.append(" ")
            return
        if self.skip:
            self.skip += 1
        elif self._skippable(tag, attrs):
            self.skip = 1

    def handle_startendtag(self, tag, attrs):
        if tag == "br" and not self.skip:
            self.out.append(" ")

    def handle_endtag(self, tag):
        if tag in VOID:
            return
        if self.skip:
            self.skip -= 1

    def handle_data(self, data):
        if not self.skip:
            self.out.append(data)


def strip_html(s):
    if not s:
        return ""
    p = _Stripper()
    p.feed(str(s))
    p.close()
    return re.sub(r"\s+", " ", "".join(p.out)).strip()


def has_english(en):
    """English counts only if it has Latin letters: some English slots hold a
    Hebrew placeholder (Steinsaltz on Berakhot 2a has "בדיקה")."""
    return bool(re.search(r"[A-Za-z]", strip_html(en)))


def as_text(v):
    """A string or nested list of strings -> one string (the app's asText)."""
    if v is None:
        return ""
    if isinstance(v, list):
        return " ".join(t for t in (as_text(x) for x in v) if t)
    return str(v)


def to_arr(v):
    if v is None:
        return []
    if isinstance(v, list):
        return [as_text(x) for x in v]
    return [as_text(v)]


def xml_esc(s):
    return (str("" if s is None else s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;").replace("'", "&#39;"))


# --------------------------------------------------------------------------
# Hebrew references
# --------------------------------------------------------------------------
_HN = [(400, "ת"), (300, "ש"), (200, "ר"), (100, "ק"), (90, "צ"), (80, "פ"),
       (70, "ע"), (60, "ס"), (50, "נ"), (40, "מ"), (30, "ל"), (20, "כ"),
       (10, "י"), (9, "ט"), (8, "ח"), (7, "ז"), (6, "ו"), (5, "ה"), (4, "ד"),
       (3, "ג"), (2, "ב"), (1, "א")]


def heb_num(n):
    """5 -> ה׳, 15 -> ט״ו (geresh / gershayim)."""
    if not n or n <= 0:
        return str(n)
    s, m = "", n
    while m > 0:
        if m == 15:
            s += "טו"
            break
        if m == 16:
            s += "טז"
            break
        for v, ch in _HN:
            if m >= v:
                s += ch
                m -= v
                break
    return s + "\u05F3" if len(s) == 1 else s[:-1] + "\u05F4" + s[-1]


def _he_addr(addr):
    def one(part):
        toks = part.split(":")
        out = []
        for i, t in enumerate(toks):
            m = re.fullmatch(r"(\d+)([ab])", t)
            if m:
                out.append(heb_num(int(m.group(1))) + " " + ("א" if m.group(2) == "a" else "ב"))
            elif re.fullmatch(r"\d+", t):
                out.append(heb_num(int(t)))
            else:
                return None
        return ":".join(out)
    parts = re.split(r"\s*[-\u2013]\s*", addr)
    conv = [one(p) for p in parts]
    if any(c is None for c in conv):
        return None
    return "\u2013".join(conv)


class HeRefs:
    def __init__(self, he_titles):
        self.t = dict(he_titles or {})
        self.keys = sorted(self.t, key=len, reverse=True)

    def ref(self, en_ref):
        """'Berakhot 2a:5' -> 'ברכות ב׳ א:ה׳' when the book's Hebrew title is known."""
        en_ref = str(en_ref or "")
        for k in self.keys:
            if en_ref == k:
                return self.t[k]
            if en_ref.startswith(k + " "):
                he = _he_addr(en_ref[len(k) + 1:].strip())
                return (self.t[k] + " " + he) if he else ""
        return ""

    def title(self, en_title):
        return self.t.get(en_title, "")


# --------------------------------------------------------------------------
# Reference helpers (ports of the app's segNum / sectionOf / commName ...)
# --------------------------------------------------------------------------
ADDR_RE = re.compile(r"\s\d+[ab]?(?::\d+)*(?:\s*[-\u2013]\s*\d+[ab]?(?::\d+)*)?$")


def seg_num(ref):
    m = re.search(r":(\d+)$", str(ref))
    return int(m.group(1)) if m else 0


def section_of(ref):
    m = re.match(r"^(.*\S):\d+(?:\s*[-\u2013]\s*\d+)?$", str(ref))
    return m.group(1) if m else ""


def index_title(ref):
    return ADDR_RE.sub("", str(ref)).strip() or str(ref)


TANAKH_BOOKS = {
    "Genesis", "Exodus", "Leviticus", "Numbers", "Deuteronomy", "Joshua", "Judges",
    "I Samuel", "II Samuel", "I Kings", "II Kings", "Isaiah", "Jeremiah", "Ezekiel",
    "Hosea", "Joel", "Amos", "Obadiah", "Jonah", "Micah", "Nahum", "Habakkuk",
    "Zephaniah", "Haggai", "Zechariah", "Malachi", "Psalms", "Proverbs", "Job",
    "Song of Songs", "Ruth", "Lamentations", "Ecclesiastes", "Esther", "Daniel",
    "Ezra", "Nehemiah", "I Chronicles", "II Chronicles",
    "Bereshit", "Shemot", "Vayikra", "Bamidbar", "Devarim"}


def comm_name_of(ref):
    """'Rashi on Genesis 1:1:1' -> Rashi; 'Siftei Chakhamim, Genesis 24:10:1' ->
    Siftei Chakhamim; 'Onkelos Genesis 24:10' -> Onkelos; otherwise the work title."""
    t = index_title(ref)
    if " on " in t:
        return t.split(" on ", 1)[0]
    if ", " in t and t.rsplit(", ", 1)[1] in TANAKH_BOOKS:
        return t.rsplit(", ", 1)[0]
    for b in TANAKH_BOOKS:
        if t.endswith(" " + b) and t[: -len(b) - 1].strip():
            return t[: -len(b) - 1].strip()
    return t


CAT_HE = {
    "commentary": "פרשנות", "tanakh": "תנ״ך", "mishnah": "משנה", "talmud": "תלמוד",
    "halakhah": "הלכה", "midrash": "מדרש", "tosefta": "תוספתא", "responsa": "שו״ת",
    "kabbalah": "קבלה", "chasidut": "חסידות", "musar": "מוסר", "liturgy": "תפילה",
    "jewish thought": "מחשבת ישראל", "targum": "תרגום", "second temple": "ספרות בית שני",
}
FIXED_ORDER = [["commentary"], ["mishnah", "mishna"], ["talmud"],
               ["halakhah", "halacha", "halakha"], ["tosefta"], ["responsa"]]


def cat_rank(c):
    lc = str(c).lower()
    for i, grp in enumerate(FIXED_ORDER):
        if lc in grp:
            return i
    return 99


COMM_ORDER = []   # default commentators in the defaults document's order


def comm_rank(name):
    """With a defaults order: its position, the rest after it. Without one:
    the app's commRank (Rashi, Tosafot, Steinsaltz lead; the rest follow)."""
    lc = str(name).lower()
    if COMM_ORDER:
        return COMM_ORDER.index(lc) if lc in COMM_ORDER else len(COMM_ORDER)
    if lc.startswith("rashi"):
        return 0
    if lc.startswith("tosafot") or lc.startswith("tosfot"):
        return 1
    if lc.startswith("steinsaltz"):
        return 2
    return 3


# --------------------------------------------------------------------------
# Bundle loading
# --------------------------------------------------------------------------
def deep_merge(a, b):
    if isinstance(a, dict) and isinstance(b, dict):
        out = dict(a)
        for k, v in b.items():
            out[k] = deep_merge(out[k], v) if k in out else v
        return out
    if isinstance(a, list) and isinstance(b, list):
        return a + b
    return b


def push_segs(out, ref, he, en):
    """Walk a (possibly nested) text down to its leaves, numbering each level."""
    ha = he if isinstance(he, list) else None
    ea = en if isinstance(en, list) else None
    if ha is None and ea is None:
        out.append({"ref": ref, "he": as_text(he), "en": as_text(en)})
        return
    n = max(len(ha or []), len(ea or []))
    for i in range(n):
        push_segs(out, ref + ":" + str(i + 1),
                  ha[i] if ha and i < len(ha) else None,
                  ea[i] if ea and i < len(ea) else None)


def source_segments(src, label_hint=""):
    """-> (segments [{ref, heRef, he, en}], label, heLabel)"""
    if not src:
        return [], "", "", []
    pieces = src if isinstance(src, list) else [src]
    segs = []
    for p in pieces:
        if "segments" in p:
            for s in p["segments"]:
                segs.append({"ref": s["ref"], "heRef": s.get("heRef", ""),
                             "he": as_text(s.get("he")), "en": as_text(s.get("en"))})
            continue
        he, en = p.get("he"), p.get("en")
        if not isinstance(he, list) and not isinstance(en, list):
            segs.append({"ref": p["ref"], "heRef": p.get("heRef", ""),
                         "he": as_text(he), "en": as_text(en)})
            continue
        start = int(p.get("start", 1))
        ha = he if isinstance(he, list) else []
        ea = en if isinstance(en, list) else []
        for i in range(max(len(ha), len(ea))):
            push_segs(segs, p["ref"] + ":" + str(start + i),
                      ha[i] if i < len(ha) else None, ea[i] if i < len(ea) else None)
    for s in segs:
        s.setdefault("heRef", "")
    label = pieces[0].get("label") or (pieces[0].get("ref") if len(pieces) == 1 else label_hint)
    if not label and segs:
        label = pieces[0].get("ref", "") + "\u2013" + (pieces[-1].get("ref") or "")
    he_label = pieces[0].get("heRef", "") if len(pieces) == 1 else ""
    return segs, label, he_label, [p.get("ref", "") for p in pieces]


def expand_comm_sections(sections):
    out = []
    for cs in sections or []:
        base, cref = cs["base"], cs["ref"]
        he, en = cs.get("he"), cs.get("en")
        ha = he if isinstance(he, list) else ([he] if he else [])
        ea = en if isinstance(en, list) else ([en] if en else [])
        for i in range(max(len(ha), len(ea))):
            leaves = []
            push_segs(leaves, cref + ":" + str(i + 1),
                      ha[i] if i < len(ha) else None, ea[i] if i < len(ea) else None)
            for lf in leaves:
                if not (strip_html(lf["he"]) or strip_html(lf["en"])):
                    continue
                link = {"ref": lf["ref"], "category": cs.get("category", "Commentary"),
                        "anchor": base + ":" + str(i + 1), "he": lf["he"], "en": lf["en"]}
                for k in ("commentator", "commentator_he"):
                    if cs.get(k):
                        link[k] = cs[k]
                out.append(link)
    return out


# --------------------------------------------------------------------------
# Content gathering (port of gatherContent)
# --------------------------------------------------------------------------
class Book:
    def __init__(self, b):
        self.b = b
        o = b.get("options", {})
        self.group = "seg" if o.get("group") == "seg" else "cat"
        h = o.get("headings", "both")
        self.hdr = h if h in ("en", "he") else "both"
        self.inc_he = o.get("hebrew", True)
        self.inc_en = o.get("english", True)
        self.inc_src = o.get("include_source", True)
        COMM_ORDER[:] = [str(x).lower() for x in (o.get("commentator_order") or [])]
        self.tlang = o.get("translation_language", "English")
        self.he = HeRefs(b.get("he_titles"))
        self.notes = {k: str(v).strip() for k, v in (b.get("notes") or {}).items() if str(v).strip()}
        self.trans = {}
        for k, v in (b.get("translations") or {}).items():
            t = v.get("text", "") if isinstance(v, dict) else str(v)
            if t.strip():
                self.trans[k] = t.strip()
        self.warnings = []

    # ---- headings in the chosen language --------------------------------
    def hdr_text(self, en, he):
        if self.hdr == "he":
            return he or en
        if self.hdr == "both" and he:
            return en + "  \u2014  " + he
        return en

    def ref_line(self, l):
        en, he = l["ref"], l.get("heRef", "")
        if self.hdr == "he":
            return he or en
        if self.hdr == "en":
            return en
        return en + ("  \u2014  " + he if he else "")


def gather(bundle):
    bk = Book(bundle)
    if not bk.inc_he and not bk.inc_en:
        raise SystemExit("options: pick Hebrew, English, or both.")
    if bundle.get("mode") == "search":
        return gather_search(bk, bundle)

    segs, src_label, src_he_label, piece_refs = source_segments(bundle.get("source"), bundle.get("ref", ""))
    src_idx = {s["ref"]: i for i, s in enumerate(segs)}
    for s in segs:
        if not s["heRef"]:
            s["heRef"] = bk.he.ref(s["ref"])
    if src_label and not src_he_label:
        if len(piece_refs) > 1:
            a, z = bk.he.ref(piece_refs[0]), bk.he.ref(piece_refs[-1])
            src_he_label = (a + "\u2013" + z) if (a and z) else ""
        else:
            src_he_label = bk.he.ref(src_label)
    have_src = bk.inc_src and any(strip_html(s["he"]) or strip_html(s["en"]) for s in segs)
    ref = bundle.get("ref") or src_label or "sefaria_links"

    # ---- links: expand, fill in, keep only those with text, dedupe by ref
    raw = list(bundle.get("links") or []) + expand_comm_sections(bundle.get("commentary_sections"))
    links, seen = [], set()
    for l in raw:
        r = l.get("ref") or l.get("sourceRef")
        lid = l.get("id") or r
        if not r or lid in seen:
            continue
        he_t, en_t = strip_html(as_text(l.get("he"))), strip_html(as_text(l.get("en") or l.get("text")))
        if not (he_t or en_t):
            bk.warnings.append("no text, left out: " + r)
            continue
        seen.add(lid)
        anchor = l.get("anchor") or ""
        if not anchor:
            if len(segs) == 1:
                anchor = segs[0]["ref"]
            else:
                m = re.match(r"^.+? on (.+:\d+):\d+$", r)
                if m and m.group(1) in src_idx:
                    anchor = m.group(1)
        if not anchor:
            anchor = "(whole page)"
            bk.warnings.append("no anchor given, filed under (whole page): " + r)
        name = l.get("commentator") or comm_name_of(r)
        name_he = l.get("commentator_he") or ""
        if not name_he:
            t = bk.he.title(index_title(r))
            name_he = t.split(" על ", 1)[0] if " on " in index_title(r) and t else t
        links.append({"i": len(links), "ref": r, "heRef": l.get("heRef") or bk.he.ref(r),
                      "cat": l.get("category") or "Other", "comm": name, "comm_he": name_he,
                      "seg": anchor, "he": he_t, "en": en_t})

    # ---- trees (as in the app's fetchLinks) ------------------------------
    tree = {}
    for l in links:
        tree.setdefault(l["cat"], {}).setdefault(l["comm"], []).append(l)
    cat_count = lambda c: sum(len(v) for v in tree[c].values())
    cat_order = sorted(tree, key=lambda c: (cat_rank(c), -cat_count(c)))
    seg_tree = {}
    for l in links:
        seg_tree.setdefault(l["seg"], {}).setdefault(l["cat"], []).append(l)
    # Sections come from the links, in the source's own order (the app's
    # SEG_ORDER): a segment nobody comments on has no section of its own.
    seg_order = sorted(seg_tree, key=lambda s: (src_idx.get(s, float("inf")), seg_num(s), s))

    def seg_cats(seg):
        st = seg_tree[seg]
        return sorted(st, key=lambda c: (cat_rank(c), -len(st[c])))

    def seg_cat_links(seg, cat):
        return sorted(seg_tree[seg][cat],
                      key=lambda l: (comm_rank(l["comm"]), l["comm"].casefold(), l["ref"].casefold()))

    def group_by_comm(ls):
        out, idx = [], {}
        for l in ls:
            if l["comm"] not in idx:
                idx[l["comm"]] = len(out)
                out.append((l["comm"], []))
            out[idx[l["comm"]]][1].append(l)
        return out

    def comm_he_of(ls):
        for l in ls:
            if l["comm_he"]:
                return l["comm_he"]
        return ""

    def incipit(seg):
        i = src_idx.get(seg)
        if i is None:
            return ""
        t = strip_html(segs[i]["he"]) or strip_html(segs[i]["en"])
        if not t:
            return ""
        out = ""
        for w in t.split(" "):
            if out and len(out) + 1 + len(w) > 30:
                break
            out += (" " if out else "") + w
            if len(out) >= 30:
                break
        return out + ("\u2026" if len(out) < len(t) else "")

    def he_seg(seg):
        i = src_idx.get(seg)
        return segs[i]["heRef"] if i is not None else bk.he.ref(seg)

    blocks, toc = [], []
    ids = [0]

    def next_id():
        ids[0] += 1
        return "sec" + str(ids[0] - 1)

    written = [0]
    notedone = set()
    note_date = datetime.now().strftime("%d/%m/%Y")

    def emit_note(key):
        nt = bk.notes.get(key)
        if nt and key not in notedone:
            notedone.add(key)
            blocks.append({"type": "note", "text": nt, "date": note_date})

    def emit_ai(key):
        t = bk.trans.get(key)
        if t:
            blocks.append({"type": "ai", "text": t})

    # Anchors for linked texts, reserved lazily: a marker row is written
    # before the pieces it points to.
    link_anchor = {}

    def anchor_for(l):
        if l["i"] not in link_anchor:
            link_anchor[l["i"]] = next_id()
        return link_anchor[l["i"]]

    seg_src_anchor = {}
    page_of = lambda s: section_of(s) or str(s or "")
    page_he = lambda s: re.sub(r":[^:]*$", "", he_seg(s) or "")
    multi_page = len({page_of(s) for s in seg_order if page_of(s)}) > 1
    last_page = [None]

    # Every commentator on one section, Rashi / Tosafot / Steinsaltz leading,
    # each jumping to its first piece (the app's rtsFor).
    def rts_for(seg):
        if seg not in seg_tree:
            return None
        seg_links = [l for c in seg_cats(seg) for l in seg_cat_links(seg, c)]
        groups = sorted(group_by_comm(seg_links), key=lambda g: comm_rank(g[0]))
        if not groups:
            return None
        return [{"name": comm_he_of(ls) or name, "target": anchor_for(ls[0])} for name, ls in groups]

    # The complete original text as one section (the app's emitWholeSource).
    # In Section mode it opens the book and the notes on single sections stay
    # with their sections further down, as they do in the app.
    def emit_whole_source(section_notes):
        label = src_he_label or src_label or ref
        a = next_id()
        toc.append({"level": 1, "text": "Source \u2014 " + label, "anchor": a})
        blocks.append({"type": "h1", "text": "Source \u2014 " + label, "anchor": a, "first": True})
        in_order = set(seg_order)
        for s in segs:
            seg = s["ref"] if s["ref"] in in_order else None
            if multi_page and seg:
                pg = page_of(seg)
                if pg and pg != last_page[0]:
                    last_page[0] = pg
                    pa = next_id()
                    pt = bk.hdr_text(pg, page_he(seg))
                    toc.append({"level": 2, "text": pt, "anchor": pa})
                    blocks.append({"type": "h2", "text": pt, "anchor": pa})
            v = None
            if seg:
                v = next_id()
                seg_src_anchor[seg] = v
            he, en = strip_html(s["he"]), strip_html(s["en"])
            if bk.inc_he and he:
                blocks.append({"type": "he", "text": he, "anchor": v})
                v = None
            if bk.inc_en and en:
                blocks.append({"type": "en", "text": en, "anchor": v})
                v = None
            if v:   # nothing printed here: keep the anchor so links still land
                blocks.append({"type": "en", "text": "", "anchor": v})
            emit_ai(s["ref"])
            if seg:
                rts = rts_for(seg)
                if rts:
                    blocks.append({"type": "rtsrow", "marks": rts})
        emit_note("__src__")
        if section_notes:
            for sg in seg_order:
                emit_note("S:" + sg)

    if not links and not have_src:
        raise SystemExit("Nothing to build: no linked texts with text, and no source text.")

    # ---------------- section mode ----------------
    if bk.group == "seg":
        if have_src:
            emit_whole_source(section_notes=False)
            last_page[0] = None   # the sections below get their own page headings
        for seg in seg_order:
            sel = [(c, seg_cat_links(seg, c)) for c in seg_cats(seg)]
            i = src_idx.get(seg) if have_src else None
            s_he = strip_html(segs[i]["he"]) if i is not None else ""
            s_en = strip_html(segs[i]["en"]) if i is not None else ""
            want_src = i is not None and (s_he or s_en)
            if not sel and not want_src:
                continue
            rts = rts_for(seg) if want_src else None
            pg = page_of(seg)
            if multi_page and pg and pg != last_page[0]:
                last_page[0] = pg
                pa = next_id()
                pt = bk.hdr_text(pg, page_he(seg))
                toc.append({"level": 1, "text": pt, "anchor": pa})
                blocks.append({"type": "h1", "text": pt, "anchor": pa, "first": not blocks})
            a = next_id()
            inc = incipit(seg)
            h1 = bk.hdr_text(seg, he_seg(seg)) + (" \u2014 " + inc if inc else "")
            toc.append({"level": 1, "text": h1, "anchor": a})
            blocks.append({"type": "h1", "text": h1, "anchor": a, "first": not blocks})
            if want_src:
                if bk.inc_he and s_he:
                    blocks.append({"type": "he", "text": s_he})
                if bk.inc_en and s_en:
                    blocks.append({"type": "en", "text": s_en})
                emit_ai(seg)
                if rts:
                    blocks.append({"type": "rtsrow", "marks": rts})
                emit_note("S:" + seg)
                emit_note("__src__")
            for cat, arr in sel:
                a2 = next_id()
                ct = bk.hdr_text(cat, CAT_HE.get(cat.lower(), ""))
                toc.append({"level": 2, "text": ct, "anchor": a2})
                blocks.append({"type": "h2", "text": ct, "anchor": a2})
                for comm, ls in group_by_comm(arr):
                    single = len(ls) == 1
                    comm_t = bk.hdr_text(comm, comm_he_of(ls))
                    h3 = (bk.ref_line(ls[0]) or comm_t) if single else comm_t
                    a3 = anchor_for(ls[0]) if single else next_id()
                    toc.append({"level": 3, "text": h3, "anchor": a3})
                    blocks.append({"type": "h3", "text": h3, "anchor": a3})
                    for l in ls:
                        if not single:
                            blocks.append({"type": "ref", "text": bk.ref_line(l), "anchor": anchor_for(l)})
                        if bk.inc_he and l["he"]:
                            blocks.append({"type": "he", "text": l["he"]})
                        if bk.inc_en and l["en"]:
                            blocks.append({"type": "en", "text": l["en"]})
                        emit_ai(l["ref"])
                        emit_note("L:" + l["ref"])
                        if want_src:
                            blocks.append({"type": "backlink", "text": seg, "anchor": a})
                        written[0] += 1
    # ---------------- category mode ----------------
    else:
        if have_src:
            emit_whole_source(section_notes=True)
        for cat in cat_order:
            comms = sorted(tree[cat], key=lambda c: ((comm_rank(c) if COMM_ORDER else 0), -len(tree[cat][c])))
            ca = next_id()
            ct = bk.hdr_text(cat, CAT_HE.get(cat.lower(), ""))
            toc.append({"level": 1, "text": ct, "anchor": ca})
            blocks.append({"type": "h1", "text": ct, "anchor": ca})
            for comm in comms:
                ls = tree[cat][comm]
                cma = next_id()
                cmt = bk.hdr_text(comm, comm_he_of(ls))
                toc.append({"level": 2, "text": cmt, "anchor": cma})
                blocks.append({"type": "h2", "text": cmt, "anchor": cma})
                for l in ls:
                    blocks.append({"type": "ref", "text": bk.ref_line(l), "anchor": anchor_for(l)})
                    if bk.inc_he and l["he"]:
                        blocks.append({"type": "he", "text": l["he"]})
                    if bk.inc_en and l["en"]:
                        blocks.append({"type": "en", "text": l["en"]})
                    emit_ai(l["ref"])
                    emit_note("L:" + l["ref"])
                    if have_src and l["seg"] in seg_src_anchor:
                        blocks.append({"type": "backlink", "text": l["seg"], "anchor": seg_src_anchor[l["seg"]]})
                    written[0] += 1

    unused = [k for k in bk.notes if k not in notedone]
    for k in unused:
        bk.warnings.append("note key matched nothing, not printed: " + k)
    return {"ref": ref, "title": bundle.get("title") or "Linked texts: " + ref,
            "blocks": blocks, "toc": toc, "have_src": have_src, "written": written[0],
            "inc_he": bk.inc_he, "inc_en": bk.inc_en, "warnings": bk.warnings}


def gather_search(bk, bundle):
    """Port of buildSelectedTexts: each chosen section's own text, no commentary."""
    srch = bundle.get("search") or {}
    q = srch.get("query") or bundle.get("ref") or "search"
    blocks, toc, n = [], [], 0
    for k, sec in enumerate(srch.get("sections") or []):
        segs, _, _, _ = source_segments(sec)
        if not any(strip_html(s["he"]) or strip_html(s["en"]) for s in segs):
            bk.warnings.append("no text, left out: " + sec.get("ref", "?"))
            continue
        a = "sec" + str(len(toc))
        h = bk.hdr_text(sec["ref"], sec.get("heRef") or bk.he.ref(sec["ref"]))
        toc.append({"level": 1, "text": h, "anchor": a})
        blocks.append({"type": "h1", "text": h, "anchor": a, "first": not blocks})
        for s in segs:
            he, en = strip_html(s["he"]), strip_html(s["en"])
            if bk.inc_he and he:
                blocks.append({"type": "he", "text": he})
            if bk.inc_en and en:
                blocks.append({"type": "en", "text": en})
        n += 1
    if not n:
        raise SystemExit("None of the search sections had any text.")
    return {"ref": q, "title": bundle.get("title") or "Sefaria search: " + q,
            "blocks": blocks, "toc": toc, "have_src": False, "written": n,
            "inc_he": bk.inc_he, "inc_en": bk.inc_en, "warnings": bk.warnings}


# --------------------------------------------------------------------------
# EPUB (port of tocTree / navOl / ncxMap / epubBody / buildEpub)
# --------------------------------------------------------------------------
def toc_tree(toc):
    tree, l1, l2 = [], None, None
    for t in toc:
        node = {"text": t["text"], "anchor": t["anchor"], "kids": []}
        if t["level"] == 1:
            tree.append(node)
            l1, l2 = node, None
        elif t["level"] == 2:
            (l1["kids"] if l1 else tree).append(node)
            l2 = node
        else:
            (l2["kids"] if l2 else (l1["kids"] if l1 else tree)).append(node)
    return tree


def nav_ol(tree):
    s = "<ol>\n"
    for n in tree:
        s += '  <li><a href="text.xhtml#%s">%s</a>' % (n["anchor"], xml_esc(n["text"]))
        if n["kids"]:
            s += "\n    " + nav_ol(n["kids"]) + "\n  "
        s += "</li>\n"
    return s + "</ol>"


def ncx_map(tree):
    po = [0]

    def walk(nodes):
        s = ""
        for n in nodes:
            po[0] += 1
            s += ('<navPoint id="np%d" playOrder="%d"><navLabel><text>%s</text></navLabel>'
                  '<content src="text.xhtml#%s"/>' % (po[0], po[0], xml_esc(n["text"]), n["anchor"]))
            if n["kids"]:
                s += walk(n["kids"])
            s += "</navPoint>\n"
        return s
    return walk(tree)


def epub_body(c):
    tree = toc_tree(c["toc"])
    node = {}

    def walk(ns):
        for n in ns:
            node[n["anchor"]] = n
            walk(n["kids"])
    walk(tree)
    menu = lambda kids: " · ".join('<a href="#%s">%s</a>' % (k["anchor"], xml_esc(k["text"])) for k in kids)

    s = '<h1 class="doctitle">%s</h1>\n' % xml_esc(c["title"])
    if tree:
        s += '<div class="contents" id="bookcontents">\n<h2>Contents</h2>\n<ol>\n'
        for sec in tree:
            kids = bool(sec["kids"])
            target = ("ctoc-" + sec["anchor"]) if kids else sec["anchor"]
            s += '<li><a href="#%s">%s%s</a></li>\n' % (target, xml_esc(sec["text"]), " ›" if kids else "")
        s += "</ol>\n</div>\n"
        for sec in tree:
            if not sec["kids"]:
                continue
            s += '<div class="ctoc" id="ctoc-%s">\n' % sec["anchor"]
            s += '<h3 class="ctoc-h"><a href="#%s">%s — read</a></h3>\n' % (sec["anchor"], xml_esc(sec["text"]))
            s += '<ol class="ctoc-cats">\n'
            for k in sec["kids"]:
                s += '<li><a href="#%s">%s</a>' % (k["anchor"], xml_esc(k["text"]))
                if k["kids"]:
                    s += '<ol class="ctoc-works">' + "".join(
                        '<li><a href="#%s">%s</a></li>' % (g["anchor"], xml_esc(g["text"])) for g in k["kids"]) + "</ol>"
                s += "</li>\n"
            s += '</ol>\n<p class="secnav"><a href="#bookcontents">⌂ contents</a></p>\n</div>\n'
    else:
        # no headings at all: keep the #bookcontents target the OPF points at
        s += '<div class="contents" id="bookcontents"></div>\n'

    cur_h1, src_anchor = None, None
    idattr = lambda b: (' id="%s"' % b["anchor"]) if b.get("anchor") else ""
    for b in c["blocks"]:
        t = b["type"]
        if t == "h1":
            cur_h1 = b["anchor"]
            if c["have_src"] and src_anchor is None and b["text"].startswith("Source \u2014 "):
                src_anchor = b["anchor"]
            s += '<h1 id="%s">%s</h1>\n' % (b["anchor"], xml_esc(b["text"]))
            kids = node.get(b["anchor"], {}).get("kids", [])
            s += '<p class="secnav"><a href="#bookcontents">⌂ contents</a>%s</p>\n' % (" · " + menu(kids) if kids else "")
        elif t == "h2":
            s += '<h2 id="%s">%s</h2>\n' % (b["anchor"], xml_esc(b["text"]))
            kids = node.get(b["anchor"], {}).get("kids", [])
            up = ('<a href="#%s">↑ section</a>' % cur_h1) if cur_h1 else '<a href="#bookcontents">⌂ contents</a>'
            src_link = (' \u00b7 <a href="#%s">\u25c4 source</a>' % src_anchor) if (src_anchor and b["anchor"] != src_anchor) else ""
            if kids or cur_h1 or src_link:
                s += '<p class="secnav">%s%s%s</p>\n' % (up, " · " + menu(kids) if kids else "", src_link)
        elif t == "h3":
            s += '<h3 id="%s">%s</h3>\n' % (b["anchor"], xml_esc(b["text"]))
        elif t == "ref":
            s += '<p class="ref"%s>%s</p>\n' % (idattr(b), xml_esc(b["text"]))
        elif t == "note":
            s += '<p class="usernote">%s</p>\n<p class="usernote-date">%s</p>\n' % (xml_esc(b["text"]), xml_esc(b.get("date", "")))
        elif t == "ai":
            s += '<p class="ai">%s</p>\n' % xml_esc(b["text"])
        elif t == "he":
            s += '<p class="he"%s dir="rtl" lang="he" xml:lang="he">%s</p>\n' % (idattr(b), xml_esc(b["text"]))
        elif t == "en":
            s += '<p class="en"%s>%s</p>\n' % (idattr(b), xml_esc(b["text"]))
        elif t == "rtsrow":
            s += '<p class="rtsrow" dir="rtl">' + " \u00b7 ".join(
                '<a class="rts" href="#%s">%s</a>' % (m["target"], xml_esc(m["name"])) for m in b["marks"]) + "</p>\n"
        elif t == "backlink":
            s += '<p class="backlink"><a href="#%s">\u25c4 %s</a></p>\n' % (b["anchor"], xml_esc(b["text"]))
    return s


CSS = """body { font-family: serif; line-height: 1.5; margin: 1em; }
h1 { font-size: 1.4em; margin: 1em 0 .4em; }
h2 { font-size: 1.15em; color: #6b5329; margin: .8em 0 .3em; }
h3 { font-size: 1.02em; color: #8a6d3b; margin: .6em 0 .2em; }
h1.doctitle { text-align: center; font-size: 1.6em; }
p.ref { font-size: .8em; color: #6f675a; font-weight: bold; margin: .6em 0 .1em; }
p.usernote { font-style: italic; color: #6b5329; margin: .4em 0 .2em; }
p.usernote-date { font-style: italic; color: #6b5329; font-size: .8em; margin: 0 0 .6em; }
p.ai-label { font-style: italic; font-size: .85em; color: #8a6d3b; margin: .5em 0 .1em; }
p.ai { line-height: 1.6; margin: 0 0 .6em; }
p.he { direction: rtl; text-align: right; font-size: 1.15em; line-height: 1.9; }
p.en { line-height: 1.6; }
div.contents ol { list-style: none; padding-left: 0; }
div.contents li { margin: .5em 0; }
div.ctoc { margin: 1.1em 0 1.4em; }
div.ctoc h3.ctoc-h { margin: .2em 0 .3em; font-size: 1.05em; }
ol.ctoc-cats { list-style: none; padding-left: 1em; margin: .2em 0; }
ol.ctoc-cats li { margin: .3em 0; }
ol.ctoc-works { list-style: none; padding-left: 1.2em; font-size: .9em; margin: .15em 0; }
ol.ctoc-works li { margin: .2em 0; }
div.ctoc a { text-decoration: none; color: inherit; }
ol.ctoc-works a { color: #6b5329; }
p.secnav { font-size: .8em; color: #6f675a; margin: 0 0 .6em; }
p.secnav a { color: #6b5329; text-decoration: none; }
nav ol { list-style: none; }
p.rtsrow { font-size: .7em; color: #6f675a; margin: .1em 0 .7em; }
a.rts { color: #6b5329; text-decoration: none; }
p.backlink { font-size: .7em; margin: .2em 0 .8em; }
p.backlink a { color: #6b5329; text-decoration: none; }
"""


def fname_for(ref):
    return re.sub(r"^_|_$", "", re.sub(r"[^A-Za-z0-9_\u0590-\u05FF]+", "_", ref)) or "sefaria_links"


def build_epub(c, out_path):
    bid = "urn:uuid:" + str(uuid.uuid4())
    title = c["title"]
    tree = toc_tree(c["toc"])
    modified = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    lang = "en" if c["inc_en"] else "he"

    container = """<?xml version="1.0" encoding="UTF-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>
"""
    opf = """<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="bookid" xml:lang="%(lang)s">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="bookid">%(id)s</dc:identifier>
    <dc:title>%(title)s</dc:title>
    <dc:language>%(lang)s</dc:language>
    <dc:creator>Sefaria</dc:creator>
    <meta property="dcterms:modified">%(mod)s</meta>
  </metadata>
  <manifest>
    <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
    <item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>
    <item id="css" href="style.css" media-type="text/css"/>
    <item id="text" href="text.xhtml" media-type="application/xhtml+xml"/>
  </manifest>
  <spine toc="ncx">
    <itemref idref="text"/>
  </spine>
  <guide>
    <reference type="toc" title="Contents" href="text.xhtml#bookcontents"/>
    <reference type="text" title="Start" href="text.xhtml#bookcontents"/>
  </guide>
</package>
""" % {"lang": lang, "id": xml_esc(bid), "title": xml_esc(title), "mod": modified}

    nav = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" lang="en">
<head><meta charset="utf-8"/><title>Contents</title><link rel="stylesheet" type="text/css" href="style.css"/></head>
<body>
<nav epub:type="toc" id="toc">
<h1>Contents</h1>
%s
</nav>
<nav epub:type="landmarks" hidden="hidden">
<ol>
<li><a epub:type="toc" href="text.xhtml#bookcontents">Table of Contents</a></li>
<li><a epub:type="bodymatter" href="text.xhtml#bookcontents">Start</a></li>
</ol>
</nav>
</body>
</html>
""" % (nav_ol(tree) if tree else '<ol><li><a href="text.xhtml#bookcontents">Start</a></li></ol>')

    ncx = """<?xml version="1.0" encoding="UTF-8"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1" xml:lang="en">
<head>
<meta name="dtb:uid" content="%s"/>
<meta name="dtb:depth" content="3"/>
<meta name="dtb:totalPageCount" content="0"/>
<meta name="dtb:maxPageNumber" content="0"/>
</head>
<docTitle><text>%s</text></docTitle>
<navMap>
%s</navMap>
</ncx>
""" % (xml_esc(bid), xml_esc(title),
       ncx_map(tree) if tree else '<navPoint id="np1" playOrder="1"><navLabel><text>Start</text></navLabel><content src="text.xhtml#bookcontents"/></navPoint>\n')

    text = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" lang="en">
<head><meta charset="utf-8"/><title>%s</title><link rel="stylesheet" type="text/css" href="style.css"/></head>
<body>
%s</body>
</html>
""" % (xml_esc(title), epub_body(c))

    files = {"META-INF/container.xml": container, "OEBPS/content.opf": opf,
             "OEBPS/nav.xhtml": nav, "OEBPS/toc.ncx": ncx,
             "OEBPS/style.css": CSS, "OEBPS/text.xhtml": text}
    with zipfile.ZipFile(out_path, "w") as z:
        z.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        for name, data in files.items():
            z.writestr(name, data, compress_type=zipfile.ZIP_DEFLATED)
    return files


# --------------------------------------------------------------------------
# Self-check: well-formed XML, every internal link lands on an id
# --------------------------------------------------------------------------
def verify(files):
    problems = []
    for name in ("META-INF/container.xml", "OEBPS/content.opf", "OEBPS/nav.xhtml",
                 "OEBPS/toc.ncx", "OEBPS/text.xhtml"):
        try:
            minidom.parseString(files[name].encode("utf-8"))
        except Exception as e:
            problems.append("%s is not well-formed XML: %s" % (name, e))
    ids = set(re.findall(r'\sid="([^"]+)"', files["OEBPS/text.xhtml"]))
    targets = set(re.findall(r'href="#([^"]+)"', files["OEBPS/text.xhtml"]))
    for f in ("OEBPS/nav.xhtml", "OEBPS/toc.ncx", "OEBPS/content.opf"):
        targets |= set(re.findall(r'text\.xhtml#([^"]+)"', files[f]))
    missing = sorted(targets - ids)
    if missing:
        problems.append("links with no target: " + ", ".join(missing[:10]))
    dup = [i for i in ids if files["OEBPS/text.xhtml"].count(' id="%s"' % i) > 1]
    if dup:
        problems.append("duplicate ids: " + ", ".join(dup[:10]))
    return problems



# ==========================================================================
# Direct fetching from Sefaria (port of the app's fetchLinks pipeline)
# ==========================================================================
# Everything below fetches from Sefaria's own REST API, the way the app does,
# and writes the result to the work folder -- so texts never pass through the
# conversation. Each function names the app function it ports.
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

API = os.environ.get("SEFARIA_API", "https://www.sefaria.org").rstrip("/")
TEXTS_API = API + "/api/texts/"
LINKS_API = API + "/api/links/"
SHAPE_API = API + "/api/shape/"
V2_INDEX_API = API + "/api/v2/index/"
SEARCH_API = API + "/api/search-wrapper/es8"
TEXT_Q = "?context=0&commentary=0&pad=0"


def enc(s):
    """JavaScript's encodeURIComponent."""
    return urllib.parse.quote(str(s), safe="-_.!~*'()")


def http_json(url, body=None):
    """GET (or POST a JSON body) and parse; None on any failure, as the app's
    try/catch blocks treat it. Retries twice on network errors and 429/5xx."""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    for attempt in range(3):
        req = urllib.request.Request(url, data=data, headers={
            "User-Agent": "sefaria-kindle-builder",
            "Content-Type": "text/plain;charset=UTF-8"})
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < 2:
                time.sleep(1.5 * (attempt + 1))
                continue
            return None
        except Exception:
            if attempt < 2:
                time.sleep(1.5 * (attempt + 1))
                continue
            return None
    return None


def texts_json(ref):
    j = http_json(TEXTS_API + enc(ref) + TEXT_Q)
    return None if (not isinstance(j, dict) or j.get("error")) else j


def raw_text(v):
    """The app's asText: a string, or a list joined with spaces (unstripped)."""
    if not v:
        return ""
    if isinstance(v, list):
        return " ".join(raw_text(x) for x in v)
    return str(v)


def to_arr_raw(v):
    """The app's toArr: one entry per segment, a nested level joined."""
    if not v:
        return []
    if isinstance(v, list):
        return [" ".join(raw_text(y) for y in x) if isinstance(x, list) else raw_text(x) for x in v]
    return [raw_text(v)]


def status(msg):
    print(msg, file=sys.stderr, flush=True)


def is_ranged_ref(ref):                         # isRangedRef
    return bool(re.search(r":\d+\s*[-\u2013]\s*\d", str(ref)))


def links_for_segs(segs):                       # linksForSegs (pool of 6)
    def one(sref):
        d = http_json(LINKS_API + enc(sref) + "?with_text=1")
        if not isinstance(d, list):
            return []
        for l in d:
            l["_seg"] = sref
        return d
    out, done = [], [0]
    with ThreadPoolExecutor(max_workers=6) as ex:
        for chunk in ex.map(one, segs):
            out.extend(chunk)
            done[0] += 1
            if done[0] % 10 == 0 or done[0] == len(segs):
                status("Fetching links... %d / %d segments" % (done[0], len(segs)))
    return out


_SHAPE_COUNTS = {}


def shape_sec_counts(base):                     # shapeSecCounts
    if base in _SHAPE_COUNTS:
        return _SHAPE_COUNTS[base]
    out = None
    j = http_json(SHAPE_API + enc(base))
    first = j[0] if isinstance(j, list) and j else j
    if isinstance(first, dict) and isinstance(first.get("chapters"), list):
        out = [len(c) if isinstance(c, list) else (c if isinstance(c, int) else 0) for c in first["chapters"]]
    _SHAPE_COUNTS[base] = out
    return out


def shape_count_for(ref):                       # shapeCountFor
    m = re.match(r"^(.*\S)\s+(\d+)$", str(ref).strip())
    if not m:
        return None
    counts = shape_sec_counts(m.group(1))
    if not counts:
        return None
    i = int(m.group(2)) - 1
    return (counts[i] or None) if 0 <= i < len(counts) else None


def segs_of_ref(ref):                           # segsOfRef
    t = str(ref).strip()
    j = texts_json(t)
    he_ref = str(j.get("heRef") or "") if j else ""
    he, en = (j.get("he"), j.get("text")) if j else (None, None)
    arr_he, arr_en = isinstance(he, list), isinstance(en, list)
    if j and not arr_he and not arr_en and (raw_text(he) or raw_text(en)):
        return {"heRef": he_ref, "segs": [{"ref": t, "he": raw_text(he), "en": raw_text(en)}], "leaf": True}
    out = []
    if arr_he or arr_en:
        push_segs(out, t, he if arr_he else None, en if arr_en else None)
    nested = (arr_he and he and isinstance(he[0], list)) or (arr_en and en and isinstance(en[0], list))
    if not nested:
        n_shape = shape_count_for(t)
        if n_shape and n_shape > len(out):
            for i in range(len(out), n_shape):
                out.append({"ref": t + ":" + str(i + 1), "he": "", "en": ""})
    return {"heRef": he_ref, "segs": out, "leaf": False}


def fetch_section_data(ref):                    # fetchSectionData
    if not re.search(r"\s\d+[ab]?(:\d+)*$", str(ref).strip()):
        return None
    status("Reading " + ref + "...")
    exp = segs_of_ref(ref)
    if exp["leaf"] or not exp["segs"]:
        return None
    links = links_for_segs([s["ref"] for s in exp["segs"]])
    src = {"ref": ref, "heRef": exp["heRef"],
           "he": [s["he"] for s in exp["segs"]], "text": [s["en"] for s in exp["segs"]]}
    if not all(s["ref"] == ref + ":" + str(i + 1) for i, s in enumerate(exp["segs"])):
        src["segMap"] = {s["ref"]: i for i, s in enumerate(exp["segs"])}
    return {"links": links, "src": src}


def expand_ranged_ref(ref):                     # expandRangedRef
    t = str(ref).strip()
    m = re.match(r"^(.+?):(\d+)\s*[-\u2013]\s*(\d+)$", t)
    if m and int(m.group(3)) >= int(m.group(2)):
        base, start = m.group(1), int(m.group(2))
        j = texts_json(t)
        if not j:
            return None
        ha = j.get("he") if isinstance(j.get("he"), list) else []
        ea = j.get("text") if isinstance(j.get("text"), list) else []
        n = max(len(ha), len(ea))
        if not n:
            return None
        out = []
        for i in range(n):
            push_segs(out, base + ":" + str(start + i), ha[i] if i < len(ha) else None, ea[i] if i < len(ea) else None)
        return {"ref": j.get("ref") or t, "heRef": j.get("heRef") or "", "segs": out}
    m = re.match(r"^(.+?)\s(\d+):(\d+)\s*[-\u2013]\s*(\d+):(\d+)$", t)
    if not m:
        return None
    book, c1, v1, c2, v2 = m.group(1), int(m.group(2)), int(m.group(3)), int(m.group(4)), int(m.group(5))
    if c2 < c1 or (c2 == c1 and v2 < v1):
        return None
    ref_name, he_ref = t, ""
    meta = texts_json(t)
    if meta:
        ref_name, he_ref = meta.get("ref") or t, meta.get("heRef") or ""
    out = []
    for c in range(c1, c2 + 1):
        j = texts_json(book + " " + str(c))
        if not j:
            return None
        ha = j.get("he") if isinstance(j.get("he"), list) else []
        ea = j.get("text") if isinstance(j.get("text"), list) else []
        n = max(len(ha), len(ea))
        if not n:
            return None
        frm = v1 if c == c1 else 1
        to = min(v2, n) if c == c2 else n
        if frm > to:
            return None
        for v in range(frm, to + 1):
            push_segs(out, book + " " + str(c) + ":" + str(v),
                      ha[v - 1] if v - 1 < len(ha) else None, ea[v - 1] if v - 1 < len(ea) else None)
    if not out:
        return None
    return {"ref": ref_name, "heRef": he_ref, "segs": out}


def fetch_range_data(ref):                      # fetchRangeData
    status("Reading " + ref + "...")
    exp = expand_ranged_ref(ref)
    if not exp:
        return None
    links = links_for_segs([s["ref"] for s in exp["segs"]])
    return {"links": links, "src": {
        "ref": exp["ref"], "heRef": exp["heRef"],
        "he": [s["he"] for s in exp["segs"]], "text": [s["en"] for s in exp["segs"]],
        "segMap": {s["ref"]: i for i, s in enumerate(exp["segs"])}}}


def daf_range_refs(ref):                        # dafRangeRefs
    m = re.match(r"^(.+?)\s(\d+)([ab])\s*[-\u2013]\s*(\d+)([ab])$", str(ref).strip())
    if not m:
        return None
    frm = int(m.group(2)) * 2 + (1 if m.group(3) == "b" else 0)
    to = int(m.group(4)) * 2 + (1 if m.group(5) == "b" else 0)
    if to < frm or to - frm > 400:
        return None
    return [m.group(1) + " " + str(s // 2) + ("a" if s % 2 == 0 else "b") for s in range(frm, to + 1)]


def section_range_refs(ref):                    # sectionRangeRefs
    m = re.match(r"^(.+?)\s(\d+)\s*[-\u2013]\s*(\d+)$", str(ref).strip())
    if not m:
        return None
    frm, to = int(m.group(2)), int(m.group(3))
    if to < frm or to - frm > 200:
        return None
    return [m.group(1) + " " + str(n) for n in range(frm, to + 1)]


def expand_page_ref(pref):                      # expandPageRef
    t = str(pref).strip()
    m = re.match(r"^(.+?):(\d+)\s*[-\u2013]\s*(\d+)$", t)
    if m and int(m.group(3)) >= int(m.group(2)):
        j = texts_json(t)
        ha = j.get("he") if j and isinstance(j.get("he"), list) else []
        ea = j.get("text") if j and isinstance(j.get("text"), list) else []
        he_ref = str(j.get("heRef")).split(":")[0] if j and j.get("heRef") else ""
        base, start = m.group(1), int(m.group(2))
        n = max(len(ha), len(ea)) or (int(m.group(3)) - start + 1)
        out = []
        for i in range(n):
            push_segs(out, base + ":" + str(start + i), ha[i] if i < len(ha) else None, ea[i] if i < len(ea) else None)
        return {"heRef": he_ref, "segs": out}
    exp = segs_of_ref(t)
    return {"heRef": str(exp["heRef"]).split(":")[0] if exp["heRef"] else "", "segs": exp["segs"]}


def fetch_pages_data(pages, label):             # fetchPagesData (pool of 4)
    status("Reading " + label + "...")
    with ThreadPoolExecutor(max_workers=4) as ex:
        per = list(ex.map(expand_page_ref, pages))
    segs = [s for p in per if p for s in p["segs"]]
    if not segs:
        return None
    links = links_for_segs([s["ref"] for s in segs])
    first_he = (per[0] or {}).get("heRef") or ""
    last_he = (per[-1] or {}).get("heRef") or ""
    he_ref = (first_he if first_he == last_he else first_he + "\u2013" + last_he) if (first_he and last_he) else (first_he or last_he)
    return {"links": links, "src": {
        "ref": label, "heRef": he_ref,
        "he": [s["he"] for s in segs], "text": [s["en"] for s in segs],
        "segMap": {s["ref"]: i for i, s in enumerate(segs)}}}


def chapter_pages(title, n):
    """A tractate chapter as the app's Browse takes it: Sefaria's own chapter
    node (exact page refs, partial end pages included) and its label."""
    j = http_json(V2_INDEX_API + enc(title))
    alts = (j or {}).get("alt_structs") or (j or {}).get("alts") or {}
    structs = [(k, v.get("nodes")) for k, v in alts.items() if isinstance(v, dict) and v.get("nodes")]
    if not structs:
        return None
    name, nodes = next((s for s in structs if re.search("chapter", s[0], re.I)), structs[0])
    if not (1 <= n <= len(nodes)) or not nodes[n - 1].get("refs"):
        return None
    refs = nodes[n - 1]["refs"]
    daf = lambda r: (re.search(r"\s(\d+[ab])(?::|\s*-|$)", str(r)) or [None, None])[1]
    first, last = daf(refs[0]), daf(refs[-1])
    label = title + " " + first + ("-" + last if last and last != first else "") if first else title
    return {"label": label, "refs": refs}


def comm_name_api(l):                           # commName / commNameHe / catName
    ct = l.get("collectiveTitle") or {}
    return ct.get("en") or l.get("index_title") or l.get("category") or "Other"


def seg_ref_of(l):                              # segRefOf
    if l.get("_seg"):
        return l["_seg"]
    exp = l.get("anchorRefExpanded")
    if isinstance(exp, list) and exp:
        return exp[0]
    return l.get("anchorRef") or ""


def fetch_book_data(ref, pages=None):
    """The app's fetchLinks, start to finish: returns the source and every
    linked text that has text, in the shape the builder takes."""
    pages = pages or daf_range_refs(ref) or section_range_refs(ref)
    pre_src, data = None, None
    if pages:
        pd = fetch_pages_data(pages, ref)
        if not pd:
            raise SystemExit("No text came back for those pages.")
        data, pre_src = pd["links"], pd["src"]
    elif is_ranged_ref(ref):
        rd = fetch_range_data(ref)
        if rd:
            data, pre_src = rd["links"], rd["src"]
        else:
            data = http_json(LINKS_API + enc(ref) + "?with_text=1")
    else:
        sd = fetch_section_data(ref)
        if sd:
            data, pre_src = sd["links"], sd["src"]
        else:
            data = http_json(LINKS_API + enc(ref) + "?with_text=1")
            if isinstance(data, list) and re.search(r":\d+$", ref):
                for l in data:
                    l["_seg"] = ref
    if not isinstance(data, list):
        data = []

    seen, links = set(), []
    for l in data:
        if not (raw_text(l.get("he")) or raw_text(l.get("text"))):
            continue
        lid = l.get("_id") or (comm_name_api(l) + "|" + (l.get("sourceRef") or l.get("ref") or "") + "|" + (l.get("anchorRef") or ""))
        if lid in seen:
            continue
        seen.add(lid)
        links.append(l)

    if pre_src:
        src = pre_src
    else:
        t = texts_json(ref)
        src = {"ref": t.get("ref") or ref, "heRef": t.get("heRef") or "",
               "he": to_arr_raw(t.get("he")), "text": to_arr_raw(t.get("text"))} if t else None

    meta = texts_json(pages[0] if pages else ref) or {}
    return to_bundle(ref, src, links, meta)


def to_bundle(ref, src, links, meta):
    """Source segments named and Hebrew-labelled exactly as the app does
    (srcSegRefs / heSegRef), and links in the builder's shape."""
    out = {"ref": ref, "meta": {"categories": meta.get("categories") or [],
                                "book": meta.get("book") or meta.get("indexTitle") or ""}}
    seg_order = sorted({seg_ref_of(l) or "(whole page)" for l in links})
    if src and (src["he"] or src["text"]):
        n = max(len(src["he"]), len(src["text"]))
        names = [""] * n
        if src.get("segMap"):
            for k, i in src["segMap"].items():
                if 0 <= i < n:
                    names[i] = k
        else:
            for i in range(n):
                names[i] = src["ref"] if n == 1 else src["ref"] + ":" + str(i + 1)
            for seg in sorted(seg_order, key=lambda x: (seg_num(x), x)):
                i = 0 if (len(seg_order) == 1 and n == 1) else seg_num(seg) - 1
                if 0 <= i < n:
                    names[i] = seg
        def he_seg(seg):
            if not src.get("heRef"):
                return ""
            if seg == src["ref"]:
                return src["heRef"]
            if not src.get("segMap") and seg.startswith(src["ref"] + ":") and ":" not in src["heRef"]:
                k = seg_num(seg)
                return src["heRef"] + ":" + heb_num(k) if k > 0 else ""
            return ""
        out["source"] = {"ref": src["ref"], "heRef": src.get("heRef") or "", "segments": [
            {"ref": names[i] or ("(segment %d)" % (i + 1)), "heRef": he_seg(names[i]) if names[i] else "",
             "he": src["he"][i] if i < len(src["he"]) else "",
             "en": src["text"][i] if i < len(src["text"]) else ""} for i in range(n)]}
    out["links"] = [{
        "id": l.get("_id") or "",
        "ref": l.get("sourceRef") or l.get("ref") or "",
        "heRef": l.get("sourceHeRef") or "",
        "category": l.get("category") or "Other",
        "commentator": comm_name_api(l),
        "commentator_he": (l.get("collectiveTitle") or {}).get("he") or "",
        "index_title": l.get("index_title") or "",
        "anchor": seg_ref_of(l) or "(whole page)",
        "he": raw_text(l.get("he")), "en": raw_text(l.get("text"))} for l in links]
    return out


# ==========================================================================
# Defaults, selection, translation, search -- the work-folder commands
# ==========================================================================
DEFAULTS_NAMES = ["sefaria_default_commentators.md"]


def find_defaults_doc(path=None):
    cands = [path] if path else []
    for d in ("/mnt/project", "/mnt/user-data/uploads", "/home/claude", "."):
        cands += [os.path.join(d, n) for n in DEFAULTS_NAMES]
    for c in cands:
        if c and os.path.isfile(c):
            return c
    return None


def load_defaults(path):
    """Tables of the defaults document: heading -> [(name, Sefaria-name pattern)]."""
    out, cur = {}, None
    if not path:
        return out
    for line in open(path, encoding="utf-8"):
        h = re.match(r"^#{2,3}\s+(.*\S)", line)
        if h:
            cur = h.group(1)
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")] if line.strip().startswith("|") else []
        if cur and len(cells) >= 2 and cells[0] not in ("Commentator",) and not set(cells[0]) <= set("-: "):
            out.setdefault(cur, []).append((cells[0], cells[1]))
    return out


def defaults_section(defaults, categories):
    cats = [str(c).lower() for c in categories or []]
    want = None
    if cats[:1] == ["talmud"] and "bavli" in cats:
        want = "talmud bavli"
    elif cats[:1] == ["mishnah"]:
        want = "mishnah"
    elif cats[:1] == ["tanakh"] and len(cats) > 1:
        want = {"torah": "torah", "prophets": "prophets", "writings": "writings"}.get(cats[1])
    if not want:
        return None, []
    for k, v in defaults.items():
        if k.lower().startswith(want):
            return k, v
    return None, []


def default_match(link, patterns, book):
    """Index of the default this link is, or None. Exact work title, and only
    for Commentary / Targum links on this book."""
    cat = str(link.get("category") or "")
    if cat not in ("Commentary", "Targum"):
        return None
    title = link.get("index_title") or index_title(link["ref"])
    short = book[len("Mishnah "):] if book.startswith("Mishnah ") else book
    for i, (name, pat) in enumerate(patterns):
        if "varies" in pat.lower():
            if cat == "Targum":
                return i
            continue
        rx = "^" + re.escape(pat).replace(re.escape("<Tractate>"), "(.+)").replace(re.escape("<Book>"), "(.+)") + "$"
        m = re.match(rx, title)
        if m and (not m.groups() or m.group(1) in (book, short)):
            return i
    return None


def wd_path(wd, name):
    return os.path.join(wd, name)


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)


def inventory(wd, defaults_path=None):
    """Groups (category, commentator), defaults first in the document's order,
    then the rest by category order and size; numbered for `select`."""
    fb = load_json(wd_path(wd, "fetched.json"), None)
    if not fb:
        raise SystemExit("Nothing fetched yet: run `fetch` first.")
    book = fb["meta"].get("book") or ""
    sec_name, patterns = defaults_section(load_defaults(find_defaults_doc(defaults_path)), fb["meta"].get("categories"))
    groups = {}
    for l in fb["links"]:
        key = l["category"] + "|" + l["commentator"]
        g = groups.setdefault(key, {"key": key, "cat": l["category"], "comm": l["commentator"],
                                    "he": l["commentator_he"], "n": 0, "chars": 0, "noen": 0, "def": None})
        g["n"] += 1
        g["chars"] += len(strip_html(l["he"]))
        if strip_html(l["he"]) and not has_english(l["en"]):
            g["noen"] += 1
        d = default_match(l, patterns, book)
        if d is not None and (g["def"] is None or d < g["def"]):
            g["def"] = d
    cat_n = {}
    for g in groups.values():
        cat_n[g["cat"]] = cat_n.get(g["cat"], 0) + g["n"]
    ordered = sorted(groups.values(), key=lambda g: (
        0 if g["def"] is not None else 1, g["def"] if g["def"] is not None else 0,
        cat_rank(g["cat"]), -cat_n[g["cat"]], g["cat"], -g["n"], g["comm"]))
    for i, g in enumerate(ordered, 1):
        g["num"] = i
    absent = [name for i, (name, _) in enumerate(patterns) if not any(g["def"] == i for g in groups.values())]
    return fb, ordered, sec_name, absent


def selection(wd, groups):
    sel = load_json(wd_path(wd, "selection.json"), None)
    if sel is None:
        sel = {"groups": [g["key"] for g in groups if g["def"] is not None]}
        save_json(wd_path(wd, "selection.json"), sel)
    return sel


def print_inventory(wd, defaults_path=None):
    fb, groups, sec_name, absent = inventory(wd, defaults_path)
    chosen = set(selection(wd, groups)["groups"])
    src = fb.get("source") or {}
    segs = src.get("segments") or []
    print("Source: %s%s - %d segments" % (src.get("ref") or fb["ref"],
          (" (" + src["heRef"] + ")") if src.get("heRef") else "", len(segs)) if segs else "Source: none returned")
    print("Defaults: " + (sec_name or "none for this type of text"))
    cur = None
    for g in groups:
        head = "Defaults" if g["def"] is not None else g["cat"]
        if head != cur:
            cur = head
            print("-- " + head)
        print(" [%s] %2d  %s%s  (%s, %d piece%s, %d without English)" % (
            "x" if g["key"] in chosen else " ", g["num"], g["comm"],
            (" / " + g["he"]) if g["he"] and g["he"] != g["comm"] else "", g["cat"], g["n"],
            "" if g["n"] == 1 else "s", g["noen"]))
    if absent:
        print("Defaults with nothing here: " + ", ".join(absent))


def selected_links(fb, sel):
    keys = set(sel.get("groups") or [])
    out = [l for l in fb["links"] if (l["category"] + "|" + l["commentator"]) in keys]
    return out


def load_translations(wd, store=None):
    t = {}
    for path in ([store] if store else []) + [wd_path(wd, "translations.json")]:
        for k, v in load_json(path, {}).items():
            if isinstance(v, dict) and str(v.get("text", "")).strip():
                t[k] = {"text": v["text"], "lang": v.get("lang", "")}
    return t


def trans_done(t, ref, lang):
    v = t.get(ref)
    if not v:
        return False
    had = str(v.get("lang") or "").strip().lower()
    return not had or had == lang.strip().lower()


def trans_groups(wd, lang, store=None, include_english=False, defaults_path=None):
    """What needs translating: the source first, section by section, then one
    group per commentator -- defaults in the document's order, then the rest."""
    fb, groups, _, _ = inventory(wd, defaults_path)
    sel = selection(wd, groups)
    opts = load_json(wd_path(wd, "options.json"), {})
    t = load_translations(wd, store)
    english = lang.strip().lower() in ("english", "en")
    need = lambda he, en, ref: strip_html(he) and not trans_done(t, ref, lang) and not (english and has_english(en) and not include_english)
    out = []
    if opts.get("include_source", True) and fb.get("source"):
        items = [(s["ref"], strip_html(s["he"])) for s in fb["source"]["segments"] if need(s["he"], s["en"], s["ref"])]
        if items:
            out.append({"name": "Source - " + fb["source"]["ref"], "items": items})
    chosen = set(sel.get("groups") or [])
    for g in groups:
        if g["key"] not in chosen:
            continue
        items = [(l["ref"], strip_html(l["he"])) for l in fb["links"]
                 if l["category"] + "|" + l["commentator"] == g["key"] and need(l["he"], l["en"], l["ref"])]
        if items:
            out.append({"name": g["comm"] + " (" + g["cat"] + ")", "items": items})
    return out


def parts_of(items, max_chars):
    parts, cur, n = [], [], 0
    for it in items:
        if cur and n + len(it[1]) > max_chars:
            parts.append(cur)
            cur, n = [], 0
        cur.append(it)
        n += len(it[1])
    if cur:
        parts.append(cur)
    return parts


def header_of(ref):
    return "@@" + str(ref).strip().replace(" ", "_") + "@@"


def norm_slug(s):
    return re.sub(r"[^a-z0-9:]", "", str(s or "").lower())


def trans_add(wd, path, lang):
    """Read a reply in the @@ref@@ block format and store each translation."""
    fb = load_json(wd_path(wd, "fetched.json"), {})
    known = {}
    for s in (fb.get("source") or {}).get("segments", []):
        known[norm_slug(header_of(s["ref"]))] = s["ref"]
    for l in fb.get("links", []):
        known[norm_slug(header_of(l["ref"]))] = l["ref"]
    text = open(path, encoding="utf-8").read().replace("\r\n", "\n")
    blocks, cur = [], None
    for line in text.split("\n"):
        m = re.match(r"^\s*[#>*`\s]*@{1,3}\s*(.+?)\s*@{1,3}[\s.,:;!?]*$", line)
        if m:
            if cur:
                blocks.append(cur)
            cur = [m.group(1), []]
        elif cur:
            cur[1].append(line)
    if cur:
        blocks.append(cur)
    tpath = wd_path(wd, "translations.json")
    t = load_json(tpath, {})
    applied, unknown, skipped = 0, [], 0
    for head, body in blocks:
        if norm_slug(head) == "done":
            continue
        ref = known.get(norm_slug("@@" + head + "@@"))
        b = "\n".join(body).strip()
        if not ref:
            unknown.append(head)
            continue
        if not b or re.fullmatch(r"\[unable to translate\]", b, re.I):
            skipped += 1
            continue
        letters = re.findall(r"[A-Za-z\u0590-\u05FF]", b)
        if len(letters) >= 10 and sum(1 for c in letters if "\u0590" <= c <= "\u05FF") / len(letters) > 0.6 \
                and "hebrew" not in lang.lower():
            skipped += 1   # still Hebrew: an echo, not a translation
            continue
        t[ref] = {"text": b, "lang": lang}
        applied += 1
    save_json(tpath, t)
    print("Stored %d translation%s into %s." % (applied, "" if applied == 1 else "s", lang))
    if skipped:
        print("Skipped %d empty, untranslatable or still-Hebrew block(s)." % skipped)
    if unknown:
        print("Headers that matched nothing: " + ", ".join(unknown[:10]))


def bundle_from_workdir(wd, store=None):
    fb = load_json(wd_path(wd, "fetched.json"), None)
    if fb is None:
        raise SystemExit("Nothing fetched yet: run `fetch` first.")
    if fb.get("mode") == "search":
        b = dict(fb)
    else:
        _, groups, _, _ = inventory(wd)
        sel = selection(wd, groups)
        b = {"ref": fb["ref"], "source": fb.get("source"), "links": selected_links(fb, sel)}
    b["options"] = load_json(wd_path(wd, "options.json"), {})
    if fb.get("mode") != "search":
        # the book lists the default commentators in the defaults document's order
        b["options"]["commentator_order"] = [g["comm"] for g in groups if g["def"] is not None]
    b["translations"] = load_translations(wd, store)
    b["notes"] = load_json(wd_path(wd, "notes.json"), {})
    return b


def search(query, filters=None, size=30):
    """Sefaria's own search (the app's searchFetch), grouped by its categories."""
    body = {"query": query, "type": "text", "field": "naive_lemmatizer",
            "source_proj": ["ref", "heRef", "categories", "path"], "size": size, "start": 0,
            "slop": 10, "sort_method": "score", "sort_fields": ["pagesheetrank"],
            "sort_score_missing": 0.04, "aggs": ["path"]}
    if filters:
        body["filters"] = list(filters)
        body["filter_fields"] = ["path"] * len(filters)
    j = http_json(SEARCH_API, body)
    if not j:
        raise SystemExit("Search failed.")
    aggs = ((j.get("aggregations") or {}).get("path") or {}).get("buckets") or []
    tops = {}
    for b in aggs:
        top = str(b.get("key", "")).split("/")[0]
        tops[top] = tops.get(top, 0) + (b.get("doc_count") or 0)
    print("Matches by area (Sefaria's own counts, per edition):")
    for k, v in sorted(tops.items(), key=lambda kv: -kv[1]):
        print("  %s: %d" % (k, v))
    print("Top results:")
    seen = set()
    for h in (j.get("hits") or {}).get("hits") or []:
        s = h.get("_source") or {}
        r = s.get("ref")
        if not r or r in seen:
            continue
        seen.add(r)
        snip = ""
        for v in (h.get("highlight") or {}).values():
            if isinstance(v, list) and v:
                snip = strip_html(v[0])[:90]
                break
        print("  %s | %s | %s" % (r, s.get("path", ""), snip))


def fetch_search_texts(wd, query, refs, scope="section"):
    """The app's buildSelectedTexts input: each section's own text only."""
    use, seen = [], set()
    for r in refs:
        u = r if scope == "segment" else (section_of(r) or r)
        if u not in seen:
            seen.add(u)
            use.append(u)
    with ThreadPoolExecutor(max_workers=6) as ex:
        got = list(ex.map(texts_json, use))
    secs = []
    for r, j in zip(use, got):
        if j:
            secs.append({"ref": j.get("ref") or r, "heRef": j.get("heRef") or "",
                         "segments": [{"ref": (j.get("ref") or r) + ":" + str(i + 1), "he": h, "en": e}
                                      for i, (h, e) in enumerate(_zip_long(to_arr_raw(j.get("he")), to_arr_raw(j.get("text"))))]})
    save_json(wd_path(wd, "fetched.json"), {"mode": "search", "ref": query,
                                            "search": {"query": query, "sections": secs}})
    print("Fetched %d of %d section(s) for the search book." % (len(secs), len(use)))


def _zip_long(a, b):
    n = max(len(a), len(b))
    return [(a[i] if i < len(a) else "", b[i] if i < len(b) else "") for i in range(n)]


STORE_NAMES = ["sefaria_translations.json"]


def default_store(wd):
    for d in ("/mnt/project", "/mnt/user-data/uploads", wd):
        p = os.path.join(d, STORE_NAMES[0])
        if os.path.isfile(p):
            return p
    return None


def build_from_bundle(bundle, out):
    c = gather(bundle)
    if os.path.isdir(out):
        out = os.path.join(out, fname_for(c["ref"]) + ".epub")
    files = build_epub(c, out)
    problems = verify(files)
    counts = {}
    for b in c["blocks"]:
        counts[b["type"]] = counts.get(b["type"], 0) + 1
    print("built   :", out)
    print("title   :", c["title"])
    print("contents:", len(c["toc"]), "entries;", sum(1 for t in c["toc"] if t["level"] == 1), "top-level")
    print("texts   :", c["written"], "linked texts" if bundle.get("mode") != "search" else "sections",
          "| source:", "yes" if c["have_src"] else "no")
    print("blocks  :", ", ".join("%s=%d" % kv for kv in sorted(counts.items())))
    for w in c["warnings"]:
        print("WARNING :", w)
    for p in problems:
        print("PROBLEM :", p)
    print("check   :", "OK" if not problems else "FAILED")
    return 0 if not problems else 1


# --------------------------------------------------------------------------
# Browse -- the app's Browse (table-of-contents walk), one level per call
# --------------------------------------------------------------------------
# Stateless: every call walks Sefaria's live contents from the top along a
# path ("Talmud > Bavli > Seder Moed > Shabbat > By daf") and prints only the
# level it lands on. Each option leads either further in ("browse: <path>") or
# to a reference, which ends the walk.
INDEX_API = API + "/api/index"
TITLES_API = API + "/api/index/titles"
NAME_API = API + "/api/name/"
SEP = " > "
ALIYOT = ["Rishon", "Sheni", "Shlishi", "Revi'i", "Chamishi", "Shishi", "Shevi'i"]
HE_ALIYOT = ["ראשון", "שני", "שלישי", "רביעי", "חמישי", "שישי", "שביעי"]
STRUCT_HE = {"parasha": "לפי פרשה", "parshiot": "לפי פרשה", "daf": "לפי דף", "essay": "לפי מאמר",
             "tikkunim": "לפי תיקון", "gate": "לפי שער", "topic": "לפי נושא", "chapters": "לפי פרק"}
SECNAME_HE = {"chapter": "לפי פרק", "perek": "לפי פרק", "daf": "לפי דף", "siman": "לפי סימן",
              "mishnah": "לפי משנה", "halakhah": "לפי הלכה", "volume": "לפי כרך",
              "paragraph": "לפי פסקה", "verse": "לפי פסוק", "section": "לפי חלק"}


def cached_json(wd, name, url, max_age=86400):
    """A large, slow-changing Sefaria file, kept a day in the work folder."""
    path = wd_path(wd, name)
    if os.path.exists(path) and time.time() - os.path.getmtime(path) < max_age:
        j = load_json(path, None)
        if j is not None:
            return j
    j = http_json(url)
    if j is None:
        j = load_json(path, None)               # a stale copy beats nothing
        if j is None:
            raise SystemExit("Couldn't load " + url)
        return j
    with open(path, "w", encoding="utf-8") as f:
        json.dump(j, f, ensure_ascii=False)
    return j


def node_en(n):                                 # nodeEn
    if n.get("title"):
        return n["title"]
    if n.get("category"):
        return n["category"]
    t = [x for x in n.get("titles") or [] if x.get("lang") == "en"]
    t = [x for x in t if x.get("primary")] or t
    return t[0]["text"] if t else (n.get("key") or "section")


def node_he(n):                                 # nodeHe
    if n.get("heTitle"):
        return n["heTitle"]
    if n.get("heCategory"):
        return n["heCategory"]
    t = [x for x in n.get("titles") or [] if x.get("lang") == "he"]
    t = [x for x in t if x.get("primary")] or t
    return t[0]["text"] if t else ""


def is_comm_book(b):
    return str(b.get("dependence") or "").lower() == "commentary"


def is_comm_cat(c):
    """Rishonim / Acharonim / Modern Commentary on ..., and any "Commentary"."""
    return (str(c.get("searchRoot") or "").lower().endswith("commentary")
            or re.search(r"commentary", str(c.get("category") or ""), re.I) is not None)


def has_primary(node):
    for ch in node.get("contents") or []:
        if "contents" in ch:
            if not is_comm_cat(ch) and has_primary(ch):
                return True
        elif ch.get("title") and not is_comm_book(ch):
            return True
    return False


def primary_contents(node):
    """A contents node's children, primary texts only: commentary categories,
    commentary books and categories left empty are dropped."""
    out = []
    for ch in node.get("contents") or []:
        if "contents" in ch:
            if not is_comm_cat(ch) and has_primary(ch):
                out.append(ch)
        elif ch.get("title") and not is_comm_book(ch):
            out.append(ch)
    # Inside Midrash, the aggadic subcategory reads above the halakhic one.
    if str(node.get("category") or "").lower() == "midrash":
        ai = next((i for i, c in enumerate(out) if re.search("aggad", node_en(c), re.I)), -1)
        hi = next((i for i, c in enumerate(out) if re.search("hala[ck]h", node_en(c), re.I)), -1)
        if ai > -1 and hi > -1 and hi < ai:
            out.insert(hi, out.pop(ai))
    return out


NAV_KEYS = ("default", "sectionNames", "addressTypes", "depth", "lengths", "refs", "wholeRef", "startingAddress")


def nav_node(n):
    """A schema or alt-structure node, cut to what the navigator reads."""
    out = {k: n[k] for k in NAV_KEYS if n.get(k)}
    en, he = node_en(n), node_he(n)
    if en and en != "section":
        out["title"] = en
    if he:
        out["heTitle"] = he
    if n.get("nodes"):
        out["nodes"] = [nav_node(c) for c in n["nodes"]]
    return out


def nav_book(title):
    """One book for the navigator: its schema, alternate structures and the
    live shape (counts per section, keyed by each leaf's full title)."""
    rec = http_json(V2_INDEX_API + enc(title))
    if not isinstance(rec, dict) or not rec.get("schema"):
        return None
    alts = rec.get("alt_structs") or rec.get("alts") or {}
    out = {"schema": nav_node(rec["schema"]),
           "alts": {k: {"nodes": [nav_node(n) for n in v["nodes"]]}
                    for k, v in alts.items() if isinstance(v, dict) and v.get("nodes")}}
    for k in ("default_struct", "exclude_structs"):
        if rec.get(k):
            out[k] = rec[k]
    shape = {}
    j = http_json(SHAPE_API + enc(title))
    for f in (j if isinstance(j, list) else [j]):
        if not isinstance(f, dict):
            continue
        for leaf in (f["chapters"] if f.get("isComplex") and isinstance(f.get("chapters"), list) else [f]):
            if isinstance(leaf, dict) and leaf.get("title") and leaf.get("chapters") is not None:
                shape[leaf["title"]] = leaf["chapters"]
    return {"i": out, "s": shape}


def browse_data(wd, out):
    """Data for the navigator widget, served from the repo so the widget
    never has to reach Sefaria: OUT (browse_toc.json) is the primary-text
    contents tree as compact arrays -- [name, hebrew, children] for a
    category, [title, hebrew, id] for a book -- and browse_books/<id>.json
    beside it holds each book's structure (nav_book)."""
    ids = {}

    def tree(node):
        rows = []
        for c in primary_contents(node):
            if "contents" in c:
                rows.append([node_en(c), node_he(c), tree(c)])
            else:
                rows.append([c["title"], c.get("heTitle") or "", ids.setdefault(c["title"], len(ids))])
        return rows
    data = tree({"contents": cached_json(wd, "toc_cache.json", INDEX_API, max_age=0)})
    with open(out, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    print("Wrote %s (%d bytes, %d books)" % (out, os.path.getsize(out), len(ids)))
    bdir = os.path.join(os.path.dirname(os.path.abspath(out)), "browse_books")
    os.makedirs(bdir, exist_ok=True)
    for old in os.listdir(bdir):
        os.remove(os.path.join(bdir, old))
    done, failed, total = [0], [], [0]

    def one(item):
        title, i = item
        bk = nav_book(title)
        done[0] += 1
        if done[0] % 100 == 0:
            status("%d / %d books" % (done[0], len(ids)))
        if not bk:
            failed.append(title)
            return
        path = os.path.join(bdir, "%d.json" % i)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(bk, f, ensure_ascii=False, separators=(",", ":"))
        total[0] += os.path.getsize(path)
    with ThreadPoolExecutor(max_workers=6) as ex:
        list(ex.map(one, ids.items()))
    print("Wrote %d book files to %s (%d bytes)" % (len(ids) - len(failed), bdir, total[0]))
    if failed:
        # Contents entries Sefaria has no book behind ("No book named ...")
        # can't be opened anywhere, so the navigator leaves them out.
        gone = set(failed)

        def prune(rows):
            out = []
            for r in rows:
                if isinstance(r[2], list):
                    kids = prune(r[2])
                    if kids:
                        out.append([r[0], r[1], kids])
                elif r[0] not in gone:
                    out.append(r)
            return out
        with open(out, "w", encoding="utf-8") as f:
            json.dump(prune(data), f, ensure_ascii=False, separators=(",", ":"))
        print("Left out (no book on Sefaria): " + ", ".join(sorted(failed)))
    return 0


def b_opt(label, he, path, go):
    return {"label": label, "he": he or "", "kind": "browse", "path": path + [label], "go": go}


def r_opt(label, he, ref, fetch=None, whole=False):
    return {"label": label, "he": he or "", "kind": "ref", "ref": ref,
            "fetch": fetch or 'fetch "%s"' % ref, "whole": whole}


def level(path, options, note=""):
    return {"path": path, "options": options, "note": note}


def final(path, ref):
    return level(path, [r_opt(ref, "", ref)])


def send_text(o):
    return "browse: " + SEP.join(o["path"]) if o["kind"] == "browse" else o["ref"]


def cat_level(node, path):
    opts = []
    for ch in primary_contents(node):
        if "contents" in ch:
            opts.append(b_opt(node_en(ch), node_he(ch), path,
                              lambda ch=ch, p=path + [node_en(ch)]: cat_level(ch, p)))
        else:
            opts.append(b_opt(ch["title"], ch.get("heTitle"), path,
                              lambda t=ch["title"], p=path + [ch["title"]]: book_level(t, p)))
    return level(path, opts)


def alt_structs(rec):                           # altStructsFrom
    alts = (rec or {}).get("alt_structs") or (rec or {}).get("alts") or {}
    return [(k, v["nodes"]) for k, v in alts.items() if isinstance(v, dict) and v.get("nodes")]


def is_daf_schema(schema):                      # isDafSchema
    return bool(schema and not schema.get("nodes") and (schema.get("addressTypes") or [None])[0] == "Talmud")


def daf_from_ref(ref):                          # dafFromRef
    m = re.search(r"\s(\d+[ab])(?::|\s*-|$)", str(ref))
    return m.group(1) if m else None


def daf_label_from(start, i):                   # dafLabelFrom
    m = re.match(r"^(\d+)\s*([ab])$", str(start))
    s = (int(m.group(1)) * 2 + (m.group(2) == "b") + i) if m else (4 + i)
    return "%d%s" % (s // 2, "ab"[s % 2])


def book_level(title, path):
    rec = http_json(V2_INDEX_API + enc(title))
    if not isinstance(rec, dict) or not rec.get("schema"):
        return final(path, title)
    schema, structs = rec["schema"], alt_structs(rec)
    # A tractate: by perek (Sefaria's chapter structure) or by daf.
    if is_daf_schema(schema):
        opts = []
        ch = next((s for s in structs if re.search("chapter", s[0], re.I)), structs[0] if structs else None)
        if ch and all(n.get("refs") for n in ch[1]):
            opts.append(b_opt("By perek", "לפי פרק", path, lambda: perek_list(title, ch[1], path + ["By perek"])))
        opts.append(b_opt("By daf", "לפי דף", path, lambda: daf_level(title, schema, path + ["By daf"])))
        return level(path, opts)
    # Anything else: the book's own schema and its alternate structures, as
    # the site offers them (default_struct first, exclude_structs hidden).
    rows = []
    ex = [str(s).lower() for s in rec.get("exclude_structs") or []]
    if "schema" not in ex:
        sn = str((schema.get("sectionNames") or [""])[0])
        rows.append(("By " + sn.lower() if sn else "Contents", SECNAME_HE.get(sn.lower(), ""), None))
    for name, nodes in structs:
        rows.append(("By " + name.lower(), STRUCT_HE.get(name.lower(), ""), (name, nodes)))
    ds = rec.get("default_struct")
    i = next((i for i, r in enumerate(rows) if r[2] and r[2][0] == ds), -1)
    if i > 0:
        rows.insert(0, rows.pop(i))
    if not rows:
        return default_schema(title, schema, path)
    opts = []
    for label, he, st in rows:
        p = path + [label]
        if st:
            opts.append(b_opt(label, he, path, lambda st=st, p=p: alt_level(title, st[1], st[0], p)))
        else:
            opts.append(b_opt(label, he, path, lambda p=p: default_schema(title, schema, p)))
    return level(path, opts)


def perek_list(title, nodes, path):
    opts = []
    for i, n in enumerate(nodes):
        en = node_en(n)
        label = en if en and en != "section" else "Chapter %d" % (i + 1)
        opts.append(b_opt(label, node_he(n), path,
                          lambda n=n, i=i, p=path + [label]: perek_level(title, n, i + 1, p)))
    return level(path, opts)


def perek_level(title, node, num, path):
    """One chapter: the whole chapter, then its pages (a page shared with the
    next chapter is Sefaria's partial ref, e.g. "Shabbat 20b:1-4")."""
    refs = node["refs"]
    first, last = daf_from_ref(refs[0]), daf_from_ref(refs[-1])
    rng = title + " " + first + ("-" + last if last and last != first else "") if first else title
    opts = [r_opt("Whole chapter — " + rng, "", "%s (chapter %d)" % (rng, num),
                  fetch='fetch "%s" --chapter %d' % (title, num), whole=True)]
    opts += [r_opt(daf_from_ref(r) or r, "", r) for r in refs]
    return level(path, opts, "a is the front of the page, b the back")


def amud_refs(prefix):
    """Every amud that has text, from Sefaria's live shape (index 0 is 1a)."""
    j = http_json(SHAPE_API + enc(prefix))
    first = j[0] if isinstance(j, list) and j else j
    if isinstance(first, dict) and first.get("isComplex") and isinstance(first.get("chapters"), list):
        first = next((c for c in first["chapters"] if isinstance(c, dict) and c.get("title") == prefix), None)
    ch = first.get("chapters") if isinstance(first, dict) else None
    if not isinstance(ch, list):
        return None
    return ["%s %d%s" % (prefix, i // 2 + 1, "ab"[i % 2]) for i, c in enumerate(ch)
            if (len(c) if isinstance(c, list) else c)]


def daf_level(prefix, node, path):
    refs = amud_refs(prefix)
    if not refs:                                # the app's fallback: 2a on
        n = (node.get("lengths") or [180])[0]
        refs = ["%s %d%s" % (prefix, 2 + i // 2, "ab"[i % 2]) for i in range(n)]
    return level(path, [r_opt(r[len(prefix) + 1:], "", r) for r in refs],
                 "a is the front of the page, b the back")


def alt_node_en(n):
    if n.get("default"):
        return "(main text)"
    t = node_en(n)
    return t if t and t != "section" else "(main text)"


def alt_level(title, nodes, sname, path):       # renderAltNodes
    d = next((n for n in nodes if n and n.get("default") and (n.get("refs") or n.get("wholeRef"))), None)
    opts = alt_leaf(title, d, sname) if d else []
    for n in nodes:
        if not n or n is d:
            continue
        label, p = alt_node_en(n), path + [alt_node_en(n)]
        he = "" if n.get("default") else node_he(n)
        if n.get("nodes"):
            opts.append(b_opt(label, he, path, lambda n=n, p=p: alt_level(title, n["nodes"], sname, p)))
        else:
            opts.append(b_opt(label, he, path, lambda n=n, p=p: level(p, alt_leaf(title, n, sname))))
    return level(path, opts)


def alt_leaf(title, node, sname):               # appendAltLeaf
    opts = []
    whole = node.get("wholeRef") or ""
    if whole:
        nm = alt_node_en(node)
        opts.append(r_opt("Whole " + (str(sname or "section").lower() if nm == "(main text)" else nm)
                          + " — " + whole, "", whole, whole=True))
    refs = node.get("refs") or []
    at = node.get("addressTypes") or []
    if node.get("startingAddress") or "Talmud" in at or "Folio" in at:
        for i, r in enumerate(refs):
            label = daf_label_from(node["startingAddress"], i) if node.get("startingAddress") else (daf_from_ref(r) or str(i + 1))
            opts.append(r_opt(label, "", r))
        return opts
    aliyot = len(refs) == 7 and re.match("parash", str(sname or ""), re.I)
    for i, r in enumerate(refs):
        opts.append(r_opt(ALIYOT[i] + " · " + r if aliyot else r, HE_ALIYOT[i] if aliyot else "", r))
    return opts


def default_schema(title, schema, path):
    if schema.get("nodes"):
        return schema_nodes(title, schema, title, path)
    return jagged(title, schema, path)


def jdepth(node):
    return node.get("depth") or len(node.get("sectionNames") or []) or 1


def schema_nodes(title, node, prefix, path):    # renderSchemaNodes
    opts = []
    for ch in node.get("nodes") or []:
        label = "(main text)" if ch.get("default") else node_en(ch)
        he = "" if ch.get("default") else node_he(ch)
        cp = prefix if ch.get("default") else prefix + ", " + node_en(ch)
        p = path + [label]
        if ch.get("nodes"):
            opts.append(b_opt(label, he, path, lambda ch=ch, cp=cp, p=p: schema_nodes(title, ch, cp, p)))
        elif jdepth(ch) == 1:
            opts.append(r_opt(label, he, cp))
        else:
            opts.append(b_opt(label, he, path, lambda ch=ch, cp=cp, p=p: jagged(cp, ch, p)))
    return level(path, opts) if opts else final(path, prefix)


def shape_len(title):                           # shapeLen
    j = http_json(SHAPE_API + enc(title))
    first = j[0] if isinstance(j, list) and j else j
    if not isinstance(first, dict):
        return None
    if first.get("isComplex") and isinstance(first.get("chapters"), list):
        first = next((c for c in first["chapters"] if isinstance(c, dict) and c.get("title") == title), None)
        if not first:
            return None
    if isinstance(first.get("chapters"), int):
        return first["chapters"] or None
    return first.get("length") or (len(first["chapters"]) if isinstance(first.get("chapters"), list) else None) or None


def fetch_count(ref):                           # fetchCount
    j = texts_json(ref)
    n = max(len(j["he"]) if j and isinstance(j.get("he"), list) else 0,
            len(j["text"]) if j and isinstance(j.get("text"), list) else 0)
    return max(n, shape_count_for(ref) or 0) or None


def ref_join(prefix, secs):
    return prefix + " " + ":".join(str(s) for s in secs)


def number_opts(prefix, node, secs, count, path):
    depth = len(node.get("sectionNames") or []) or 1
    opts = []
    for i in range(1, count + 1):
        s = secs + [i]
        if len(s) >= depth:
            opts.append(r_opt(str(i), "", ref_join(prefix, s)))
        else:
            opts.append(b_opt(str(i), "", path, lambda s=s, p=path + [str(i)]: pick_number(prefix, node, s, p)))
    return opts


def jagged(prefix, node, path):                 # renderJagged
    if jdepth(node) == 1:
        return final(path, prefix)
    if (node.get("addressTypes") or ["Integer"])[0] == "Talmud":
        return daf_level(prefix, node, path)
    sname = str((node.get("sectionNames") or ["section"])[0]).lower()
    n = shape_len(prefix) or (node.get("lengths") or [None])[0] or fetch_count(prefix)
    if not n:
        return final(path, prefix)
    return level(path, number_opts(prefix, node, [], n, path), "pick a " + sname)


def pick_number(prefix, node, secs, path):      # pickNumber
    cur = ref_join(prefix, secs)
    nxt = str((node.get("sectionNames") or [])[len(secs)] if len(node.get("sectionNames") or []) > len(secs) else "part").lower()
    c = fetch_count(cur)
    opts = [r_opt("All of " + cur, "", cur, whole=True)]
    if c:
        opts += number_opts(prefix, node, secs, c, path)
    return level(path, opts, "all of it, or narrow to a " + nxt)


def find_opt(lvl, part):
    p = part.strip().casefold()
    for o in lvl["options"]:
        if p in (o["label"].casefold(), o["he"].casefold()) or (o["kind"] == "ref" and p == o["ref"].casefold()):
            return o
    return None


def browse_walk(wd, parts):
    """Walk the live contents along parts; levels with one option are passed
    through (and may or may not be named in the path)."""
    toc = cached_json(wd, "toc_cache.json", INDEX_API)
    root = cat_level({"contents": toc}, [])
    if parts and not find_opt(root, parts[0]):
        # a bare book title: start from its place in the contents
        want = parts[0].casefold()
        hit = []

        def look(nodes):
            for n in nodes:
                if "contents" in n:
                    look(n["contents"])
                elif want in (str(n.get("title", "")).casefold(), str(n.get("heTitle", "")).casefold()):
                    hit.append(n)
        look(toc)
        if hit:
            parts = list(hit[0].get("categories") or []) + [hit[0]["title"]] + parts[1:]
    lvl, skipped, i = root, [], 0
    while True:
        while len(lvl["options"]) == 1 and lvl["options"][0]["kind"] == "browse":
            o = lvl["options"][0]
            skipped.append(o["label"])
            if i < len(parts) and find_opt(lvl, parts[i]):
                i += 1
            lvl = o["go"]()
        if i >= len(parts):
            return lvl, skipped, None
        o = find_opt(lvl, parts[i])
        if not o:
            return lvl, skipped, parts[i]
        i += 1
        if o["kind"] == "ref":
            return level(lvl["path"], [o]), skipped, None
        lvl = o["go"]()


def jq(s):
    return json.dumps(s, ensure_ascii=False)


WIDGET_HEAD = ('<style>#w button{margin:2px;padding:6px 9px;border:1px solid #aaa;border-radius:6px;'
               'background:#f4f4f4;color:#222;font:inherit;cursor:pointer}#w div{margin:3px 0}'
               '@media(prefers-color-scheme:dark){#w button{background:#333;color:#eee;border-color:#666}}'
               '</style><div id=w></div><script>const w=document.getElementById("w"),'
               'B=(l,t,p=w)=>{const b=document.createElement("button");b.textContent=l;'
               'b.onclick=()=>sendPrompt(t);p.append(b)};')
DAF_TOK = re.compile(r"^(.*\S)\s(\d+[ab](?::\d+(?:-\d+)?)?)$")


def daf_js(opts):
    """Talmud pages: one row per daf with its a / b buttons. Data is a token
    string; runs of whole amudim are written as ranges ("2a-157b")."""
    m = [DAF_TOK.match(o["ref"]) if o["kind"] == "ref" else None for o in opts]
    if not opts or not all(m) or len({x.group(1) for x in m}) != 1:
        return None
    if any(daf_from_ref(o["ref"]) != o["label"] and o["label"] != x.group(2) for o, x in zip(opts, m)):
        return None
    side = lambda t: int(t[:-1]) * 2 + (t[-1] == "b")
    toks, run = [], []
    for x in m:
        t = x.group(2)
        if ":" not in t and run and side(t) == side(run[-1]) + 1:
            run.append(t)
            continue
        if run:
            toks.append(run[0] + ("-" + run[-1] if len(run) > 1 else ""))
        run = [] if ":" in t else [t]
        if ":" in t:
            toks.append(t)
    if run:
        toks.append(run[0] + ("-" + run[-1] if len(run) > 1 else ""))
    return ('const A=%s,S=x=>parseInt(x)*2+(x[x.length-1]=="b");let r,d;'
            'for(const t of %s.split(" ")){const m=t.match(/^(\\d+[ab])-(\\d+[ab])$/),k=m?[]:[t];'
            'if(m)for(let s=S(m[1]);s<=S(m[2]);s++)k.push((s>>1)+"ab"[s&1]);'
            'for(const x of k){const n=parseInt(x);if(n!==d){d=n;r=document.createElement("div");'
            'r.append(n+" ");w.append(r)}B(x.replace(/^\\d+/,""),A+x,r)}}'
            % (jq(m[0].group(1) + " "), jq(" ".join(toks))))


def num_js(opts):
    """1..N, each sending a common prefix plus its number."""
    if not opts or [o["label"] for o in opts] != [str(i) for i in range(1, len(opts) + 1)]:
        return None
    sends = [send_text(o) for o in opts]
    a = sends[0][:-1]
    if not all(s == a + o["label"] for s, o in zip(sends, opts)):
        return None
    return "const A=%s;for(let i=1;i<=%d;i++)B(i,A+i);" % (jq(a), len(opts))


def list_js(path, opts):
    """[label, hebrew, what it sends]; the last is left out when it is just
    this level's path plus the label."""
    p = "browse: " + "".join(x + SEP for x in path)
    rows = []
    for o in opts:
        s = send_text(o)
        row = [o["label"], o["he"]] + ([] if s == p + o["label"] else [s])
        while row and row[-1] == "":
            row.pop()
        rows.append(row)
    return ('const P=%s;for(const[l,h,t]of %s)B(h?l+" · "+h:l,t||P+l);'
            % (jq(p), json.dumps(rows, ensure_ascii=False, separators=(",", ":"))))


def widget(lvl):
    opts = lvl["options"]
    whole = [o for o in opts if o.get("whole")]
    rest = [o for o in opts if not o.get("whole")]
    js = "".join("B(%s,%s);" % (jq(o["label"]), jq(send_text(o))) for o in whole)
    js += daf_js(rest) or num_js(rest) or list_js(lvl["path"], rest)
    return WIDGET_HEAD + js + "</script>"


def browse(wd, where):
    where = re.sub(r"^\s*browse:\s*", "", where or "", flags=re.I).strip()
    parts = [p.strip() for p in where.split(">") if p.strip()] if where else []
    lvl, skipped, missing = browse_walk(wd, parts)
    opts = lvl["options"]
    if missing:
        print("NOT FOUND: %r is not an option here; showing this level instead." % missing)
    print("LEVEL: " + (SEP.join(lvl["path"]) or "Top"))
    for s in skipped:
        print("SKIPPED: %s (the only option)" % s)
    if len(opts) == 1 and opts[0]["kind"] == "ref":
        print("FINAL: " + opts[0]["ref"])
        print("FETCH: " + opts[0]["fetch"])
        return 0
    if not opts:
        print("Nothing to browse here.")
        return 1
    show = "buttons" if len(opts) <= 4 else "grid"
    print("SHOW: %s (%d option%s)%s" % (show, len(opts), "" if len(opts) == 1 else "s",
                                        (" - " + lvl["note"]) if lvl.get("note") else ""))
    for i, o in enumerate(opts, 1):
        print(" %d. %s%s -> %s%s" % (i, o["label"], (" | " + o["he"]) if o["he"] else "", send_text(o),
                                     ("   [" + o["fetch"] + "]") if o["kind"] == "ref" and "--chapter" in o["fetch"] else ""))
    if show == "grid":
        html_w = widget(lvl)
        print("WIDGET (%d bytes):" % len(html_w.encode("utf-8")))
        print(html_w)
    return 0


# --------------------------------------------------------------------------
# Resolve -- a typed source (English or Hebrew, even misspelled) to a ref
# --------------------------------------------------------------------------
RESOLVE_ADDR = re.compile(r"^(.*?[^\d\s:.,\-–])[\s,.]*(\d+[ab]?(?:[:.]\d+[ab]?)*(?:\s*[-–]\s*\d+[ab]?(?:[:.]\d+[ab]?)*)?)$")


def name_api(q, refs_only=False):
    j = http_json(NAME_API + enc(q) + "?limit=10" + ("&type=ref" if refs_only else ""))
    return j if isinstance(j, dict) else {}


def resolve(wd, text):
    """Sefaria's name API first; then near spellings of the title (Sefaria's
    own completions and the closest of its title variants) with the address
    put back, kept only when Sefaria accepts the result as a reference."""
    q = re.sub(r"\s+", " ", text or "").strip()
    chap = re.match(r"^(.*?)\s*\(chapter (\d+)\)$", q, re.I)
    if chap:
        q = chap.group(1)
    if not q:
        raise SystemExit("resolve needs some text.")
    found = []
    j = name_api(q)
    if j.get("is_ref") and j.get("ref"):
        found = [j]
    else:
        splits = []
        m = RESOLVE_ADDR.match(q)
        if m:
            splits.append((m.group(1).strip(" ,."), m.group(2)))
        else:                                   # Hebrew numerals: try 0-2 short trailing words
            toks = q.split(" ")
            for k in (0, 1, 2):
                if len(toks) > k and all(len(t) <= 4 for t in toks[len(toks) - k:]):
                    splits.append((" ".join(toks[:len(toks) - k]), " ".join(toks[len(toks) - k:])))
        books = (cached_json(wd, "titles_cache.json", TITLES_API) or {}).get("books") or []
        he_books = []

        def look(nodes):
            for n in nodes:
                if "contents" in n:
                    look(n["contents"])
                elif n.get("heTitle"):
                    he_books.append(n["heTitle"])
        look(cached_json(wd, "toc_cache.json", INDEX_API))
        cands = []
        for title, addr in splits:
            pool = he_books if re.search("[֐-׿]", title) else books
            close = lambda t: difflib.SequenceMatcher(None, title.casefold(), str(t).casefold()).ratio() >= 0.6
            names = [c["key"] for c in name_api(title, True).get("completion_objects") or []
                     if c.get("type") == "ref" and isinstance(c.get("key"), str) and close(c.get("title"))]
            names += difflib.get_close_matches(title, pool, n=6, cutoff=0.6)
            for nm in names:
                c = (nm + " " + addr).strip()
                if c not in cands:
                    cands.append(c)
        with ThreadPoolExecutor(max_workers=6) as ex:
            checked = list(ex.map(name_api, cands[:16]))
        seen = set()
        for c in checked:
            if c.get("is_ref") and c.get("ref") and c["ref"] not in seen:
                seen.add(c["ref"])
                found.append(c)
    if not found:
        print("NONE: Sefaria doesn't recognise %r as a source." % text)
        return 1
    if len(found) == 1:
        f = found[0]
        if chap:
            print("EXACT: %s (chapter %s)" % (f["ref"], chap.group(2)))
            print('FETCH: fetch "%s" --chapter %s' % (f.get("index") or f["ref"], chap.group(2)))
        elif f.get("is_book"):
            print("BOOK: " + f["ref"])
            print("NEXT: browse " + f["ref"])
        else:
            print("EXACT: " + f["ref"])
            print('FETCH: fetch "%s"' % f["ref"])
        return 0
    print("CANDIDATES:")
    for i, f in enumerate(found[:5], 1):
        print(" %d. %s%s" % (i, f["ref"], "  (whole book)" if f.get("is_book") else ""))
    return 0


USAGE = """build_epub.py -- fetch from Sefaria and build a Kindle EPUB like the Sefaria Links app.

Work-folder commands (default folder /home/claude/book, change with --dir):
  browse [PATH]                               Sefaria's contents, one level: browse,
                                              browse "Talmud > Bavli", browse Shabbat
  resolve TEXT                                a typed source -> exact ref or candidates
  browse-data [OUT]                           contents file for the navigator widget
  fetch REF [--chapter N] [--pages REF ...]   fetch source + every linked text; list them
  show                                        list the linked texts again (numbered)
  select [--add N ...] [--remove N ...] [--only N ...] [--all] [--none]
  options [--group cat|seg] [--headings both|en|he] [--hebrew on|off]
          [--english on|off] [--source on|off]
  trans-list --lang L [--all-english] [--max-chars 6000]
  trans-get  --lang L --group N [--part K] [--all-english] [--max-chars 6000]
  trans-add  FILE --lang L                    store a reply written in @@ref@@ blocks
  trans-export [OUT]                          merged reuse file -> sefaria_translations.json
  note KEY TEXT                               KEY = L:<linked ref> | S:<segment ref> | __src__
  search QUERY [--in PATH ...] [--size N]
  search-book QUERY REF ... [--scope section|segment]
  build [OUT]                                 build the EPUB (default /mnt/user-data/outputs/)
  bundle OUT part.json ...                    build from hand-written part files
"""


def main(argv):
    args = argv[1:]
    if not args:
        print(USAGE)
        return 2
    # older form: build_epub.py OUT part.json ...
    if args[0] not in COMMANDS and len(args) >= 2 and args[1].endswith(".json"):
        args = ["bundle"] + args
    cmd, rest = args[0], args[1:]
    if cmd not in COMMANDS:
        print(USAGE)
        return 2

    def opt(name, n=1, default=None):
        if name in rest:
            i = rest.index(name)
            vals = []
            for v in rest[i + 1:]:
                if v.startswith("--"):
                    break
                vals.append(v)
            del rest[i:i + 1 + len(vals)]
            return vals if n != 1 else (vals[0] if vals else default)
        return default

    def flag(name):
        if name in rest:
            rest.remove(name)
            return True
        return False

    wd = opt("--dir", default="/home/claude/book")
    os.makedirs(wd, exist_ok=True)
    store = opt("--store") or default_store(wd)
    defaults_path = opt("--defaults")

    if cmd == "bundle":
        bundle = {}
        for p in rest[1:]:
            with open(p, encoding="utf-8") as f:
                bundle = deep_merge(bundle, json.load(f))
        return build_from_bundle(bundle, rest[0])

    if cmd == "fetch":
        chapter = opt("--chapter")
        pages = opt("--pages", n=0)
        flag("--refresh")
        ref = " ".join(rest).strip()
        if not ref:
            raise SystemExit("fetch needs a reference.")
        if chapter:
            cp = chapter_pages(ref, int(chapter))
            if not cp:
                raise SystemExit("Couldn't find chapter %s of %s." % (chapter, ref))
            ref, pages = cp["label"], cp["refs"]
            status("%s: %d pages" % (ref, len(pages)))
        save_json(wd_path(wd, "fetched.json"), fetch_book_data(ref, pages or None))
        for stale in ("selection.json", "notes.json", "translations.json"):
            if os.path.exists(wd_path(wd, stale)):
                os.remove(wd_path(wd, stale))
        print_inventory(wd, defaults_path)
        return 0

    if cmd == "show":
        print_inventory(wd, defaults_path)
        return 0

    if cmd == "select":
        _, groups, _, _ = inventory(wd, defaults_path)
        sel = selection(wd, groups)
        by_num = {str(g["num"]): g["key"] for g in groups}
        cur = list(sel["groups"])
        if flag("--all"):
            cur = [g["key"] for g in groups]
        if flag("--none"):
            cur = []
        only = opt("--only", n=0)
        if only:
            cur = [by_num[x] for x in only if x in by_num]
        for x in opt("--add", n=0) or []:
            if x in by_num and by_num[x] not in cur:
                cur.append(by_num[x])
        for x in opt("--remove", n=0) or []:
            if x in by_num and by_num[x] in cur:
                cur.remove(by_num[x])
        save_json(wd_path(wd, "selection.json"), {"groups": cur})
        print_inventory(wd, defaults_path)
        return 0

    if cmd == "options":
        o = load_json(wd_path(wd, "options.json"), {})
        for k, name in (("group", "--group"), ("headings", "--headings")):
            v = opt(name)
            if v:
                o[k] = v
        for k, name in (("hebrew", "--hebrew"), ("english", "--english"), ("include_source", "--source")):
            v = opt(name)
            if v:
                o[k] = v.lower() in ("on", "yes", "true", "1")
        save_json(wd_path(wd, "options.json"), o)
        print(json.dumps({"group": o.get("group", "cat"), "headings": o.get("headings", "both"),
                          "hebrew": o.get("hebrew", True), "english": o.get("english", True),
                          "include_source": o.get("include_source", True)}))
        return 0

    if cmd in ("trans-list", "trans-get"):
        lang = opt("--lang") or "English"
        mx = int(opt("--max-chars") or 6000)
        allen = flag("--all-english")
        groups = trans_groups(wd, lang, store, allen, defaults_path)
        if cmd == "trans-list":
            if not groups:
                print("Nothing needs translating into %s." % lang)
                return 0
            tot = 0
            for i, g in enumerate(groups, 1):
                ch = sum(len(x[1]) for x in g["items"])
                tot += ch
                print("%2d  %s - %d text%s, %d Hebrew characters, %d part%s" % (
                    i, g["name"], len(g["items"]), "" if len(g["items"]) == 1 else "s", ch,
                    len(parts_of(g["items"], mx)), "" if len(parts_of(g["items"], mx)) == 1 else "s"))
            print("Total: %d Hebrew characters to translate into %s." % (tot, lang))
            return 0
        n = int(opt("--group") or 0)
        if not (1 <= n <= len(groups)):
            raise SystemExit("No such group; run trans-list.")
        parts = parts_of(groups[n - 1]["items"], mx)
        k = int(opt("--part") or 1)
        if not (1 <= k <= len(parts)):
            raise SystemExit("No such part.")
        print("# %s - part %d of %d" % (groups[n - 1]["name"], k, len(parts)))
        for ref, he in parts[k - 1]:
            print(header_of(ref))
            print(he)
            print()
        return 0

    if cmd == "trans-add":
        lang = opt("--lang") or "English"
        trans_add(wd, rest[0], lang)
        return 0

    if cmd == "trans-export":
        out = rest[0] if rest else "/mnt/user-data/outputs/" + STORE_NAMES[0]
        merged = load_translations(wd, store)
        save_json(out, merged)
        print("Wrote %d translation%s to %s" % (len(merged), "" if len(merged) == 1 else "s", out))
        return 0

    if cmd == "note":
        notes = load_json(wd_path(wd, "notes.json"), {})
        key, text = rest[0], " ".join(rest[1:]).strip()
        if text:
            notes[key] = text
        else:
            notes.pop(key, None)
        save_json(wd_path(wd, "notes.json"), notes)
        print("%d note%s" % (len(notes), "" if len(notes) == 1 else "s"))
        return 0

    if cmd == "search":
        filters = opt("--in", n=0)
        size = int(opt("--size") or 30)
        search(" ".join(rest), filters, size)
        return 0

    if cmd == "search-book":
        scope = opt("--scope") or "section"
        fetch_search_texts(wd, rest[0], rest[1:], scope)
        return 0

    if cmd == "browse":
        return browse(wd, " ".join(rest))

    if cmd == "resolve":
        return resolve(wd, " ".join(rest))

    if cmd == "browse-data":
        return browse_data(wd, rest[0] if rest else "browse_toc.json")

    if cmd == "build":
        out = rest[0] if rest else "/mnt/user-data/outputs/"
        return build_from_bundle(bundle_from_workdir(wd, store), out)
    return 2


COMMANDS = ["fetch", "show", "select", "options", "trans-list", "trans-get", "trans-add",
            "trans-export", "note", "search", "search-book", "build", "bundle", "browse", "resolve", "browse-data"]


if __name__ == "__main__":
    sys.exit(main(sys.argv))
