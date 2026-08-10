"""Self-contained HTML export.

One file, inline CSS, no scripts and no external requests — so it survives
being emailed, archived or printed to PDF years after the app that made it.
Rendered from the stored snapshot rather than from live queries, so a report
sent last month still says exactly what it said.

Every value arrives pre-formatted from the builder. This module decides only
where things sit on the page; it never computes, rounds or reinterprets a
figure, because a renderer that does arithmetic is a second place for the
numbers to disagree.
"""
from datetime import datetime
from html import escape
from typing import Any

_SEVERITY_STYLE = {
    "critical": ("Critical", "#FEE2E2", "#B91C1C"),
    "high": ("High", "#FFEDD5", "#C2410C"),
    "medium": ("Medium", "#E1ECFF", "#0129AC"),
    "opportunity": ("Opportunity", "#D1FAE5", "#047857"),
}

_CSS = """
*{box-sizing:border-box}
body{margin:0;background:#EDF1F5;color:#2E2E2E;line-height:1.55;
 font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Inter,Arial,sans-serif}
.page{max-width:1320px;margin:auto;background:#fff;box-shadow:0 0 48px #14203a1a}
/* minmax(0,1fr), not 1fr: a grid item's min-width defaults to auto, so
   any wide table sets the track's floor and the whole document scrolls
   sideways instead of the table scrolling inside its own box. */
.layout{display:grid;grid-template-columns:250px minmax(0,1fr)}
nav{position:sticky;top:0;align-self:start;max-height:100vh;overflow:auto;padding:28px 18px;
 background:#FAFBFE;border-right:1px solid #E2E8F0}
nav h3{font-size:.66rem;text-transform:uppercase;letter-spacing:.15em;color:#707070;margin:0 0 10px}
nav a{display:block;padding:5px 8px;border-radius:6px;text-decoration:none;color:#3c4658;font-size:.76rem}
nav a:hover{background:#E1ECFF;color:#0129AC}
.lead{font-size:1.02rem;color:#3c4658;max-width:760px;margin:.2em 0 1em}
.cover{padding:56px 56px 48px;color:#fff;
 background:linear-gradient(140deg,#0129AC,#001a6e 70%,#00113f)}
.brandbar{display:flex;align-items:center;gap:11px;padding-bottom:16px}
.mark{width:27px;height:27px;border-radius:7px;background:#fff;color:#0129AC;flex:none;
 display:flex;align-items:center;justify-content:center;font-weight:900;font-size:.72rem;
 letter-spacing:-.04em;print-color-adjust:exact;-webkit-print-color-adjust:exact}
.eyebrow{text-transform:uppercase;letter-spacing:.16em;font-weight:800;font-size:.7rem;color:#809EFC}
h1{font-size:2.7rem;line-height:1.04;letter-spacing:-.03em;margin:.5em 0 .1em}
.period{font-size:1.3rem;font-weight:600;letter-spacing:-.01em;color:#fff;margin:0}
.cover .site{font-size:.86rem;color:#bfc9ea;margin:.5em 0 0;word-break:break-all}
.dek{font-size:1.02rem;color:#dce4ff;max-width:640px;margin:1.1em 0 0}
.cover-meta{display:grid;grid-template-columns:repeat(4,1fr);gap:20px;margin-top:34px;
 padding-top:20px;border-top:1px solid #ffffff33;font-size:.75rem;color:#bfc9ea}
.cover-meta b{display:block;color:#fff;font-size:1.02rem;font-weight:700;
 letter-spacing:-.01em;margin-bottom:3px}
.chips{display:flex;flex-wrap:wrap;gap:8px;margin-top:20px}
.chip{display:flex;align-items:center;gap:7px;border:1px solid #ffffff40;border-radius:999px;
 padding:5px 13px 5px 9px;font-size:.74rem;color:#fff;white-space:nowrap}
/* Border AND background in the same colour: on screen it reads as a solid
   dot, and with "Background graphics" off — the print dialog's default — the
   ring still draws. The chip's label carries the severity either way, so
   colour is never the only signal. */
.chip i{width:9px;height:9px;border-radius:50%;flex:none;border:2px solid;
 print-color-adjust:exact;-webkit-print-color-adjust:exact}
.chip b{font-weight:800}
/* The screen has a sticky sidebar for this; print has nothing, so the cover
   carries the contents instead of the document having none. */
.toc{display:none}
.cover-foot{margin-top:30px;padding-top:16px;border-top:1px solid #ffffff26;
 font-size:.71rem;color:#a9b6e0;max-width:80ch;line-height:1.65}
main{padding:40px 48px 72px;min-width:0}
section{padding:26px 0 40px;border-bottom:1px solid #E2E8F0}
section:last-child{border-bottom:0}
.sec-no{font-size:.7rem;text-transform:uppercase;letter-spacing:.15em;color:#0129AC;font-weight:800}
h2{font-size:1.65rem;line-height:1.15;letter-spacing:-.02em;margin:.25em 0 .4em}
h3{font-size:1.02rem;margin:.3em 0}
h4{font-size:.7rem;text-transform:uppercase;letter-spacing:.09em;color:#707070;margin:1.1em 0 .35em}
.cards{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin:18px 0 22px}
.card{padding:15px;border:1px solid #E2E8F0;border-radius:12px;background:#fff}
.card .v{font-size:1.5rem;font-weight:800;letter-spacing:-.03em}
.card .l{font-size:.68rem;text-transform:uppercase;letter-spacing:.07em;color:#707070;font-weight:700}
.card .b{font-size:.72rem;color:#707070;margin-top:5px;line-height:1.4}
.finding{border:1px solid #E2E8F0;border-radius:12px;padding:18px;margin:16px 0}
.fh{display:flex;justify-content:space-between;align-items:flex-start;gap:16px}
.fid{font-size:.64rem;color:#0129AC;font-weight:800;letter-spacing:.09em}
.badge{font-size:.63rem;font-weight:800;text-transform:uppercase;letter-spacing:.06em;
 padding:5px 9px;border-radius:99px;white-space:nowrap}
.fgrid{display:grid;grid-template-columns:1fr 1fr;gap:20px;background:#FAFBFE;
 padding:4px 15px 10px;border-radius:9px;margin:12px 0}
.finding ul{padding-left:18px;margin:.3em 0 .9em}
.finding li{margin:.2em 0}
.meta{display:flex;gap:18px;border-top:1px solid #E2E8F0;padding-top:10px;
 font-size:.75rem;color:#707070}
table{border-collapse:collapse;width:100%;font-size:.8rem;margin:6px 0 4px}
th{background:#FAFBFE;text-align:left;text-transform:uppercase;letter-spacing:.05em;
 font-size:.65rem;color:#707070;font-weight:700}
th,td{padding:8px 10px;border-bottom:1px solid #EEF2F6;vertical-align:top}
td:nth-child(n+2){white-space:nowrap}
td:first-child{max-width:26rem;overflow-wrap:anywhere}
.twrap{overflow:auto;border:1px solid #E2E8F0;border-radius:10px;margin:14px 0 20px}
.note{padding:12px 16px;border-left:3px solid #809EFC;background:#F4F7FF;
 border-radius:0 8px 8px 0;margin:10px 0;font-size:.82rem;color:#3c4658}
.gap{border-left-color:#D97706;background:#FFF8EC}
.src{display:grid;grid-template-columns:auto 1fr;gap:8px 14px;font-size:.82rem;margin:14px 0}
.dot{width:9px;height:9px;border-radius:50%;margin-top:6px}
.on{background:#059669}.off{background:#D97706}
.src b{font-weight:600}.src span{color:#707070}
.foot{font-size:.75rem;color:#707070;margin-top:8px}
@media print{
 @page{margin:14mm}
 body{background:#fff}.page{box-shadow:none;max-width:none}
 .layout{display:block}nav{display:none}
 main{padding:0}
 /* The cover was white text on a gradient. Browsers do not print background
    graphics by default, so the first page came out blank — and even with
    them on it was a band of text at the top of an otherwise empty sheet.
    Print gets its own skin: dark ink on white, brand presence carried by
    rules and borders (which always print), filling the page properly. */
 .cover{background:#fff;color:#2E2E2E;padding:0;break-after:page;
  min-height:100vh;display:flex;flex-direction:column}
 .brandbar{border-bottom:3px solid #0129AC;padding-bottom:13px}
 /* Outlined rather than filled: a filled mark with backgrounds off leaves
    white letters on white paper. A border always prints. */
 .mark{background:none;color:#0129AC;border:2px solid #0129AC}
 .eyebrow{color:#0129AC}
 .cover h1{color:#14203a;font-size:3.1rem;margin-top:1em}
 .period{color:#0129AC}
 .cover .site{color:#707070}
 .dek{color:#3c4658}
 /* Pushed to the foot of the page, so the cover reads title-then-facts down
    a full sheet rather than crowding into the top third. */
 .cover-meta{margin-top:auto;border-top:1px solid #C9D4E4;color:#707070}
 .cover-meta b{color:#14203a}
 .chip{border-color:#C9D4E4;color:#2E2E2E}
 .cover-foot{border-top:1px solid #E2E8F0;color:#707070}
 .toc{display:block;margin-top:26px;padding-top:18px;border-top:1px solid #E2E8F0}
 .toc h3{font-size:.66rem;text-transform:uppercase;letter-spacing:.15em;color:#707070;
  margin:0 0 10px;font-weight:800}
 .toc ol{columns:2;column-gap:34px;margin:0;padding:0;list-style:none;
  font-size:.78rem;color:#3c4658}
 .toc li{break-inside:avoid;padding:3px 0;display:flex;gap:9px}
 .toc span{color:#0129AC;font-weight:700;font-variant-numeric:tabular-nums}
 /* Each section starts its own page, as in a printed deliverable. */
 section{break-before:page;break-inside:auto;border:0;padding-top:6px}
 section:first-child{break-before:avoid}
 .finding,.twrap,table{break-inside:avoid}
 thead{display:table-header-group}
 tr{break-inside:avoid}
 h2,h3{break-after:avoid}
 a{color:inherit;text-decoration:none}
}
@media(max-width:1000px){.layout{display:block}nav{display:none}}
@media(max-width:820px){
 .cards{grid-template-columns:repeat(2,1fr)}.fgrid{grid-template-columns:1fr}
 .cover,main{padding-left:24px;padding-right:24px}
}
"""


