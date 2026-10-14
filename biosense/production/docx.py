"""Writing a .docx with the standard library.

A scientist asked for the protocol as a Word document, which is a reasonable
thing to want: it is what gets printed, taped inside a hood, emailed to a
collaborator and marked up by a supervisor. A Markdown file is none of those
things in most labs.

A .docx is a zip of XML parts, and the subset a protocol needs — headings,
paragraphs, bold and italic runs, bullets, numbered steps, tables — is small
and completely specified. So it is written here rather than adding a
dependency: this project's budget is jsonschema, numpy and scipy, and a
document library would be a fourth for about two hundred lines of XML, most
of which is a fixed preamble.

What this is not is a Word implementation. There are no styles beyond the
handful defined in `styles.xml`, no images, no headers or footers, no change
tracking. If any of those are ever wanted, that is the moment to take the
dependency — not now.

The one thing worth being careful about is escaping: a quote from a paper may
contain `&`, `<` or a character Word reads as markup, and an unescaped one
produces a file Word refuses to open with no useful message. Every piece of
text goes through `_esc`.
"""
from __future__ import annotations

import re
import zipfile
from xml.sax.saxutils import escape

W = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'

CONTENT_TYPES = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>
<Override PartName="/word/numbering.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.numbering+xml"/>
<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>
<Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>
</Types>'''

RELS = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>
<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/>
</Relationships>'''

DOC_RELS = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/numbering" Target="numbering.xml"/>
</Relationships>'''

# Heading sizes in half-points: 28 = 14 pt. Deliberately plain; a lab protocol
# is read, not admired, and a document with no theme opens the same everywhere.
STYLES = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles xmlns:w="%s">
<w:docDefaults><w:rPrDefault><w:rPr>
<w:rFonts w:ascii="Calibri" w:hAnsi="Calibri" w:cs="Calibri"/><w:sz w:val="20"/>
<w:color w:val="1B2420"/>
</w:rPr></w:rPrDefault></w:docDefaults>
<w:style w:type="paragraph" w:styleId="Normal" w:default="1"><w:name w:val="Normal"/>
<w:pPr><w:spacing w:after="120" w:line="300" w:lineRule="auto"/></w:pPr></w:style>
<w:style w:type="paragraph" w:styleId="Title"><w:name w:val="Title"/>
<w:pPr><w:pBdr><w:bottom w:val="single" w:sz="12" w:space="6"
w:color="0F7A45"/></w:pBdr><w:spacing w:after="80"/></w:pPr>
<w:rPr><w:rFonts w:ascii="Calibri" w:hAnsi="Calibri" w:cs="Calibri"/><w:b/><w:sz w:val="44"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/>
<w:pPr><w:keepNext/><w:pBdr><w:bottom w:val="single" w:sz="6" w:space="3"
w:color="0F7A45"/></w:pBdr><w:spacing w:before="320" w:after="140"/><w:outlineLvl w:val="0"/>
</w:pPr><w:rPr><w:rFonts w:ascii="Calibri" w:hAnsi="Calibri" w:cs="Calibri"/><w:b/><w:sz w:val="30"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="Heading2"><w:name w:val="heading 2"/>
<w:pPr><w:keepNext/><w:spacing w:before="220" w:after="80"/><w:outlineLvl w:val="1"/>
</w:pPr><w:rPr><w:rFonts w:ascii="Calibri" w:hAnsi="Calibri" w:cs="Calibri"/><w:b/><w:sz w:val="24"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="Heading3"><w:name w:val="heading 3"/>
<w:pPr><w:keepNext/><w:spacing w:before="160" w:after="40"/><w:outlineLvl w:val="2"/>
</w:pPr><w:rPr><w:rFonts w:ascii="Calibri" w:hAnsi="Calibri" w:cs="Calibri"/><w:b/><w:color w:val="3D4843"/><w:sz w:val="21"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="Caveat"><w:name w:val="Caveat"/>
<w:pPr><w:pBdr><w:left w:val="single" w:sz="18" w:space="8" w:color="C8E1D3"/></w:pBdr>
<w:spacing w:after="140" w:line="264" w:lineRule="auto"/><w:ind w:left="220"/></w:pPr>
<w:rPr><w:i/><w:color w:val="616D68"/><w:sz w:val="18"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="ListBullet"><w:name w:val="List Bullet"/>
<w:pPr><w:numPr><w:ilvl w:val="0"/><w:numId w:val="1"/></w:numPr>
<w:spacing w:after="60"/></w:pPr></w:style>
<w:style w:type="paragraph" w:styleId="ListNumber"><w:name w:val="List Number"/>
<w:pPr><w:numPr><w:ilvl w:val="0"/><w:numId w:val="2"/></w:numPr>
<w:spacing w:after="60"/></w:pPr></w:style>
<w:style w:type="table" w:styleId="TableGrid"><w:name w:val="Table Grid"/>
<w:rPr><w:sz w:val="18"/></w:rPr>
<w:tblPr><w:tblBorders>
<w:top w:val="single" w:sz="8" w:color="1B2420"/><w:bottom w:val="single" w:sz="8" w:color="1B2420"/>
<w:insideH w:val="single" w:sz="2" w:color="E0E6E3"/>
</w:tblBorders><w:tblCellMar><w:top w:w="50" w:type="dxa"/><w:left w:w="90" w:type="dxa"/>
<w:bottom w:w="50" w:type="dxa"/><w:right w:w="90" w:type="dxa"/></w:tblCellMar></w:tblPr></w:style>
<w:style w:type="table" w:styleId="KeyValue"><w:name w:val="Key Value"/>
<w:rPr><w:sz w:val="18"/></w:rPr>
<w:tblPr><w:tblBorders>
<w:top w:val="single" w:sz="4" w:color="CCD5D0"/><w:bottom w:val="single" w:sz="4" w:color="CCD5D0"/>
<w:insideH w:val="single" w:sz="2" w:color="E0E6E3"/>
</w:tblBorders><w:tblCellMar><w:top w:w="40" w:type="dxa"/><w:left w:w="90" w:type="dxa"/>
<w:bottom w:w="40" w:type="dxa"/><w:right w:w="90" w:type="dxa"/></w:tblCellMar></w:tblPr></w:style>
</w:styles>''' % W

