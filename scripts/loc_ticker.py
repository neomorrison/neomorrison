#!/usr/bin/env python3
"""
LOC ticker: total lines of code across every repo you own (public + private),
replayed from git history and rendered as a stock-chart SVG.

  python scripts/loc_ticker.py collect   # clone/refresh repos, update cache + history
  python scripts/loc_ticker.py render    # history -> assets/loc-ticker-{dark,light}.svg
  python scripts/loc_ticker.py all       # both

Env:
  GH_TOKEN     token that can read your private repos (falls back to `gh auth token`)
  GH_OWNER     account to chart (default: the token's user)
  LOC_CACHE    per-repo cache dir (default: .loc-cache, never committed: it
               holds private repo names)
Only aggregated, anonymous daily totals are written to data/loc-history.json.
"""
import base64, datetime as dt, json, os, subprocess, sys, urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HISTORY = ROOT / "data" / "loc-history.json"
ASSETS = ROOT / "assets"
CACHE = Path(os.environ.get("LOC_CACHE", ROOT / ".loc-cache"))
CACHE_VERSION = 3

# extension -> language bucket. Only these count as "code".
LANGS = {
    "TypeScript": ["ts", "tsx", "mts", "cts"],
    "JavaScript": ["js", "jsx", "mjs", "cjs"],
    "Python": ["py"],
    "HTML": ["html", "htm"],
    "CSS": ["css", "scss", "sass", "less"],
    "C/C++": ["c", "h", "cpp", "hpp", "cc", "cxx", "hh"],
    "C#": ["cs"],
    "Java/Kotlin": ["java", "kt", "kts"],
    "Go": ["go"],
    "Rust": ["rs"],
    "Swift": ["swift"],
    "Shell": ["sh", "bash", "ps1", "psm1", "bat", "cmd"],
    "SQL": ["sql"],
    "Lua": ["lua"],
    "Svelte/Vue": ["svelte", "vue", "astro"],
    "Game scripts": ["nut", "gsc", "csc", "gd", "sp", "inc"],
    "GLSL": ["glsl", "vert", "frag", "wgsl", "hlsl", "shader"],
    "PHP/Ruby": ["php", "rb"],
}
EXT2LANG = {e: l for l, es in LANGS.items() for e in es}

EXCLUDE_DIRS = ["node_modules", "dist", "build", "out", ".next", "vendor",
                "third_party", "coverage", "__pycache__", ".venv", "venv",
                "public/vendor", "lib/vendor", "Library", "Temp"]
EXCLUDE_GLOBS = ["*.min.js", "*.min.css", "*.bundle.js", "*.chunk.js",
                 "*-lock.*", "*.lock", "*.map", "*.d.ts", "*.generated.*"]
# a single file change bigger than this is a paste/vendor/generated blob, not typing
MAX_FILE_DELTA = 20000


def pathspecs():
    s = [f":(glob)**/*.{e}" for e in EXT2LANG]
    s += [f":(exclude,glob)**/{d}/**" for d in EXCLUDE_DIRS]
    s += [f":(exclude,glob)**/{g}" for g in EXCLUDE_GLOBS]
    return s


def token():
    t = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if t:
        return t
    return subprocess.check_output(["gh", "auth", "token"], text=True).strip()


def api(path, tok):
    out, url = [], f"https://api.github.com{path}"
    while url:
        req = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {tok}", "Accept": "application/vnd.github+json",
            "User-Agent": "loc-ticker"})
        with urllib.request.urlopen(req) as r:
            data = json.load(r)
            out += data if isinstance(data, list) else [data]
            link = r.headers.get("Link", "")
            url = next((p.split(";")[0].strip("<> ") for p in link.split(",")
                        if 'rel="next"' in p), None)
    return out


def git(*args, cwd=None, auth=None):
    pre = ["git"]
    if auth:
        pre += ["-c", f"http.extraHeader=Authorization: Basic {auth}"]
    return subprocess.run(pre + list(args), cwd=cwd, check=True, capture_output=True,
                          text=True, encoding="utf-8", errors="replace").stdout


