"""PDF から文字と位置を取り出す最小限の実装（標準ライブラリのみ）.

Excel や「Microsoft Print to PDF」などで作られた、文字情報を持つ PDF が対象です。
スキャン画像の PDF（文字が画像になっているもの）は読めません。
"""

import re
import zlib

WS = b" \t\r\n\f\x00"
DELIM = b"()<>[]{}/%"


class Ref:
    __slots__ = ("num",)

    def __init__(self, num):
        self.num = num


class Name(str):
    pass


class Op(str):
    pass


# ---------------------------------------------------------------- 字句解析

def _tokens(data, pos=0):
    n = len(data)
    while pos < n:
        c = data[pos]
        if c in WS:
            pos += 1
        elif c == 0x25:  # % コメント
            while pos < n and data[pos] not in b"\r\n":
                pos += 1
        elif c == 0x2F:  # /Name
            end = pos + 1
            while end < n and data[end] not in WS and data[end] not in DELIM:
                end += 1
            raw = data[pos + 1:end]
            raw = re.sub(rb"#([0-9A-Fa-f]{2})", lambda m: bytes([int(m.group(1), 16)]), raw)
            yield Name(raw.decode("latin-1")), end
            pos = end
        elif c == 0x28:  # (literal string)
            depth, i, out = 1, pos + 1, bytearray()
            while i < n and depth:
                ch = data[i]
                if ch == 0x5C:  # バックスラッシュ
                    i += 1
                    e = data[i:i + 1]
                    if e in b"01234567":
                        m = re.match(rb"[0-7]{1,3}", data[i:i + 3])
                        out.append(int(m.group(0), 8) & 0xFF)
                        i += len(m.group(0))
                        continue
                    if e == b"\r":
                        i += 2 if data[i + 1:i + 2] == b"\n" else 1
                        continue
                    if e == b"\n":
                        i += 1
                        continue
                    out += {b"n": b"\n", b"r": b"\r", b"t": b"\t", b"b": b"\b", b"f": b"\f"}.get(e, e)
                    i += 1
                    continue
                if ch == 0x28:
                    depth += 1
                elif ch == 0x29:
                    depth -= 1
                    if not depth:
                        i += 1
                        break
                out.append(ch)
                i += 1
            yield bytes(out), i
            pos = i
        elif c == 0x3C and data[pos + 1:pos + 2] == b"<":
            yield Op("<<"), pos + 2
            pos += 2
        elif c == 0x3E and data[pos + 1:pos + 2] == b">":
            yield Op(">>"), pos + 2
            pos += 2
        elif c == 0x3C:  # <hex string>
            end = data.index(b">", pos)
            hexs = re.sub(rb"\s", b"", data[pos + 1:end])
            if len(hexs) % 2:
                hexs += b"0"
            yield bytes.fromhex(hexs.decode("latin-1")), end + 1
            pos = end + 1
        elif c in b"[]{}":
            yield Op(chr(c)), pos + 1
            pos += 1
        else:
            end = pos
            while end < n and data[end] not in WS and data[end] not in DELIM:
                end += 1
            if end == pos:
                end += 1
            word = data[pos:end].decode("latin-1")
            try:
                yield (float(word) if "." in word else int(word)), end
            except ValueError:
                yield Op(word), end
            pos = end


def _parse_objects(tokens):
    """トークン列を PDF オブジェクトに組み立てる（R 参照・配列・辞書）。演算子はそのまま残す。"""
    stack = [[]]
    for tok, _ in tokens:
        if isinstance(tok, Op):
            if tok in ("[", "<<"):
                stack.append([tok])
                continue
            if tok == "]" and len(stack) > 1:
                items = stack.pop()[1:]
                stack[-1].append(items)
                continue
            if tok == ">>" and len(stack) > 1:
                items = stack.pop()[1:]
                stack[-1].append({items[i]: items[i + 1] for i in range(0, len(items) - 1, 2)})
                continue
            if tok == "R" and len(stack[-1]) >= 2 and isinstance(stack[-1][-1], int) \
                    and isinstance(stack[-1][-2], int):
                stack[-1].pop()
                stack[-1].append(Ref(stack[-1].pop()))
                continue
        stack[-1].append(tok)
    return stack[0]


def parse_value(data):
    items = _parse_objects(_tokens(data))
    return items[0] if items else None


# ---------------------------------------------------------------- 文書構造