NUMBERING = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:numbering xmlns:w="%s">
<w:abstractNum w:abstractNumId="0"><w:lvl w:ilvl="0"><w:start w:val="1"/>
<w:numFmt w:val="bullet"/><w:lvlText w:val="•"/><w:lvlJc w:val="left"/>
<w:pPr><w:ind w:left="454" w:hanging="227"/></w:pPr>
<w:rPr><w:rFonts w:ascii="Symbol" w:hAnsi="Symbol"/></w:rPr></w:lvl></w:abstractNum>
<w:abstractNum w:abstractNumId="1"><w:lvl w:ilvl="0"><w:start w:val="1"/>
<w:numFmt w:val="decimal"/><w:lvlText w:val="%%1."/><w:lvlJc w:val="left"/>
<w:pPr><w:ind w:left="454" w:hanging="227"/></w:pPr></w:lvl></w:abstractNum>
<w:num w:numId="1"><w:abstractNumId w:val="0"/></w:num>
<w:num w:numId="2"><w:abstractNumId w:val="1"/></w:num>
</w:numbering>''' % W

APP_PROPS = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties">
<Application>BioSense</Application></Properties>'''


def _esc(text):
    """Text into XML. A quote with an ampersand in it must not break the file."""
    s = '' if text is None else str(text)
    # Control characters are not valid in XML 1.0 at all, and a stray one from
    # a scraped quote makes Word refuse the whole document.
    s = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', s)
    return escape(s)


def _run(text, *, bold=False, italic=False, mono=False, color=None):
    pr = ''
    if bold:
        pr += '<w:b/>'
    if italic:
        pr += '<w:i/>'
    if mono:
        pr += '<w:rFonts w:ascii="Consolas" w:hAnsi="Consolas"/>'
    if color:
        pr += f'<w:color w:val="{color}"/>'
    pr = f'<w:rPr>{pr}</w:rPr>' if pr else ''
    # xml:space=preserve: a run that ends in a space is a run that ends in a
    # space, and without this Word eats it and words run together.
    return f'<w:r>{pr}<w:t xml:space="preserve">{_esc(text)}</w:t></w:r>'


