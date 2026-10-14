"""Writing a PDF with the standard library.

The Word export exists because a protocol gets printed, signed and taped
inside a hood. A PDF exists for the same reason plus one more: it opens the
same on every machine, including the one in the lab that has no Word, and it
is what a journal, a collaborator or a quality system actually files.

The same blocks the Word writer takes — a title, headings, paragraphs,
bullets, numbered steps, check lines, caveats and tables — are laid out here
onto A4 pages, so `lab_protocol.build` and the other renderers write to either
writer and the two documents say the same things in the same order.

The text is set in real fonts, embedded and subset to the glyphs the document
uses (`fonts/`, read by `ttf.py`): Carlito, metric-compatible with the Calibri
the Word copy names, and DejaVu Sans, per character, for a symbol Carlito
lacks. The fourteen built-in PDF fonts this writer used before hold
Windows-1252 and nothing else, so a scientific document's own characters came
out wrong — MARCO⁺ as "MARCO?", ≥ as ">=", IL-1α as "IL-1alpha". Now they print
as written. A character neither font has is spelled out or shown as `?`,
visibly, never silently dropped.

Bold and italic runs inside a paragraph keep their weight, lines are wrapped
on the fonts' own advance widths, and a table row that does not fit moves to
the next page with its header repeated. What this is not is a typesetter:
no hyphenation, no kerning, no justification — ragged right reads well and
cannot produce rivers.

The output is deterministic: the same protocol exported twice is the same
bytes.
"""
from __future__ import annotations

import hashlib
import re
import zlib
from pathlib import Path

from . import ttf

# ── page geometry (points) ───────────────────────────────────────────────
PAGE_W, PAGE_H = 595.0, 842.0          # A4
MARGIN = 56.0
TEXT_W = PAGE_W - 2 * MARGIN
FOOT_Y = 28.0
HEAD_DROP = 30.0                        # running header, down from the top edge

# ── fonts ────────────────────────────────────────────────────────────────
FONT_DIR = Path(__file__).with_name('fonts')
FILES = {'R': 'Carlito-Regular.ttf', 'B': 'Carlito-Bold.ttf', 'I': 'Carlito-Italic.ttf',
         'S': 'DejaVuSans.ttf'}
NAMES = {'R': 'Carlito-Regular', 'B': 'Carlito-Bold', 'I': 'Carlito-Italic',
         'S': 'DejaVuSans'}
FALLBACK = 'S'
# The style keys the renderers and the Word writer's vocabulary use.
_KEY = {'F1': 'R', 'F2': 'B', 'F3': 'I', 'F4': 'B', 'F5': 'R',
        'R': 'R', 'B': 'B', 'I': 'I', 'S': 'S'}

# For a character no embedded font has: visible, never dropped.
SPELL = {'☐': '[ ]', '☑': '[x]', '✓': 'v', '✗': 'x'}
_INVISIBLE = dict.fromkeys(map(ord, '­​‌‍⁠﻿'))
_SPACES = dict.fromkeys(map(ord, '       \t\r\n'), ' ')

# ── colours (rg/RG operands) ─────────────────────────────────────────────
INK = '0.106 0.141 0.125'              # #1B2420
GREY = '0.380 0.427 0.408'             # #616D68
FAINT = '0.560 0.600 0.584'            # #8F9995
ACCENT = '0.059 0.478 0.271'           # #0F7A45, the brand's deep green
ACCENT_SOFT = '0.784 0.882 0.827'      # #C8E1D3
RULE = '0.800 0.835 0.816'             # #CCD5D0
HAIR = '0.878 0.902 0.890'             # #E0E6E3
HEAD_TINT = '0.933 0.953 0.941'        # #EEF3F0
ZEBRA = '0.969 0.976 0.973'            # #F7F9F8
DARK = INK


def _face(key):
    return ttf.load(str(FONT_DIR / FILES[key]))


def _rgb(hexcolour):
    """'FFF1C2' -> '1 0.945 0.761', the fill operands a PDF wants."""
    h = str(hexcolour).lstrip('#')
    return ' '.join(f'{int(h[i:i + 2], 16) / 255:.3f}' for i in (0, 2, 4))


