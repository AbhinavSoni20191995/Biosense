"""Reading a TrueType font, and cutting it down to the glyphs a document uses.

The PDF writer used the fourteen fonts every PDF reader has built in. They need
no file, and they hold Windows-1252 and nothing else, so a protocol's own
characters came out wrong: MARCO⁺ printed as "MARCO?", ≥ as ">=", IL-1α as
"IL-1alpha". A scientific document cannot spell its symbols out. So the PDF
now embeds real fonts, and this module is what it needs to do that with the
standard library: each glyph's advance width (to wrap lines), the character to
glyph map (to encode text), and a subset of the font file holding only the
glyphs one document drew (to keep the file small).

The subset keeps every glyph id where it is and empties the outlines nobody
used, rather than renumbering: the PDF then addresses glyphs by their own ids
(Identity-H), the cmap, hinting and metrics stay valid, and the code stays
short. A composite glyph keeps the glyphs it is built from.

Only what a PDF needs is read. This is not a font library.
"""
from __future__ import annotations

import struct
from functools import lru_cache
from pathlib import Path

_TABLES_KEPT = (b'cvt ', b'fpgm', b'glyf', b'head', b'hhea', b'hmtx', b'loca', b'maxp',
                b'prep')


def _u16(b, o):
    return struct.unpack_from('>H', b, o)[0]


def _i16(b, o):
    return struct.unpack_from('>h', b, o)[0]


def _u32(b, o):
    return struct.unpack_from('>I', b, o)[0]


