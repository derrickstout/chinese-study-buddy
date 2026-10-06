"""Render a Pleco snapshot as a static dashboard.html (HTML + CSS only, no JavaScript).

Used by pleco_import.py, or run on its own:
    python3 make_dashboard.py pleco-snapshot-123.json dashboard.html
"""
import json, sys, time
from collections import Counter, defaultdict
from datetime import datetime
from html import escape
from pathlib import Path

CSS = """
:root{--bg:#eef1f2;--ink:#1b2a34;--mut:#5f6f78;--line:#d3dadd;--m:#2c7a62;--l:#d9a21b}
@media (prefers-color-scheme:dark){:root{--bg:#12191d;--ink:#e6ecee;--mut:#8b9aa2;--line:#27343a;--m:#4fb593;--l:#e6b840}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.55 -apple-system,BlinkMacSystemFont,"Helvetica Neue",sans-serif}
main{max-width:980px;margin:0 auto;padding:28px 18px 56px}
h1,.han{font-family:"Songti SC","STSong","Noto Serif CJK SC",serif}
h1{font-size:28px;line-height:1.2;margin:0 0 4px}
h2{font-size:16px;margin:0 0 10px}
header{margin-bottom:28px}
section{margin-bottom:36px}
.mut{color:var(--mut)}.num{font-variant-numeric:tabular-nums}
.lead{font-size:19px;margin:0 0 12px}
.bar{display:flex;height:10px;border-radius:5px;overflow:hidden;background:var(--line)}
.bar.big{height:16px;border-radius:8px}
.cols{display:grid;grid-template-columns:1fr 1fr;gap:40px}
@media (max-width:720px){.cols{grid-template-columns:1fr;gap:28px}}
.row{display:grid;grid-template-columns:96px 1fr 64px;gap:10px;align-items:center;margin:8px 0}
.row .num{text-align:right}
.act{display:flex;align-items:flex-end;gap:3px;height:96px}
.act i{flex:1;background:var(--m);border-radius:2px 2px 0 0;min-height:2px}
.axis{display:flex;justify-content:space-between;font-size:13px;margin-top:4px}
ul{list-style:none;margin:0;padding:0}
li{display:grid;grid-template-columns:40px 1fr auto;gap:12px;align-items:center;padding:7px 0;border-bottom:1px solid var(--line)}
li .han{font-size:26px;line-height:1}
.gl{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
"""

PAGE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Chinese Character Study Progress</title>
<style>{css}</style></head>
<body><main>
{body}
</main></body></html>
"""


def ymd(ts):
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")


def bar(m, l, total, big=False):
    total = total or 1
    return (f'<div class="bar{" big" if big else ""}">'
            f'<i style="width:{m / total * 100:.1f}%;background:var(--m)"></i>'
            f'<i style="width:{l / total * 100:.1f}%;background:var(--l)"></i></div>')


def hsk_name(k):
    return "No HSK level" if k is None else "HSK 7-9" if k >= 7 else f"HSK {k}"


def render(snap):
    chars = snap["chars"]
    exp = snap.get("exported") or int(time.time())
    tot = Counter(r["status"] for r in chars.values())
    m, l, u, n = tot["mastered"], tot["learning"], tot["unknown"], len(chars)
    week = sum(1 for r in chars.values() if r["last"] >= exp - 7 * 86400)

    by_hsk = defaultdict(Counter)
    for r in chars.values():
        by_hsk[r.get("hsk")][r["status"]] += 1
    hsk_rows = "".join(
        f'<div class="row"><span>{hsk_name(k)}</span>'
        f'{bar(c["mastered"], c["learning"], sum(c.values()))}'
        f'<span class="mut num">{c["mastered"]}/{sum(c.values())}</span></div>'
        for k, c in sorted(by_hsk.items(), key=lambda kv: (kv[0] is None, kv[0] or 0)))

    days = [ymd(exp - i * 86400) for i in range(29, -1, -1)]
    per = dict.fromkeys(days, 0)
    for r in chars.values():
        if r["last"] and ymd(r["last"]) in per:
            per[ymd(r["last"])] += 1
    mx = max(1, max(per.values()))
    act = "".join(f'<i title="{d}: {c}" style="height:{c / mx * 100:.0f}%"></i>'
                  for d, c in per.items())

    weak = sorted(((ch, r) for ch, r in chars.items() if r["incorrect"] > 0),
                  key=lambda kv: (-kv[1]["incorrect"], kv[1]["score"]))[:10]
    weak_html = "".join(
        f'<li><span class="han">{escape(r.get("trad") or ch)}</span>'
        f'<span class="gl"><b>{escape(r["pinyin"])}</b> {escape(r["gloss"])}</span>'
        f'<span class="mut num">missed {r["incorrect"]} of {r["reviewed"]}</span></li>'
        for ch, r in weak) or '<li class="mut">No missed reviews in this snapshot.</li>'

    body = f"""<header><h1>Character progress</h1>
<div class="mut">Snapshot from {datetime.fromtimestamp(exp).strftime("%b %d, %Y")}</div></header>
<section><p class="lead"><b>{m:,}</b> of {n:,} characters mastered, {l:,} in progress, {u:,} not started.</p>
{bar(m, l, n, big=True)}
<p class="mut">{week} characters reviewed in the week before this export.</p></section>
<div class="cols">
<section><h2>Mastered by HSK level</h2>{hsk_rows}</section>
<section><h2>Characters reviewed per day</h2><div class="act">{act}</div>
<div class="axis mut"><span>{days[0]}</span><span>{days[-1]}</span></div>
<p class="mut">Counts each character on the date of its latest review, over the 30 days before the export.</p></section>
</div>
<section><h2>Most missed</h2><ul>{weak_html}</ul></section>"""
    return PAGE.format(css=CSS, body=body)


def write_dashboard(snap, out_path):
    Path(out_path).write_text(render(snap), encoding="utf-8")


if __name__ == "__main__":
    snap = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    write_dashboard(snap, sys.argv[2])
    print("wrote", sys.argv[2])
