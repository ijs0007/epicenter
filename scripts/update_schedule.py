#!/usr/bin/env python3
"""
Epicenter Theater — weekly schedule auto-updater (v2.1).

v2 additions:
  * Auto-resolves REAL YouTube trailer links for new movies (scrapes YouTube
    search results; uses the official YouTube Data API instead if a
    YOUTUBE_API_KEY repo secret is configured).
  * Auto-fetches Rotten Tomatoes AUDIENCE (Popcornmeter) scores for every
    scheduled movie on every run — fills in nulls once scores appear, and
    keeps settling scores current.
  * Upgrades any placeholder "youtube.com/results?search_query=" trailer
    links left by earlier runs to real watch links when it can find one.
  * v2.1 FIX: each day's shows are now anchored to that day's own listings
    table (time + movie link pairs). The old position-based guess shifted
    every day by one on the live page. A new cross-check gate aborts if
    tables and metadata blocks ever disagree.
  * v2.2 FIX (2026-07-21 incident): the 07-20 run wrote raw multi-line page
    HTML into synopsis strings (JS SyntaxError -> blank site) and empty
    poster/studio/genre/date fields after navymwr.org changed its detail
    markup. Detail parsing is now label-based on tag-stripped text, every
    string is forced to a single sanitised line at write time (js_str),
    and new hard gates (quote parity per line, no HTML fragments, every
    schedule title has an entry, runtime format) abort the run before
    index.html is touched. A node --check step in the workflow is the
    final gate before any commit.

Safety rules (unchanged from v1):
  * Bad schedule parse -> ABORT, index.html untouched, workflow fails loudly.
  * Trailer/score lookups can NEVER abort or corrupt the run — any failure
    just means "keep the old value / use the fallback".
  * A real audience number is never overwritten with null.
  * Manually-set trailer links (watch?v=...) are never touched.
  * Structural assertion suite gates every write.
"""

import html
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import date, datetime

SCHEDULE_URL = "https://www.navymwrgreatlakes.com/programs/ed66a539-aa5c-44c5-9ae2-31c484dd5ab2"
DETAIL_URL = "https://www.navymwr.org/programs/motion-pictures?id={mid}"
INDEX_PATH = "index.html"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"}

MONTHS = {m: i + 1 for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June",
     "July", "August", "September", "October", "November", "December"])}
RATING_MAP = {"g": "G", "pg": "PG", "pg13": "PG-13", "r": "R", "nc17": "NC-17"}

STRUCTURAL_MARKERS = [
    "PLAY_ICON", "modal-hero", "card-actions", "pulseGlow", "sweep",
    ">i</button>", "no-cache, no-store, must-revalidate",
    'pragma" content="no-cache"', 'expires" content="0"',
    "modal-trailer-btn", "dateScroll", "const MOVIE_INFO", "const schedule",
]


# ──────────────────────────── plumbing ────────────────────────────

def http_get(url, timeout=60):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")


def minutes_to_runtime(mins):
    h, m = divmod(int(mins), 60)
    return f"{h}h {m}m"


def js_str(s):
    """Make ANY value safe inside a double-quoted, single-line JS string.
    Collapsing whitespace here makes multi-line output impossible no
    matter how badly upstream parsing goes wrong."""
    s = re.sub(r"\s+", " ", str(s)).strip()
    return s.replace("\\", "\\\\").replace('"', '\\"').replace("</", "<\\/")


def _norm(s):
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


LABELS = (r"(?:Rating|Rated|Genres?|Studio|Release Date|Released|Cast|"
          r"Synopsis|Runtime|Running Time|Show ?times?)")


def strip_tags(page):
    """HTML -> plain text, one element per line, entities unescaped."""
    t = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", page)
    t = re.sub(r"(?s)<[^>]+>", "\n", t)
    t = html.unescape(t)
    t = re.sub(r"[ \t\r\f\v ]+", " ", t)
    t = re.sub(r"\n\s*", "\n", t)
    return t


def clean_text(s, max_len=600):
    """Single line, no double quotes, sensible length (cut at a sentence)."""
    s = re.sub(r"\s+", " ", str(s)).strip().strip(" :;,–— ")
    s = s.replace('"', "'")
    if len(s) > max_len:
        cut = s[:max_len]
        dot = cut.rfind(". ")
        s = (cut[:dot + 1] if dot > 40 else cut).strip()
    return s


