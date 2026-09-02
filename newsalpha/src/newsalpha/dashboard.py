"""Signal dashboard.

Renders scored filings as a research screen: one row per filing, rated STRONG
BUY through STRONG SELL, sorted by conviction. This is the product for a swing
trader - a shortlist to look at each morning, not an execution system.

The rating is deliberately five buckets rather than a score. A number like 0.73
invites false precision; five labels force the model's view into something you
either act on or ignore.
"""

from __future__ import annotations

import html
import json
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .models import Direction, Rating, rate, utcnow
from .utils import IST, parse_dt, read_jsonl


@dataclass(frozen=True, slots=True)
class SignalRow:
    """One rated filing, flattened for display."""

    symbol: str
    rating: str
    side: str
    conviction: float
    confidence: float
    materiality: int
    horizon_days: int
    headline: str
    rationale: str
    key_risk: str
    key_numbers: list[str]
    source: str
    category: str
    filed_at: str
    filed_sort: str
    url: str


def _ist(value: Any) -> tuple[str, str]:
    moment = parse_dt(value)
    if moment is None:
        return "", ""
    local = moment.astimezone(IST)
    return local.strftime("%d %b, %H:%M"), local.isoformat()


def row_from_record(record: dict[str, Any]) -> SignalRow | None:
    """Build a display row from a journal record, or None if it is not a signal."""
    direction_raw = str(record.get("direction", "")).upper()
    if direction_raw not in ("BULLISH", "BEARISH", "NEUTRAL"):
        return None

    direction = Direction(direction_raw)
    confidence = float(record.get("confidence", 0.0) or 0.0)
    materiality = int(record.get("materiality", 0) or 0)
    rating = rate(direction, confidence, materiality)
    shown, sortable = _ist(record.get("filed_at") or record.get("at"))

    return SignalRow(
        symbol=str(record.get("symbol", "")).upper() or "—",
        rating=rating.value,
        side=rating.side,
        conviction=round(confidence * materiality / 5.0, 3),
        confidence=round(confidence, 2),
        materiality=materiality,
        horizon_days=int(record.get("horizon_days", 0) or 0),
        headline=str(record.get("headline", "")),
        rationale=str(record.get("rationale", "")),
        key_risk=str(record.get("key_risk", "")),
        key_numbers=[str(n) for n in (record.get("key_numbers") or [])][:4],
        source=str(record.get("source", "")).upper(),
        category=str(record.get("category", "")),
        filed_at=shown,
        filed_sort=sortable,
        url=str(record.get("attachment_url", "")),
    )


def collect(*paths: str | Path) -> list[SignalRow]:
    """Read rated filings from one or more journals, newest-highest-conviction first.

    Journals hold several record types and one filing can appear more than once
    (announcement, then decision). Keyed by uid so the richest record wins.
    """
    merged: dict[str, dict[str, Any]] = {}
    for path in paths:
        for record in read_jsonl(path):
            uid = str(record.get("uid", ""))
            if not uid:
                continue
            merged.setdefault(uid, {}).update(
                {k: v for k, v in record.items() if v not in (None, "")}
            )

    rows = [row for record in merged.values() if (row := row_from_record(record))]
    # Conviction order, matching what the page's default sort claims to do.
    # Ties break to the most recent filing.
    rows.sort(key=lambda r: (-r.conviction, r.filed_sort), reverse=False)
    return rows


def summarise(rows: list[SignalRow]) -> dict[str, int]:
    counts = {r.value: 0 for r in Rating}
    for row in rows:
        counts[row.rating] += 1
    return counts


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------

