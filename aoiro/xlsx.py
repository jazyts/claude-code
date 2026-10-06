"""標準ライブラリだけで .xlsx を読み書きする最小限の実装."""

import datetime
import io
import re
import zipfile
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

NS = {
    "m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "rel": "http://schemas.openxmlformats.org/package/2006/relationships",
}
EXCEL_EPOCH = datetime.date(1899, 12, 30)


def _col_index(ref):
    letters = re.match(r"[A-Z]+", ref).group(0)
    n = 0
    for ch in letters:
        n = n * 26 + ord(ch) - 64
    return n - 1


def _text(el):
    return "".join(t.text or "" for t in el.iter(f"{{{NS['m']}}}t"))


def read(data):
    """xlsx のバイト列を {シート名: [[セル値, ...], ...]} にする。数値は float、文字列は str。"""
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        names = set(z.namelist())
        shared = []
        if "xl/sharedStrings.xml" in names:
            root = ET.fromstring(z.read("xl/sharedStrings.xml"))
            shared = [_text(si) for si in root.findall("m:si", NS)]
        rels = {}
        root = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
        for rel in root.findall("rel:Relationship", NS):
            target = rel.get("Target").lstrip("/")
            rels[rel.get("Id")] = target if target.startswith("xl/") else "xl/" + target
        wb = ET.fromstring(z.read("xl/workbook.xml"))
        sheets = {}
        for s in wb.findall("m:sheets/m:sheet", NS):
            path = rels[s.get(f"{{{NS['r']}}}id")]
            sheets[s.get("name")] = _read_sheet(ET.fromstring(z.read(path)), shared)
        return sheets


def _read_sheet(root, shared):
    rows = []
    for row in root.findall("m:sheetData/m:row", NS):
        values = {}
        for c in row.findall("m:c", NS):
            t = c.get("t")
            v = c.find("m:v", NS)
            if t == "s":
                value = shared[int(v.text)] if v is not None else ""
            elif t == "inlineStr":
                is_ = c.find("m:is", NS)
                value = _text(is_) if is_ is not None else ""
            elif t in ("str", "e"):
                value = v.text if v is not None else ""
            elif t == "b":
                value = v.text == "1" if v is not None else ""
            else:
                value = float(v.text) if v is not None and v.text else ""
            values[_col_index(c.get("r"))] = value
        idx = int(row.get("r")) - 1
        while len(rows) < idx:
            rows.append([])
        width = max(values) + 1 if values else 0
        rows.append([values.get(i, "") for i in range(width)])
    return rows


def serial_to_date(value):
    return EXCEL_EPOCH + datetime.timedelta(days=int(value))


def _col_letter(i):
    s = ""
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


def write(sheets):
    """{シート名: [[値, ...], ...]} から xlsx のバイト列を作る（1行目は太字の見出し）。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        n = len(sheets)
        z.writestr("[Content_Types].xml", (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
            + "".join(f'<Override PartName="/xl/worksheets/sheet{i + 1}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>' for i in range(n))
            + "</Types>"))
        z.writestr("_rels/.rels", (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
            "</Relationships>"))
        z.writestr("xl/workbook.xml", (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<workbook xmlns="{NS["m"]}" xmlns:r="{NS["r"]}"><sheets>'
            + "".join(f'<sheet name="{escape(name)}" sheetId="{i + 1}" r:id="rId{i + 1}"/>' for i, name in enumerate(sheets))
            + "</sheets></workbook>"))
        z.writestr("xl/_rels/workbook.xml.rels", (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            + "".join(f'<Relationship Id="rId{i + 1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{i + 1}.xml"/>' for i in range(n))
            + f'<Relationship Id="rId{n + 1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
            "</Relationships>"))
        z.writestr("xl/styles.xml", (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<styleSheet xmlns="{NS["m"]}">'
            '<fonts count="2"><font><sz val="11"/><name val="Yu Gothic"/></font><font><b/><sz val="11"/><name val="Yu Gothic"/></font></fonts>'
            '<fills count="2"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill></fills>'
            '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
            '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
            '<cellXfs count="2"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
            '<xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1"/></cellXfs>'
            '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles></styleSheet>'))
        for i, rows in enumerate(sheets.values()):
            out = []
            for r, row in enumerate(rows):
                cells = []
                for c, value in enumerate(row):
                    ref = f"{_col_letter(c)}{r + 1}"
                    style = ' s="1"' if r == 0 else ""
                    if isinstance(value, (int, float)) and not isinstance(value, bool):
                        cells.append(f'<c r="{ref}"{style}><v>{value}</v></c>')
                    elif value not in (None, ""):
                        cells.append(f'<c r="{ref}" t="inlineStr"{style}><is><t>{escape(str(value))}</t></is></c>')
                out.append(f'<row r="{r + 1}">{"".join(cells)}</row>')
            widths = "".join(f'<col min="{c + 1}" max="{c + 1}" width="16" customWidth="1"/>'
                             for c in range(max((len(r) for r in rows), default=0)))
            z.writestr(f"xl/worksheets/sheet{i + 1}.xml", (
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                f'<worksheet xmlns="{NS["m"]}">{f"<cols>{widths}</cols>" if widths else ""}<sheetData>{"".join(out)}</sheetData></worksheet>'))
    return buf.getvalue()