# ──────────────────────── MWR schedule parse ───────────────────────

def parse_schedule(page):
    """Parse the MWR showtimes page (raw HTML).

    v2.1: day association is anchored to each day's own listings table —
    the rows pairing a showtime with a motion-pictures?id= link that sit
    directly under each date header. The floating metadata blocks are used
    only as a lookup for title/type/rating/runtime, never for day placement.
    """
    text = html.unescape(page)

    date_pat = re.compile(
        r"(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\s*-\s*"
        r"(January|February|March|April|May|June|July|August|September|October|November|December)"
        r"\s+(\d{1,2}),\s+(\d{4})")
    dates = [(m.start(), m) for m in date_pat.finditer(text)]
    if len(dates) < 3:
        sys.exit(f"ABORT: only {len(dates)} date headers found - MWR layout may "
                 "have changed. index.html NOT modified.")

    meta_pat = re.compile(
        r'\{Key:\s*"id",\s*Value:\s*"([0-9a-fA-F-]{36})"\}.{0,600}?'
        r'\{Key:\s*"title",\s*Value:\s*"(.*?)"\}.{0,600}?'
        r'\{Key:\s*"type",\s*Value:\s*"(.*?)"\}.{0,600}?'
        r'\{Key:\s*"showTime",\s*Value:\s*"(.*?)"\}.{0,600}?'
        r'\{Key:\s*"rating",\s*Value:\s*"(.*?)"\}.{0,600}?'
        r'\{Key:\s*"runningTime",\s*Value:\s*"(\d+)"\}', re.S)
    meta = {}
    for m in meta_pat.finditer(text):
        mid, title, stype, showtime, rating, run_min = m.groups()
        t = re.match(r"(\d{1,2}:\d{2})\s*(AM|PM)", showtime.strip(), re.I)
        if t:
            meta[(mid.lower(), t.group(1), t.group(2).upper())] = {
                "title": title.strip(), "stype": stype,
                "rating": rating, "run_min": run_min}

    row_pat = re.compile(
        r'(\d{1,2}:\d{2})\s*(AM|PM).{0,600}?'
        r'motion-pictures\?id=([0-9a-fA-F-]{36})[^>]*>\s*([^<]{0,150})', re.S)

    days = []
    for idx, (dpos, dm) in enumerate(dates):
        seg_end = dates[idx + 1][0] if idx + 1 < len(dates) else len(text)
        seg = text[dm.end():seg_end]
        dow, mon, dom, yr = dm.group(1), dm.group(2), int(dm.group(3)), int(dm.group(4))
        day = {"date_obj": date(yr, MONTHS[mon], dom), "dow": dow[:3],
               "dom": str(dom), "month": MONTHS[mon], "shows": []}
        for rm in row_pat.finditer(seg):
            hhmm, ap = rm.group(1), rm.group(2).upper()
            mid, anchor_title = rm.group(3).lower(), (rm.group(4) or "").strip()
            mm = meta.get((mid, hhmm, ap))
            if mm:
                title, stype = mm["title"], mm["stype"]
                rating, run_min = mm["rating"], mm["run_min"]
            else:
                if not anchor_title:
                    continue
                title, stype = anchor_title, ""
                window = seg[rm.start():rm.end() + 300]
                rmt = re.search(r'ico-([a-z0-9]+)\.png', window)
                rating = rmt.group(1) if rmt else ""
                mmin = re.search(r'(\d{2,3})\s*min', window)
                run_min = mmin.group(1) if mmin else "120"
            su = stype.upper()
            day["shows"].append({
                "id": mid, "title": title,
                "time": hhmm, "period": ap,
                "rating": RATING_MAP.get(rating.lower(), rating.upper()),
                "runtime": minutes_to_runtime(run_min),
                "free": ("FREE" in su) or ("NDVD" in su) or ("ADVANCE" in su),
                "advance": "ADVANCE" in su,
            })
        days.append(day)

    result = [d for d in days if d["shows"]]
    total = sum(len(d["shows"]) for d in result)
    if len(result) < 3 or total < 4:
        sys.exit(f"ABORT: parsed only {len(result)} dates / {total} shows. "
                 "index.html NOT modified.")
    if len(meta) >= 4 and total < len(meta) / 2:
        sys.exit(f"ABORT: listing tables and metadata blocks disagree "
                 f"({total} rows vs {len(meta)} meta blocks) - layout change? "
                 "index.html NOT modified.")
    for d in result:
        seen, uniq = set(), []
        for s in sorted(d["shows"], key=lambda s: datetime.strptime(s["time"] + s["period"], "%I:%M%p")):
            k = (s["id"], s["time"], s["period"])
            if k not in seen:
                seen.add(k)
                uniq.append(s)
        d["shows"] = uniq
    return result