def scan_repo(repo, auth):
    """Replay a repo's history -> {day: {lang: net}}, {day: [adds, dels]}, commits."""
    d = CACHE / "git" / (repo["id_key"] + ".git")
    url = repo["clone_url"]
    if d.exists():
        git("fetch", "--quiet", "--force", "origin",
            "+refs/heads/*:refs/heads/*", cwd=d, auth=auth)
    else:
        d.parent.mkdir(parents=True, exist_ok=True)
        git("clone", "--quiet", "--bare", "--filter=blob:none", url, str(d), auth=auth)
    branch = repo["default_branch"]
    try:
        git("rev-parse", "--verify", f"refs/heads/{branch}", cwd=d)
    except subprocess.CalledProcessError:
        return None  # empty repo
    log = git("-c", "core.quotepath=off", "log", f"refs/heads/{branch}", "--numstat",
              "--no-renames", "--format=@%at", "--", *pathspecs(),
              *[f":(exclude,glob){p}/**" for p in repo["skip_paths"]], cwd=d, auth=auth)
    net = defaultdict(lambda: defaultdict(int))
    vol = defaultdict(lambda: [0, 0])
    commits, day = 0, None
    for line in log.splitlines():
        if line.startswith("@"):
            day = dt.datetime.fromtimestamp(int(line[1:]), dt.timezone.utc).date().isoformat()
            commits += 1
            continue
        parts = line.split("\t")
        if len(parts) != 3 or parts[0] == "-":
            continue
        a, r, path = int(parts[0]), int(parts[1]), parts[2]
        if a > MAX_FILE_DELTA or r > MAX_FILE_DELTA:
            continue
        lang = EXT2LANG.get(path.rsplit(".", 1)[-1].lower())
        if not lang:
            continue
        net[day][lang] += a - r
        vol[day][0] += a
        vol[day][1] += r
    return {"net": {k: dict(v) for k, v in net.items()}, "vol": dict(vol), "commits": commits}


def collect():
    tok = token()
    owner = os.environ.get("GH_OWNER") or api("/user", tok)[0]["login"]
    auth = base64.b64encode(f"x-access-token:{tok}".encode()).decode()
    repos = [r for r in api("/user/repos?per_page=100&affiliation=owner", tok)
             if r["owner"]["login"].lower() == owner.lower() and not r["fork"]]
    idx_path = CACHE / "index.json"
    idx = json.loads(idx_path.read_text()) if idx_path.exists() else {}
    if idx.get("version") != CACHE_VERSION:
        idx = {"version": CACHE_VERSION, "repos": {}}
    import hashlib
    # LOC_EXCLUDE="repo,repo:path/dir,..." keeps whole repos or folders out of the count.
    # Pass it as a secret so private repo names never land in this public repo.
    skip_repos, skip_paths = set(), defaultdict(list)
    for item in filter(None, (x.strip() for x in os.environ.get("LOC_EXCLUDE", "").split(","))):
        name, _, path = item.partition(":")
        (skip_paths[name.lower()].append(path.strip("/")) if path else skip_repos.add(name.lower()))
    repos = [r for r in repos if r["name"].lower() not in skip_repos]
    for r in repos:
        r["skip_paths"] = sorted(skip_paths.get(r["name"].lower(), []))
        r["id_key"] = hashlib.sha256(str(r["id"]).encode()).hexdigest()[:16]
        # include the excludes in the change key so editing them forces a rescan
        r["scan_key"] = r["pushed_at"] + "|" + ",".join(r["skip_paths"])

    def work(r):
        prev = idx["repos"].get(r["id_key"])
        if prev and prev.get("scan_key") == r["scan_key"]:
            return r, prev, "cached"
        try:
            res = scan_repo(r, auth)
        except subprocess.CalledProcessError as e:
            print(f"  ! {r['name']}: {e.stderr.strip()[:200]}", file=sys.stderr)
            return r, prev, "error"
        if res is None:
            return r, None, "empty"
        res.update(scan_key=r["scan_key"], private=r["private"])
        return r, res, "scanned"

    with ThreadPoolExecutor(6) as ex:
        for r, res, how in ex.map(work, repos):
            if res:
                idx["repos"][r["id_key"]] = res
            tot = sum(sum(v.values()) for v in res["net"].values()) if res else 0
            print(f"  {how:8} {'priv' if r['private'] else 'pub '} {tot:>10,}  {r['name']}")
    live = {r["id_key"] for r in repos}
    idx["repos"] = {k: v for k, v in idx["repos"].items() if k in live}
    CACHE.mkdir(parents=True, exist_ok=True)
    idx_path.write_text(json.dumps(idx))

    # aggregate -> anonymous daily series
    net = defaultdict(int); adds = defaultdict(int); dels = defaultdict(int)
    langs = defaultdict(int); commits = 0
    for rr in idx["repos"].values():
        commits += rr["commits"]
        for day, by in rr["net"].items():
            for lang, n in by.items():
                net[day] += n; langs[lang] += n
        for day, (a, d) in rr["vol"].items():
            adds[day] += a; dels[day] += d
    days = sorted(set(net) | set(adds))
    total, series = 0, []
    for day in days:
        total += net[day]
        series.append([day, total, adds[day], dels[day]])
    out = {
        "owner": owner,
        "updated": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "repos": len(idx["repos"]),
        "private": sum(1 for r in idx["repos"].values() if r["private"]),
        "commits": commits,
        "languages": dict(sorted(((k, v) for k, v in langs.items() if v > 0),
                                 key=lambda kv: -kv[1])),
        "days": series,  # [date, running_total, added, deleted]
    }
    HISTORY.parent.mkdir(parents=True, exist_ok=True)
    HISTORY.write_text(json.dumps(out, separators=(",", ":")))
    print(f"total {total:,} LOC across {out['repos']} repos ({out['private']} private), "
          f"{commits:,} commits, {days[0]} -> {days[-1]}")


