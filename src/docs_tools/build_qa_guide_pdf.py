"""
Builds the PDFs handed to the QA team, with the same Edge print-to-PDF
pipeline and styling as the PRD:

    docs/QA_Test_Guide.md    -> docs/Commitment_Tracker_QA_Test_Guide_v<version>.pdf
    docs/E2E_Test_Runbook.md -> docs/Commitment_Tracker_E2E_Test_Runbook_v<version>.pdf

    pip install markdown          # docs-only dependency, not needed by the app
    python src/docs_tools/build_qa_guide_pdf.py
"""

from datetime import date

import markdown

from build_prd_pdf import CSS, ROOT, app_version, print_to_pdf

# (source in docs/, output name, title for the footer)
DOCUMENTS = [
    ("QA_Test_Guide.md", "Commitment_Tracker_QA_Test_Guide", "QA Test Guide"),
    ("E2E_Test_Runbook.md", "Commitment_Tracker_E2E_Test_Runbook", "End-to-End Test Runbook"),
]

EXTRA_CSS = """
blockquote { margin: 6pt 0; padding: 5pt 9pt; border-left: 3pt solid #9a6700; background: #fff8c5; }
blockquote p { margin: 0; }
pre { background: #f2f4f6; border: 0.6pt solid #d0d7de; border-radius: 3pt; padding: 6pt 8pt; white-space: pre-wrap; break-inside: avoid; }
pre code { background: none; padding: 0; }
td:first-child { white-space: nowrap; font-weight: 600; }
h1 + p strong { color: #0b2545; }
"""


def build(source, output, title, version):
    text = (ROOT / "docs" / source).read_text(encoding="utf-8")
    text = text.replace("{{VERSION}}", version).replace("{{DATE}}", f"{date.today():%d %B %Y}")
    body = markdown.markdown(text, extensions=["tables", "fenced_code", "sane_lists"])
    css = CSS.replace("Product Requirements Document", title) + EXTRA_CSS
    html = (f'<!doctype html><html><head><meta charset="utf-8">'
            f'<title>Commitment Tracker — {title} v{version}</title>'
            f'<style>{css}</style></head><body>{body}</body></html>')
    print_to_pdf(html, ROOT / "docs" / f"_{output}_print.html", ROOT / "docs" / f"{output}_v{version}.pdf")


def main():
    version = app_version()
    for source, output, title in DOCUMENTS:
        build(source, output, title, version)


if __name__ == "__main__":
    main()
