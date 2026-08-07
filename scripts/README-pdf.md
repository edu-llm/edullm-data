# Rendering a report to PDF

No markdown converter is installed on this machine (`pandoc`, `weasyprint`, `wkhtmltopdf`,
`typst`, `pdflatex` — all absent). Chrome is present, and its headless print path produces
good output. So the pipeline is two steps:

```bash
python3 scripts/md2html.py docs/FINAL-DATASET-REPORT.md \
  --out /tmp/final-report.html \
  --tag "Awaiting approval · 2026-08-07"

"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  --headless --disable-gpu --no-pdf-header-footer \
  --print-to-pdf=docs/FINAL-DATASET-REPORT.pdf \
  file:///tmp/final-report.html
```

`--no-pdf-header-footer` drops Chrome's default date/URL furniture. The page geometry and all
typography live in `CSS` at the top of `md2html.py`.

## What the converter supports

ATX headings, pipe tables with an alignment row, fenced code, blockquotes, `-`/`1.` lists,
thematic breaks, and inline `code` / bold / italic / links. Inline code is stashed *before*
escaping and emphasis, so `**` inside backticks stays literal.

Not supported, deliberately: nested lists, reference links, HTML passthrough, footnotes. A
silent partial render is worse than a loud one, so anything unrecognised is left as literal
text rather than half-parsed.

## Check the output before shipping it

Unbalanced emphasis in the source markdown survives into the HTML as a literal `**`. Grep for
it — two real instances were caught this way:

```bash
python3 -c "import re;print(len(re.findall(r'\*\*',open('/tmp/final-report.html').read())))"
```

Zero is the expected answer. Then read the PDF itself; do not trust a script that claims to
extract its text. Chrome writes subset fonts with a `ToUnicode` map, and a naive
`re.findall(r'\((.*?)\)')` over the decompressed streams returns **nothing** even when the
document is perfect — which looks exactly like a blank render. Open the file.