# ────────────────────── movie detail scraping ──────────────────────

def parse_detail(page, title):
    """v2.2: label-based parsing on tag-stripped text.

    Immune to markup changes leaking HTML into output: fields are read
    from plain text, forced to a single line, and anything unparsable
    falls back to a safe placeholder instead of garbage.
    """
    raw = page
    text = strip_tags(page)
    month_pat = "|".join(MONTHS)

    def field(label_pat, max_len=200):
        m = re.search(rf"(?im)^\s*(?:{label_pat})\s*:\s*(.*)$", text)
        if not m:
            return ""
        val = m.group(1).strip()
        if not val:  # value sits on the following line(s)
            for line in text[m.end():m.end() + 400].split("\n"):
                line = line.strip()
                if not line:
                    continue
                if re.match(rf"(?i)^{LABELS}\s*:?\s*$", line) or \
                   re.match(rf"(?i)^{LABELS}\s*:", line):
                    break
                val = line
                break
        val = re.split(rf"(?i)\b{LABELS}\s*:", val)[0]
        return clean_text(val, max_len)

    # poster \u2014 absolute, protocol-relative, or relative nmps-image URL
    poster = ""
    pm = re.search(r"(?i)(?:https?:)?(?://www\.navymwr\.org)?/?"
                   r"(nmps-image/[0-9A-Fa-f]+\.(?:jpe?g|png))", raw)
    if pm:
        poster = "https://www.navymwr.org/" + pm.group(1)

    # release date \u2014 accepts 04/01/2026 or "April 1, 2026"
    released = ""
    rel_raw = field(r"Release Date|Released", 80)
    m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", rel_raw)
    if m:
        mo, dd, yy = int(m.group(1)), int(m.group(2)), int(m.group(3))
        try:
            released = f"{date(yy, mo, dd):%b} {dd}, {yy}"
        except ValueError:
            pass
    else:
        m = re.search(rf"(?i)\b({month_pat})\.?\s+(\d{{1,2}}),?\s+(\d{{4}})", rel_raw)
        if m:
            released = f"{m.group(1)[:3].title()} {int(m.group(2))}, {m.group(3)}"

    # genre(s) \u2014 "Genres: A, B, C" or "Genre: Documentary Comedy"
    graw = field(r"Genres?", 120)
    parts = ([p.strip() for p in graw.split(",")] if "," in graw else graw.split())
    parts = [p for p in parts if re.fullmatch(r"[A-Za-z][A-Za-z\-]{1,19}", p)][:3]
    genre = " \u00b7 ".join(parts) if parts else "Feature"

    # studio
    studio = re.sub(r"[^A-Za-z0-9 &\-\.]", "", field(r"Studio", 60)).strip() or "\u2014"

    # cast \u2014 comma-separated on one line, or one name per line
    cast_names = []
    cm = re.search(r"(?i)\bCast\b\s*:?", text)
    if cm:
        for line in text[cm.end():cm.end() + 800].split("\n"):
            line = line.strip(" ,\u00b7")
            if not line:
                if cast_names:
                    break
                continue
            if re.match(rf"(?i)^{LABELS}\s*:?", line):
                break
            if "," in line:
                cast_names += [c.strip() for c in line.split(",") if c.strip()]
                break
            if not re.fullmatch(r"[A-Za-z\u00c0-\u024f'\u2019\. \-]{2,40}", line):
                break
            cast_names.append(line)
            if len(cast_names) >= 8:
                break
    cast = clean_text(", ".join(cast_names[:5]), 200) or "\u2014"

    # synopsis \u2014 first prose block after the Synopsis label
    syn = ""
    sm = re.search(r"(?i)\bSynopsis\b\s*:?", text)
    if sm:
        lines = []
        for line in text[sm.end():sm.end() + 2000].split("\n"):
            line = line.strip()
            if not line:
                if lines:
                    break
                continue
            if re.match(rf"(?i)^{LABELS}\s*:?", line):
                break
            lines.append(line)
            if sum(len(x) for x in lines) > 900:
                break
        syn = clean_text(" ".join(lines), 600)
    if len(syn) < 20:
        syn = f"{title} \u2014 now showing at the Epicenter Theater."

    det = {"poster": poster, "genre": genre, "studio": studio,
           "released": released or "\u2014", "synopsis": syn, "cast": cast}
    # hard sanitation: single line, never any angle brackets
    for k, v in det.items():
        v = re.sub(r"<[^>]*>", " ", str(v)).replace("<", " ").replace(">", " ")
        det[k] = re.sub(r"\s+", " ", v).strip()
    if not re.fullmatch(r"https://www\.navymwr\.org/nmps-image/"
                        r"[0-9A-Fa-f]+\.(?:jpe?g|png)", det["poster"]):
        det["poster"] = ""
    if len(det["synopsis"]) < 20:
        det["synopsis"] = f"{title} \u2014 now showing at the Epicenter Theater."
    return det


