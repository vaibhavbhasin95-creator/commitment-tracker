"""
Turns an uploaded transcript file into plain text (0.9.0).

Supported: .txt / .md / .vtt / .srt (text), .docx (Word), .pdf (with a text
layer). The result goes through exactly the same parsing and extraction as
pasted text, so a Word or PDF transcript gets the built-in model or the
chosen AI engine like any other upload.

  * .docx is read with the standard library (it's a ZIP of XML): paragraphs
    in order, table rows as "cell: cell" (so a two-column speaker | words
    table becomes "Speaker: words"), line breaks and tabs kept.
  * .pdf uses pypdf (already in requirements.txt). A scanned PDF has no text
    layer — that's reported clearly rather than returning nothing, since OCR
    isn't supported.
  * The old binary .doc format isn't supported: save it as .docx first.

Never trusts the extension alone for binary formats: a .docx must really be
a ZIP containing word/document.xml, a .pdf must start with %PDF. Sizes are
capped, including the uncompressed size inside the .docx (zip bombs).
"""

import io
import re
import zipfile
from xml.etree import ElementTree

MAX_UPLOAD_BYTES = 20 * 1024 * 1024
MAX_DOCX_XML_BYTES = 60 * 1024 * 1024
MAX_PDF_PAGES = 500
TEXT_EXTENSIONS = (".txt", ".md", ".vtt", ".srt")
SUPPORTED = TEXT_EXTENSIONS + (".docx", ".pdf")

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


class DocumentError(ValueError):
    """A user-facing reason the file couldn't be read."""


def extension(filename):
    match = re.search(r"\.[A-Za-z0-9]+$", filename or "")
    return match.group(0).lower() if match else ""


def extract_text(filename, data):
    """Returns (text, kind) where kind is 'text' | 'docx' | 'pdf'. Raises DocumentError."""
    ext = extension(filename)
    if len(data) > MAX_UPLOAD_BYTES:
        raise DocumentError(f"the file is larger than {MAX_UPLOAD_BYTES // (1024 * 1024)} MB")
    if not data:
        raise DocumentError("the file is empty")
    if ext == ".doc":
        raise DocumentError("old Word .doc files aren't supported — open it in Word and save as .docx")
    if ext == ".docx":
        text, kind = _docx_text(data), "docx"
    elif ext == ".pdf":
        text, kind = _pdf_text(data), "pdf"
    elif ext in TEXT_EXTENSIONS:
        text, kind = _decode_text(data), "text"
    else:
        raise DocumentError(f"'{ext or filename}' files aren't supported — use .txt, .docx or .pdf")
    text = _tidy(text)
    if not text.strip():
        raise DocumentError("no text was found in the file")
    return text, kind


def _decode_text(data):
    if data[:4] == b"PK\x03\x04" or data[:5] == b"%PDF-":
        raise DocumentError("this looks like a Word or PDF file renamed to a text extension — upload it with its real extension")
    for encoding in ("utf-8-sig", "utf-16") if data[:2] in (b"\xff\xfe", b"\xfe\xff") else ("utf-8-sig", "cp1252"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _docx_text(data):
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
        info = archive.getinfo("word/document.xml")
    except (zipfile.BadZipFile, KeyError):
        raise DocumentError("this isn't a valid .docx file (if it's an old .doc, save it as .docx)") from None
    if info.file_size > MAX_DOCX_XML_BYTES:
        raise DocumentError("the document is too large to read")
    try:
        root = ElementTree.fromstring(archive.read(info))
    except ElementTree.ParseError:
        raise DocumentError("the .docx file is damaged and couldn't be read") from None
    body = root.find(f"{W}body")
    if body is None:
        return ""
    lines = []
    for block in body:
        if block.tag == f"{W}p":
            lines.append(_paragraph_text(block))
        elif block.tag == f"{W}tbl":
            for row in block.iter(f"{W}tr"):
                cells = [" ".join(_paragraph_text(p) for p in cell.iter(f"{W}p")).strip() for cell in row.iter(f"{W}tc")]
                cells = [c for c in cells if c]
                if not cells:
                    continue
                if len(cells) == 2 and len(cells[0]) <= 40:
                    lines.append(f"{cells[0].rstrip(':')}: {cells[1]}")
                else:
                    lines.append(" | ".join(cells))
    return "\n".join(lines)


def _paragraph_text(p):
    parts = []
    for node in p.iter():
        if node.tag == f"{W}t" and node.text:
            parts.append(node.text)
        elif node.tag == f"{W}tab":
            parts.append("\t")
        elif node.tag in (f"{W}br", f"{W}cr"):
            parts.append("\n")
    return "".join(parts)


def _pdf_text(data):
    if not data.lstrip()[:5] == b"%PDF-":
        raise DocumentError("this isn't a valid PDF file")
    try:
        from pypdf import PdfReader
        from pypdf.errors import PdfReadError
    except ImportError:
        raise DocumentError("PDF support isn't installed — run: pip install pypdf") from None
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            try:
                reader.decrypt("")
            except Exception:
                raise DocumentError("the PDF is password-protected — remove the password and upload it again") from None
        if len(reader.pages) > MAX_PDF_PAGES:
            raise DocumentError(f"the PDF has more than {MAX_PDF_PAGES} pages")
        pages = [(page.extract_text() or "") for page in reader.pages]
    except DocumentError:
        raise
    except (PdfReadError, ValueError, KeyError, TypeError) as e:
        raise DocumentError(f"the PDF couldn't be read ({e})") from None
    text = "\n".join(pages)
    if len(text.strip()) < 20:
        raise DocumentError("no text layer found — this looks like a scanned PDF. OCR isn't supported; "
                            "export the transcript as text or .docx instead")
    return text


def _tidy(text):
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace(" ", " ").replace("\x00", "")
    text = re.sub(r"[ \t]+\n", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()
