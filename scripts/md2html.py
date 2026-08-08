#!/usr/bin/env python3
"""Render a Markdown report to print-ready HTML, for Chrome's headless PDF writer.

WHY THIS EXISTS RATHER THAN `pandoc`. No markdown converter is installed on this machine and
installing one is a bigger change than the task warrants. Chrome IS present, and
`--headless --print-to-pdf` produces good output from HTML. So the only missing piece is
Markdown -> HTML, which for the subset this document uses is ~150 lines.

WHAT IT SUPPORTS, because the report uses exactly this and nothing more: ATX headings,
pipe tables with an alignment row, fenced code, blockquotes, `-`/`1.` lists, thematic breaks,
and inline `code` / **bold** / *italic*. Inline spans are escaped BEFORE emphasis is applied,
so a `**` inside backticks stays literal.

WHAT IT DELIBERATELY DOES NOT DO: nested lists, reference links, HTML passthrough, footnotes.
A silent partial render is worse than a loud refusal, so unsupported-looking input is left as
literal text rather than half-parsed.
"""

from __future__ import annotations

import argparse
import html
import re
from pathlib import Path

CSS = """
@page { size: A4; margin: 16mm 15mm 18mm 15mm; }
html { -webkit-print-color-adjust: exact; print-color-adjust: exact; }
body { font: 10.5pt/1.5 "Charter","Georgia",serif; color:#14161a; margin:0; }
h1 { font-size:20pt; line-height:1.2; margin:0 0 2pt; letter-spacing:-.01em; }
h1+p { color:#5b6470; margin-top:0; }
h2 { font-size:13pt; margin:20pt 0 6pt; padding-bottom:3pt;
     border-bottom:1.5px solid #14161a; break-after:avoid; }
h3 { font-size:11pt; margin:14pt 0 4pt; break-after:avoid; }
p, li { orphans:2; widows:2; }
strong { font-weight:700; }
code { font:9pt/1.4 "SF Mono","Menlo",monospace; background:#f2f3f5;
       padding:.5pt 2.5pt; border-radius:2px; }
pre { background:#f7f8f9; border-left:2.5px solid #c9ced6; padding:7pt 9pt;
      overflow-x:auto; break-inside:avoid; }
pre code { background:none; padding:0; font-size:8.5pt; }
blockquote { margin:9pt 0; padding:7pt 11pt; background:#fdf6e8;
             border-left:3px solid #d8a838; break-inside:avoid; }
blockquote p { margin:0; }
blockquote p + p { margin-top:5pt; }
table { border-collapse:collapse; width:100%; margin:9pt 0; font-size:8.7pt;
        break-inside:avoid; }
th { background:#14161a; color:#fff; font-weight:600; text-align:left;
     padding:4pt 6pt; font-size:8.4pt; }
td { padding:3.6pt 6pt; border-bottom:.5px solid #dfe3e8; vertical-align:top; }
tr:nth-child(even) td { background:#fafbfc; }
td code, th code { font-size:8pt; }
hr { border:none; border-top:.5px solid #dfe3e8; margin:16pt 0; }
ul, ol { margin:6pt 0; padding-left:17pt; }
li { margin:2.5pt 0; }
.tag { display:inline-block; font:600 7.5pt/1 "SF Mono",monospace;
       letter-spacing:.04em; text-transform:uppercase; padding:3pt 6pt;
       border-radius:3px; background:#14161a; color:#fff; margin-bottom:9pt; }
"""

_INLINE_CODE = re.compile(r"`([^`]+)`")
_BOLD = re.compile(r"\*\*([^*]+)\*\*")
_ITAL = re.compile(r"(?<![*\w])\*([^*\n]+)\*(?!\*)")
_LINK = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")


def inline(text: str) -> str:
    """Escape, then protect code spans, then apply emphasis outside them."""
    slots: list[str] = []

    def stash(m: re.Match) -> str:
        slots.append(f"<code>{html.escape(m.group(1), quote=False)}</code>")
        return f"\x00{len(slots) - 1}\x00"

    text = _INLINE_CODE.sub(stash, text)
    text = html.escape(text, quote=False)
    text = _LINK.sub(r'<a href="\2">\1</a>', text)
    text = _BOLD.sub(r"<strong>\1</strong>", text)
    text = _ITAL.sub(r"<em>\1</em>", text)
    return re.sub(r"\x00(\d+)\x00", lambda m: slots[int(m.group(1))], text)


def is_table_rule(line: str) -> bool:
    s = line.strip()
    return bool(s.startswith("|") and re.fullmatch(r"[|\s:-]+", s) and "-" in s)