def _plain(text):
    """The text as it will be set: controls and invisibles out, odd spaces as spaces,
    and a character no embedded font holds spelled out rather than lost."""
    s = '' if text is None else str(text)
    s = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', s)
    s = s.translate(_INVISIBLE).translate(_SPACES)
    main, fall = _face('R'), _face(FALLBACK)
    if all(main.has(c) or fall.has(c) for c in s):
        return s
    return ''.join(c if main.has(c) or fall.has(c) else SPELL.get(c, '?') for c in s)


def _split(text, key):
    """[(font key, text)] — each character in the style's face, or the fallback's."""
    face, out = _face(key), []
    for ch in text:
        k = key if face.has(ch) or not _face(FALLBACK).has(ch) else FALLBACK
        if out and out[-1][0] == k:
            out[-1][1].append(ch)
        else:
            out.append((k, [ch]))
    return [(k, ''.join(cs)) for k, cs in out]


def _width(text, size, key='R'):
    key = _KEY.get(key, key)
    return sum(_face(k).width(t, size) for k, t in _split(_plain(text), key))


def _words(runs):
    """Styled runs -> words, each a list of (text, key) pieces. Whitespace separates
    words; a word may change style inside itself ('**MARCO**⁺')."""
    words, cur = [], []
    for text, key in runs:
        for part in re.split(r'(\s+)', _plain(text)):
            if not part:
                continue
            if part.isspace():
                if cur:
                    words.append(cur)
                    cur = []
            else:
                cur.append((part, key))
    if cur:
        words.append(cur)
    return words


def _wrap_runs(runs, size, width):
    """Lines, each a list of (text, key), no wider than *width*. A word wider than
    the line is broken at the width rather than overflowing the page."""
    lines, line, lw = [], [], 0.0
    for w in _words(runs):
        ww = sum(_width(t, size, k) for t, k in w)
        if line:
            sp = _width(' ', size, w[0][1])
            if lw + sp + ww <= width:
                line += [(' ', w[0][1])] + w
                lw += sp + ww
                continue
            lines.append(line)
            line, lw = [], 0.0
        if ww <= width:
            line, lw = list(w), ww
            continue
        chunk, cw = [], 0.0
        for t, k in w:
            for ch in t:
                c = _width(ch, size, k)
                if chunk and cw + c > width:
                    lines.append(chunk)
                    chunk, cw = [], 0.0
                chunk.append((ch, k))
                cw += c
        line, lw = chunk, cw
    if line:
        lines.append(line)
    return [_merge(ln) for ln in lines] or [[]]


def _merge(line):
    out = []
    for t, k in line:
        if out and out[-1][1] == k:
            out[-1] = (out[-1][0] + t, k)
        else:
            out.append((t, k))
    return out


def _wrap(text, size, width, bold=False):
    """Plain-text lines of *text*, for a caller that has no runs."""
    key = 'B' if bold else 'R'
    return [''.join(t for t, _ in ln) for ln in _wrap_runs([(text, key)], size, width)]


def _run_key(kw, base):
    kw = kw or {}
    if kw.get('bold'):
        return 'B'
    if kw.get('italic'):
        return 'B' if base == 'B' else 'I'
    return base


# ── block styles: (font key, size, leading, space before, space after, colour) ──
STYLE = {
    'Title': ('B', 22, 26, 0, 4, INK), 'Subtitle': ('I', 11.5, 15, 0, 6, GREY),
    'Heading1': ('B', 15, 19, 16, 8, INK), 'Heading2': ('B', 12, 15, 11, 4, INK),
    'Heading3': ('B', 10.5, 13.5, 8, 2, GREY), 'Normal': ('R', 10, 14, 0, 6, INK),
    'Caveat': ('I', 9, 12.2, 2, 8, GREY), 'ListBullet': ('R', 10, 13.6, 0, 3, INK),
    'ListNumber': ('R', 10, 13.6, 0, 3, INK), 'Check': ('R', 10, 13.6, 0, 3, INK),
}


