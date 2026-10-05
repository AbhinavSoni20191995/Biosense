"""Whether a benchmark may be published, and the refusal when it may not.

The rule is blunt on purpose: **a public-safe export refuses when private
lineage exists.** It does not try to anonymise. Scientifically sensitive
material is not made safe by removing a name — a population frequency from an
unpublished experiment is still that experiment's result, and a sample id is
still a sample id. Automatic anonymisation would create exactly the false
confidence this project exists to avoid.

So `check()` looks for private lineage and, separately, `scan()` looks for
things that leak even when lineage is clean: absolute local paths, usernames,
home directories, private dataset ids, expert-knowledge statements. The second
is a backstop for the first, not a replacement — a benchmark that passes the
scan but has a private parent is still refused.
"""
from __future__ import annotations

import json
import os
import re

from .. import contracts as K

PRIVATE_CLASSES = ('private_user_dataset', 'expert_knowledge')

# Patterns that must never appear in a public export.
LEAKS = (
    (re.compile(r'/home/[^/\s"\']+'), 'an absolute home-directory path'),
    (re.compile(r'/Users/[^/\s"\']+'), 'an absolute macOS home path'),
    (re.compile(r'[A-Za-z]:\\\\Users\\\\[^\\\\\s"\']+'), 'an absolute Windows user path'),
    (re.compile(r'/private_data(/|\b)'), 'a path inside the private data root'),
    (re.compile(r'/(?:var|tmp)/[^\s"\']{8,}'), 'an absolute temporary path'),
)


def _walk(obj, path='$'):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from _walk(v, f'{path}.{k}')
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from _walk(v, f'{path}[{i}]')
    else:
        yield path, obj


def scan(obj, *, private_ids=()):
    """Find things that must not be in a public export. Returns a list of findings."""
    found = []
    for path, v in _walk(obj):
        if not isinstance(v, str):
            continue
        for pattern, what in LEAKS:
            if pattern.search(v):
                found.append({'where': path, 'what': what,
                              'sample': pattern.search(v).group(0)[:60]})
        for pid in private_ids:
            if pid and pid in v:
                found.append({'where': path, 'what': f'the private dataset id {pid!r}',
                              'sample': pid})
    user = os.environ.get('USER') or os.environ.get('USERNAME')
    if user and len(user) > 2:
        blob = json.dumps(obj, default=str)
        if re.search(rf'\b{re.escape(user)}\b', blob):
            found.append({'where': '$', 'what': 'the current username', 'sample': user})
    return found


def lineage(result):
    """Private sources referenced anywhere in the result."""
    sources = []
    for d in result.get('datasets') or []:
        if d.get('visibility') == 'private' or d.get('evidence_class') in PRIVATE_CLASSES:
            sources.append(f'dataset:{d.get("dataset_id")}')
    for a in result.get('analyses') or []:
        if a.get('source_visibility') == 'private':
            sources.append(f'analysis:{a.get("analysis_id")}')
    for h in result.get('hypotheses') or []:
        for e in h.get('evidence') or []:
            if e.get('visibility') == 'private' or e['evidence_class'] in PRIVATE_CLASSES:
                sources.append(f'evidence:{e.get("ref") or e["evidence_class"]}')
    if result.get('expert_knowledge'):
        sources.append('expert_knowledge')
    return sorted(set(sources))


def check(result, policy, *, private_ids=(), what='benchmark'):
    """Decide whether this result may be exported under *policy*.

    Returns the `privacy` block for the BenchmarkResult. A public_safe export
    with private lineage is refused — not scrubbed.
    """
    if policy not in ('public_safe', 'private'):
        raise K.ContractError("export_policy must be 'public_safe' or 'private'")
    private = lineage(result)
    block = {'export_policy': policy, 'has_private_lineage': bool(private),
             'private_sources': private, 'safe_to_publish': False, 'refusal_reason': None}
    if policy == 'private':
        block['refusal_reason'] = ('Marked private. This bundle stays outside public repository '
                                   'paths and is not published.')
        return block
    if private:
        block['refusal_reason'] = (
            f'This {what} declares export_policy public_safe but its results depend on '
            f'private lineage ({", ".join(private)}). BioSense refuses rather than attempting '
            f'to anonymise: a population frequency from an unpublished experiment is still that '
            f'experiment\'s result, and removing a name does not change that. Re-run it against '
            f'public or fixture data, or set export_policy to private.')
        return block
    findings = scan(result, private_ids=private_ids)
    if findings:
        block['refusal_reason'] = ('Public export refused: ' +
                                   '; '.join(f'{f["what"]} at {f["where"]}' for f in findings[:6]))
        block['leaks'] = findings
        return block
    block['safe_to_publish'] = True
    return block


def validate_bundle(bundle_dir, result, *, private_ids=()):
    """Scan everything a public bundle is about to contain."""
    from pathlib import Path
    findings = scan(result, private_ids=private_ids)
    for p in sorted(Path(bundle_dir).rglob('*')):
        if not p.is_file() or p.suffix not in ('.json', '.md', '.html', '.svg', '.csv'):
            continue
        text = p.read_text(errors='replace')
        for pattern, what in LEAKS:
            m = pattern.search(text)
            if m:
                findings.append({'where': str(p.relative_to(bundle_dir)), 'what': what,
                                 'sample': m.group(0)[:60]})
        for pid in private_ids:
            if pid and pid in text:
                findings.append({'where': str(p.relative_to(bundle_dir)),
                                 'what': f'the private dataset id {pid!r}', 'sample': pid})
    return findings
