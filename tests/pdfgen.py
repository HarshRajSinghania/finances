"""
The smallest PDF that pdfplumber reads: one page of text lines, or a page with
no text layer at all (what a scanner produces). For tests only.
"""

from __future__ import annotations


def _pdf(content: bytes) -> bytes:
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out, offsets = bytearray(b"%PDF-1.4\n"), []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    return bytes(out)


def text_pdf(lines: list[str]) -> bytes:
    """Each line at the left margin, top to bottom, 10pt Helvetica."""
    ops = [b"BT /F1 10 Tf 14 TL 50 750 Td"]
    for line in lines:
        safe = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        ops.append(b"(" + safe.encode("latin-1") + b") Tj T*")
    ops.append(b"ET")
    return _pdf(b"\n".join(ops))


def scanned_pdf() -> bytes:
    """A page with nothing to extract: a filled box where the text would be."""
    return _pdf(b"0.9 g 50 50 500 700 re f")
