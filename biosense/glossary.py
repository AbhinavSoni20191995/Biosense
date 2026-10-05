"""What every badge in the interface means, in one place.

The vocabulary this system runs on — MEASURED against DERIVED, a public dataset
against a derived analysis over a private one, a value that is reported against
one that is a design choice — is the whole point of it. A reader who does not
know which is which is reading decoration.

Those definitions used to live in docstrings and in `docs/`, which means a
scientist looking at a number in a browser had to go and find a repository to
learn what the word beside it meant. Nobody does that. So the definitions live
here, are served by the application, and are rendered as a floating explanation
on every badge that carries one.

One place, for a reason: a definition that exists twice drifts, and the version
people read would be the one nobody maintained.
"""
from __future__ import annotations

from . import contracts as K

# ── how a number was produced ───────────────────────────────────────────
ESTIMATE_TYPES = {
    'measured': {
        'label': 'MEASURED',
        'short': 'An instrument read it.',
        'long': 'A real instrument produced this value — a cytometer, a sensor, a sequencer. '
                'It is the most direct kind of number here.',
        'tone': 'ok',
    },
    'derived': {
        'label': 'DERIVED',
        'short': 'Code computed it from measured values.',
        'long': 'No instrument measured this. Code computed it from values that were measured — '
                'a difference between two readings is DERIVED, not MEASURED, because nothing '
                'read the difference.',
        'tone': 'ok',
    },
    'simulated': {
        'label': 'SIMULATED',
        'short': 'A model produced it.',
        'long': 'A mechanistic model produced this number. The model reproduces published '
                'ranges and known trade-offs; it is not a validated digital twin and it did not '
                'measure your cells.',
        'tone': 'warn',
    },
    'predicted': {
        'label': 'PREDICTED',
        'short': 'Expected, not yet run.',
        'long': 'What is expected to happen, before anything has been run. A direction may be '
                'predicted even when no magnitude can be.',
        'tone': 'warn',
    },
    'target': {
        'label': 'TARGET',
        'short': 'What was asked for.',
        'long': 'A goal somebody set. It is never evidence of anything — it says what success '
                'would look like, not what happened.',
        'tone': 'neutral',
    },
}

ESTIMATE_RULES = [
    {'title': 'The weakest link wins',
     'body': 'Combining a measured value with a simulated one gives SIMULATED. Two measured '
             'values give DERIVED, because nothing measured the difference between them.'},
    {'title': 'Points and percentages stay apart',
     'body': '42% rising to 68% is +26 percentage POINTS and +62% RELATIVE. One figure standing '
             'for both would be a much larger-sounding claim about a different quantity, so the '
             'two are always shown separately.'},
    {'title': 'No magnitude is a state, not an absence',
     'body': 'A direction with no size is shown as "direction known, magnitude not estimated" '
             'with a reason — never as a blank somebody will fill in later.'},
]

# ── where a number's model came from ────────────────────────────────────
MODEL_BASIS = {
    'calibrated': {
        'label': 'CALIBRATED MODEL',
        'short': "The project's own mechanistic model.",
        'long': 'Produced by the model this project runs, whose terms were written against '
                'published protocol ranges. Still a stand-in, not a validated twin.',
        'tone': 'warn',
    },
    'ai_proposed': {
        'label': 'DE NOVO · UNCALIBRATED',
        'short': 'An AI proposed this response; nothing was fitted to data.',
        'long': 'The discovery agents proposed how this parameter behaves — a shape and its '
                'constants — from cited evidence. The arithmetic is BioSense\'s; the constants '
                'are a proposal, fitted to nothing. Treat the number as what that proposal '
                'implies, not as a prediction anybody has checked.',
        'tone': 'warn',
    },
    'expert_declared': {
        'label': 'EXPERT-DECLARED',
        'short': 'A person described this behaviour from experience.',
        'long': 'Somebody supplied the shape and the constants from their own experience. That '
                'is expert knowledge: private, not citable, and a protocol value resting on it '
                'is a design choice rather than a reported one.',
        'tone': 'warn',
    },
}

