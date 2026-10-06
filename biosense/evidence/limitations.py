"""Grouping what a run could not settle, so the reader can act on it.

A run's limitations are collected from every hypothesis it formed, and each
hypothesis states its own in its own words. Concatenated, a good run produced
forty-two lines in which "no dataset was reachable" appeared three times,
"no simulated prediction is possible" three times, and the single structural
finding — the project has no parameter for the lever the evidence actually
supports — sat at number thirty, between two restatements of a machine limit.
The science was right; the list was unreadable.

So the lines are grouped by what the reader can do about them, and near
duplicates are folded into the fullest wording:

    blocks        a gap or a model mismatch that stops the wet lab
    unsettled     what the evidence does not establish (the real science)
    installation  what this machine does not have: no annotation set, no
                  reachable dataset, no mechanistic model
    untestable    what no amount of evidence here could settle
    provenance    warnings about the run's own artifacts and searches
    standing      the rules that are true of every run

Nothing is deleted: a folded duplicate keeps the longest version, and every
group is shown. Ordering is by what a person would act on first.
"""
from __future__ import annotations

import re

# Order is the reading order; the label is what the page and the report show.
GROUPS = (
    ('blocks', 'These block the wet lab'),
    ('unsettled', 'What the evidence does not establish'),
    ('untestable', 'What this run could not settle at all'),
    ('installation', 'What this installation does not have'),
    ('provenance', 'About this run\'s own record'),
    ('standing', 'True of every run'),
)
LABELS = dict(GROUPS)

# Matched against the lower-cased line. First hit wins, so the more specific
# patterns come first.
_RULES = (
    ('standing', (r'\bnothing (here|in this run) approves\b', r'\bneeds a named human approver\b',
                  r'\bno part of this session can be\b')),
    ('provenance', (r'\bprovenance warning\b', r'\bsearch_complete\b', r'\bwas not read\b',
                    r'\bwere not read\b', r'\bnot fetched\b', r'\bwithout a backup\b',
                    r'\boverwrote\b', r'\bliterature search incomplete\b',
                    r'\babstract-only\b', r'\brests on an abstract\b',
                    r'\bunreadable by these tools\b', r'\bcannot read figures\b')),
    ('installation', (r'\bno annotation\b', r'\bfound:\s*false\b', r'\bknowledge set\b',
                      r'\bno dataset\b', r'\bsynthetic_fixture\b', r'\bcitable:\s*false\b',
                      r'\bno simulated prediction\b', r'\bnot modelled\b',
                      r'\bno mechanistic model\b', r'\bsimulator status\b',
                      r'\bno network call was made\b')),
    ('untestable', (r'\buntestable in this machine\b', r'\bcannot be settled in this machine\b',
                    r'\bunaskable in this run\b', r'\bnot offered as (open )?hypothes')),
    ('blocks', (r'\bblocks the wet lab\b', r'\bcannot be run\b', r'\bhas gaps\b',
                r'\bnot yet registered\b', r'\bnot a project parameter\b',
                r'\bnot in the canonical (parameter )?registry\b',
                r'\bno protocol may adopt\b', r'\bproject-model gap\b',
                r'\bunregistered candidate parameters\b', r'\bhas no growth-factor term\b')),
)
# Everything else is science: what the evidence does not establish.
_DEFAULT = 'unsettled'

_PUNCT = re.compile(r'[^a-z0-9_ ]+')
_SPACE = re.compile(r'\s+')
# An agent opens a point with a short capitalised headline ("NO DATASET.",
# "NO SIMULATED PREDICTION IS POSSIBLE.") and every hypothesis restates it in
# its own prose. The headline, not the prose, is what repeats.
_HEADLINE = re.compile(r'^([A-Z][A-Z0-9 ,\-—/()\'"]{3,90}?)[.:]\s')
_QUOTED = re.compile(r"'([^']{2,120})'")
# Shared vocabulary needed, beyond the headline, before two lines are called
# the same point. Low enough for two paraphrases, high enough that two
# different findings that happen to share a headline stay apart.
_OVERLAP = 0.3
_KEY_CHARS = 48


def group_of(text):
    low = (text or '').lower()
    for name, patterns in _RULES:
        if any(re.search(p, low) for p in patterns):
            return name
    return _DEFAULT


def _flat(text):
    return _SPACE.sub(' ', _PUNCT.sub(' ', (text or '').lower())).strip()


def _headline(text):
    m = _HEADLINE.match(text or '')
    return _flat(m.group(1)) if m else None


def _words(text):
    return {w for w in _flat(text).split() if len(w) > 3}


def same_point(a, b):
    """Whether two lines make the same point.

    Either they open identically, or they share a capitalised headline, name
    the same quoted subjects, and share enough vocabulary. The quoted subject
    is what keeps "CANDIDATE PARAMETER — NOT YET REGISTERED: 'GM-CSF …'" and
    the same template for 'TGF-beta1' apart: same headline, same sentence,
    different finding.
    """
    if _flat(a)[:_KEY_CHARS] == _flat(b)[:_KEY_CHARS]:
        return set(_QUOTED.findall(a)) == set(_QUOTED.findall(b))
    ha, hb = _headline(a), _headline(b)
    if not ha or ha != hb:
        return False
    if set(_QUOTED.findall(a)) != set(_QUOTED.findall(b)):
        return False
    wa, wb = _words(a), _words(b)
    return bool(wa and wb) and len(wa & wb) / len(wa | wb) >= _OVERLAP


def fold(lines):
    """Near-duplicates folded into the fullest wording, first-seen order kept.

    Two lines that make the same point are the hypotheses each writing it,
    and the longer one says more.
    """
    kept = []
    for line in lines:
        text = (line or '').strip()
        if not text:
            continue
        for i, other in enumerate(kept):
            if same_point(text, other):
                if len(text) > len(other):
                    kept[i] = text
                break
        else:
            kept.append(text)
    return kept


def digest(lines):
    """[{kind, label, items}] — grouped, folded, in reading order.

    Empty groups are left out; nothing else is.
    """
    kept = fold(lines)
    by_kind = {}
    for text in kept:
        by_kind.setdefault(group_of(text), []).append(text)
    out = []
    for kind, label in GROUPS:
        items = sorted(by_kind.get(kind) or [])
        if items:
            out.append({'kind': kind, 'label': label, 'items': items})
    return out


def headline(lines, limit=3):
    """The few a reader should see first: what blocks, then what is unsettled."""
    out = []
    for group in digest(lines):
        if group['kind'] in ('blocks', 'unsettled'):
            out += group['items']
    return out[:limit]