_STYLE = """
@import url('https://fonts.googleapis.com/css2?family=Newsreader:ital,opsz,wght@0,6..72,400;0,6..72,500;0,6..72,600;1,6..72,400&family=IBM+Plex+Mono:wght@400;500;600&family=IBM+Plex+Sans:wght@400;450;500;600&display=swap');

:root {
  --ground: #f7f8fa;
  --surface: #ffffff;
  --surface-2: #f1f3f7;
  --ink: #171a21;
  --muted: #5b6474;
  --faint: #868f9f;
  --line: #e2e5eb;
  --accent: #2f4b8f;
  --pos: #157f52;
  --pos-soft: #e6f2ec;
  --neg: #b3261e;
  --neg-soft: #fbeae9;
  --flat: #8a7b4e;
  --flat-soft: #f4f0e4;
  --shadow: 0 1px 2px rgba(23, 26, 33, .06), 0 1px 1px rgba(23, 26, 33, .04);
}

:root:not([data-theme="light"]) { }

@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --ground: #101319;
    --surface: #171b23;
    --surface-2: #1e2430;
    --ink: #e8ebf0;
    --muted: #98a1b2;
    --faint: #6f7889;
    --line: #262c38;
    --accent: #7c9be0;
    --pos: #35b37e;
    --pos-soft: #123024;
    --neg: #e5645c;
    --neg-soft: #34181a;
    --flat: #c4a961;
    --flat-soft: #2c2718;
    --shadow: 0 1px 2px rgba(0, 0, 0, .4);
  }
}

:root[data-theme="dark"] {
  --ground: #101319;
  --surface: #171b23;
  --surface-2: #1e2430;
  --ink: #e8ebf0;
  --muted: #98a1b2;
  --faint: #6f7889;
  --line: #262c38;
  --accent: #7c9be0;
  --pos: #35b37e;
  --pos-soft: #123024;
  --neg: #e5645c;
  --neg-soft: #34181a;
  --flat: #c4a961;
  --flat-soft: #2c2718;
  --shadow: 0 1px 2px rgba(0, 0, 0, .4);
}

* { box-sizing: border-box; }

body {
  margin: 0;
  background: var(--ground);
  color: var(--ink);
  font-family: 'IBM Plex Sans', ui-sans-serif, system-ui, -apple-system, sans-serif;
  font-size: 15px;
  line-height: 1.5;
  -webkit-font-smoothing: antialiased;
}

.wrap { max-width: 1080px; margin: 0 auto; padding: 28px 20px 72px; }

/* ---------- masthead ---------- */

.masthead {
  display: flex; flex-wrap: wrap; align-items: baseline;
  gap: 10px 16px; padding-bottom: 14px; border-bottom: 2px solid var(--ink);
}
.masthead h1 {
  margin: 0; font-family: Newsreader, Georgia, serif;
  font-size: 30px; font-weight: 600; letter-spacing: -.01em; text-wrap: balance;
}
.masthead .stamp {
  margin-left: auto; font-family: 'IBM Plex Mono', ui-monospace, monospace;
  font-size: 12px; color: var(--muted); font-variant-numeric: tabular-nums;
}

.notice {
  display: flex; gap: 10px; align-items: flex-start;
  margin-top: 16px; padding: 11px 14px;
  background: var(--flat-soft); border-left: 3px solid var(--flat);
  border-radius: 0 4px 4px 0; font-size: 13.5px; color: var(--ink);
}
.notice strong { font-weight: 600; }

/* ---------- counts ---------- */

.counts {
  display: flex; flex-wrap: wrap; gap: 6px 20px;
  margin: 20px 0 4px; font-size: 13px;
  font-family: 'IBM Plex Mono', ui-monospace, monospace;
  font-variant-numeric: tabular-nums; color: var(--muted);
}
.counts span { display: inline-flex; align-items: center; gap: 7px; }
.counts i { width: 8px; height: 8px; border-radius: 2px; display: inline-block; }
.counts b { color: var(--ink); font-weight: 600; }

/* ---------- controls ---------- */

.controls {
  display: flex; flex-wrap: wrap; gap: 8px; align-items: center;
  margin: 18px 0 20px; padding-top: 16px; border-top: 1px solid var(--line);
}
.chip {
  font: inherit; font-size: 12.5px; font-weight: 500;
  padding: 5px 11px; border-radius: 999px; cursor: pointer;
  background: var(--surface); color: var(--muted);
  border: 1px solid var(--line);
}
.chip:hover { color: var(--ink); border-color: var(--faint); }
.chip[aria-pressed="true"] {
  background: var(--ink); color: var(--ground); border-color: var(--ink);
}
.chip:focus-visible, .search:focus-visible, .sort:focus-visible {
  outline: 2px solid var(--accent); outline-offset: 2px;
}
.search {
  flex: 1 1 190px; min-width: 160px; font: inherit; font-size: 13.5px;
  padding: 6px 11px; border-radius: 6px;
  border: 1px solid var(--line); background: var(--surface); color: var(--ink);
}
.search::placeholder { color: var(--faint); }
.sort {
  font: inherit; font-size: 12.5px; padding: 6px 10px; border-radius: 6px;
  border: 1px solid var(--line); background: var(--surface); color: var(--muted);
  cursor: pointer;
}

/* ---------- rows ---------- */

.rows { display: flex; flex-direction: column; gap: 8px; }

.row {
  display: grid;
  grid-template-columns: 3px 132px minmax(0, 1fr);
  background: var(--surface); border: 1px solid var(--line);
  border-radius: 6px; box-shadow: var(--shadow); overflow: hidden;
}
.row .stripe { background: var(--flat); }
.row.pos .stripe { background: var(--pos); }
.row.neg .stripe { background: var(--neg); }

.rail {
  display: flex; flex-direction: column; gap: 6px;
  padding: 13px 12px 13px 14px; border-right: 1px solid var(--line);
}

.pill {
  display: inline-block; align-self: flex-start;
  font-family: 'IBM Plex Mono', ui-monospace, monospace;
  font-size: 10.5px; font-weight: 600; letter-spacing: .06em;
  padding: 3px 7px; border-radius: 3px; white-space: nowrap;
  border: 1px solid transparent;
}
.pill.strong-buy { background: var(--pos); color: var(--surface); }
.pill.buy { background: var(--pos-soft); color: var(--pos); border-color: var(--pos); }
.pill.strong-sell { background: var(--neg); color: var(--surface); }
.pill.sell { background: var(--neg-soft); color: var(--neg); border-color: var(--neg); }
.pill.hold { background: var(--flat-soft); color: var(--flat); border-color: var(--flat); }

.ticker {
  font-family: 'IBM Plex Mono', ui-monospace, monospace;
  font-size: 13px; font-weight: 600; letter-spacing: -.01em;
  overflow-wrap: anywhere;
}
.rail .meter { display: flex; align-items: center; gap: 6px; }
.meter .track {
  flex: 1; height: 3px; border-radius: 2px; background: var(--surface-2);
  overflow: hidden;
}
.meter .fill { display: block; height: 100%; background: var(--muted); }
.row.pos .meter .fill { background: var(--pos); }
.row.neg .meter .fill { background: var(--neg); }
.meter .val {
  font-family: 'IBM Plex Mono', ui-monospace, monospace;
  font-size: 10.5px; color: var(--faint); font-variant-numeric: tabular-nums;
}

.body { padding: 13px 16px; display: flex; flex-direction: column; gap: 7px; min-width: 0; }
.headline {
  font-family: Newsreader, Georgia, serif;
  font-size: 17px; line-height: 1.35; font-weight: 500; text-wrap: balance;
}
.headline a { color: inherit; text-decoration-color: var(--line); }
.headline a:hover { text-decoration-color: var(--accent); }
.rationale { font-size: 13.5px; color: var(--muted); }
.risk { font-size: 12.5px; color: var(--muted); }
.risk b {
  color: var(--flat); font-weight: 600; font-size: 11px;
  letter-spacing: .05em; text-transform: uppercase;
}
.figures { display: flex; flex-wrap: wrap; gap: 5px; }
.figure {
  font-family: 'IBM Plex Mono', ui-monospace, monospace;
  font-size: 11px; padding: 2px 6px; border-radius: 3px;
  background: var(--surface-2); color: var(--muted);
  font-variant-numeric: tabular-nums;
}
.meta {
  display: flex; flex-wrap: wrap; gap: 4px 14px;
  font-family: 'IBM Plex Mono', ui-monospace, monospace;
  font-size: 11px; color: var(--faint); font-variant-numeric: tabular-nums;
}

.empty {
  padding: 44px 20px; text-align: center; color: var(--muted);
  border: 1px dashed var(--line); border-radius: 6px; background: var(--surface);
}

footer {
  margin-top: 32px; padding-top: 16px; border-top: 1px solid var(--line);
  font-size: 12px; color: var(--faint); line-height: 1.6;
}

@media (max-width: 640px) {
  .row { grid-template-columns: 3px minmax(0, 1fr); }
  .rail {
    flex-direction: row; align-items: center; gap: 10px;
    border-right: none; border-bottom: 1px solid var(--line); padding: 10px 14px;
  }
  .rail .meter { flex: 1; max-width: 130px; }
  .masthead .stamp { margin-left: 0; width: 100%; }
}

@media (prefers-reduced-motion: reduce) {
  * { animation: none !important; transition: none !important; }
}
"""