# ── what a piece of evidence IS ─────────────────────────────────────────
EVIDENCE_CLASSES = {
    'published_literature': {
        'label': 'Published literature',
        'short': 'A claim from a paper, with its source.',
        'long': 'Extracted from a publication and carried with the identifier and the quoted '
                'sentence it came from, so it can be checked.',
    },
    'public_dataset': {
        'label': 'Public data',
        'short': 'A dataset anyone can fetch.',
        'long': 'A repository dataset — GEO, ENCODE, EBI — identified by accession and '
                'checksum, so the exact bytes that were analysed are recoverable.',
    },
    'private_user_dataset': {
        'label': 'Private data',
        'short': 'A dataset you registered. It never leaves your machine.',
        'long': 'A file you ingested. It is never served over HTTP, never copied into a '
                'benchmark, and never cited as a public source. Analyses over it are reported '
                'as derived analyses with a private source.',
    },
    'derived_analysis': {
        'label': 'Derived analysis',
        'short': 'Something deterministic code computed from data.',
        'long': 'A result a registered tool computed over a dataset, carrying the dataset ids, '
                'the checksums of every input, the tool version and the code hash. What it was '
                'computed FROM is recorded separately from what it IS.',
    },
    'simulation': {
        'label': 'Simulation',
        'short': 'A model produced it.',
        'long': 'Output of a mechanistic model. Useful for comparing conditions; never a '
                'measurement, and never evidence that a protocol will work.',
    },
    'real_measurement': {
        'label': 'Real measurement',
        'short': 'An instrument read it in a real run.',
        'long': 'A reading from an actual experiment. The only class that is evidence about a '
                'real culture.',
    },
    'synthetic_fixture': {
        'label': 'Synthetic fixture',
        'short': 'Invented data, committed for testing.',
        'long': 'Data this project invented so the software could be exercised. It demonstrates '
                'what the system can express and measures nothing at all.',
    },
    'expert_knowledge': {
        'label': 'Expert knowledge',
        'short': 'Somebody\'s experience. Private, and not citable.',
        'long': 'An internal observation a person recorded. It may narrow a search range or '
                'rule a region out; it may never supply a cited protocol value, and it makes a '
                'run private.',
    },
}

# ── how firm a protocol value is ────────────────────────────────────────
PROVENANCE = {
    'reported': {
        'label': 'R · reported',
        'short': 'A published source states this value.',
        'long': 'The value appears in a cited source, for a context close enough to use. The '
                'claim identifier travels with it.',
    },
    'adapted': {
        'label': 'A · adapted',
        'short': 'Derived from cited claims, not stated by one.',
        'long': 'No source states this number, but cited claims and an analysis together imply '
                'it. The reasoning is recorded with it.',
    },
    'design_choice': {
        'label': 'D · design choice',
        'short': 'Somebody chose it. No source states it.',
        'long': 'A value picked by a person or implied by a model, with a stated rationale. It '
                'is not evidence, and a wet-lab protocol needs it acknowledged by a named human.',
    },
    'gap': {
        'label': 'GAP · no evidence',
        'short': 'Nothing supports a value here. This blocks the wet lab.',
        'long': 'No source, no analysis and no design choice gives this quantity a value. It is '
                'left empty on purpose: filling it with a plausible number is how an invented '
                'figure ends up in a protocol.',
    },
}

# ── what the simulator can say about a parameter ────────────────────────
SIMULATOR_COVERAGE = {
    'modelled': {
        'label': 'MODELLED',
        'short': "The project's calibrated model has a term for it.",
        'long': 'A prediction for this parameter is meaningful, and the model names the term it '
                'uses.',
    },
    'not_modelled': {
        'label': 'NOT MODELLED',
        'short': 'Real, usable, and not predicted.',
        'long': 'The parameter matters and you can set it in the lab, but this project\'s model '
                'has no equation for it, so no prediction is produced. It is shown rather than '
                'dropped: "we cannot model that" and "nobody suggested that" are not the same '
                'answer.',
    },
    'no_simulator': {
        'label': 'NO MODEL',
        'short': 'This project has no mechanistic model.',
        'long': 'Nothing predicts anything for this process, and no other project\'s model is '
                'borrowed to pretend otherwise.',
    },
    'de_novo_ai': {
        'label': 'DE NOVO',
        'short': 'Predicted through a response the AI proposed.',
        'long': 'The agents proposed how this behaves from cited evidence. It predicts, and '
                'every number it produces is uncalibrated and says so.',
    },
    'expert_declared': {
        'label': 'EXPERT-DECLARED',
        'short': 'Predicted through a response you described.',
        'long': 'You supplied the shape and the constants. It predicts, and a value resting on '
                'it is a design choice rather than a cited one.',
    },
    'not_in_project': {
        'label': 'NOT IN THIS PROJECT',
        'short': 'This process does not have that knob.',
        'long': 'The canonical registry defines the parameter, but this project does not expose '
                'it. A project does not inherit a knob merely because another one needed it.',
    },
}