# ─────────────────── trailer + audience resolvers ──────────────────

def find_trailer(title):
    """Best-effort real YouTube trailer URL, else None (caller falls back)."""
    q = urllib.parse.quote(f"{title} official trailer")
    cands = []
    key = os.environ.get("YOUTUBE_API_KEY", "").strip()
    if key:
        try:
            data = json.loads(http_get(
                "https://www.googleapis.com/youtube/v3/search?part=snippet"
                f"&type=video&maxResults=8&q={q}&key={key}"))
            cands = [(i["id"]["videoId"], i["snippet"]["title"])
                     for i in data.get("items", []) if "videoId" in i.get("id", {})]
        except Exception as e:
            print(f"  [trailer] youtube api failed for {title!r}: {e}")
    if not cands:
        try:
            page = http_get(f"https://www.youtube.com/results?search_query={q}")
            cands = re.findall(
                r'"videoRenderer":\{"videoId":"([\w-]{6,20})".{0,700}?'
                r'"title":\{"runs":\[\{"text":"(.*?)"\}', page, re.S)[:8]
        except Exception as e:
            print(f"  [trailer] youtube scrape failed for {title!r}: {e}")
            return None
    officials = [v for v, t in cands if "trailer" in t.lower() and "official" in t.lower()]
    any_tr = [v for v, t in cands if "trailer" in t.lower()]
    vid = (officials or any_tr or [None])[0]
    if vid:
        print(f"  [trailer] {title!r} -> https://www.youtube.com/watch?v={vid}")
        return f"https://www.youtube.com/watch?v={vid}"
    print(f"  [trailer] no confident match for {title!r}")
    return None


def find_rt_audience(title, year=None):
    """Rotten Tomatoes AUDIENCE (Popcornmeter) score as int, else None."""
    try:
        page = http_get("https://www.rottentomatoes.com/search?search="
                        + urllib.parse.quote(title))
    except Exception as e:
        print(f"  [rt] search failed for {title!r}: {e}")
        return None

    cands = re.findall(
        r'"name"\s*:\s*"((?:[^"\\]|\\.)+)"[^{}]*?'
        r'"url"\s*:\s*"(https://www\.rottentomatoes\.com/m/[^"]+)"'
        r'[^{}]*?(?:"releaseYear"\s*:\s*"?(\d{4}))?', page)
    tn = _norm(title)
    url = None
    for name, u, yr in cands:
        try:
            name = name.encode().decode("unicode_escape")
        except Exception:
            pass
        if _norm(name) == tn and (year is None or not yr or abs(int(yr) - year) <= 1):
            url = u
            break
    if not url:
        for name, u, yr in cands:
            if _norm(name).startswith(tn) or tn.startswith(_norm(name)):
                url = u
                break
    if not url:
        print(f"  [rt] no confident match for {title!r}")
        return None
    try:
        mp = http_get(url)
    except Exception as e:
        print(f"  [rt] page fetch failed {url}: {e}")
        return None
    for pat in (r'"audienceScore"\s*:\s*\{[^{}]*?"score"\s*:\s*"?(\d{1,3})',
                r'audiencescore="(\d{1,3})"',
                r'"popcornMeter(?:Score)?"[^0-9]{0,60}?(\d{1,3})'):
        m = re.search(pat, mp)
        if m and 0 <= int(m.group(1)) <= 100:
            print(f"  [rt] {title!r} -> {m.group(1)}% ({url})")
            return int(m.group(1))
    print(f"  [rt] page found but no audience score yet for {title!r}")
    return None