class Document:
    """Paragraphs and tables, in order. Build it, then `save`."""

    def __init__(self, title=None, author='BioSense', landscape=False):
        self.title = title
        self.author = author
        # A one-page plan reads across: stages left to right, arms down.
        self.landscape = landscape
        self._body = []

    # ── blocks ────────────────────────────────────────────────────────
    def para(self, text='', *, style=None, bold=False, italic=False, runs=None):
        """One paragraph. *runs* is [(text, {bold:..}), ...] for mixed formatting."""
        pr = f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>' if style else ''
        if runs is not None:
            body = ''.join(_run(t, **(kw or {})) for t, kw in runs)
        else:
            body = _run(text, bold=bold, italic=italic) if text != '' else ''
        self._body.append(f'<w:p>{pr}{body}</w:p>')
        return self

    def heading(self, text, level=1):
        return self.para(text, style=f'Heading{min(max(level, 1), 3)}')

    def title_block(self, text, subtitle=None):
        self.para(text, style='Title')
        if subtitle:
            self.para(subtitle, style='Caveat')
        return self

    def bullet(self, text=None, *, runs=None):
        return self.para(text or '', style='ListBullet', runs=runs)

    def step(self, text=None, *, runs=None):
        return self.para(text or '', style='ListNumber', runs=runs)

    def check(self, text=None, *, runs=None):
        """A line to tick at the bench: a box, then the text."""
        body = [('\u2610  ', {})] + (list(runs) if runs is not None else [(text or '', {})])
        return self.para(runs=body)

    def caveat(self, text):
        return self.para(text, style='Caveat')

    def text_width(self):
        # A4 less two 2 cm margins, in twentieths of a point.
        return 14570 if self.landscape else 9638

    def table(self, headers, rows, widths=None, fills=None, head_fills=None):
        """A grid. *widths* are relative; they are normalised to the page.

        A cell is a string, or a list of lines `[(text, bold), ...]` for a cell
        that lists several things. *head_fills* (one hex colour or None per
        column) and *fills* (one per cell, row by row) shade cells: a stage's
        colour, an arm's change from control.
        """
        cols = len(headers)
        widths = widths or [1] * cols
        total = float(sum(widths)) or 1.0
        page = self.text_width()
        grid = ''.join(f'<w:gridCol w:w="{int(page * w / total)}"/>' for w in widths)

        def cell(content, *, bold=False, w=1, fill=None, color=None, rule=False):
            width = int(page * w / total)
            shade = (f'<w:shd w:val="clear" w:color="auto" w:fill="{fill}"/>' if fill else '')
            under = ('<w:tcBorders><w:bottom w:val="single" w:sz="4" w:color="616D68"/>'
                     '</w:tcBorders>' if rule else '')
            # A cell given as lines keeps each line's own weight: a header's day
            # range under its bold stage name is not bold.
            lines = content if isinstance(content, list) else [(content, bold)]
            paras = ''.join(
                f'<w:p><w:pPr><w:spacing w:after="20" w:line="252" w:lineRule="auto"/></w:pPr>'
                f'{_run(t, bold=b, color=color)}</w:p>' for t, b in (lines or [('', False)]))
            return (f'<w:tc><w:tcPr><w:tcW w:w="{width}" w:type="dxa"/>{under}{shade}'
                    f'</w:tcPr>{paras}</w:tc>')

        # Blank headers make a list of labels and values (Run, Project, Status):
        # no header row, the label in grey, as in the PDF.
        plain = not any(str(h).strip() if not isinstance(h, list) else
                        any(str(t).strip() for t, _ in h) for h in headers)
        style = 'KeyValue' if plain else 'TableGrid'
        out = [f'<w:tbl><w:tblPr><w:tblStyle w:val="{style}"/>'
               f'<w:tblW w:w="{page}" w:type="dxa"/></w:tblPr><w:tblGrid>{grid}</w:tblGrid>']
        hf = list(head_fills or []) + [None] * cols
        if not plain:
            tint = None if any(head_fills or []) else 'EEF3F0'
            out.append('<w:tr><w:trPr><w:tblHeader/></w:trPr>'
                       + ''.join(cell(h, bold=True, w=w, fill=f or tint, rule=True)
                                 for h, w, f in zip(headers, widths, hf))
                       + '</w:tr>')
        for i, r in enumerate(rows):
            cells = list(r) + [''] * (cols - len(r))
            rf = list((fills or [])[i] if fills and i < len(fills) else []) + [None] * cols
            out.append('<w:tr>' + ''.join(
                cell(c, w=w, fill=f, color='616D68' if plain and j == 0 else None)
                for j, (c, w, f) in enumerate(zip(cells, widths, rf))) + '</w:tr>')
        out.append('</w:tbl>')
        # Word runs two adjacent tables together; an empty paragraph after one
        # keeps them apart and gives the next heading its spacing.
        self._body.append(''.join(out))
        self._body.append('<w:p/>')
        return self

    def page_break(self):
        self._body.append('<w:p><w:r><w:br w:type="page"/></w:r></w:p>')
        return self

    # ── output ────────────────────────────────────────────────────────
    def _document_xml(self):
        return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                f'<w:document xmlns:w="{W}"><w:body>' + ''.join(self._body)
                + ('<w:sectPr><w:pgSz w:w="16838" w:h="11906" w:orient="landscape"/>'
                   if self.landscape else '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/>')
                + '<w:pgMar w:top="1134" w:right="1134" w:bottom="1134" w:left="1134"/>'
                  '</w:sectPr></w:body></w:document>')

    def _core_xml(self):
        return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<cp:coreProperties '
                'xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
                'xmlns:dc="http://purl.org/dc/elements/1.1/">'
                f'<dc:title>{_esc(self.title or "")}</dc:title>'
                f'<dc:creator>{_esc(self.author)}</dc:creator>'
                f'<cp:lastModifiedBy>{_esc(self.author)}</cp:lastModifiedBy>'
                '</cp:coreProperties>')

    def tobytes(self):
        import io
        buf = io.BytesIO()
        # Deterministic: the same protocol exported twice is the same file, so
        # a diff of two exports shows what changed in the protocol.
        with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
            for name, data in (
                    ('[Content_Types].xml', CONTENT_TYPES),
                    ('_rels/.rels', RELS),
                    ('word/document.xml', self._document_xml()),
                    ('word/_rels/document.xml.rels', DOC_RELS),
                    ('word/styles.xml', STYLES),
                    ('word/numbering.xml', NUMBERING),
                    ('docProps/core.xml', self._core_xml()),
                    ('docProps/app.xml', APP_PROPS)):
                info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o644 << 16
                z.writestr(info, data)
        return buf.getvalue()

    def save(self, path):
        from pathlib import Path
        p = Path(path)
        tmp = p.with_suffix(p.suffix + '.tmp')
        tmp.write_bytes(self.tobytes())
        tmp.replace(p)
        return p