class Font:
    """One TrueType file: metrics, the character map and the glyph data."""

    def __init__(self, path):
        self.path = Path(path)
        self.data = data = self.path.read_bytes()
        if data[:4] not in (b'\x00\x01\x00\x00', b'true'):
            raise ValueError(f'{self.path.name}: not a TrueType (glyf) font')
        n = _u16(data, 4)
        self.tables = {}
        for i in range(n):
            o = 12 + 16 * i
            self.tables[data[o:o + 4]] = (_u32(data, o + 8), _u32(data, o + 12))
        head = self._t(b'head')
        self.units = _u16(head, 18)
        self.bbox = [_i16(head, 36 + 2 * i) for i in range(4)]
        self.long_loca = _i16(head, 50) == 1
        hhea = self._t(b'hhea')
        self.ascent, self.descent = _i16(hhea, 4), _i16(hhea, 6)
        n_hmetrics = _u16(hhea, 34)
        self.num_glyphs = _u16(self._t(b'maxp'), 4)
        hmtx = self._t(b'hmtx')
        adv = [_u16(hmtx, 4 * i) for i in range(n_hmetrics)]
        self.advances = adv + [adv[-1]] * (self.num_glyphs - n_hmetrics)
        post = self._t(b'post')
        self.italic_angle = struct.unpack_from('>i', post, 4)[0] / 65536.0 if post else 0.0
        os2 = self._t(b'OS/2')
        self.cap_height = (_i16(os2, 88) if os2 and len(os2) > 90 and _u16(os2, 0) >= 2
                           else self.ascent)
        self.weight = _u16(os2, 4) if os2 else 400
        self.cmap = self._read_cmap()

    def _t(self, tag):
        off_len = self.tables.get(tag)
        if not off_len:
            return b''
        off, length = off_len
        return self.data[off:off + length]

    def _read_cmap(self):
        cmap = self._t(b'cmap')
        subtables = {}
        for i in range(_u16(cmap, 2)):
            o = 4 + 8 * i
            subtables[(_u16(cmap, o), _u16(cmap, o + 2))] = _u32(cmap, o + 4)
        for key in ((3, 10), (0, 4), (3, 1), (0, 3), (0, 1), (0, 0)):
            if key in subtables:
                off = subtables[key]
                fmt = _u16(cmap, off)
                if fmt == 12:
                    return self._cmap12(cmap, off)
                if fmt == 4:
                    return self._cmap4(cmap, off)
        raise ValueError(f'{self.path.name}: no Unicode cmap this reader understands')

    @staticmethod
    def _cmap4(b, off):
        segs = _u16(b, off + 6) // 2
        ends, starts = off + 14, off + 16 + 2 * segs
        deltas, ranges = starts + 2 * segs, starts + 4 * segs
        out = {}
        for s in range(segs):
            end, start = _u16(b, ends + 2 * s), _u16(b, starts + 2 * s)
            delta, ro = _i16(b, deltas + 2 * s), _u16(b, ranges + 2 * s)
            for c in range(start, end + 1):
                if c == 0xFFFF:
                    continue
                if ro == 0:
                    g = (c + delta) & 0xFFFF
                else:
                    g = _u16(b, ranges + 2 * s + ro + 2 * (c - start))
                    g = (g + delta) & 0xFFFF if g else 0
                if g:
                    out[c] = g
        return out

    @staticmethod
    def _cmap12(b, off):
        out = {}
        for i in range(_u32(b, off + 12)):
            o = off + 16 + 12 * i
            start, end, gid = _u32(b, o), _u32(b, o + 4), _u32(b, o + 8)
            for c in range(start, end + 1):
                out[c] = gid + c - start
        return out

    # ── measuring ─────────────────────────────────────────────────────────
    def has(self, ch):
        return ord(ch) in self.cmap

    def gid(self, ch):
        return self.cmap.get(ord(ch), 0)

    def width(self, text, size):
        """Advance width of *text* at *size* points (characters it lacks count as .notdef)."""
        adv, units = self.advances, self.units
        return sum(adv[self.cmap.get(ord(c), 0)] for c in text) * size / units

    def pdf_width(self, gid):
        """A glyph's advance in the PDF's thousandths of an em."""
        return round(self.advances[gid] * 1000 / self.units)

    # ── subsetting ───────────────────────────────────────────────────────
    def _glyph_ranges(self):
        loca = self._t(b'loca')
        n = self.num_glyphs + 1
        if self.long_loca:
            return [_u32(loca, 4 * i) for i in range(n)]
        return [_u16(loca, 2 * i) * 2 for i in range(n)]

    def subset(self, gids):
        """The font file with every glyph not in *gids* (and their components) emptied."""
        glyf = self._t(b'glyf')
        loca = self._glyph_ranges()
        keep = {0} | {g for g in gids if 0 <= g < self.num_glyphs}
        todo = list(keep)
        while todo:                              # a composite keeps its components
            g = todo.pop()
            start, end = loca[g], loca[g + 1]
            if end - start < 10 or _i16(glyf, start) >= 0:
                continue
            o = start + 10
            while True:
                flags, comp = _u16(glyf, o), _u16(glyf, o + 2)
                if comp not in keep:
                    keep.add(comp)
                    todo.append(comp)
                o += 4 + (4 if flags & 0x0001 else 2)
                if flags & 0x0008:
                    o += 2
                elif flags & 0x0040:
                    o += 4
                elif flags & 0x0080:
                    o += 8
                if not flags & 0x0020:
                    break
        new_glyf, new_loca = bytearray(), []
        for g in range(self.num_glyphs):
            new_loca.append(len(new_glyf))
            if g in keep:
                chunk = glyf[loca[g]:loca[g + 1]]
                new_glyf += chunk + b'\x00' * (-len(chunk) % 4)
        new_loca.append(len(new_glyf))
        tables = {tag: self._t(tag) for tag in _TABLES_KEPT if tag in self.tables}
        tables[b'glyf'] = bytes(new_glyf)
        tables[b'loca'] = b''.join(struct.pack('>I', x) for x in new_loca)
        head = bytearray(tables[b'head'])
        struct.pack_into('>I', head, 8, 0)           # checksumAdjustment, set below
        struct.pack_into('>h', head, 50, 1)          # long loca
        tables[b'head'] = bytes(head)
        return _assemble(tables)


def _checksum(data):
    data = data + b'\x00' * (-len(data) % 4)
    return sum(struct.unpack(f'>{len(data) // 4}I', data)) & 0xFFFFFFFF


def _assemble(tables):
    tags = sorted(tables)
    n = len(tags)
    es = max(i for i in range(n.bit_length()) if 2 ** i <= n)
    header = struct.pack('>IHHHH', 0x00010000, n, 16 * 2 ** es, es, n * 16 - 16 * 2 ** es)
    offset = 12 + 16 * n
    directory, body = bytearray(), bytearray()
    for tag in tags:
        data = tables[tag]
        directory += struct.pack('>4sIII', tag, _checksum(data), offset + len(body), len(data))
        body += data + b'\x00' * (-len(data) % 4)
    font = bytearray(header + directory + body)
    head_at = font.find(b'head', 12, 12 + 16 * n)
    head_off = _u32(font, head_at + 8)
    struct.pack_into('>I', font, head_off + 8, (0xB1B0AFBA - _checksum(bytes(font))) & 0xFFFFFFFF)
    return bytes(font)


@lru_cache(maxsize=None)
def load(path):
    return Font(path)