# ───────────────────── index.html manipulation ─────────────────────

def extract_block(content, anchor, open_ch, close_ch):
    start = content.index(anchor)
    i = content.index(open_ch, start) + 1
    depth = 1
    while depth:
        if content[i] == open_ch:
            depth += 1
        elif content[i] == close_ch:
            depth -= 1
        i += 1
    if i < len(content) and content[i] == ";":
        i += 1
    return start, i


def extract_movie_entries(info_block):
    body = info_block[info_block.index("{") + 1:info_block.rindex("}")]
    entries, pos = {}, 0
    pat = re.compile(r'"((?:[^"\\]|\\.)+)":\s*\{')
    while True:
        m = pat.search(body, pos)
        if not m:
            break
        depth, i = 1, m.end()
        while depth:
            if body[i] == "{":
                depth += 1
            elif body[i] == "}":
                depth -= 1
            i += 1
        entries[m.group(1)] = body[m.start():i]
        pos = i
    return entries


def entry_year(entry):
    m = re.search(r'released:\s*"[^"]*?(\d{4})"', entry)
    return int(m.group(1)) if m else None


def refresh_entry(entry, title):
    """Upgrade placeholder trailer + refresh audience score. Never degrades."""
    changed = False
    if "results?search_query" in entry:
        t = find_trailer(title)
        if t:
            entry = re.sub(r'trailer:\s*"[^"]*"', f'trailer: "{t}"', entry, count=1)
            changed = True
        time.sleep(1)
    score = find_rt_audience(title, entry_year(entry))
    time.sleep(1)
    m = re.search(r'audience:\s*(null|\d+)', entry)
    if score is not None and m and m.group(1) != str(score):
        entry = re.sub(r'audience:\s*(null|\d+)', f'audience: {score}', entry, count=1)
        changed = True
    return entry, changed


def new_movie_entry(title, det, rating, runtime):
    trailer = None
    try:
        trailer = find_trailer(title)
    except Exception as e:
        print(f"  [trailer] unexpected error for {title!r}: {e}")
    if not trailer:
        trailer = ("https://www.youtube.com/results?search_query="
                   + urllib.parse.quote(f"{title} official trailer"))
    audience = None
    try:
        audience = find_rt_audience(title, entry_year(f'released: "{det["released"]}"'))
    except Exception as e:
        print(f"  [rt] unexpected error for {title!r}: {e}")
    aud_js = str(audience) if audience is not None else "null"
    return (f'"{js_str(title)}": {{\n'
            f'    poster: "{js_str(det["poster"])}",\n'
            f'    rating: "{js_str(rating)}", runtime: "{js_str(runtime)}", genre: "{js_str(det["genre"])}",\n'
            f'    studio: "{js_str(det["studio"])}", released: "{js_str(det["released"])}",\n'
            f'    synopsis: "{js_str(det["synopsis"])}",\n'
            f'    cast: "{js_str(det["cast"])}",\n'
            f'    trailer: "{js_str(trailer)}",\n'
            f'    audience: {aud_js}\n'
            f'  }}'), audience is None


def build_schedule_js(days):
    out = ["const schedule = ["]
    for i, d in enumerate(days):
        label = f"{d['dow']}, {d['date_obj']:%b} {int(d['dom'])}"
        out.append(f'  {{ date: "{label}", dom: "{d["dom"]}", dow: "{d["dow"]}", '
                   f'month: {d["month"]}, shows: [')
        rows = []
        for s in d["shows"]:
            extra = ", free: true" if s["free"] else ", free: false"
            if s["advance"]:
                extra += ", advance: true"
            rows.append(f'    {{ time: "{s["time"]}", period: "{s["period"]}", '
                        f'title: "{js_str(s["title"])}", runtime: "{s["runtime"]}", '
                        f'rating: "{s["rating"]}"{extra} }}')
        out.append(",\n".join(rows))
        out.append("  ]}" + ("," if i < len(days) - 1 else ""))
    out.append("];")
    return "\n".join(out)


# ─────────────────────────────── main ──────────────────────────────