class Document:
    def __init__(self, data):
        self.data = data
        self.objects = {}  # 番号 -> (値, ストリームのバイト列 or None)
        for m in re.finditer(rb"(\d+)\s+(\d+)\s+obj\b", data):
            num = int(m.group(1))
            start = m.end()
            end = data.find(b"endobj", start)
            if end < 0:
                continue
            body = data[start:end]
            stream = None
            sm = re.search(rb"stream\r?\n", body)
            if sm:
                head = parse_value(body[:sm.start()])
                raw = body[sm.end():]
                length = head.get("Length") if isinstance(head, dict) else None
                if isinstance(length, int) and length <= len(raw):
                    raw = raw[:length]
                else:
                    raw = raw[:raw.rfind(b"endstream")].rstrip(b"\r\n")
                self.objects[num] = (head, raw)
            else:
                self.objects[num] = (parse_value(body), None)
        self._expand_object_streams()

    def _expand_object_streams(self):
        for num, (head, raw) in list(self.objects.items()):
            if isinstance(head, dict) and head.get("Type") == "ObjStm":
                data = self.stream_data(num)
                n, first = head.get("N", 0), head.get("First", 0)
                nums = [t for t, _ in _tokens(data[:first])]
                for i in range(n):
                    onum, off = nums[2 * i], nums[2 * i + 1]
                    nxt = nums[2 * i + 3] if i + 1 < n else len(data) - first
                    if onum not in self.objects:
                        self.objects[onum] = (parse_value(data[first + off:first + nxt]), None)

    def get(self, value):
        seen = 0
        while isinstance(value, Ref) and seen < 50:
            value = self.objects.get(value.num, (None, None))[0]
            seen += 1
        return value

    def stream_data(self, ref):
        num = ref.num if isinstance(ref, Ref) else ref
        head, raw = self.objects.get(num, (None, None))
        if raw is None:
            return b""
        filters = self.get(head.get("Filter")) if isinstance(head, dict) else None
        if isinstance(filters, str):
            filters = [filters]
        for f in filters or []:
            if f == "FlateDecode":
                try:
                    raw = zlib.decompress(raw)
                except zlib.error:
                    raw = zlib.decompressobj().decompress(raw)
            else:
                return b""  # 画像など未対応のフィルタ
        return raw

    def stream_of(self, value):
        """値が参照先のストリームならその中身"""
        if isinstance(value, Ref):
            return self.stream_data(value)
        return b""

    def pages(self):
        root = None
        for num, (head, _) in self.objects.items():
            if isinstance(head, dict) and head.get("Type") == "Catalog":
                root = head
                break
        result = []

        def walk(node, inherited):
            node = self.get(node)
            if not isinstance(node, dict):
                return
            res = node.get("Resources", inherited)
            if node.get("Type") == "Pages":
                for kid in self.get(node.get("Kids")) or []:
                    walk(kid, res)
            else:
                result.append((node, self.get(res) or {}))

        if root:
            walk(root.get("Pages"), None)
        else:  # カタログが見つからない場合はページらしきものを順に
            for num, (head, _) in sorted(self.objects.items()):
                if isinstance(head, dict) and head.get("Type") == "Page":
                    result.append((head, self.get(head.get("Resources")) or {}))
        return result


# ---------------------------------------------------------------- フォント

class Font:
    def __init__(self, doc, fdict):
        fdict = doc.get(fdict) or {}
        self.two_byte = fdict.get("Subtype") == "Type0"
        self.cmap = {}
        tu = fdict.get("ToUnicode")
        if isinstance(tu, Ref):
            self._parse_cmap(doc.stream_data(tu))

    def _parse_cmap(self, data):
        def uni(b):
            try:
                return b.decode("utf-16-be")
            except UnicodeDecodeError:
                return ""

        for block in re.findall(rb"beginbfchar(.*?)endbfchar", data, re.S):
            items = [t for t, _ in _tokens(block)]
            for i in range(0, len(items) - 1, 2):
                if isinstance(items[i], bytes) and isinstance(items[i + 1], bytes):
                    self.cmap[items[i]] = uni(items[i + 1])
        for block in re.findall(rb"beginbfrange(.*?)endbfrange", data, re.S):
            items = _parse_objects(_tokens(block))
            for i in range(0, len(items) - 2, 3):
                lo, hi, dst = items[i], items[i + 1], items[i + 2]
                if not (isinstance(lo, bytes) and isinstance(hi, bytes)):
                    continue
                width = len(lo)
                a, b = int.from_bytes(lo, "big"), int.from_bytes(hi, "big")
                if b - a > 65535:
                    continue
                for k, code in enumerate(range(a, b + 1)):
                    key = code.to_bytes(width, "big")
                    if isinstance(dst, list):
                        if k < len(dst) and isinstance(dst[k], bytes):
                            self.cmap[key] = uni(dst[k])
                    elif isinstance(dst, bytes):
                        base = int.from_bytes(dst, "big") + k
                        self.cmap[key] = uni(base.to_bytes(len(dst), "big"))

    def decode(self, s):
        if self.two_byte:
            return "".join(self.cmap.get(s[i:i + 2], "") for i in range(0, len(s) - 1, 2))
        if self.cmap:
            return "".join(self.cmap.get(bytes([c]), chr(c)) for c in s)
        return s.decode("cp1252", errors="replace")


# ---------------------------------------------------------------- 本文

def _mul(a, b):
    return [
        a[0] * b[0] + a[1] * b[2], a[0] * b[1] + a[1] * b[3],
        a[2] * b[0] + a[3] * b[2], a[2] * b[1] + a[3] * b[3],
        a[4] * b[0] + a[5] * b[2] + b[4], a[4] * b[1] + a[5] * b[3] + b[5],
    ]


def _text_width(text, size):
    return sum(size * (0.5 if ord(ch) < 0x2E80 else 1.0) for ch in text)


