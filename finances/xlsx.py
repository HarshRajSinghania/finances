"""
xlsx.py — an Excel workbook, written with the standard library.

An .xlsx file is a zip of a few XML parts. This writes the smallest set Excel,
LibreOffice, Numbers and Google Sheets all open: one sheet per table, a bold
frozen header with a filter on it, real dates and money formats, and column
widths that fit. No formulas, no charts: the numbers people want to work with,
in the place they already work.

    write("money.xlsx", [Sheet("Transactions", ["Date", "Amount"], rows,
                               formats=["date", "money"])])

Column formats: "text", "date" (a date or ISO string), "money", "int", "pct"
(a fraction: 0.25 shows as 25.0%).
"""

from __future__ import annotations

import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime
from xml.sax.saxutils import escape

_EPOCH = date(1899, 12, 30)
# Style index per format, matching the cellXfs in _STYLES below.
_STYLE = {"text": 0, "header": 1, "date": 2, "money": 3, "int": 4, "pct": 5}

_STYLES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
<numFmts count="3"><numFmt numFmtId="164" formatCode="yyyy-mm-dd"/><numFmt numFmtId="165" formatCode="#,##0.00;[Red]-#,##0.00"/><numFmt numFmtId="166" formatCode="0.0%"/></numFmts>
<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font><font><b/><sz val="11"/><name val="Calibri"/></font></fonts>
<fills count="3"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill><fill><patternFill patternType="solid"><fgColor rgb="FFF1F0EC"/><bgColor indexed="64"/></patternFill></fill></fills>
<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>
<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
<cellXfs count="6"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/><xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1"/><xf numFmtId="164" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/><xf numFmtId="165" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/><xf numFmtId="1" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/><xf numFmtId="166" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/></cellXfs>
<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>
</styleSheet>"""


@dataclass
class Sheet:
    name: str
    header: list[str]
    rows: list[list]
    formats: list[str] = field(default_factory=list)


def col_letter(i: int) -> str:
    """0 -> A, 25 -> Z, 26 -> AA."""
    out = ""
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        out = chr(65 + r) + out
    return out


def _cell(ref: str, value, fmt: str) -> str:
    if value is None or value == "":
        return ""
    if fmt == "date":
        d = value if isinstance(value, date) else _as_date(value)
        if d is not None:
            return f'<c r="{ref}" s="{_STYLE["date"]}"><v>{(d - _EPOCH).days}</v></c>'
    if fmt in ("money", "int", "pct") and isinstance(value, (int, float)) \
            and not isinstance(value, bool):
        return f'<c r="{ref}" s="{_STYLE[fmt]}"><v>{value!r}</v></c>'
    text = escape(str(value)).replace("\n", "&#10;")
    return (f'<c r="{ref}" t="inlineStr" s="{_STYLE["text"]}"><is><t xml:space="preserve">'
            f"{text}</t></is></c>")


def _as_date(v) -> date | None:
    if isinstance(v, datetime):
        return v.date()
    try:
        return date.fromisoformat(str(v)[:10])
    except ValueError:
        return None


def _width(values: list, fmt: str) -> float:
    if fmt == "date":
        return 12
    if fmt in ("money", "int", "pct"):
        return 14
    longest = max((len(str(v)) for v in values if v not in (None, "")), default=8)
    return min(max(longest + 2, 8), 60)


def _sheet_xml(sheet: Sheet) -> str:
    n = len(sheet.header)
    fmts = (sheet.formats + ["text"] * n)[:n]
    widths = [_width([sheet.header[c]] + [r[c] if c < len(r) else "" for r in sheet.rows],
                     fmts[c]) for c in range(n)]
    cols = "".join(f'<col min="{i + 1}" max="{i + 1}" width="{w}" customWidth="1"/>'
                   for i, w in enumerate(widths))
    lines = ['<row r="1">' + "".join(
        f'<c r="{col_letter(c)}1" t="inlineStr" s="{_STYLE["header"]}"><is><t>'
        f"{escape(h)}</t></is></c>" for c, h in enumerate(sheet.header)) + "</row>"]
    for i, row in enumerate(sheet.rows, start=2):
        cells = "".join(_cell(f"{col_letter(c)}{i}", row[c] if c < len(row) else None, fmts[c])
                        for c in range(n))
        lines.append(f'<row r="{i}">{cells}</row>')
    last = f"{col_letter(n - 1)}{max(len(sheet.rows) + 1, 1)}"
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            '<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" '
            'activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>'
            f"<cols>{cols}</cols><sheetData>{''.join(lines)}</sheetData>"
            f'<autoFilter ref="A1:{last}"/></worksheet>')


def _safe_name(name: str, taken: set) -> str:
    """Excel sheet names: 31 characters, none of []:*?/\\, unique."""
    base = "".join("-" if ch in "[]:*?/\\" else ch for ch in name)[:31] or "Sheet"
    out, n = base, 2
    while out.lower() in taken:
        out = f"{base[:28]} {n}"
        n += 1
    taken.add(out.lower())
    return out


def write(path: str, sheets: list[Sheet]) -> str:
    taken: set = set()
    names = [_safe_name(s.name, taken) for s in sheets]
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                   '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                   '<Default Extension="xml" ContentType="application/xml"/>'
                   '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
                   '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
                   + "".join(f'<Override PartName="/xl/worksheets/sheet{i}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
                             for i in range(1, len(sheets) + 1))
                   + "</Types>")
        z.writestr("_rels/.rels",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
                   "</Relationships>")
        z.writestr("xl/workbook.xml",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
                   'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>'
                   + "".join(f'<sheet name="{escape(n, {chr(34): "&quot;"})}" sheetId="{i}" r:id="rId{i}"/>'
                             for i, n in enumerate(names, start=1))
                   + "</sheets>"
                   + "<definedNames>" + "".join(
                       f'<definedName name="_xlnm._FilterDatabase" localSheetId="{i}" hidden="1">'
                       f"&apos;{escape(n)}&apos;!$A$1:${col_letter(len(s.header) - 1)}"
                       f"${max(len(s.rows) + 1, 1)}</definedName>"
                       for i, (n, s) in enumerate(zip(names, sheets)))
                   + "</definedNames></workbook>")
        z.writestr("xl/_rels/workbook.xml.rels",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   + "".join(f'<Relationship Id="rId{i}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{i}.xml"/>'
                             for i in range(1, len(sheets) + 1))
                   + f'<Relationship Id="rId{len(sheets) + 1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
                   "</Relationships>")
        z.writestr("xl/styles.xml", _STYLES)
        for i, s in enumerate(sheets, start=1):
            z.writestr(f"xl/worksheets/sheet{i}.xml", _sheet_xml(s))
    return path