class Document:
    """The same blocks as `docx.Document`, laid out as pages. Build it, then `tobytes`."""

    def __init__(self, title=None, author='BioSense', footer=None, landscape=False):
        self.title = title
        self.author = author
        self.footer = footer
        # A one-page plan reads across: stages left to right, arms down.
        self.pw, self.ph = (PAGE_H, PAGE_W) if landscape else (PAGE_W, PAGE_H)
        self.tw = self.pw - 2 * MARGIN
        self._pages = []                 # each a list of content-stream lines
        self._cur = None
        self._y = 0.0
        self._steps = 0
        self._used = {k: {} for k in FILES}          # key -> {glyph id: character}
        self._new_page()

    # ── pages ──────────────────────────────────────────────────────────
    def _new_page(self):
        self._cur = []
        self._pages.append(self._cur)
        self._y = self.ph - MARGIN

    def _need(self, height):
        if self._y - height < MARGIN + 8 and self._cur:
            self._new_page()

    def page_break(self):
        if self._cur:
            self._new_page()
        return self

    # ── drawing ────────────────────────────────────────────────────────
    def _show_str(self, x, y, segs, size, color=INK):
        """One BT block: every segment in its face, the fallback where it lacks a glyph."""
        parts = [f'BT {color} rg {x:.2f} {y:.2f} Td']
        for text, key in segs:
            for k, t in _split(_plain(text), _KEY.get(key, key)):
                face, used = _face(k), self._used[k]
                gids = []
                for ch in t:
                    g = face.gid(ch)
                    used.setdefault(g, ch)
                    gids.append(g)
                parts.append(f'/{k} {size:g} Tf <{"".join(f"{g:04X}" for g in gids)}> Tj')
        parts.append('ET')
        return ' '.join(parts)

    def _show(self, x, y, segs, size, color=INK):
        self._cur.append(self._show_str(x, y, segs, size, color))

    def _text(self, x, y, s, font='R', size=10, color=INK):
        self._show(x, y, [(s, font)], size, color)

    def _line(self, x1, y1, x2, y2, color=RULE, w=0.6):
        self._cur.append(f'{color} RG {w:g} w {x1:.2f} {y1:.2f} m {x2:.2f} {y2:.2f} l S')

    def _rect(self, x, y, w, h, fill):
        self._cur.append(f'{fill} rg {x:.2f} {y:.2f} {w:.2f} {h:.2f} re f')

    # ── blocks ─────────────────────────────────────────────────────────
    def para(self, text='', *, style=None, bold=False, italic=False, runs=None):
        style = style if style in STYLE else 'Normal'
        key, size, leading, before, after, color = STYLE[style]
        if style == 'Normal':
            key = 'B' if bold else 'I' if italic else 'R'
        if runs is not None:
            segs = [(t, _run_key(kw, key)) for t, kw in runs]
        else:
            segs = [(text, key)]
        if style.startswith('Heading'):
            self._need(before + leading * 3)      # never a heading alone at a page foot
        self._y -= before
        x, width, prefix = MARGIN, self.tw, None
        if style == 'Caveat':
            x, width = MARGIN + 11, self.tw - 11
            top = self._y
        if style == 'ListBullet':
            x, width, prefix = MARGIN + 15, self.tw - 15, ('•', 10, 'R')
        if style == 'ListNumber':
            self._steps += 1
            x, width, prefix = MARGIN + 19, self.tw - 19, (f'{self._steps}.', 19, 'B')
        if style == 'Check':
            x, width = MARGIN + 17, self.tw - 17
        lines = _wrap_runs(segs, size, width)
        if style == 'Check':
            self._need(leading)
            y0 = self._y - leading + 2.2
            self._cur.append(f'{GREY} RG 0.8 w {MARGIN + 1:.2f} {y0:.2f} 8.5 8.5 re S')
        first = True
        for ln in lines:
            self._need(leading)
            self._y -= leading
            if first and prefix is not None:
                self._show(x - prefix[1], self._y, [(prefix[0], prefix[2])], size, ACCENT)
            self._show(x, self._y, ln, size, color)
            first = False
        if style == 'Caveat' and self._y < top:
            self._line(MARGIN + 2.5, top - 2, MARGIN + 2.5, self._y - 3, color=ACCENT_SOFT, w=2.4)
        if style == 'Heading1':
            self._line(MARGIN, self._y - 4.5, self.pw - MARGIN, self._y - 4.5, color=ACCENT, w=0.9)
        self._y -= after
        return self

    def heading(self, text, level=1):
        if level <= 1:
            self._steps = 0              # a new section restarts its step numbers
        return self.para(text, style=f'Heading{min(max(level, 1), 3)}')

    def title_block(self, text, subtitle=None):
        self.para(text, style='Title')
        if subtitle:
            self.para(subtitle, style='Subtitle')
        self._y -= 4
        self._line(MARGIN, self._y, self.pw - MARGIN, self._y, color=ACCENT, w=1.6)
        self._y -= 14
        return self

    def bullet(self, text=None, *, runs=None):
        return self.para(text or '', style='ListBullet', runs=runs)

    def step(self, text=None, *, runs=None):
        return self.para(text or '', style='ListNumber', runs=runs)

    def check(self, text=None, *, runs=None):
        return self.para(text or '', style='Check', runs=runs)

    def caveat(self, text):
        return self.para(text, style='Caveat')

    def table(self, headers, rows, widths=None, fills=None, head_fills=None):
        """A grid. A cell is a string or a list of lines `[(text, bold), ...]`;
        *head_fills* and *fills* (hex colours or None) shade cells, as in the
        Word writer, so the two documents say and show the same things. Rows
        without a fill of their own are striped faintly, for the eye to follow
        a row across a wide table."""
        cols = len(headers)
        widths = widths or [1] * cols
        total = float(sum(widths)) or 1.0
        px = [self.tw * w / total for w in widths]
        size, leading, pad = 8.8, 11.4, 4.2

        def lines_of(content, bold):
            pairs = content if isinstance(content, list) else [(content, bold)]
            return pairs or [('', bold)]

        def row_lines(cells, bold):
            # A cell given as lines keeps each line's own weight.
            return [[ln for t, b in lines_of(c, bold)
                     for ln in _wrap_runs([(t, 'B' if b else 'R')], size, px[j] - 2 * pad)]
                    for j, c in enumerate(list(cells)[:cols])]

        def height(cells_lines):
            n = max((len(ls) for ls in cells_lines), default=1)
            return max(n, 1) * leading + 2 * pad - 1.5

        def draw(cells_lines, row_fills, base_fill, colors=()):
            h = height(cells_lines)
            top = self._y
            if base_fill:
                self._rect(MARGIN, top - h, self.tw, h, base_fill)
            x = MARGIN
            for j, ls in enumerate(cells_lines):
                f = row_fills[j] if j < len(row_fills) else None
                if f:
                    self._rect(x, top - h, px[j], h, _rgb(f))
                y = top - pad - size + 0.6
                for ln in ls:
                    self._show(x + pad, y, ln, size, colors[j] if j < len(colors) else INK)
                    y -= leading
                x += px[j]
            self._y = top - h

        # A table called with blank headers is a list of labels and values (Run,
        # Project, Status): no header bar, no stripes, the label in grey.
        plain = not any(str(h).strip() if not isinstance(h, list) else
                        any(str(t).strip() for t, _ in h) for h in headers)
        head = row_lines(headers, True)
        hf = list(head_fills or [])
        edge, edge_w = (RULE, 0.6) if plain else (INK, 0.9)

        def draw_head():
            self._line(MARGIN, self._y, self.pw - MARGIN, self._y, color=edge, w=edge_w)
            if plain:
                return
            draw(head, hf, None if any(hf) else HEAD_TINT)
            self._line(MARGIN, self._y, self.pw - MARGIN, self._y, color=GREY, w=0.5)

        if self._cur and self._y < self.ph - MARGIN - 1:
            self._y -= 4                     # air between a table and what precedes it
        self._need((0 if plain else height(head)) + leading + 2 * pad + 4)
        draw_head()
        for i, r in enumerate(rows):
            cells = list(r) + [''] * (cols - len(r))
            ls = row_lines(cells, False)
            rf = list(fills[i]) if fills and i < len(fills) else []
            h = height(ls)
            if self._y - h < MARGIN + 8:
                self._line(MARGIN, self._y, self.pw - MARGIN, self._y, color=edge, w=edge_w)
                self._new_page()
                draw_head()
            if plain:
                draw(ls, rf, None, colors=(GREY,))
            else:
                draw(ls, rf, ZEBRA if i % 2 and not any(rf) else None)
            if i < len(rows) - 1:
                self._line(MARGIN, self._y, self.pw - MARGIN, self._y, color=HAIR, w=0.4)
        self._line(MARGIN, self._y, self.pw - MARGIN, self._y, color=edge, w=edge_w)
        self._y -= 10
        return self

    # ── output ─────────────────────────────────────────────────────────
    def _fit(self, text, size, width, key='R'):
        s = _plain(text)
        if _width(s, size, key) <= width:
            return s
        while s and _width(s + '…', size, key) > width:
            s = s[:-1]
        return s.rstrip() + '…'

    def _chrome(self, n, total):
        """The running header (from page 2) and the footer on every page."""
        out = []
        right = f'page {n} of {total}'
        rw = _width(right, 8)
        left = self._fit(self.footer or self.title or '', 8, self.tw - rw - 18)
        out.append(f'{HAIR} RG 0.5 w {MARGIN:.2f} {FOOT_Y + 11:.2f} m '
                   f'{self.pw - MARGIN:.2f} {FOOT_Y + 11:.2f} l S')
        out.append(self._show_str(MARGIN, FOOT_Y, [(left, 'R')], 8, GREY))
        out.append(self._show_str(self.pw - MARGIN - rw, FOOT_Y, [(right, 'R')], 8, GREY))
        if n > 1 and self.title:
            hy = self.ph - HEAD_DROP
            out.append(self._show_str(MARGIN, hy, [(self._fit(self.title, 8, self.tw), 'B')],
                                      8, FAINT))
            out.append(f'{HAIR} RG 0.5 w {MARGIN:.2f} {hy - 5:.2f} m '
                       f'{self.pw - MARGIN:.2f} {hy - 5:.2f} l S')
        return out

    def tobytes(self):
        pages = [p for p in self._pages if p] or [[]]
        total = len(pages)
        streams = ['\n'.join(lines + self._chrome(n, total)) for n, lines in
                   enumerate(pages, 1)]
        objs = []                        # object bodies, 1-based in order

        def add(body):
            objs.append(body)
            return len(objs)

        catalog = add(None)              # filled once the page tree id is known
        tree = add(None)
        font_ids = {k: self._font_objects(k, add) for k in FILES if self._used[k]}
        fonts = ' '.join(f'/{k} {i} 0 R' for k, i in font_ids.items())
        info = add(f'<< /Title {_pdf_text(self.title or "")} /Author {_pdf_text(self.author)} '
                   f'/Producer (BioSense) >>')
        page_ids = []
        for content in streams:
            data = content.encode('latin-1')
            stream = add(f'<< /Length {len(data)} >>\nstream\n'.encode('latin-1')
                         + data + b'\nendstream')
            page_ids.append(add(f'<< /Type /Page /Parent {tree} 0 R '
                                f'/MediaBox [0 0 {self.pw:g} {self.ph:g}] '
                                f'/Resources << /Font << {fonts} >> >> '
                                f'/Contents {stream} 0 R >>'))
        objs[catalog - 1] = f'<< /Type /Catalog /Pages {tree} 0 R >>'
        objs[tree - 1] = (f'<< /Type /Pages /Count {total} /Kids ['
                          + ' '.join(f'{i} 0 R' for i in page_ids) + '] >>')

        out = bytearray(b'%PDF-1.4\n%\xe2\xe3\xcf\xd3\n')
        offsets = []
        for i, body in enumerate(objs, 1):
            offsets.append(len(out))
            out += f'{i} 0 obj\n'.encode('latin-1')
            out += body if isinstance(body, bytes) else body.encode('latin-1')
            out += b'\nendobj\n'
        xref = len(out)
        out += f'xref\n0 {len(objs) + 1}\n0000000000 65535 f \n'.encode('latin-1')
        for off in offsets:
            out += f'{off:010d} 00000 n \n'.encode('latin-1')
        out += (f'trailer\n<< /Size {len(objs) + 1} /Root {catalog} 0 R /Info {info} 0 R >>\n'
                f'startxref\n{xref}\n%%EOF\n').encode('latin-1')
        return bytes(out)

    def _font_objects(self, key, add):
        """A Type0 font with an embedded TrueType subset; returns the Type0's id."""
        face, used = _face(key), self._used[key]
        gids = sorted(used)
        tag = ''.join(chr(65 + b % 26) for b in hashlib.sha1(
            (key + ','.join(map(str, gids))).encode()).digest()[:6])
        name = f'{tag}+{NAMES[key]}'
        sub = face.subset(gids)
        packed = zlib.compress(sub, 9)
        ff = add(f'<< /Length {len(packed)} /Length1 {len(sub)} /Filter /FlateDecode >>\n'
                 f'stream\n'.encode('latin-1') + packed + b'\nendstream')
        k = 1000 / face.units
        bbox = ' '.join(str(round(v * k)) for v in face.bbox)
        flags = 32 | (64 if face.italic_angle else 0)
        desc = add(f'<< /Type /FontDescriptor /FontName /{name} /Flags {flags} '
                   f'/FontBBox [{bbox}] /ItalicAngle {face.italic_angle:g} '
                   f'/Ascent {round(face.ascent * k)} /Descent {round(face.descent * k)} '
                   f'/CapHeight {round(face.cap_height * k)} '
                   f'/StemV {140 if face.weight >= 600 else 80} /FontFile2 {ff} 0 R >>')
        w = ' '.join(f'{g} [{face.pdf_width(g)}]' for g in gids)
        cid = add(f'<< /Type /Font /Subtype /CIDFontType2 /BaseFont /{name} '
                  f'/CIDSystemInfo << /Registry (Adobe) /Ordering (Identity) /Supplement 0 >> '
                  f'/FontDescriptor {desc} 0 R /DW 1000 /W [{w}] /CIDToGIDMap /Identity >>')
        cmap = _to_unicode(used).encode('latin-1')
        tu = add(f'<< /Length {len(cmap)} >>\nstream\n'.encode('latin-1') + cmap
                 + b'\nendstream')
        return add(f'<< /Type /Font /Subtype /Type0 /BaseFont /{name} /Encoding /Identity-H '
                   f'/DescendantFonts [{cid} 0 R] /ToUnicode {tu} 0 R >>')

    def save(self, path):
        p = Path(path)
        tmp = p.with_suffix(p.suffix + '.tmp')
        tmp.write_bytes(self.tobytes())
        tmp.replace(p)
        return p