def main():
    content = open(INDEX_PATH, encoding="utf-8").read()
    original = content

    days = parse_schedule(http_get(SCHEDULE_URL))
    new_sched_js = build_schedule_js(days)

    s0, s1 = extract_block(content, "const schedule = [", "[", "]")
    schedule_changed = (re.sub(r"\s+", " ", content[s0:s1])
                        != re.sub(r"\s+", " ", new_sched_js))

    sched_movies = {}
    for d in days:
        for s in d["shows"]:
            sched_movies.setdefault(s["title"], {"id": s["id"], "rating": s["rating"],
                                                 "runtime": s["runtime"]})

    m0, m1 = extract_block(content, "const MOVIE_INFO = {", "{", "}")
    existing = extract_movie_entries(content[m0:m1])

    kept, needs_review, any_entry_changed = [], [], False
    for title, meta in sched_movies.items():
        if title in existing:
            try:
                entry, ch = refresh_entry(existing[title], title)
            except Exception as e:
                print(f"  refresh failed for {title!r} (keeping as-is): {e}")
                entry, ch = existing[title], False
            kept.append(entry)
            any_entry_changed |= ch
        else:
            det = parse_detail(http_get(DETAIL_URL.format(mid=meta["id"])), title)
            entry, unresolved = new_movie_entry(title, det, meta["rating"], meta["runtime"])
            kept.append(entry)
            any_entry_changed = True
            print(f"NEW MOVIE added: {title}")
            if unresolved:
                needs_review.append(title)

    if not schedule_changed and not any_entry_changed:
        print("No schedule changes and no trailer/score updates. Nothing to do.")
        return

    new_info_js = "const MOVIE_INFO = {\n  " + ",\n  ".join(kept) + "\n};"
    content = content[:m0] + new_info_js + content[m1:]
    s0, s1 = extract_block(content, "const schedule = [", "[", "]")
    content = content[:s0] + new_sched_js + content[s1:]

    if schedule_changed:
        today = date.today()
        content = re.sub(r"Schedule updated [A-Za-z]{3,9} \d{1,2}, \d{4}",
                         f"Schedule updated {today:%b} {today.day}, {today.year}",
                         content, count=1)

    problems = [mk for mk in STRUCTURAL_MARKERS if mk not in content]
    if problems:
        sys.exit(f"ABORT: structural markers missing after rewrite: {problems}. "
                 "index.html NOT modified.")
    if abs(len(content) - len(original)) > len(original) * 0.5:
        sys.exit("ABORT: output size changed suspiciously. index.html NOT modified.")
    for blk, o, c_ in (("MOVIE_INFO = {", "{", "}"), ("schedule = [", "[", "]")):
        b0, b1 = extract_block(content, "const " + blk, o, c_)
        seg = content[b0:b1]
        if seg.count(o) != seg.count(c_):
            sys.exit(f"ABORT: unbalanced {blk[:-4]} block. index.html NOT modified.")

    # ── v2.2 value-level gates (the 2026-07-20 failure could not pass these) ──
    m0b, m1b = extract_block(content, "const MOVIE_INFO = {", "{", "}")
    info_block = content[m0b:m1b]
    for ln in info_block.split("\n"):
        if len(re.findall(r'(?<!\\)"', ln)) % 2:
            sys.exit("ABORT: unbalanced quotes in MOVIE_INFO (multi-line string?). "
                     "index.html NOT modified.")
    if "</" in info_block.replace("<\\/", "") or "<script" in info_block.lower():
        sys.exit("ABORT: HTML fragments inside MOVIE_INFO strings. "
                 "index.html NOT modified.")
    entry_keys = set(extract_movie_entries(info_block))
    missing = [t for t in sched_movies
               if t not in entry_keys and js_str(t) not in entry_keys]
    if missing:
        sys.exit(f"ABORT: schedule titles without MOVIE_INFO entry: {missing}. "
                 "index.html NOT modified.")
    bad_rt = [r for r in re.findall(r'runtime: "([^"]*)"', content)
              if not re.fullmatch(r"\d+h \d+m", r)]
    if bad_rt:
        sys.exit(f"ABORT: malformed runtimes {bad_rt}. index.html NOT modified.")

    open(INDEX_PATH, "w", encoding="utf-8").write(content)
    print(f"index.html updated: {len(days)} dates, "
          f"{sum(len(d['shows']) for d in days)} showtimes, "
          f"schedule_changed={schedule_changed}.")
    if needs_review:
        print("MANUAL FOLLOW-UP (trailer/score not resolved): " + ", ".join(needs_review))


if __name__ == "__main__":
    main()
