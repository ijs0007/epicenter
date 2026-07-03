#!/usr/bin/env python3
"""
Epicenter Theater — weekly schedule auto-updater (v2).

v2 additions:
  * Auto-resolves REAL YouTube trailer links for new movies (scrapes YouTube
    search results; uses the official YouTube Data API instead if a
    YOUTUBE_API_KEY repo secret is configured).
  * Auto-fetches Rotten Tomatoes AUDIENCE (Popcornmeter) scores for every
    scheduled movie on every run — fills in nulls once scores appear, and
    keeps settling scores current.
  * Upgrades any placeholder "youtube.com/results?search_query=" trailer
    links left by earlier runs to real watch links when it can find one.

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
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _norm(s):
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


# ──────────────────────── MWR schedule parse ───────────────────────

def parse_schedule(page):
    text = html.unescape(page)
    date_pat = re.compile(
        r"(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\s*-\s*"
        r"(January|February|March|April|May|June|July|August|September|October|November|December)"
        r"\s+(\d{1,2}),\s+(\d{4})")
    dates = [(m.start(), m) for m in date_pat.finditer(text)]

    show_pat = re.compile(
        r'\{Key:\s*"id",\s*Value:\s*"([0-9a-fA-F-]{36})"\}.{0,400}?'
        r'\{Key:\s*"title",\s*Value:\s*"(.*?)"\}.{0,400}?'
        r'\{Key:\s*"type",\s*Value:\s*"(.*?)"\}.{0,400}?'
        r'\{Key:\s*"showTime",\s*Value:\s*"(.*?)"\}.{0,400}?'
        r'\{Key:\s*"rating",\s*Value:\s*"(.*?)"\}.{0,400}?'
        r'\{Key:\s*"runningTime",\s*Value:\s*"(\d+)"\}', re.S)
    shows = [(m.start(), m) for m in show_pat.finditer(text)]

    if len(dates) < 3 or len(shows) < 4:
        sys.exit(f"ABORT: parser found only {len(dates)} dates / {len(shows)} shows — "
                 "MWR page layout may have changed. index.html NOT modified.")

    days, order = {}, []
    for spos, sm in shows:
        nxt = next((dm for dpos, dm in dates if dpos > spos), None)
        if nxt is None:
            continue
        key = nxt.start()
        if key not in days:
            dow, mon, dom, yr = nxt.group(1), nxt.group(2), int(nxt.group(3)), int(nxt.group(4))
            days[key] = {"date_obj": date(yr, MONTHS[mon], dom), "dow": dow[:3],
                         "dom": str(dom), "month": MONTHS[mon], "shows": []}
            order.append(key)
        mid, title, stype, showtime, rating, run_min = (sm.group(i) for i in range(1, 7))
        t = re.match(r"(\d{1,2}:\d{2})\s*(AM|PM)", showtime.strip(), re.I)
        if not t:
            continue
        su = stype.upper()
        days[key]["shows"].append({
            "id": mid, "title": title.strip(),
            "time": t.group(1), "period": t.group(2).upper(),
            "rating": RATING_MAP.get(rating.lower(), rating.upper()),
            "runtime": minutes_to_runtime(run_min),
            "free": ("FREE" in su) or ("NDVD" in su) or ("ADVANCE" in su),
            "advance": "ADVANCE" in su,
        })

    result = [days[k] for k in order if days[k]["shows"]]
    if len(result) < 3:
        sys.exit("ABORT: fewer than 3 populated dates after parsing. index.html NOT modified.")
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
    text = html.unescape(page)

    def grab(pat, default=""):
        m = re.search(pat, text, re.S)
        return m.group(1).strip() if m else default

    poster = grab(r'(https://www\.navymwr\.org/nmps-image/[0-9A-Fa-f]+\.jpg)')
    released = ""
    m = re.search(r'(\d{2})/(\d{2})/(\d{4})',
                  grab(r'Release Date:\s*<?/?[^>]*>?\s*([\d/]+)') or "")
    if m:
        mo, dd, yy = int(m.group(1)), int(m.group(2)), int(m.group(3))
        released = f"{date(yy, mo, dd):%b} {dd}, {yy}"
    genre_raw = grab(r'Genre:\s*<?/?[^>]*>?\s*([A-Za-z \-]+?)\s*(?:Studio:|<)')
    genre = " \u00b7 ".join(genre_raw.split()[:3]) if genre_raw else "Feature"
    studio = grab(r'Studio:\s*<?/?[^>]*>?\s*([A-Za-z0-9 &\-\.]+?)\s*(?:Cast:|<)') or "\u2014"
    cast_raw = grab(r'Cast:\s*(.*?)(?:###|Synopsis|<h3)', "")
    cast = ", ".join(re.findall(r"[A-Z][\w'\.\-]+(?: [A-Z][\w'\.\-]+)+", cast_raw)[:5]) or "\u2014"
    synopsis = (grab(r'Synopsis\s*</?h?3?>?\s*(.{20,600}?)(?:\n\n|###|<h3)')
                .replace('"', "'").strip()
                or f"{title} \u2014 now showing at the Epicenter Theater.")
    return {"poster": poster, "genre": genre, "studio": studio,
            "released": released or "\u2014", "synopsis": synopsis, "cast": cast}


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
            f'    poster: "{det["poster"]}",\n'
            f'    rating: "{rating}", runtime: "{runtime}", genre: "{js_str(det["genre"])}",\n'
            f'    studio: "{js_str(det["studio"])}", released: "{det["released"]}",\n'
            f'    synopsis: "{js_str(det["synopsis"])}",\n'
            f'    cast: "{js_str(det["cast"])}",\n'
            f'    trailer: "{trailer}",\n'
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

    open(INDEX_PATH, "w", encoding="utf-8").write(content)
    print(f"index.html updated: {len(days)} dates, "
          f"{sum(len(d['shows']) for d in days)} showtimes, "
          f"schedule_changed={schedule_changed}.")
    if needs_review:
        print("MANUAL FOLLOW-UP (trailer/score not resolved): " + ", ".join(needs_review))


if __name__ == "__main__":
    main()
