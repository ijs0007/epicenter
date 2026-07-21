#!/usr/bin/env python3
"""Offline unit tests for update_schedule.py v2.2 — no network calls.

Feeds parse_detail fabricated pages in BOTH the current navymwr.org
detail-page shape (labels like "Genres:", prose release dates, cast one
per line) and hostile shapes (synopsis running into closing-tag soup —
the exact 2026-07-20 failure), and asserts every output field is a safe
single line. Also tests js_str hardening and the quote-parity gate idea.
"""
import re
import sys
import importlib.util

spec = importlib.util.spec_from_file_location(
    "upd", __file__.replace("test_updater_offline.py", "update_schedule.py"))
upd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(upd)

fails = []


def check(name, cond, extra=""):
    print(("PASS" if cond else "FAIL"), "-", name, extra if not cond else "")
    if not cond:
        fails.append(name)


# ── fixture 1: current-style detail page (like Supergirl today) ──
PAGE_NEW_STYLE = """
<html><body><div class="hero">
<img src="/nmps-image/B6CFC2F5BF4B5D4425986ACA82BA20EAF8725ECB.jpg" alt="poster">
<h1>Supergirl</h1>
<div><span>Rating:</span> PG-13 (for sequences of strong violence, action, language, and smoking)</div>
<div><span>Runtime:</span> 110 minutes</div>
<div><span>Genres:</span> Action, Adventure, Drama, Sci-Fi</div>
<div><span>Studio:</span> Warner Brothers</div>
<div><span>Release Date:</span> June 26, 2026</div>
<h3>Synopsis</h3>
<p>Kara Zor-El, aka Supergirl, joins forces with an unlikely companion on an
interstellar journey of vengeance and justice when an unexpected adversary
strikes too close to home.</p>
<h3>Cast</h3>
<ul><li>Milly Alcock</li><li>Eve Ridley</li><li>Matthias Schoenaerts</li>
<li>Jason Momoa</li><li>David Corenswet</li></ul>
</div>
<script src='/_templates/junk.js?v=123' defer=''></script>
</body></html>
"""

d = upd.parse_detail(PAGE_NEW_STYLE, "Supergirl")
check("poster resolved from relative URL",
      d["poster"] == "https://www.navymwr.org/nmps-image/B6CFC2F5BF4B5D4425986ACA82BA20EAF8725ECB.jpg", str(d))
check("genre parsed (first three)", d["genre"] == "Action · Adventure · Drama", d["genre"])
check("studio parsed", d["studio"] == "Warner Brothers", d["studio"])
check("prose release date parsed", d["released"] == "Jun 26, 2026", d["released"])
check("synopsis clean prose", d["synopsis"].startswith("Kara Zor-El") and
      "script" not in d["synopsis"] and len(d["synopsis"]) < 400, d["synopsis"][:80])
check("cast one-per-line incl. single-word names",
      d["cast"] == "Milly Alcock, Eve Ridley, Matthias Schoenaerts, Jason Momoa, David Corenswet", d["cast"])

# ── fixture 2: old-style page (numeric date, singular Genre inline) ──
PAGE_OLD_STYLE = """
<html><body>
<img src="https://www.navymwr.org/nmps-image/AA12BB34CC56.jpg">
<p>Release Date: 04/01/2026</p>
<p>Genre: Documentary Comedy</p><p>Studio: Paramount</p>
<p>Cast: Johnny Knoxville, Steve-O, Chris Pontius, Jason 'Wee Man' Acuna, Preston Lacy</p>
<h3>Synopsis</h3><p>Follows the crew as they perform one final series of
dangerous stunts and pranks, marking the end of the franchise.</p>
</body></html>
"""
d2 = upd.parse_detail(PAGE_OLD_STYLE, "Jackass: Best and Last")
check("absolute poster URL kept", d2["poster"].endswith("AA12BB34CC56.jpg"), d2["poster"])
check("numeric release date parsed", d2["released"] == "Apr 1, 2026", d2["released"])
check("space-separated genre", d2["genre"] == "Documentary · Comedy", d2["genre"])
check("comma cast keeps Steve-O and nickname",
      "Steve-O" in d2["cast"] and "Wee Man" in d2["cast"], d2["cast"])

# ── fixture 3: hostile page — the 2026-07-20 failure shape ──
PAGE_HOSTILE = """
<html><body><div><h3>Synopsis</h3>
Mario ventures into space, exploring cosmic worlds.
\t\t\t\t\t\t            </div>
\t\t\t\t\t            </div>
\t\t\t\t\t\t    </div>

<script src='/_templates/components/global/js/commonexternallinks/x.js?v=1' type='text/javascript' defer=''></script>
</body></html>
"""
d3 = upd.parse_detail(PAGE_HOSTILE, "The Super Mario Galaxy Movie")
bad = any(("\n" in v) or ("<" in v) or (">" in v) or ("script" in v.lower() and k == "synopsis")
          for k, v in d3.items())
check("hostile page yields only clean single-line fields", not bad, str(d3))
check("hostile synopsis is the real sentence",
      d3["synopsis"].startswith("Mario ventures into space"), d3["synopsis"])
check("missing fields fall back safely",
      d3["poster"] == "" and d3["studio"] == "—" and d3["released"] == "—", str(d3))

# ── js_str hardening ──
evil = 'line one\n\t\t</div>\n</html> and a "quote" \\ backslash'
out = upd.js_str(evil)
check("js_str output is single-line", "\n" not in out and "\r" not in out, out)
check("js_str escapes quotes/backslashes/closing tags",
      '\\"' in out and "\\\\" in out and "</" not in out.replace("<\\/", ""), out)

# ── entry generation from hostile detail can never break a JS literal ──
entry_txt = (f'"T": {{\n    poster: "{upd.js_str(d3["poster"])}",\n'
             f'    synopsis: "{upd.js_str(evil)}",\n    audience: null\n  }}')
for ln in entry_txt.split("\n"):
    if len(re.findall(r'(?<!\\)"', ln)) % 2:
        check("quote parity per generated line", False, ln)
        break
else:
    check("quote parity per generated line", True)

# ── the July 20 broken block would be caught by the new gate ──
JULY20_BROKEN = '''const MOVIE_INFO = {
  "X": {
    synopsis: "Mario ventures into space.
\t\t</div>
</html></p>",
    audience: null
  }
};'''
caught = any(len(re.findall(r'(?<!\\)"', ln)) % 2 for ln in JULY20_BROKEN.split("\n"))
check("gate catches the actual 2026-07-20 corruption", caught)

print()
if fails:
    sys.exit(f"{len(fails)} TEST(S) FAILED: {fails}")
print("ALL OFFLINE TESTS PASSED")