def _utf16(ch):
    return ch.encode('utf-16-be').hex().upper()


def _pdf_text(s):
    """A PDF text string that holds any character: UTF-16BE with its byte-order mark."""
    return '<FEFF' + ''.join(_utf16(c) for c in _plain(s)) + '>'


def _to_unicode(used):
    """The CMap that lets a reader copy, search and extract the text again."""
    pairs = [f'<{g:04X}> <{_utf16(c)}>' for g, c in sorted(used.items()) if g]
    body = []
    for i in range(0, len(pairs), 100):
        chunk = pairs[i:i + 100]
        body.append(f'{len(chunk)} beginbfchar\n' + '\n'.join(chunk) + '\nendbfchar')
    return ('/CIDInit /ProcSet findresource begin\n12 dict begin\nbegincmap\n'
            '/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def\n'
            '/CMapName /Adobe-Identity-UCS def\n/CMapType 2 def\n'
            '1 begincodespacerange\n<0000> <FFFF>\nendcodespacerange\n'
            + '\n'.join(body) + '\nendcmap\nCMapName currentdict /CMap defineresource pop\n'
            'end\nend')


def text_of(data):
    """Every string shown on the pages, in order — one per text block. For tests and
    for grep. Decoded through the document's own ToUnicode maps, so what this
    returns is what a reader copying the text would get."""
    objs = {int(m.group(1)): m.group(2)
            for m in re.finditer(rb'(\d+) 0 obj\n(.*?)\nendobj', data, re.S)}
    maps = {}
    for num, body in objs.items():
        m = re.search(rb'/Subtype /Type0 .*?/ToUnicode (\d+) 0 R', body)
        if m:
            cmap = objs.get(int(m.group(1)), b'')
            maps[num] = {int(g, 16): bytes.fromhex(u.decode()).decode('utf-16-be')
                         for g, u in re.findall(rb'<([0-9A-F]{4})> <([0-9A-F]+)>', cmap)}
    names = {}
    for m in re.finditer(rb'/Font << (.*?) >>', data, re.S):
        for name, num in re.findall(rb'/(\w+) (\d+) 0 R', m.group(1)):
            names[name.decode()] = maps.get(int(num), {})
    out = []
    for body in objs.values():
        if not body.startswith(b'<< /Length ') or b'/Filter' in body.split(b'\n', 1)[0]:
            continue
        stream = body.split(b'stream\n', 1)[-1]
        for block in re.findall(rb'BT (.*?) ET', stream, re.S):
            cur, text = {}, []
            for font, hexes in re.findall(rb'/(\w+) [\d.]+ Tf <([0-9A-F]*)> Tj', block):
                cur = names.get(font.decode(), {})
                text += [cur.get(int(hexes[i:i + 4], 16), '?') for i in range(0, len(hexes), 4)]
            out.append(''.join(text))
    return out
