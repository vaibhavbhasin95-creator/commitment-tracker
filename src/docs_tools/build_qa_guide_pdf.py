"""
Builds the PDF handed to the QA team from docs/QA_Test_Guide.md, with the
same Edge print-to-PDF pipeline and styling as the PRD.

    pip install markdown          # docs-only dependency, not needed by the app
    python src/docs_tools/build_qa_guide_pdf.py

Output: docs/Commitment_Tracker_QA_Test_Guide_v<version>.pdf
"""

from datetime import date

import markdown

from build_prd_pdf import CSS, ROOT, app_version, print_to_pdf

QA_CSS = CSS.replace("Product Requirements Document", "QA Test Guide") + """
blockquote { margin: 6pt 0; padding: 5pt 9pt; border-left: 3pt solid #9a6700; background: #fff8c5; }
blockquote p { margin: 0; }
pre { background: #f2f4f6; border: 0.6pt solid #d0d7de; border-radius: 3pt; padding: 6pt 8pt; white-space: pre-wrap; break-inside: avoid; }
pre code { background: none; padding: 0; }
td:first-child { white-space: nowrap; font-weight: 600; }
h1 + p strong { color: #0b2545; }
"""


def main():
    version = app_version()
    text = (ROOT / "docs" / "QA_Test_Guide.md").read_text(encoding="utf-8")
    text = text.replace("{{VERSION}}", version).replace("{{DATE}}", f"{date.today():%d %B %Y}")
    body = markdown.markdown(text, extensions=["tables", "fenced_code", "sane_lists"])
    html = (f'<!doctype html><html><head><meta charset="utf-8">'
            f'<title>Commitment Tracker — QA Test Guide v{version}</title>'
            f'<style>{QA_CSS}</style></head><body>{body}</body></html>')
    print_to_pdf(html, ROOT / "docs" / "_qa_guide_print.html",
                 ROOT / "docs" / f"Commitment_Tracker_QA_Test_Guide_v{version}.pdf")


if __name__ == "__main__":
    main()
