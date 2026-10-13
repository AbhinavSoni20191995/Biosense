"""Writing a PDF with the standard library.

The Word export exists because a protocol gets printed, signed and taped
inside a hood. A PDF exists for the same reason plus one more: it opens the
same on every machine, including the one in the lab that has no Word, and it
is what a journal, a collaborator or a quality system actually files.

The same blocks the Word writer takes — a title, headings, paragraphs,
bullets, numbered steps, check lines, caveats and tables — are laid out here
onto A4 pages with the fourteen built-in fonts, so a PDF needs no font file,
no image and no dependency. `lab_protocol.build` writes to either; the two
documents say the same things in the same order.

What this is not is a typesetter. Text is wrapped on the Helvetica widths
below, a table row that does not fit moves to the next page with its header
repeated, and that is all. Characters outside Windows-1252 that a protocol
actually uses (arrows, check boxes, ≥) are written as their plain-text
spelling rather than lost; anything else becomes `?`, visibly, never
silently dropped.

Every string goes through one escape, and the output is deterministic: the
same protocol exported twice is the same bytes.
"""
from __future__ import annotations

import re

# ── page geometry (points) ───────────────────────────────────────────────
PAGE_W, PAGE_H = 595.0, 842.0          # A4
MARGIN = 56.0
TEXT_W = PAGE_W - 2 * MARGIN
FOOT_Y = 32.0

# ── fonts: the built-in fourteen need no file ────────────────────────────
FONTS = {'F1': 'Helvetica', 'F2': 'Helvetica-Bold', 'F3': 'Helvetica-Oblique',
         'F4': 'Helvetica-BoldOblique', 'F5': 'Courier'}

# Glyph widths (per 1000 em) for the printable ASCII range of Helvetica and
# Helvetica-Bold, from the Adobe AFM files. Wrapping needs them; nothing else.
_HELV = [278, 278, 355, 556, 556, 889, 667, 191, 333, 333, 389, 584, 278, 333, 278, 278,
         556, 556, 556, 556, 556, 556, 556, 556, 556, 556, 278, 278, 584, 584, 584, 556,
         1015, 667, 667, 722, 722, 667, 611, 778, 722, 278, 500, 667, 556, 833, 722, 778,
         667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611, 278, 278, 278, 469, 556,
         333, 556, 556, 500, 556, 556, 278, 556, 556, 222, 222, 500, 222, 833, 556, 556,
         556, 556, 333, 500, 278, 556, 500, 722, 500, 500, 500, 334, 260, 334, 584]
_HELV_B = [278, 333, 474, 556, 556, 889, 722, 238, 333, 333, 389, 584, 278, 333, 278, 278,
           556, 556, 556, 556, 556, 556, 556, 556, 556, 556, 333, 333, 584, 584, 584, 611,
           975, 722, 722, 722, 722, 667, 611, 778, 722, 278, 556, 722, 611, 833, 722, 778,
           667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611, 333, 278, 333, 584, 556,
           333, 556, 611, 556, 611, 556, 333, 611, 611, 278, 278, 556, 278, 889, 611, 611,
           611, 611, 389, 556, 333, 611, 556, 778, 556, 556, 500, 389, 280, 389, 584]
# A few Windows-1252 glyphs a protocol uses, by code point; the rest of the
# range is taken as an average letter.
_EXTRA = {0x2014: 1000, 0x2013: 556, 0x2022: 350, 0xB7: 278, 0xB0: 400, 0xB2: 333,
          0xB5: 556, 0xD7: 584, 0xF7: 584, 0x2026: 1000, 0x201C: 333, 0x201D: 333,
          0x2018: 222, 0x2019: 222}

# Spellings for what Windows-1252 cannot hold. Visible, never dropped.
SPELL = {'→': '->', '←': '<-', '≥': '>=', '≤': '<=', '☐': '[ ]', '☑': '[x]', '✓': 'v',
         '✗': 'x', '≈': '~', '−': '-', ' ': ' ', ' ': ' ', ' ': ' ',
         'Δ': 'delta ', 'α': 'alpha', 'β': 'beta', 'γ': 'gamma', 'κ': 'kappa', 'ω': 'omega',
         '′': "'", '­': ''}


def _rgb(hexcolour):
    """'FFF1C2' -> '1 0.945 0.761', the fill operands a PDF wants."""
    h = str(hexcolour).lstrip('#')
    return ' '.join(f'{int(h[i:i + 2], 16) / 255:.3f}' for i in (0, 2, 4))


def _plain(text):
    s = '' if text is None else str(text)
    s = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', s)
    for k, v in SPELL.items():
        s = s.replace(k, v)
    return s.encode('cp1252', errors='replace').decode('cp1252')


def _width(text, size, bold=False):
    table = _HELV_B if bold else _HELV
    total = 0
    for ch in text:
        o = ord(ch)
        if 32 <= o < 127:
            total += table[o - 32]
        else:
            total += _EXTRA.get(o, 556)
    return total * size / 1000.0