_SCRIPT = """
const ROWS = __DATA__;
const PILL = {
  "STRONG BUY": "strong-buy", "BUY": "buy", "HOLD": "hold",
  "SELL": "sell", "STRONG SELL": "strong-sell"
};
const SIDE = { LONG: "pos", SHORT: "neg", FLAT: "" };

let filter = "ALL";
let sort = "conviction";
let query = "";

try {
  const saved = localStorage.getItem("newsalpha.filter");
  if (saved) filter = saved;
} catch (e) { /* private window, blocked storage - defaults are fine */ }

const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

function visible() {
  let out = ROWS.filter((r) => {
    if (filter === "ACTIONABLE" && r.rating === "HOLD") return false;
    if (filter !== "ALL" && filter !== "ACTIONABLE" && r.rating !== filter) return false;
    if (!query) return true;
    const hay = (r.symbol + " " + r.headline + " " + r.rationale + " " + r.category).toLowerCase();
    return hay.includes(query);
  });
  if (sort === "recent") {
    out = out.slice().sort((a, b) => (b.filed_sort || "").localeCompare(a.filed_sort || ""));
  }
  return out;
}

function render() {
  const list = visible();
  const host = document.getElementById("rows");

  if (!list.length) {
    host.innerHTML = '<div class="empty">No filings match this filter.</div>';
    return;
  }

  host.innerHTML = list.map((r) => {
    const side = SIDE[r.side] || "";
    const pct = Math.round(r.conviction * 100);
    const headline = r.url
      ? '<a href="' + esc(r.url) + '" target="_blank" rel="noopener">' + esc(r.headline) + "</a>"
      : esc(r.headline);
    const figures = (r.key_numbers || []).length
      ? '<div class="figures">' +
        r.key_numbers.map((n) => '<span class="figure">' + esc(n) + "</span>").join("") +
        "</div>"
      : "";
    const risk = r.key_risk
      ? '<div class="risk"><b>Main risk</b> &nbsp;' + esc(r.key_risk) + "</div>"
      : "";
    const meta = [
      r.filed_at, r.source, r.category,
      r.horizon_days ? "~" + r.horizon_days + "d view" : "",
      "conf " + r.confidence.toFixed(2), "materiality " + r.materiality + "/5"
    ].filter(Boolean).map((m) => "<span>" + esc(m) + "</span>").join("");

    return '<article class="row ' + side + '">' +
      '<div class="stripe"></div>' +
      '<div class="rail">' +
        '<span class="pill ' + PILL[r.rating] + '">' + esc(r.rating) + "</span>" +
        '<span class="ticker">' + esc(r.symbol) + "</span>" +
        '<span class="meter"><span class="track">' +
          '<span class="fill" style="width:' + pct + '%"></span>' +
        '</span><span class="val">' + pct + "</span></span>" +
      "</div>" +
      '<div class="body">' +
        '<div class="headline">' + headline + "</div>" +
        (r.rationale ? '<div class="rationale">' + esc(r.rationale) + "</div>" : "") +
        risk + figures +
        '<div class="meta">' + meta + "</div>" +
      "</div>" +
    "</article>";
  }).join("");
}

document.querySelectorAll(".chip").forEach((chip) => {
  chip.addEventListener("click", () => {
    filter = chip.dataset.filter;
    document.querySelectorAll(".chip").forEach((c) =>
      c.setAttribute("aria-pressed", String(c.dataset.filter === filter)));
    try { localStorage.setItem("newsalpha.filter", filter); } catch (e) { /* ignore */ }
    render();
  });
  chip.setAttribute("aria-pressed", String(chip.dataset.filter === filter));
});

document.getElementById("search").addEventListener("input", (e) => {
  query = e.target.value.trim().toLowerCase();
  render();
});

document.getElementById("sort").addEventListener("change", (e) => {
  sort = e.target.value;
  render();
});

render();
"""