# ---------------------------------------------------------------- rendering

THEMES = {
    "dark": dict(bg="#0b0f17", panel="#111827", grid="#1f2937", axis="#6b7280",
                 text="#e5e7eb", dim="#9ca3af", up="#22c55e", down="#ef4444",
                 ma="#f59e0b", glow="#22c55e", tag_text="#0b0f17", border="#1f2937"),
    "light": dict(bg="#ffffff", panel="#f8fafc", grid="#e5e7eb", axis="#6b7280",
                  text="#0f172a", dim="#64748b", up="#16a34a", down="#dc2626",
                  ma="#d97706", glow="#16a34a", tag_text="#ffffff", border="#e2e8f0"),
}
LANG_COLORS = ["#3b82f6", "#eab308", "#22c55e", "#f97316", "#a855f7", "#06b6d4",
               "#ec4899", "#84cc16", "#64748b"]


def fmt(n):
    a = abs(n)
    if a >= 1e6:
        return f"{n/1e6:.2f}M"
    if a >= 1e4:
        return f"{n/1e3:.0f}K"
    if a >= 1e3:
        return f"{n/1e3:.1f}K"
    return f"{n:.0f}"


def make_candles(days, max_bars=200):
    """days: [date, total, adds, dels] -> OHLC candles.

    Daily bars when the active span fits in max_bars, else weekly. The chart starts
    where the total first reaches 1% of today's, so years of near-zero history do
    not flatten it. Wicks show churn: lines both written and removed that period.
    """
    by_day = {dt.date.fromisoformat(d): (t, a, r) for d, t, a, r in days}
    last_total = days[-1][1]
    start = next(dt.date.fromisoformat(d) for d, t, *_ in days if t >= last_total * 0.01)
    start -= dt.timedelta(days=3)
    end = dt.date.today()
    step = 1 if (end - start).days <= max_bars else 7
    if step == 7:
        start -= dt.timedelta(days=start.weekday())
    total = 0
    for d, t, *_ in days:
        if dt.date.fromisoformat(d) < start:
            total = t
    candles, cur = [], start
    while cur <= end:
        o = total; va = vd = 0
        for i in range(step):
            day = cur + dt.timedelta(days=i)
            if day in by_day:
                total, a, r = by_day[day]
                va += a; vd += r
        churn = min(va, vd) / 2
        candles.append(dict(week=cur, o=o, c=total, va=va, vd=vd,
                            h=max(o, total) + churn, l=max(0, min(o, total) - churn)))
        cur += dt.timedelta(days=step)
    return candles, step


def nice_ticks(lo, hi, n=5):
    import math
    span = max(hi - lo, 1)
    raw = span / n
    mag = 10 ** math.floor(math.log10(raw))
    step = next(m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw)
    t = math.floor(lo / step) * step
    out = []
    while t <= hi + step * 0.01:
        out.append(t); t += step
    return out


