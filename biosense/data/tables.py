"""Reading delimited tables with the standard library and numpy.

No pandas. The project's dependency budget is jsonschema, numpy and scipy, and a
dataframe library earns its weight when you need joins, reshaping and a type
system; here the job is "read a rectangle of numbers and strings and tell me
honestly which columns are numeric".

`read_table` returns a Table: column names, row dicts, and a per-column numeric
view where every value in the column parsed as a number. A column that is numeric
in 9 rows out of 10 is NOT numeric: a partially parsed column is how a blank cell
silently becomes a zero.
"""
from __future__ import annotations

import csv
import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .. import contracts as K

MAX_BYTES = 64 * 1024 * 1024
MAX_ROWS = 200_000
DELIMS = {'csv': ',', 'tsv': '\t'}
MISSING = {'', 'na', 'n/a', 'nan', 'null', 'none', '.', '-'}


@dataclass
class Table:
    path: str
    columns: list
    rows: list              # list[dict[str, str]]
    numeric: dict           # column -> np.ndarray (only fully numeric columns)

    @property
    def n(self):
        return len(self.rows)

    def column(self, name):
        if name not in self.columns:
            raise K.ContractError(
                f'column {name!r} is not in {Path(self.path).name}. '
                f'Columns present: {", ".join(self.columns)}')
        return [r[name] for r in self.rows]

    def numeric_column(self, name):
        if name not in self.numeric:
            raise K.ContractError(
                f'column {name!r} in {Path(self.path).name} is not fully numeric, so it cannot be '
                f'compared as a measurement. Blank or non-numeric cells are left as they are '
                f'rather than read as zero.')
        return self.numeric[name]

    def levels(self, name):
        return list(dict.fromkeys(self.column(name)))      # distinct, in order

    def where(self, name, value):
        return [i for i, r in enumerate(self.rows) if r[name] == value]


def _number(s):
    t = (s or '').strip()
    if t.lower() in MISSING:
        return None
    try:
        v = float(t)
    except ValueError:
        return None
    return v if math.isfinite(v) else None


def read_table(path, file_type=None, *, max_rows=MAX_ROWS):
    p = Path(path)
    if not p.is_file():
        raise K.ContractError(f'{p} is not a file')
    size = p.stat().st_size
    if size > MAX_BYTES:
        raise K.ContractError(f'{p.name} is {size / 1e6:.0f} MB; the limit for an in-process read '
                              f'is {MAX_BYTES / 1e6:.0f} MB. Summarise it outside the loop and '
                              f'register the summary.')
    ft = (file_type or p.suffix.lstrip('.')).lower()
    if ft == 'txt':
        ft = 'tsv'
    if ft not in DELIMS:
        raise K.ContractError(
            f'{p.name}: this version reads {", ".join(sorted(DELIMS))} tables. '
            f'{ft!r} files can be described in a manifest but not analysed yet.')
    with p.open(newline='', encoding='utf-8-sig') as fh:
        reader = csv.DictReader(fh, delimiter=DELIMS[ft])
        cols = [c.strip() for c in (reader.fieldnames or [])]
        if not cols or any(not c for c in cols):
            raise K.ContractError(f'{p.name}: the header row is empty or has an unnamed column')
        if len(set(cols)) != len(cols):
            raise K.ContractError(f'{p.name}: duplicate column names in the header')
        rows = []
        for i, raw in enumerate(reader):
            if i >= max_rows:
                raise K.ContractError(f'{p.name} has more than {max_rows} rows')
            if None in raw:
                raise K.ContractError(f'{p.name}: row {i + 2} has more fields than the header')
            rows.append({c: (raw.get(c) or '').strip() for c in cols})
    if not rows:
        raise K.ContractError(f'{p.name} has a header but no rows')

    numeric = {}
    for c in cols:
        vals = [_number(r[c]) for r in rows]
        if vals and all(v is not None for v in vals):
            numeric[c] = np.asarray(vals, dtype=float)
    return Table(path=str(p), columns=cols, rows=rows, numeric=numeric)


def describe(t, max_levels=24):
    """The sample_metadata block for a manifest: columns and their observed values."""
    levels = {}
    for c in t.columns:
        if c in t.numeric:
            continue
        vs = t.levels(c)
        levels[c] = vs if len(vs) <= max_levels else vs[:max_levels] + ['…']
    # A long table has one row per feature and sample: its samples are the
    # distinct ids, not its rows (5000 genes x 10 samples is 10 samples).
    sid = next((c for c in t.columns if c.lower() in SAMPLE_ID_COLUMNS), None)
    n = len(t.levels(sid)) if sid else t.n
    out = {'sample_count': n, 'columns': list(t.columns), 'levels': levels}
    if sid:
        out['row_count'] = t.n
    return out


SAMPLE_ID_COLUMNS = ('sample_id', 'sample', 'gsm', 'sample_name')