def _script_safe_json(payload: Any) -> str:
    """Serialise for embedding inside a <script> block.

    Filing headlines are third-party text scraped from exchange sites. JSON
    escaping alone does not protect a script context: a headline containing
    "</script>" closes the block early and everything after it is parsed as
    markup. Escaping the three characters that can start a tag or entity keeps
    the payload valid JSON and inert as HTML.
    """
    raw = json.dumps(payload, ensure_ascii=False)
    return raw.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def render(
    rows: list[SignalRow],
    generated_at: datetime | None = None,
    sample: bool = False,
    standalone: bool = True,
) -> str:
    """Render the dashboard.

    ``standalone`` wraps the output in a full HTML document for writing to disk.
    """
    generated_at = generated_at or utcnow()
    stamp = generated_at.astimezone(IST).strftime("%d %b %Y, %H:%M IST")
    counts = summarise(rows)

    # Solid semantic colours: the soft fills used inside the pills disappear at
    # 8px against the page ground. Strong vs plain is carried by the label.
    swatch = {
        Rating.STRONG_BUY.value: "var(--pos)",
        Rating.BUY.value: "var(--pos)",
        Rating.HOLD.value: "var(--flat)",
        Rating.SELL.value: "var(--neg)",
        Rating.STRONG_SELL.value: "var(--neg)",
    }
    faded = {Rating.BUY.value, Rating.SELL.value}
    counts_html = "".join(
        f'<span><i style="background:{swatch[name]}'
        f'{";opacity:.45" if name in faded else ""}"></i>'
        f"{html.escape(name.title())} <b>{count}</b></span>"
        for name, count in counts.items()
    )

    chips = "".join(
        f'<button class="chip" data-filter="{html.escape(value)}">{html.escape(label)}</button>'
        for value, label in (
            ("ALL", "All"),
            ("ACTIONABLE", "Actionable only"),
            ("STRONG BUY", "Strong Buy"),
            ("BUY", "Buy"),
            ("SELL", "Sell"),
            ("STRONG SELL", "Strong Sell"),
        )
    )

    notice = (
        '<div class="notice"><strong>Sample data.</strong>&nbsp;These are '
        "illustrative filings from fictional issuers, shown to demonstrate the "
        "format. Nothing here is a real filing or investment advice.</div>"
        if sample
        else ""
    )

    data = _script_safe_json([asdict(r) for r in rows])
    script = _SCRIPT.replace("__DATA__", data)

    head = f"<title>Filing Signals</title>\n<style>{_STYLE}</style>"
    page = f"""<div class="wrap">
  <header class="masthead">
    <h1>Filing Signals</h1>
    <span class="stamp">{html.escape(stamp)}</span>
  </header>
  {notice}
  <div class="counts">{counts_html}</div>
  <div class="controls">
    {chips}
    <input id="search" class="search" type="search" placeholder="Filter by symbol or text"
           aria-label="Filter filings">
    <select id="sort" class="sort" aria-label="Sort order">
      <option value="conviction">Conviction</option>
      <option value="recent">Most recent</option>
    </select>
  </div>
  <main id="rows" class="rows"></main>
  <footer>
    Corporate filings from BSE and NSE, read and rated by Claude. Conviction is
    confidence weighted by how material the filing is; a certain read on a trivial
    filing is not a trade. Ratings are research input for your own judgment, not
    advice, and nothing here places an order.
  </footer>
</div>
<script>{script}</script>"""

    # The Artifact host supplies the document shell, so the published page is
    # just head fragments followed by content. A file written to disk needs the
    # whole document, with the markup in <body> where it belongs.
    if not standalone:
        return f"{head}\n{page}"
    return (
        '<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"{head}\n</head>\n<body>\n{page}\n</body>\n</html>"
    )


def write(rows: list[SignalRow], out_path: str | Path, sample: bool = False) -> Path:
    path = Path(out_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render(rows, sample=sample), encoding="utf-8")
    return path