def render(md: str) -> str:
    lines = md.split("\n")
    out: list[str] = []
    i, n = 0, len(lines)

    while i < n:
        raw = lines[i]
        line = raw.rstrip()
        s = line.strip()

        if not s:
            i += 1
            continue

        if s.startswith("```"):
            lang = s[3:].strip().lower()
            i += 1
            body: list[str] = []
            while i < n and not lines[i].strip().startswith("```"):
                body.append(lines[i])
                i += 1
            i += 1
            if lang == "mermaid":
                # A mermaid fence needs a live renderer, so it is emitted verbatim inside
                # `<pre class="mermaid">` and mermaid.js converts it to SVG at load time. Still
                # escaped: node labels contain `<br/>`, and mermaid reads the element's text
                # content, so escaping is both safe here and necessary for any `&` or `<`.
                out.append(
                    '<pre class="mermaid">' + html.escape("\n".join(body), quote=False) + "</pre>"
                )
            else:
                out.append(
                    "<pre><code>" + html.escape("\n".join(body), quote=False) + "</code></pre>"
                )
            continue

        if re.fullmatch(r"-{3,}|\*{3,}", s):
            out.append("<hr>")
            i += 1
            continue

        if m := re.match(r"(#{1,6})\s+(.*)", s):
            lvl = len(m.group(1))
            out.append(f"<h{lvl}>{inline(m.group(2))}</h{lvl}>")
            i += 1
            continue

        # Table: a pipe row followed by an alignment rule.
        if s.startswith("|") and i + 1 < n and is_table_rule(lines[i + 1]):
            def cells(t: str) -> list[str]:
                return [c.strip() for c in t.strip().strip("|").split("|")]

            head = cells(s)
            i += 2
            rows: list[list[str]] = []
            while i < n and lines[i].strip().startswith("|"):
                rows.append(cells(lines[i]))
                i += 1
            th = "".join(f"<th>{inline(c)}</th>" for c in head)
            body = "".join(
                "<tr>" + "".join(f"<td>{inline(c)}</td>" for c in r) + "</tr>" for r in rows
            )
            out.append(f"<table><thead><tr>{th}</tr></thead><tbody>{body}</tbody></table>")
            continue

        if s.startswith(">"):
            block: list[str] = []
            while i < n and lines[i].strip().startswith(">"):
                block.append(re.sub(r"^\s*>\s?", "", lines[i]))
                i += 1
            paras = [p for p in re.split(r"\n\s*\n", "\n".join(block)) if p.strip()]
            inner = "".join(f"<p>{inline(p.replace(chr(10), ' ').strip())}</p>" for p in paras)
            out.append(f"<blockquote>{inner}</blockquote>")
            continue

        if re.match(r"[-*]\s+|\d+[.)]\s+", s):
            ordered = bool(re.match(r"\d+[.)]\s+", s))
            items: list[str] = []
            while i < n and lines[i].strip():
                t = lines[i].strip()
                if re.match(r"[-*]\s+|\d+[.)]\s+", t):
                    items.append(re.sub(r"^([-*]|\d+[.)])\s+", "", t))
                elif items:                      # continuation of the previous item
                    items[-1] += " " + t
                else:
                    break
                i += 1
            tag = "ol" if ordered else "ul"
            out.append(
                f"<{tag}>" + "".join(f"<li>{inline(x)}</li>" for x in items) + f"</{tag}>"
            )
            continue

        para = [s]
        i += 1
        while i < n and lines[i].strip() and not re.match(
            r"#{1,6}\s|[-*]\s|\d+[.)]\s|>|```|\||-{3,}", lines[i].strip()
        ):
            para.append(lines[i].strip())
            i += 1
        out.append(f"<p>{inline(' '.join(para))}</p>")

    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("source", type=Path)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--tag", default="", help="Small label above the title, e.g. a status.")
    a = ap.parse_args()

    body = render(a.source.read_text(encoding="utf-8"))
    if a.tag:
        body = f'<div class="tag">{html.escape(a.tag)}</div>\n' + body
    title = a.source.stem.replace("-", " ")

    # Mermaid support is opt-in per document, and deliberately so: the renderer is a ~3 MB script
    # loaded from a CDN, which makes the page non-self-contained and useless offline. A document
    # with no diagram must not pay that, so the tag is injected only when a fence was actually
    # found. `startOnLoad: false` plus an explicit `run()` is what makes Chrome's --print-to-pdf
    # work: the default async path can begin printing before the SVG exists, which yields a PDF
    # with the diagram missing and no error anywhere.
    head_extra = ""
    if 'class="mermaid"' in body:
        head_extra = (
            '<script src="https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.min.js"></script>'
            "<script>mermaid.initialize({startOnLoad:false,theme:'base',"
            "flowchart:{useMaxWidth:true,htmlLabels:true}});"
            "window.addEventListener('load',()=>mermaid.run());</script>"
            # A dependency graph is unreadable at a third of a page, so it gets a LANDSCAPE page of
            # its own via a named @page rule — the prose stays portrait. A `graph LR` diagram is
            # wider than it is tall, so landscape is what makes the labels legible; rotating the
            # element with a transform was tried first and is worse (sideways text, bad centring).
            "<style>@page landscape{size:A4 landscape;margin:10mm}"
            "pre.mermaid{page:landscape;background:none;border:none;padding:0;text-align:center;"
            "page-break-before:always;break-before:page;"
            "page-break-after:always;break-after:page;"
            "page-break-inside:avoid;break-inside:avoid;"
            "width:100vw;max-width:none;margin-left:calc(50% - 50vw)}"
            "pre.mermaid svg{width:99%;height:auto}</style>"
        )

    a.out.write_text(
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>{html.escape(title)}</title><style>{CSS}</style>{head_extra}</head>"
        f"<body>{body}</body></html>",
        encoding="utf-8",
    )
    print(f"wrote {a.out}  ({a.out.stat().st_size:,} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