def _e(value: Any) -> str:
    return escape(str(value if value is not None else ""), quote=True)


def _num(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, int):
        return f"{value:,}"
    if isinstance(value, float):
        return f"{value:,.1f}"
    return _e(value)


def _cards(metrics: list[dict]) -> str:
    if not metrics:
        return ""
    cells = "".join(
        f'<div class="card"><div class="l">{_e(m["label"])}</div>'
        f'<div class="v">{_num(m.get("value"))}{_e(m.get("unit") or "")}</div>'
        f'<div class="b">{_e(m.get("sub") or "")}'
        f'{"<br>" if m.get("sub") else ""}{_e(m.get("basis"))}</div></div>'
        for m in metrics
    )
    return f'<div class="cards">{cells}</div>'


def _table(table: dict) -> str:
    head = "".join(f"<th>{_e(c)}</th>" for c in table.get("columns", []))
    body = "".join(
        "<tr>" + "".join(f"<td>{_e(cell)}</td>" for cell in row) + "</tr>"
        for row in table.get("rows", [])
    )
    note = f'<div class="foot">{_e(table["note"])}</div>' if table.get("note") else ""
    return (
        f'<h4>{_e(table.get("title", ""))}</h4><div class="twrap">'
        f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>{note}"
    )


def _finding(f: dict) -> str:
    label, bg, fg = _SEVERITY_STYLE.get(f.get("severity", "medium"), _SEVERITY_STYLE["medium"])
    actions = "".join(f"<li>{_e(a)}</li>" for a in f.get("actions", []))
    measures = "".join(f"<li>{_e(m)}</li>" for m in f.get("measures", []))
    lists = ""
    if actions or measures:
        lists = (
            f'<div class="fgrid">'
            f'<div><h4>Recommended actions</h4><ul>{actions}</ul></div>'
            f'<div><h4>How success is measured</h4><ul>{measures}</ul></div></div>'
        )
    return (
        f'<article class="finding"><div class="fh"><div>'
        f'<span class="fid">{_e(f.get("id"))}</span><h3>{_e(f.get("title"))}</h3></div>'
        f'<span class="badge" style="background:{bg};color:{fg}">{label}</span></div>'
        f'<div class="fgrid"><div><h4>Evidence</h4><p>{_e(f.get("evidence"))}</p></div>'
        f'<div><h4>Why it matters</h4><p>{_e(f.get("implication"))}</p></div></div>'
        f'{lists}<div class="meta"><span><b>Effort:</b> {_e(f.get("effort"))}</span></div></article>'
    )


