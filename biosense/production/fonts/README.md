# Fonts embedded in PDF exports

The PDF writer (`biosense/production/pdf.py`) embeds these fonts, subset to the
glyphs each document uses, so a protocol's own characters (MARCO⁺, ≥ 10 %,
IL-1α, CD34⁺CD45⁻, µL) print as written instead of as `?` or a spelling.

| File | Role | Licence |
|---|---|---|
| `Carlito-Regular/Bold/Italic.ttf` | the document typeface (metric-compatible with Calibri, so the Word and PDF copies lay out alike) | SIL Open Font License 1.1 — `LICENSE-Carlito-OFL.txt` |
| `DejaVuSans.ttf` | fallback, per character, for a symbol Carlito lacks (☐ ✓ ✗ ⇌ …) | Bitstream Vera / DejaVu licence — `LICENSE-DejaVu.txt` |

The files are the unmodified upstream releases (as packaged by Debian:
`fonts-crosextra-carlito`, `fonts-dejavu-core`). Both licences permit
embedding the fonts in documents; subsetting happens only inside each exported
PDF, never to these files.
