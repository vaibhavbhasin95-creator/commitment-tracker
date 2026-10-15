"""
Builds a shareable PDF of prd.md (including the status diagram) using
Microsoft Edge's built-in "print to PDF" — the same rendering as the browser,
so tables and the SVG diagram come out exactly as previewed.

    pip install markdown          # docs-only dependency, not needed by the app
    python src/docs_tools/make_status_diagram.py   # if a status changed
    python src/docs_tools/build_prd_pdf.py

Output: docs/Commitment_Tracker_PRD_v<version>.pdf
"""

import re
import subprocess
import sys
import tempfile
import time
from datetime import date
from pathlib import Path

import markdown

ROOT = Path(__file__).resolve().parents[2]   # project root (this script lives in src/docs_tools/)
EDGE_CANDIDATES = [
    Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
    Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
]

CSS = """
@page { size: A4; margin: 16mm 14mm 18mm 14mm;
  @bottom-left { content: "Commitment Tracker — Product Requirements Document"; font: 8pt 'Segoe UI', Arial, sans-serif; color: #6e7781; }
  @bottom-right { content: "Page " counter(page) " of " counter(pages); font: 8pt 'Segoe UI', Arial, sans-serif; color: #6e7781; } }
html { -webkit-print-color-adjust: exact; print-color-adjust: exact; }
body { font-family: 'Segoe UI', 'Helvetica Neue', Arial, sans-serif; font-size: 9.3pt; line-height: 1.42; color: #1f2328; }
h1 { font-size: 21pt; margin: 0 0 4pt; color: #0b2545; }
h1 + p { color: #59636e; margin-top: 0; }
h2 { font-size: 14pt; margin: 15pt 0 6pt; padding-bottom: 3pt; border-bottom: 1.5pt solid #0b2545; color: #0b2545; break-after: avoid; }
h3 { font-size: 11pt; margin: 12pt 0 5pt; color: #24292f; break-after: avoid; }
p, li { orphans: 3; widows: 3; }
ul, ol { padding-left: 18pt; margin: 4pt 0; }
li { margin: 2pt 0; }
code { font-family: Consolas, 'Courier New', monospace; font-size: 8.4pt; background: #f2f4f6; padding: 0 2pt; border-radius: 2pt; }
a { color: #0b5cad; text-decoration: none; }
table { width: 100%; border-collapse: collapse; margin: 6pt 0 10pt; font-size: 8.2pt; line-height: 1.35; }
th { background: #0b2545; color: #fff; text-align: left; padding: 4pt 5pt; font-weight: 600; }
td { padding: 4pt 5pt; border-bottom: 0.6pt solid #d0d7de; vertical-align: top; }
tr:nth-child(even) td { background: #f6f8fa; }
tr, img { break-inside: avoid; }
img { width: 100%; height: auto; border: 0.6pt solid #d0d7de; border-radius: 4pt; margin: 4pt 0 2pt; }
img + em, p > em:only-child { font-size: 8.4pt; color: #59636e; }
hr { border: 0; border-top: 0.6pt solid #d0d7de; margin: 14pt 0; }
del { color: #6e7781; }
.cover-note { font-size: 8.6pt; color: #59636e; margin: 2pt 0 10pt; }
/* The status diagram gets its own landscape page so the boxes are readable. */
@page landscape { size: A4 landscape; margin: 12mm 14mm 16mm 14mm;
  @bottom-left { content: "Commitment Tracker — Product Requirements Document"; font: 8pt 'Segoe UI', Arial, sans-serif; color: #6e7781; }
  @bottom-right { content: "Page " counter(page) " of " counter(pages); font: 8pt 'Segoe UI', Arial, sans-serif; color: #6e7781; } }
.landscape { page: landscape; }
.landscape h2 { margin-top: 0; }
.landscape img { border: 0; margin: 0; }
"""


def app_version():
    match = re.search(r'APP_VERSION = "([^"]+)"', (ROOT / "src" / "app.py").read_text(encoding="utf-8"))
    return match.group(1) if match else "unknown"


def main():
    edge = next((p for p in EDGE_CANDIDATES if p.exists()), None)
    if edge is None:
        sys.exit("Microsoft Edge not found — install it or open docs/_prd_print.html in a browser and print to PDF.")

    version = app_version()
    text = (ROOT / "prd.md").read_text(encoding="utf-8")
    text = re.sub(r"~~(.+?)~~", r"<del>\1</del>", text)  # python-markdown has no ~~strikethrough~~
    body = markdown.markdown(text, extensions=["tables", "fenced_code", "sane_lists"])
    # "Current status" heading + diagram + caption -> their own landscape page
    body = re.sub(r"(<h2>Current status</h2>.*?<img [^>]*status-diagram[^>]*>.*?<p><em>.*?</em></p>)",
                  r'<section class="landscape">\1</section>', body, count=1, flags=re.S)
    note = f'<p class="cover-note">PDF generated {date.today():%d %B %Y} from prd.md · app version {version}</p>'
    body = body.replace("</h1>", "</h1>" + note, 1)
    html = (f'<!doctype html><html><head><meta charset="utf-8">'
            f'<base href="{ROOT.as_uri()}/"><title>Commitment Tracker — PRD v{version}</title>'
            f'<style>{CSS}</style></head><body>{body}</body></html>')

    page = ROOT / "docs" / "_prd_print.html"
    out = ROOT / "docs" / f"Commitment_Tracker_PRD_v{version}.pdf"
    page.write_text(html, encoding="utf-8")
    out.unlink(missing_ok=True)
    # A separate profile stops Edge from silently handing the job to an Edge
    # window that's already open (which then never writes the PDF).
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as profile:  # Edge may still hold cache files
        subprocess.run([str(edge), "--headless=new", "--disable-gpu", "--no-pdf-header-footer",
                        f"--user-data-dir={profile}", f"--print-to-pdf={out}", page.as_uri()],
                       check=True, capture_output=True, timeout=120)
        # The msedge.exe launcher returns before its print process finishes,
        # so wait for the file to appear and stop growing.
        last_size, deadline = -1, time.time() + 90
        while time.time() < deadline:
            size = out.stat().st_size if out.exists() else -1
            if size > 0 and size == last_size:
                break
            last_size = size
            time.sleep(1.5)
    if not out.exists():
        sys.exit("Edge didn't produce the PDF — open docs/_prd_print.html in a browser and print to PDF instead.")
    page.unlink()
    print(f"wrote {out} ({out.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