def runs(data):
    """PDF 内の文字列を [(ページ, x, y, 文字サイズ, 文字列)] で返す（y は上が大きい）"""
    doc = Document(data)
    out = []
    for pno, (page, res) in enumerate(doc.pages()):
        fonts_dict = doc.get(res.get("Font")) or {}
        fonts = {}
        contents = doc.get(page.get("Contents"))
        if isinstance(contents, list):
            stream = b"\n".join(doc.stream_data(c) for c in contents)
        else:
            stream = doc.stream_of(page.get("Contents"))
        _run_content(doc, stream, fonts_dict, fonts, pno, out, depth=0, res=res)
    return out


def _run_content(doc, stream, fonts_dict, fonts, pno, out, depth, res, ctm0=None):
    ctm = list(ctm0 or [1, 0, 0, 1, 0, 0])
    gstack = []
    tm = [1, 0, 0, 1, 0, 0]
    tlm = list(tm)
    font = None
    size = 1
    leading = 0
    operands = []
    for tok in _parse_objects(_tokens(stream)):
        if not isinstance(tok, Op):
            operands.append(tok)
            continue
        op = str(tok)
        try:
            if op == "q":
                gstack.append(list(ctm))
            elif op == "Q":
                ctm = gstack.pop() if gstack else ctm
            elif op == "cm" and len(operands) >= 6:
                ctm = _mul([float(v) for v in operands[-6:]], ctm)
            elif op == "BT":
                tm, tlm = [1, 0, 0, 1, 0, 0], [1, 0, 0, 1, 0, 0]
            elif op == "Tf" and len(operands) >= 2:
                name, size = operands[-2], float(operands[-1])
                if name not in fonts:
                    fonts[name] = Font(doc, fonts_dict.get(name))
                font = fonts[name]
            elif op == "TL" and operands:
                leading = float(operands[-1])
            elif op in ("Td", "TD") and len(operands) >= 2:
                tx, ty = float(operands[-2]), float(operands[-1])
                if op == "TD":
                    leading = -ty
                tlm = _mul([1, 0, 0, 1, tx, ty], tlm)
                tm = list(tlm)
            elif op == "Tm" and len(operands) >= 6:
                tlm = [float(v) for v in operands[-6:]]
                tm = list(tlm)
            elif op == "T*":
                tlm = _mul([1, 0, 0, 1, 0, -leading], tlm)
                tm = list(tlm)
            elif op in ("Tj", "'", '"', "TJ") and font is not None:
                if op in ("'", '"'):
                    tlm = _mul([1, 0, 0, 1, 0, -leading], tlm)
                    tm = list(tlm)
                arg = operands[-1] if operands else b""
                parts = arg if isinstance(arg, list) else [arg]
                text = ""
                for p in parts:
                    if isinstance(p, bytes):
                        text += font.decode(p)
                    elif isinstance(p, (int, float)) and p < -250:
                        text += " "
                m = _mul(tm, ctm)
                scale = (m[2] ** 2 + m[3] ** 2) ** 0.5 or 1
                if text.strip():
                    out.append((pno, m[4], m[5], size * scale, text))
                # 次の文字列のために大まかに進める（幅情報は使わない）
                tm = _mul([1, 0, 0, 1, _text_width(text, size), 0], tm)
            elif op == "Do" and operands and depth < 3:
                xobjs = doc.get(res.get("XObject")) or {}
                ref = xobjs.get(operands[-1])
                head = doc.get(ref)
                if isinstance(head, dict) and head.get("Subtype") == "Form":
                    sub_res = doc.get(head.get("Resources")) or res
                    sub_fonts = doc.get(sub_res.get("Font")) or fonts_dict
                    matrix = head.get("Matrix") or [1, 0, 0, 1, 0, 0]
                    _run_content(doc, doc.stream_data(ref), sub_fonts, {}, pno, out, depth + 1,
                                 sub_res, _mul([float(v) for v in matrix], ctm))
        except (TypeError, ValueError, IndexError):
            pass
        operands = []


def lines(data):
    """文字列を行ごとにまとめ、各行を左から順のセル（文字列）の配列で返す。"""
    items = runs(data)
    result = []
    for pno in sorted({r[0] for r in items}):
        page = sorted((r for r in items if r[0] == pno), key=lambda r: (-r[2], r[1]))
        rows = []
        for r in page:
            for row in rows:
                if abs(row["y"] - r[2]) <= max(row["size"], r[3]) * 0.4:
                    row["runs"].append(r)
                    break
            else:
                rows.append({"y": r[2], "size": r[3], "runs": [r]})
        rows.sort(key=lambda row: -row["y"])
        for row in rows:
            cells = []
            end = None
            for _, x, _, size, text in sorted(row["runs"], key=lambda r: r[1]):
                if cells and end is not None and x - end < size * 0.6:
                    cells[-1] += text
                else:
                    cells.append(text)
                end = x + _text_width(text, size)
            cells = [c.strip() for c in cells if c.strip()]
            if cells:
                result.append(cells)
    return result


def text(data):
    return "\n".join(" ".join(cells) for cells in lines(data))