def _section(s: dict) -> str:
    anchor = _e(s.get("key") or "")
    head = (
        f'<div class="sec-no">{_e(s.get("number"))} / {_e(s.get("title"))}</div>'
        f'<h2>{_e(s.get("headline") or s.get("title"))}</h2>'
    )
    if s.get("unavailable"):
        return (
            f'<section id="{anchor}">{head}<div class="note gap"><b>Not available.</b> '
            f'{_e(s["unavailable"])}</div></section>'
        )
    notes = "".join(f'<div class="note">{_e(n)}</div>' for n in s.get("notes", []))
    findings = "".join(_finding(f) for f in s.get("findings", []))
    tables = "".join(_table(t) for t in s.get("tables", []))
    return (f'<section id="{anchor}">{head}{_cards(s.get("metrics", []))}'
            f"{notes}{findings}{tables}</section>")


def _contents(sections: list[dict]) -> str:
    links = "".join(
        f'<a href="#{_e(s.get("key"))}">{_e(s.get("number"))} {_e(s.get("title"))}</a>'
        for s in sections
    )
    return (
        f'<nav><h3>Contents</h3>{links}'
        f'<a href="#coverage">Appendix · Data coverage</a></nav>'
    )


def _sources(sources: list[dict]) -> str:
    rows = "".join(
        f'<div class="dot {"on" if s.get("available") else "off"}"></div>'
        f'<div><b>{_e(s.get("label"))}</b> — <span>{_e(s.get("detail"))}'
        f'{(" · " + _e(s["coverage"])) if s.get("coverage") else ""}</span></div>'
        for s in sources
    )
    return (
        '<section id="coverage"><div class="sec-no">Appendix / Data coverage</div>'
        "<h2>What this report could and could not measure</h2>"
        "<p>Every figure above is computed from the sources below. Nothing is estimated, "
        "inferred or generated: where a source was unavailable the section says so rather "
        "than reporting zero, because “measured, and the answer was none” and “we could not "
        "look” are different conclusions.</p>"
        f'<div class="src">{rows}</div></section>'
    )