def _esc(text):
    b = _plain(text).encode('cp1252', errors='replace')
    out = bytearray()
    for c in b:
        if c in (0x28, 0x29, 0x5c):          # ( ) \
            out += b'\\' + bytes([c])
        elif c < 32 or c > 126:
            out += b'\\%03o' % c
        else:
            out.append(c)
    return out.decode('latin-1')


def _wrap(text, size, width, bold=False):
    """Lines of *text* no wider than *width*. A word wider than the line is
    broken at the width rather than overflowing the page."""
    words = _plain(text).split()
    if not words:
        return ['']
    lines, cur = [], ''
    for w in words:
        trial = f'{cur} {w}' if cur else w
        if _width(trial, size, bold) <= width:
            cur = trial
            continue
        if cur:
            lines.append(cur)
        while _width(w, size, bold) > width and len(w) > 1:
            n = max(1, int(len(w) * width / _width(w, size, bold)) - 1)
            lines.append(w[:n])
            w = w[n:]
        cur = w
    lines.append(cur)
    return lines


# ── block styles: (font key, size, leading, space before, space after) ───
STYLE = {
    'Title': ('F2', 20, 24, 0, 6), 'Subtitle': ('F3', 11, 14, 0, 10),
    'Heading1': ('F2', 14, 18, 14, 6), 'Heading2': ('F2', 12, 15, 10, 4),
    'Heading3': ('F4', 11, 14, 8, 3), 'Normal': ('F1', 10, 13, 0, 6),
    'Caveat': ('F3', 9.5, 12, 2, 8), 'ListBullet': ('F1', 10, 13, 0, 3),
    'ListNumber': ('F1', 10, 13, 0, 3), 'Check': ('F1', 10, 13, 0, 3),
}
GREY, DARK, RULE = '0.35 0.35 0.35', '0.08 0.08 0.08', '0.72 0.72 0.72'


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
    def _text(self, x, y, s, font='F1', size=10, color=DARK):
        self._cur.append(f'BT {color} rg /{font} {size:g} Tf {x:.1f} {y:.1f} Td ({_esc(s)}) Tj ET')

    def _line(self, x1, y1, x2, y2, color=RULE, w=0.6):
        self._cur.append(f'{color} RG {w:g} w {x1:.1f} {y1:.1f} m {x2:.1f} {y2:.1f} l S')

    def _rect(self, x, y, w, h, fill):
        self._cur.append(f'{fill} rg {x:.1f} {y:.1f} {w:.1f} {h:.1f} re f')

    def _lines(self, lines, *, x, width, font, size, leading, color=DARK, bold=False,
               prefix=None):
        """Wrapped lines from *x*; the first carries *prefix* in the hanging indent."""
        first = True
        for ln in lines:
            self._need(leading)
            self._y -= leading
            if first and prefix is not None:
                self._text(x - prefix[1], self._y, prefix[0], font=prefix[2], size=size,
                           color=color)
            self._text(x, self._y, ln, font=font, size=size, color=color)
            first = False

    # ── blocks ─────────────────────────────────────────────────────────
    def para(self, text='', *, style=None, bold=False, italic=False, runs=None):
        if runs is not None:
            # Mixed formatting is flattened: the words matter on paper, the
            # weights do not, and a run-level layout is not worth its code.
            text = ''.join(t for t, _ in runs)
            bold = bool(runs) and all((kw or {}).get('bold') for _, kw in runs)
        style = style or 'Normal'
        font, size, leading, before, after = STYLE.get(style, STYLE['Normal'])
        if style == 'Normal':
            font = 'F2' if bold else 'F3' if italic else 'F1'
        if style.startswith('Heading'):
            self._need(before + leading * 2)
        self._y -= before
        x, width, prefix, color = MARGIN, self.tw, None, DARK
        if style == 'Caveat':
            x, width, color = MARGIN + 10, self.tw - 10, GREY
            top = self._y
        if style == 'ListBullet':
            x, width, prefix = MARGIN + 14, self.tw - 14, ('•', 10, 'F1')
        if style == 'ListNumber':
            self._steps += 1
            x, width, prefix = MARGIN + 18, self.tw - 18, (f'{self._steps}.', 18, 'F2')
        if style == 'Check':
            x, width = MARGIN + 16, self.tw - 16
        lines = _wrap(text, size, width, bold=font in ('F2', 'F4'))
        if style == 'Check':
            self._need(leading)
            y0 = self._y - leading + 2.5
            self._cur.append(f'{GREY} RG 0.8 w {MARGIN + 1:.1f} {y0:.1f} 8 8 re S')
        self._lines(lines, x=x, width=width, font=font, size=size, leading=leading,
                    color=color, prefix=prefix)
        if style == 'Caveat' and self._y < top:
            self._line(MARGIN + 2, top - 2, MARGIN + 2, self._y - 2, color=RULE, w=2)
        if style == 'Heading1':
            self._line(MARGIN, self._y - 3, self.pw - MARGIN, self._y - 3, w=0.8)
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
        Word writer, so the two documents say and show the same things."""
        cols = len(headers)
        widths = widths or [1] * cols
        total = float(sum(widths)) or 1.0
        px = [self.tw * w / total for w in widths]
        size, leading, pad = 9, 11.5, 3.5

        def lines_of(content, bold):
            pairs = content if isinstance(content, list) else [(content, bold)]
            return pairs or [('', bold)]

        def row_lines(cells, bold):
            # A cell given as lines keeps each line's own weight.
            return [[(ln, b) for t, b in lines_of(c, bold)
                     for ln in _wrap(t, size, px[j] - 2 * pad, bold=b)]
                    for j, c in enumerate(list(cells)[:cols])]

        def draw(cells_lines, row_fills, default_shade):
            n = max(len(ls) for ls in cells_lines) if cells_lines else 1
            h = n * leading + 2 * pad
            top = self._y
            if default_shade:
                self._rect(MARGIN, top - h, self.tw, h, '0.93 0.93 0.93')
            x = MARGIN
            for j, ls in enumerate(cells_lines):
                f = row_fills[j] if j < len(row_fills) else None
                if f:
                    self._rect(x, top - h, px[j], h, _rgb(f))
                y = top - pad
                for ln, b in ls:
                    y -= leading - 2
                    self._text(x + pad, y, ln, font='F2' if b else 'F1', size=size)
                    y -= 2
                x += px[j]
            self._y = top - h
            self._line(MARGIN, self._y, self.pw - MARGIN, self._y)

        head = row_lines(headers, True)
        hf = list(head_fills or [])
        head_h = (max(len(ls) for ls in head) if head else 1) * leading + 2 * pad
        self._need(head_h + leading + 2 * pad)
        self._line(MARGIN, self._y, self.pw - MARGIN, self._y, w=0.8)
        draw(head, hf, not any(hf))
        for i, r in enumerate(rows):
            cells = list(r) + [''] * (cols - len(r))
            ls = row_lines(cells, False)
            rf = list(fills[i]) if fills and i < len(fills) else []
            h = (max(len(x) for x in ls) if ls else 1) * leading + 2 * pad
            if self._y - h < MARGIN + 8:
                self._new_page()
                self._line(MARGIN, self._y, self.pw - MARGIN, self._y, w=0.8)
                draw(head, hf, not any(hf))
            draw(ls, rf, False)
        self._y -= 8
        return self

    # ── output ─────────────────────────────────────────────────────────
    def _footer(self, n, total):
        left = _plain(self.footer or self.title or '')
        right = f'page {n} of {total}'
        return [f'BT {GREY} rg /F1 8 Tf {MARGIN:.1f} {FOOT_Y:.1f} Td ({_esc(left[:110])}) Tj ET',
                f'BT {GREY} rg /F1 8 Tf '
                f'{self.pw - MARGIN - _width(right, 8):.1f} {FOOT_Y:.1f} Td ({_esc(right)}) Tj ET']

    def tobytes(self):
        pages = [p for p in self._pages if p] or [[]]
        total = len(pages)
        objs = []                        # object bodies, 1-based in order

        def add(body):
            objs.append(body)
            return len(objs)

        catalog = add(None)              # filled once the page tree id is known
        tree = add(None)
        font_ids = {k: add(f'<< /Type /Font /Subtype /Type1 /BaseFont /{v} '
                           f'/Encoding /WinAnsiEncoding >>') for k, v in FONTS.items()}
        fonts = ' '.join(f'/{k} {i} 0 R' for k, i in font_ids.items())
        info = add(f'<< /Title ({_esc(self.title or "")}) /Author ({_esc(self.author)}) '
                   f'/Producer (BioSense) >>')
        page_ids = []
        for n, lines in enumerate(pages, 1):
            content = '\n'.join(lines + self._footer(n, total)).encode('latin-1')
            stream = add(f'<< /Length {len(content)} >>\nstream\n'.encode('latin-1')
                         + content + b'\nendstream')
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

    def save(self, path):
        from pathlib import Path
        p = Path(path)
        tmp = p.with_suffix(p.suffix + '.tmp')
        tmp.write_bytes(self.tobytes())
        tmp.replace(p)
        return p


def text_of(data):
    """Every string shown on the pages, in order. For tests and for grep."""
    out = []
    for m in re.finditer(rb'stream\n(.*?)\nendstream', data, re.S):
        for s in re.findall(rb'\((.*?)(?<!\\)\) Tj', m.group(1), re.S):
            s = re.sub(rb'\\([0-7]{3})', lambda x: bytes([int(x.group(1), 8)]), s)
            s = s.replace(b'\\(', b'(').replace(b'\\)', b')').replace(b'\\\\', b'\\')
            out.append(s.decode('cp1252', errors='replace'))
    return out