# ── who may see something ───────────────────────────────────────────────
VISIBILITY = {
    'public': {'label': 'public', 'short': 'May be published.',
               'long': 'Nothing in this depends on data a person registered privately.'},
    'private': {'label': 'private', 'short': 'Must not be published.',
                'long': 'Its lineage includes private data or expert knowledge. BioSense refuses '
                        'to publish it rather than attempting to anonymise: a population '
                        'frequency from an unpublished experiment is still that experiment\'s '
                        'result, and removing a name does not change that.'},
}

# ── what became of a hypothesis ─────────────────────────────────────────
HYPOTHESIS_STATUS = {
    'proposed': {'label': 'proposed', 'short': 'Formed, not yet tested against evidence.'},
    'supported': {'label': 'supported', 'short': 'The evidence collected backs it.'},
    'contradicted': {'label': 'contradicted', 'short': 'The evidence collected argues against it.'},
    'superseded': {'label': 'superseded', 'short': 'A later hypothesis replaced it.'},
    'rejected': {'label': 'rejected', 'short': 'Ruled out, with a stated reason.'},
}

# ── which runtime answered ──────────────────────────────────────────────
RUNTIME_MODES = {
    'synthetic_demo': {
        'label': 'SYNTHETIC DEMO',
        'short': 'Deterministic code over committed fixtures. No model was called.',
        'long': 'Nothing here contacted a model and no number measures a real cell. It shows '
                'what the system can express, reproducibly and for free.',
    },
    'local_real_ai': {
        'label': 'REAL AI — LOCAL',
        'short': 'Real agents, orchestrated on this machine.',
        'long': 'The discovery agents did the work: real literature search, real analyses over '
                'the data you provided, a real hypothesis. The reactor is still a stand-in.',
    },
    'remote_real_ai': {
        'label': 'REAL AI — REMOTE',
        'short': 'Real agents, orchestrated by a configured server.',
        'long': 'As local, with the orchestration running on an Omnigent server you configured.',
    },
}

GROUPS = {
    'estimate_type': {'title': 'How a number was produced', 'terms': ESTIMATE_TYPES,
                      'rules': ESTIMATE_RULES},
    'model_basis': {'title': 'Where a predicted number\'s model came from', 'terms': MODEL_BASIS},
    'evidence_class': {'title': 'What a piece of evidence is', 'terms': EVIDENCE_CLASSES},
    'provenance': {'title': 'How firm a protocol value is', 'terms': PROVENANCE},
    'simulator_coverage': {'title': 'What the simulator can say about a parameter',
                           'terms': SIMULATOR_COVERAGE},
    'visibility': {'title': 'Who may see it', 'terms': VISIBILITY},
    'hypothesis_status': {'title': 'What became of a hypothesis', 'terms': HYPOTHESIS_STATUS},
    'runtime_mode': {'title': 'Which runtime answered', 'terms': RUNTIME_MODES},
}


def lookup(group, term):
    """One definition, or None. Never a guess: an unknown term gets no explanation."""
    g = GROUPS.get(group)
    if not g:
        return None
    return g['terms'].get(term)


def describe():
    """The whole glossary, as the interface fetches it once and caches."""
    return {
        'version': K.BIOSENSE_VERSION,
        'groups': {name: {'title': g['title'], 'terms': g['terms'],
                          'rules': g.get('rules') or []}
                   for name, g in GROUPS.items()},
        'note': 'These are the words BioSense uses about its own numbers. They are shown beside '
                'every value rather than kept in documentation, because a reader who does not '
                'know which kind of number they are looking at is reading decoration.',
    }


def missing_terms():
    """Vocabulary the code uses that this glossary does not define.

    A guard, run by the tests: a badge the interface can render but cannot
    explain is exactly the gap this module exists to close, and it would
    otherwise appear silently the next time somebody adds a class.
    """
    from . import projects as PJ
    gaps = []
    for cls in K.EVIDENCE_CLASSES:
        if cls not in EVIDENCE_CLASSES:
            gaps.append(f'evidence_class:{cls}')
    for v in K.VISIBILITIES:
        if v not in VISIBILITY:
            gaps.append(f'visibility:{v}')
    for cov in PJ.COVERAGE:
        if cov not in SIMULATOR_COVERAGE:
            gaps.append(f'simulator_coverage:{cov}')
    from .evidence import estimates as E
    for t in E.TYPES:
        if t not in ESTIMATE_TYPES:
            gaps.append(f'estimate_type:{t}')
    from .production import runtime as RT
    for m in RT.MODES:
        if m not in RUNTIME_MODES:
            gaps.append(f'runtime_mode:{m}')
    return sorted(gaps)