def render_svg(h, theme):
    T = THEMES[theme]
    W, H = 960, 572
    candles, step = make_candles(h["days"])
    last = candles[-1]["c"]
    totals = {dt.date.fromisoformat(d): t for d, t, *_ in h["days"]}

    def total_on(day):
        return max((t for d, t in totals.items() if d <= day), default=0)
    today = dt.date.today()
    m1 = total_on(today - dt.timedelta(days=30))
    ch = last - m1
    pct = (ch / m1 * 100) if m1 else 0
    ath = max(t for t in totals.values())
    ytd_base = total_on(dt.date(today.year, 1, 1) - dt.timedelta(days=1))
    ytd = (last - ytd_base) / ytd_base * 100 if ytd_base else 0
    w7 = last - total_on(today - dt.timedelta(days=7))
    best = max(h["days"], key=lambda r: r[2] - r[3])
    best_net = best[2] - best[3]
    vol30 = sum(a + r for d, t, a, r in h["days"]
                if dt.date.fromisoformat(d) > today - dt.timedelta(days=30))
    up = ch >= 0

    # chart geometry
    L, R, TOP = 24, W - 84, 172
    CH = 232                       # price pane height
    VT, VH = TOP + CH + 14, 54     # volume pane
    n = len(candles)
    slot = (R - L) / n
    bw = max(1.5, min(9, slot * 0.62))
    ymin = min(c["l"] for c in candles); ymax = max(c["h"] for c in candles)
    ticks = nice_ticks(0 if ymin < ymax * 0.25 else ymin, ymax)
    y0, y1 = ticks[0], max(ticks[-1], ymax)

    def Y(v):
        return TOP + CH - (v - y0) / (y1 - y0) * CH

    vmax = max((c["va"] + c["vd"]) for c in candles) or 1

    s = []
    a = s.append
    font = "ui-monospace,SFMono-Regular,Menlo,Consolas,'Liberation Mono',monospace"
    sans = "-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif"
    a(f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" '
      f'role="img" aria-label="{h["owner"]}: {last:,} lines of code across {h["repos"]} repositories">')
    a(f"""<defs>
<linearGradient id="area" x1="0" y1="0" x2="0" y2="1">
  <stop offset="0" stop-color="{T['up']}" stop-opacity="0.28"/>
  <stop offset="1" stop-color="{T['up']}" stop-opacity="0"/>
</linearGradient>
</defs>
<style>
.mono{{font-family:{font}}} .sans{{font-family:{sans}}}
.pulse{{animation:p 2s ease-out infinite;transform-origin:center;transform-box:fill-box}}
@keyframes p{{0%{{opacity:.7;transform:scale(1)}}100%{{opacity:0;transform:scale(3.2)}}}}
.blink{{animation:b 1.2s steps(2,start) infinite}} @keyframes b{{to{{visibility:hidden}}}}
.draw{{stroke-dasharray:1;animation:d 2.4s cubic-bezier(.22,1,.36,1) both}}
@keyframes d{{from{{stroke-dashoffset:1}}to{{stroke-dashoffset:0}}}}
</style>""")
    a(f'<rect width="{W}" height="{H}" rx="14" fill="{T["bg"]}" stroke="{T["border"]}"/>')

    # ---- header
    col = T["up"] if up else T["down"]
    arrow = "▲" if up else "▼"
    a(f'<g class="mono">')
    a(f'<rect x="24" y="22" width="72" height="24" rx="5" fill="{col}"/>')
    a(f'<text x="60" y="39" text-anchor="middle" font-size="13" font-weight="700" fill="{T["tag_text"]}">$LOC</text>')
    a(f'<text x="108" y="39" font-size="13" fill="{T["dim"]}" class="sans">'
      f'{h["owner"]} · Lines of Code · public + private repos</text>')
    a(f'<circle cx="{W-110}" cy="34" r="4" fill="{T["up"]}"/>'
      f'<circle cx="{W-110}" cy="34" r="4" fill="{T["up"]}" class="pulse"/>')
    a(f'<text x="{W-100}" y="38.5" font-size="11.5" fill="{T["dim"]}" letter-spacing="1.5">LIVE</text>')
    a(f'<text x="24" y="98" font-size="46" font-weight="700" fill="{T["text"]}">{last:,}</text>')
    vx = 24 + len(f"{last:,}") * 27.8 + 14
    a(f'<text x="{vx:.0f}" y="98" font-size="15" fill="{T["dim"]}">LOC</text>')
    a(f'<text x="{vx+44:.0f}" y="98" font-size="20" font-weight="700" fill="{col}">'
      f'{arrow} {"+" if up else ""}{ch:,} ({"+" if up else ""}{pct:.2f}%)</text>')
    a(f'<text x="{vx+44:.0f}" y="76" font-size="11" fill="{T["dim"]}" letter-spacing="1">PAST 30 DAYS</text>')

    stats = [("ATH", fmt(ath)), ("7D", ("+" if w7 >= 0 else "") + fmt(w7)),
             ("YTD", f"{ytd:+,.0f}%"), ("BEST DAY", "+" + fmt(best_net)),
             ("30D VOL", fmt(vol30)), ("REPOS", str(h["repos"])),
             ("COMMITS", f'{h["commits"]:,}')]
    sx, sw = 24, (W - 48) / len(stats)
    a(f'<line x1="24" x2="{W-24}" y1="116" y2="116" stroke="{T["grid"]}"/>')
    for i, (k, v) in enumerate(stats):
        x = sx + i * sw
        a(f'<text x="{x:.0f}" y="133" font-size="10" fill="{T["dim"]}" letter-spacing="1">{k}</text>')
        vc = T["up"] if v.startswith("+") else T["down"] if v.startswith("-") else T["text"]
        a(f'<text x="{x+ (len(k)*6.6)+8:.0f}" y="133" font-size="11.5" font-weight="700" fill="{vc}">{v}</text>')
    a('</g>')

    # ---- grid + y axis
    a(f'<g class="mono" font-size="10.5" fill="{T["axis"]}">')
    for t in ticks:
        y = Y(t)
        a(f'<line x1="{L}" x2="{R}" y1="{y:.1f}" y2="{y:.1f}" stroke="{T["grid"]}" stroke-dasharray="2 4"/>')
        a(f'<text x="{R+10}" y="{y+3.5:.1f}">{fmt(t)}</text>')
    # x labels: first week of each quarter (or month if short)
    span_days = n * step
    step_months = 1 if span_days <= 250 else 3 if span_days <= 800 else 6
    seen = set()
    for i, c in enumerate(candles):
        m = c["week"].month
        key = (c["week"].year, m)
        if key in seen or (m - 1) % step_months:
            continue
        seen.add(key)
        if i < 3:
            continue
        x = L + (i + 0.5) * slot
        lab = c["week"].strftime("%b '%y") if m == 1 or len(seen) == 2 else c["week"].strftime("%b")
        a(f'<line x1="{x:.1f}" x2="{x:.1f}" y1="{TOP}" y2="{VT+VH}" stroke="{T["grid"]}" stroke-opacity="0.6"/>')
        a(f'<text x="{x:.1f}" y="{VT+VH+16}" text-anchor="middle">{lab}</text>')
    a('</g>')

    # ---- area under close + candles + MA (revealed left to right)
    a('<g>')
    pts = [(L + (i + 0.5) * slot, Y(c["c"])) for i, c in enumerate(candles)]
    path = "M" + " L".join(f"{x:.1f},{y:.1f}" for x, y in pts)
    a(f'<path d="{path} L{pts[-1][0]:.1f},{TOP+CH} L{pts[0][0]:.1f},{TOP+CH} Z" fill="url(#area)"/>')
    for i, c in enumerate(candles):
        x = L + (i + 0.5) * slot
        rising = c["c"] >= c["o"]
        k = T["up"] if rising else T["down"]
        yt, yb = Y(max(c["o"], c["c"])), Y(min(c["o"], c["c"]))
        if yb - yt < 1.2:
            yt, yb = (yt + yb) / 2 - 0.6, (yt + yb) / 2 + 0.6
        a(f'<line x1="{x:.1f}" x2="{x:.1f}" y1="{Y(c["h"]):.1f}" y2="{Y(c["l"]):.1f}" stroke="{k}" stroke-width="1"/>')
        a(f'<rect x="{x-bw/2:.1f}" y="{yt:.1f}" width="{bw:.1f}" height="{yb-yt:.1f}" fill="{k}" rx="0.6"/>')
        v = c["va"] + c["vd"]
        if v:
            vh = max(1, v / vmax * VH)
            a(f'<rect x="{x-bw/2:.1f}" y="{VT+VH-vh:.1f}" width="{bw:.1f}" height="{vh:.1f}" fill="{k}" fill-opacity="0.45"/>')
    # moving average
    ma, win = [], 7 if step == 1 else 8
    for i in range(len(candles)):
        w = candles[max(0, i - win + 1): i + 1]
        ma.append((L + (i + 0.5) * slot, Y(sum(c["c"] for c in w) / len(w))))
    a(f'<path d="M' + " L".join(f"{x:.1f},{y:.1f}" for x, y in ma) +
      f'" fill="none" stroke="{T["ma"]}" stroke-width="1.6" stroke-opacity="0.9" pathLength="1" class="draw"/>')
    a('</g>')

    # ---- last price line + tag
    ly = Y(last); lx = pts[-1][0]
    a(f'<line x1="{L}" x2="{R}" y1="{ly:.1f}" y2="{ly:.1f}" stroke="{col}" stroke-dasharray="3 3" stroke-opacity="0.8"/>')
    a(f'<circle cx="{lx:.1f}" cy="{ly:.1f}" r="3.5" fill="{col}"/>'
      f'<circle cx="{lx:.1f}" cy="{ly:.1f}" r="3.5" fill="{col}" class="pulse"/>')
    a(f'<rect x="{R+2}" y="{ly-10:.1f}" width="62" height="20" rx="4" fill="{col}"/>')
    a(f'<text x="{R+33}" y="{ly+4:.1f}" text-anchor="middle" class="mono" font-size="11" font-weight="700" fill="{T["tag_text"]}">{fmt(last)}</text>')

    # legend
    a(f'<g class="mono" font-size="10" fill="{T["dim"]}">'
      f'<rect x="{L}" y="{TOP-14}" width="8" height="8" fill="{T["up"]}"/><text x="{L+12}" y="{TOP-6}">{"1D" if step == 1 else "1W"} CANDLES</text>'
      f'<line x1="{L+100}" x2="{L+116}" y1="{TOP-10}" y2="{TOP-10}" stroke="{T["ma"]}" stroke-width="2"/>'
      f'<text x="{L+122}" y="{TOP-6}">MA({win})</text>'
      f'<text x="{L}" y="{VT+10}" fill-opacity="0.8">VOL (lines changed)</text></g>')

    # ---- holdings bar (language allocation)
    langs = list(h["languages"].items())
    top = langs[:8]
    rest = sum(v for _, v in langs[8:])
    if rest:
        top.append(("Other", rest))
    tot = sum(v for _, v in top) or 1
    by = H - 38
    a(f'<text x="24" y="{by-10}" class="mono" font-size="10" fill="{T["dim"]}" letter-spacing="1">HOLDINGS</text>')
    x = 24.0; bwid = W - 48
    for i, (k, v) in enumerate(top):
        w = v / tot * bwid
        a(f'<rect x="{x:.1f}" y="{by-4}" width="{max(w-1.5,0.5):.1f}" height="6" rx="2" fill="{LANG_COLORS[i % len(LANG_COLORS)]}"/>')
        x += w
    x = 94.0
    for i, (k, v) in enumerate(top[:7]):
        lab = f"{k} {v/tot*100:.1f}%"
        a(f'<circle cx="{x:.0f}" cy="{by-13.5}" r="3.5" fill="{LANG_COLORS[i]}"/>'
          f'<text x="{x+7:.0f}" y="{by-10}" class="sans" font-size="11" fill="{T["dim"]}">{lab}</text>')
        x += len(lab) * 6.3 + 22
    a(f'<text x="{W-24}" y="{H-14}" text-anchor="end" class="mono" font-size="9.5" fill="{T["axis"]}">'
      f'updated {h["updated"]} · {h["repos"]} repos ({h["private"]} private) · replayed from git history</text>')
    a(f'<text x="24" y="{H-14}" class="mono" font-size="9.5" fill="{T["axis"]}">'
      f'<tspan class="blink">█</tspan> code lines only: no lockfiles, builds, vendored or generated files</text>')
    a("</svg>")
    return "\n".join(s)


def render():
    h = json.loads(HISTORY.read_text())
    ASSETS.mkdir(exist_ok=True)
    for t in THEMES:
        (ASSETS / f"loc-ticker-{t}.svg").write_text(render_svg(h, t), encoding="utf-8")
    print("rendered", ", ".join(f"assets/loc-ticker-{t}.svg" for t in THEMES))


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "all"
    if cmd in ("collect", "all"):
        collect()
    if cmd in ("render", "all"):
        render()