def _on_date(iso: Any) -> str:
    """"10 Aug 2026", matching how the period reads. A raw ISO stamp beside a
    formatted period label looks like two different kinds of date."""
    try:
        return datetime.fromisoformat(str(iso)).strftime("%d %b %Y")
    except (TypeError, ValueError):
        return str(iso or "")[:10]


def _chips(counts: dict) -> str:
    """Findings by severity. Each carries its label, so severity is never
    conveyed by colour alone."""
    present = [
        (label, colour, counts.get(key, 0))
        for key, (label, _bg, colour) in _SEVERITY_STYLE.items()
        if counts.get(key)
    ]
    if not present:
        return '<div class="chips"><div class="chip">No findings raised</div></div>'
    return '<div class="chips">' + "".join(
        f'<div class="chip"><i style="background:{colour};border-color:{colour}"></i>'
        f"<b>{count}</b> {_e(label.lower())}</div>"
        for label, colour, count in present
    ) + "</div>"


def _toc(sections: list[dict]) -> str:
    """Printed contents. The screen has a sticky sidebar for this; print hides
    it, which left the printed document with no way in at all."""
    items = "".join(
        f'<li><span>{_e(s.get("number"))}</span>{_e(s.get("title"))}</li>'
        for s in sections
    )
    return (
        f'<div class="toc"><h3>What is inside</h3><ol>{items}'
        "<li><span>—</span>Appendix · Data coverage</li></ol></div>"
    )


def _cover(data: dict, sections: list[dict]) -> str:
    counts = data.get("severity_counts", {})
    total = sum(int(v or 0) for v in counts.values())
    sources = data.get("sources", [])
    available = sum(1 for s in sources if s.get("available"))
    period = data.get("period_label") or (
        f'{data.get("period_start", "")} → {data.get("period_end", "")}'
    )
    days = data.get("period_days") or 0
    generated = _on_date(data.get("generated_at"))
    scope = data.get("scope_note") or ""

    return f"""<header class="cover">
<div class="brandbar"><div class="mark">WP</div>
<div class="eyebrow">WP Command Center · Site Report</div></div>
<h1>{_e(data.get("site_name"))}</h1>
<p class="period">{_e(period)}</p>
<p class="site">{_e(data.get("site_url"))}</p>
<p class="dek">Every figure in this report is computed from measured data and states what it
counted. Where something could not be measured, it says so instead of reporting zero.</p>
{_chips(counts)}
<div class="cover-meta">
<div><b>{_e(f"{days} days") if days else "—"}</b>Period length</div>
<div><b>{_e(generated)}</b>Generated</div>
<div><b>{total or "None"}</b>Findings raised</div>
<div><b>{available} of {len(sources)}</b>Data sources available</div>
</div>
{_toc(sections)}
<p class="cover-foot">{_e(scope)} Nothing in this document is estimated, inferred or
generated: every number is the result of a query, and the appendix lists exactly which
sources answered and which did not.</p>
</header>"""


def render_report(data: dict) -> str:
    """A complete, standalone HTML document from a stored report snapshot."""
    section_data = data.get("sections", [])
    sections = "".join(_section(s) for s in section_data)
    title = f"{data.get('site_name', 'Site')} — Site Report"
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_e(title)}</title><style>{_CSS}</style></head>
<body><div class="page">
{_cover(data, section_data)}
<div class="layout">{_contents(section_data)}
<main>{sections}{_sources(data.get("sources", []))}</main></div>
</div></body></html>"""


__all__ = ["render_report"]
