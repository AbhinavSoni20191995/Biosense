"""Deterministic protocol designer for the iPSC -> T-lineage process.

This is NOT the literature agent. The literature agent searches Europe PMC, reads
full texts and extracts numeric claims; it needs network access and a model. This
module stands in for it on the offline path: it assembles a ProductionProtocol
from a small committed citation set plus explicit design choices, so the loop can
run end to end with no credentials and no network.

The distinction is kept visible rather than papered over:

* Every citation it uses is a real publication, named in `CITATIONS` with its DOI.
* Not one numeric value is attributed to any of them. The full texts were not
  retrieved, so every quantity this module emits is `design_choice` with a
  rationale, and the citations support only the *direction* of a lever. A
  protocol built here therefore reaches the human approver as a designed
  starting point, which is what it is.
* The handoff it writes records that in `readiness_reason`,
  `missing_required_parameters` and `limitations`, so a reader does not have to
  infer it from the provenance tags.

`build_handoff` and `build_protocol` are pure functions of the request. Given the
same request they return the same documents, which is what lets the web app, the
tests and the committed fixtures agree.
"""
from __future__ import annotations

import copy

from .. import contracts as K

# Real publications. Located from publisher and PubMed/PMC records; full texts
# were NOT retrieved, so nothing numeric below is attributed to any of them.
CITATIONS = {
    'michaels2022': {
        'source_id': 'DOI-10.1126-sciadv.abn5522',
        'citation': 'Michaels YS, Edgar JM, Major MC, et al. DLL4 and VCAM1 enhance the emergence of '
                    'T cell-competent hematopoietic progenitors from human pluripotent stem cells. '
                    'Sci Adv. 2022;8(34):eabn5522.',
        'doi': '10.1126/sciadv.abn5522',
        'pmcid': 'PMC9401626',
    },
    'roychoudhuri2016': {
        'source_id': 'DOI-10.1038-ni.3441',
        'citation': 'Roychoudhuri R, Clever D, Li P, et al. BACH2 regulates CD8+ T cell differentiation '
                    'by controlling access of AP-1 factors to enhancers. Nat Immunol. 2016;17(7):851-860.',
        'doi': '10.1038/ni.3441',
        'pmcid': 'PMC4918801',
    },
    'tsukumo2013': {
        'source_id': 'DOI-10.1073-pnas.1306691110',
        'citation': 'Tsukumo S, Unno M, Muto A, et al. Bach2 maintains T cells in a naive state by '
                    'suppressing effector memory-related genes. Proc Natl Acad Sci USA. '
                    '2013;110(26):10735-10740.',
        'doi': '10.1073/pnas.1306691110',
        'pmcid': 'PMC3696756',
    },
    'bach2_dosage_2025': {
        'source_id': 'DOI-10.1038-s41590-025-02389-z',
        'citation': 'Fine-tuning BACH2 dosage balances stemness and effector function to enhance '
                    'antitumor T cell therapy. Nat Immunol. 2025.',
        'doi': '10.1038/s41590-025-02389-z',
        'pmcid': None,
    },
}

PARAPHRASE = 'PARAPHRASE of the publication record; full text was not retrieved, so this is not a quote'

# Directional claims only. `value` is text in every case: a direction, never a number.
CLAIMS = [
    {'id': 'L01', 'key': 'michaels2022', 'parameter': 'notch_ligand_requirement', 'stage': 't_commitment',
     'value': 'Presenting the Notch ligand DLL4, together with VCAM1, during the endothelial-to-haematopoietic '
              'transition increases downstream progenitor T cell output from human pluripotent stem cells.',
     'note': 'Direction only. The reported fold-increase is not carried into any protocol quantity.'},
    {'id': 'L02', 'key': 'michaels2022', 'parameter': 'stage_order', 'stage': 'hemogenic_endothelium',
     'value': 'T-competent haematopoietic progenitors arise from an endothelial-to-haematopoietic transition, '
              'so a mesoderm stage and a hemogenic-endothelium stage precede T-lineage commitment.',
     'note': 'Supports the stage architecture, not any stage duration.'},
    {'id': 'L03', 'key': 'roychoudhuri2016', 'parameter': 'bach2_terminal_differentiation', 'stage': 'expansion',
     'value': 'BACH2 restrains terminal differentiation of CD8+ T cells and enables generation of long-lived '
              'memory cells, by limiting access of AP-1 factors to enhancers.',
     'note': 'Mouse CD8+ T cells in a viral-infection model. Not an iPSC-derived culture.'},
    {'id': 'L04', 'key': 'tsukumo2013', 'parameter': 'bach2_naive_maintenance', 'stage': 'expansion',
     'value': 'Bach2 maintains T cells in a naive state by suppressing effector-memory-related genes; '
              'Bach2-deficient naive T cells highly express effector-memory genes.',
     'note': 'Bach2-deficient mice, peripheral CD4+ T cells. Not an iPSC-derived culture.'},
    {'id': 'L05', 'key': 'bach2_dosage_2025', 'parameter': 'bach2_dosage_effect', 'stage': 'expansion',
     'value': 'BACH2 dosage tunes the balance between stem-like and effector CD8+ T cell states; the effect of '
              'changing BACH2 level is dose-dependent rather than monotonic.',
     'note': 'Engineered CAR T cells. Recorded because it means a BACH2 perturbation has no single direction.'},
]

# The baseline schedule. Every number here is this module's design choice.
STAGE_PLAN = [
    {'stage_id': 'mesoderm', 'name': 'Mesoderm induction', 'days': (0, 3),
     'goal': 'Commit iPSC aggregates toward mesoderm',
     'medium': 'serum-free, xeno-free basal medium with mesoderm supplements',
     'factors': [('BMP4', 20.0, 'ng/mL', 0, 3), ('VEGF', 50.0, 'ng/mL', 0, 3),
                 ('FGF2', 20.0, 'ng/mL', 0, 3), ('CHIR99021', 4.0, 'uM', 0, 1)]},
    {'stage_id': 'hemogenic_endothelium', 'name': 'Hemogenic endothelium', 'days': (3, 8),
     'goal': 'Carry the culture through the endothelial-to-haematopoietic transition',
     'medium': 'serum-free basal medium with haematopoietic supplements',
     'factors': [('VEGF', 50.0, 'ng/mL', 3, 8), ('FGF2', 20.0, 'ng/mL', 3, 8),
                 ('SCF', 50.0, 'ng/mL', 3, 8), ('SB431542', 5.0, 'uM', 3, 5)]},
    {'stage_id': 't_commitment', 'name': 'T-lineage commitment', 'days': (8, 20),
     'goal': 'Drive Notch-dependent commitment to the T lineage',
     'medium': 'serum-free lymphoid medium on a DLL4-presenting surface',
     'factors': [('DLL4', 500.0, 'ng/mL', 8, 20), ('SCF', 50.0, 'ng/mL', 8, 20),
                 ('FLT3L', 10.0, 'ng/mL', 8, 20), ('IL-7', 10.0, 'ng/mL', 8, 20),
                 ('TPO', 20.0, 'ng/mL', 8, 14)]},
    {'stage_id': 'maturation', 'name': 'Maturation', 'days': (20, 30),
     'goal': 'Mature the committed population with a TCR/CD3 stimulus',
     'medium': 'serum-free lymphoid medium',
     'factors': [('anti-CD3', 50.0, 'ng/mL', 20, 23), ('IL-7', 10.0, 'ng/mL', 20, 30),
                 ('SCF', 50.0, 'ng/mL', 20, 25)]},
    {'stage_id': 'expansion', 'name': 'Expansion', 'days': (30, 40),
     'goal': 'Expand the committed population on homeostatic cytokines',
     'medium': 'serum-free lymphoid medium',
     'factors': [('IL-7', 10.0, 'ng/mL', 30, 40), ('IL-15', 10.0, 'ng/mL', 30, 40),
                 ('IL-2', 50.0, 'ng/mL', 30, 40)]},
]
SEED_DENSITY = 2.0e5        # cells/mL, design choice
WORKING_VOLUME = 100.0      # mL, design choice
FEED_FRACTION = 0.5         # fraction, design choice

# Why each factor is in the schedule at all. Used as the design-choice rationale,
# so no quantity reaches the approver without a stated reason.
FACTOR_BASIS = {
    'BMP4': ('Standard mesoderm-inducing signal in pluripotent-stem-cell haematopoietic differentiation. '
             'Dose is a mid-range starting point chosen here, not a retrieved value.', []),
    'VEGF': ('Supports endothelial specification and the endothelial-to-haematopoietic transition. '
             'Dose chosen here.', ['L02']),
    'FGF2': ('General proliferative support across the early stages. Dose chosen here.', []),
    'CHIR99021': ('GSK3 inhibitor used as a Wnt agonist for mesoderm patterning, applied as a short pulse. '
                  'Dose and pulse length chosen here.', []),
    'SCF': ('Haematopoietic progenitor survival and proliferation factor. Dose chosen here.', []),
    'SB431542': ('TGF-beta receptor inhibitor, applied briefly to favour the haematopoietic transition. '
                 'Dose and window chosen here.', []),
    'DLL4': ('Notch ligand presentation is the lever that commits progenitors to the T lineage; the cited work '
             'reports that providing DLL4 increases T-competent progenitor output. The direction is cited; the '
             'surface density here is a design choice.', ['L01']),
    'FLT3L': ('Supports lymphoid progenitor expansion alongside SCF. Dose chosen here.', []),
    'IL-7': ('The central homeostatic cytokine for T-lineage survival and proliferation, and the lever the cited '
             'BACH2 work implicates for maintaining a less differentiated state. Dose chosen here.', ['L04']),
    'TPO': ('Supports early haematopoietic progenitors; withdrawn mid-stage. Dose and window chosen here.', []),
    'anti-CD3': ('TCR/CD3 agonist providing the maturation signal, applied as a limited pulse because sustained '
                 'TCR pressure drives differentiation. Dose and pulse length chosen here.', ['L03']),
    'IL-15': ('Homeostatic cytokine supporting expansion with less differentiation pressure than IL-2. '
              'Dose chosen here.', ['L03']),
    'IL-2': ('Classical T-cell expansion cytokine. Included for expansion drive; dose chosen here.', []),
}


def _q(value, unit, rationale, claim_ids=()):
    """A design-choice quantity. Every number this module emits goes through here."""
    return {'value': value, 'unit': unit, 'provenance': 'design_choice',
            'claim_ids': list(claim_ids), 'rationale': rationale}


def _text(value, rationale, claim_ids=()):
    return _q(value, 'text', rationale, claim_ids)


def build_handoff(request):
    """The evidence handoff for this request: directional claims only, no numbers."""
    rid = request['request_id']
    keys = sorted({c['key'] for c in CLAIMS})
    return {
        'schema_version': '0.1',
        'request_id': rid,
        'ready_for_simulation': False,
        'readiness_reason': 'Directional evidence only. No numeric parameter was retrieved for this process: '
                            'the Europe PMC full-text path was not exercised when this handoff was built, so '
                            'every protocol quantity downstream is a design choice awaiting human approval.',
        'source_manifest': [{'id': CITATIONS[k]['source_id'], 'url': f'https://doi.org/{CITATIONS[k]["doi"]}',
                             'sha256': None, 'retrieved_at': None, 'article_type': 'research-article',
                             'supplement_count': 0, 'supplements_retrieved': False,
                             'citation': CITATIONS[k]['citation'], 'doi': CITATIONS[k]['doi'],
                             'pmcid': CITATIONS[k]['pmcid'],
                             'full_text_retrieved': False} for k in keys],
        'candidate_protocols': [{
            'protocol_id': 'designed_ipsc_tcell_baseline',
            'contexts': [{'species': request['product']['species'],
                          'cell_origin': request['product']['cell_origin'],
                          'target_cell': request['product']['target_cell'],
                          'cell_line': request['product'].get('cell_line'),
                          'culture_format': request['product'].get('culture_format') or 'suspension_aggregate',
                          'medium': 'serum-free, xeno-free',
                          'time_origin': 'iPSC seeding into the vessel (day 0)',
                          'time_window': None}],
            'note': 'Not a protocol extracted from a paper. A schedule designed here from the directional '
                    'claims below, for a human to approve or reject.'}],
        'claims': [{
            'id': c['id'],
            'protocol_id': 'designed_ipsc_tcell_baseline',
            'parameter': c['parameter'],
            'stage': c['stage'],
            'role': 'directional_finding',
            'value': c['value'],
            'unit': 'text',
            'context': {'species': 'human or mouse as published', 'cell_origin': 'see citation',
                        'target_cell': 'see citation', 'cell_line': None, 'culture_format': None,
                        'medium': None, 'time_origin': None, 'time_window': None},
            'condition_signature': 'as_published',
            'evidence': {'source_id': CITATIONS[c['key']]['source_id'], 'paragraph_id': None,
                         'quote': f'{PARAPHRASE}. {c["value"]}'},
            'status': 'reported',
            'notes': f'{c["note"]} Citation: {CITATIONS[c["key"]]["citation"]} '
                     f'doi:{CITATIONS[c["key"]]["doi"]}',
        } for c in CLAIMS],
        'rejected_claims': [],
        'excluded_claims': [],
        'conflicts': [],
        'candidate_parameters': [],
        'missing_required_parameters': [
            'Every numeric value: seeding density, all factor doses, all stage durations, the harvest day, '
            'and the feeding regime. None was retrieved from a full text.',
            'Any reported fold-expansion or marker-positive fraction for an iPSC-derived T-lineage process '
            'under a defined schedule.',
            'Any BACH2-perturbation measurement in an iPSC-derived T-lineage culture; the cited BACH2 work '
            'is in primary or engineered peripheral T cells.',
        ],
        'selected_parameters': [],
        'optimization_domains': [],
        'search_log': [{'query': 'publication-record lookup for the citations in '
                                 'biosense.production.design.CITATIONS',
                        'date': '2026-10-04', 'hits': len(keys),
                        'note': 'Located by publication record, not by the Europe PMC full-text endpoint. '
                                'agent_tools.py was not used to build this handoff.'}],
        'search_complete': False,
        'limitations': [
            'No numeric parameter in this handoff, and therefore none in any protocol built from it, is '
            'attributed to a publication.',
            'Directional claims are paraphrases of publication records, not verified quotations.',
            'The cited BACH2 studies used mouse or human peripheral/engineered T cells. Transfer to an '
            'iPSC-derived T-lineage differentiation is a hypothesis, not a finding.',
            'A designed schedule is not an optimised or validated schedule.',
        ],
    }


def build_protocol(request, iteration=0):
    """The baseline iteration-0 protocol for this request. Every quantity is a design choice."""
    rid = request['request_id']
    arms = request['genotype_arms']
    target = request['desired_output']
    harvest = float(target['at_day'])
    plan = copy.deepcopy(STAGE_PLAN)
    # The request's day fixes the harvest, so the expansion stage ends there.
    if harvest <= plan[-2]['days'][1]:
        raise K.ContractError(
            f'desired_output.at_day {harvest} leaves no expansion stage: the designed schedule reaches '
            f'day {plan[-2]["days"][1]} before expansion begins. Ask for a later day, or supply a protocol.')
    # Expansion runs to the requested day; its factor windows follow the stage end below.
    plan[-1]['days'] = (plan[-1]['days'][0], harvest)

    stages, n = [], 0
    for st in plan:
        s0, s1 = st['days']
        steps = []
        if st['stage_id'] == 'mesoderm':
            n += 1
            steps.append({'step_id': f'S{n:02d}', 'day': s0, 'end_day': None, 'action': 'seed',
                          'factor': None,
                          'quantity': _q(SEED_DENSITY, 'cells/mL',
                                         'Starting iPSC density chosen here as a mid-range aggregate seeding '
                                         'density. No retrieved value supports it.'),
                          'description': 'Seed iPSC into the vessel as aggregates'})
        for (name, dose, unit, d0, d1) in st['factors']:
            n += 1
            basis, cids = FACTOR_BASIS.get(name, ('Dose chosen here; no retrieved value supports it.', []))
            end = min(float(d1), s1) if st['stage_id'] != 'expansion' else s1
            steps.append({'step_id': f'S{n:02d}', 'day': float(max(d0, s0)), 'end_day': float(end),
                          'action': 'add_factor', 'factor': name,
                          'quantity': _q(dose, unit, basis, cids),
                          'description': f'{name} through day {end:g}'})
        if st['stage_id'] == 'maturation':
            n += 1
            # The agonist pulse ends inside the stage, which is the point of a pulse.
            steps.append({'step_id': f'S{n:02d}', 'day': 23.0, 'end_day': None,
                          'action': 'remove_factor', 'factor': 'anti-CD3', 'quantity': None,
                          'description': 'Withdraw the TCR/CD3 agonist; medium exchange removes it'})
        n += 1
        steps.append({'step_id': f'S{n:02d}', 'day': float(s1 if st['stage_id'] != 'expansion' else s1),
                      'end_day': None, 'action': 'sample', 'factor': None, 'quantity': None,
                      'description': f'Sample for cell count and viability at the end of {st["name"].lower()}'})
        if st['stage_id'] == 'expansion':
            n += 1
            steps.append({'step_id': f'S{n:02d}', 'day': harvest, 'end_day': None, 'action': 'harvest',
                          'factor': None, 'quantity': None,
                          'description': 'Harvest, count, and read identity and residual-pluripotency markers'})
        stages.append({
            'stage_id': st['stage_id'], 'name': st['name'], 'start_day': float(s0), 'end_day': float(s1),
            'medium': _text(st['medium'],
                            'Medium class chosen here to match the stage goal. No retrieved medium '
                            'formulation supports it.'),
            'goal': st['goal'], 'steps': steps})

    gene_arms = [a for a in arms if a.get('gene')]
    effects = []
    for i, a in enumerate(gene_arms, 1):
        gene = a['gene']
        if gene.upper() == 'BACH2' and a['genotype'] == 'knockout':
            effects.append({
                'effect_id': f'E{i}', 'arm_id': a['arm_id'], 'gene': gene, 'stage_id': 'expansion',
                'affected_quantity': 'sustained expansion of the committed pool late in the window',
                'readout_metric': 'target_cells_per_input_cell', 'predicted_direction': 'decrease',
                'predicted_magnitude': None,
                'mechanism': 'Hypothesis. The cited work reports that BACH2 restrains terminal differentiation '
                             'and supports naive/stem-like maintenance in peripheral T cells. If that transfers, '
                             'a knockout should differentiate further and sooner and so sustain less expansion '
                             'late in the window. No cited study measures this in an iPSC-derived culture, and '
                             'the dosage work shows BACH2 effects are not monotonic, so the direction is a '
                             'prediction to be measured, not an expectation to be assumed.',
                'claim_ids': ['L03', 'L04', 'L05'], 'evidence_level': 'mechanistic_inference'})
        else:
            effects.append({
                'effect_id': f'E{i}', 'arm_id': a['arm_id'], 'gene': gene, 'stage_id': 'expansion',
                'affected_quantity': 'output under the control schedule',
                'readout_metric': 'target_cells_per_input_cell', 'predicted_direction': 'unknown',
                'predicted_magnitude': None,
                'mechanism': f'No directional evidence for {gene} in this process was available when this '
                             f'protocol was built, so the arm runs the control schedule and its behaviour is '
                             f'measured rather than predicted.',
                'claim_ids': [], 'evidence_level': 'none'})

    insights = [
        {'insight_id': 'I1',
         'statement': 'The stage architecture - mesoderm, hemogenic endothelium, Notch-driven T commitment, '
                      'maturation, expansion - follows the cited route from pluripotent cells through an '
                      'endothelial-to-haematopoietic transition to T-competent progenitors.',
         'claim_ids': ['L01', 'L02'], 'evidence_level': 'direct_same_cell_type',
         'relevance': 'Fixes the stage order and makes DLL4 presentation a named lever.'},
        {'insight_id': 'I2',
         'statement': 'No numeric parameter was retrieved for this process, so every dose, duration and '
                      'density in this protocol is a design choice and the whole schedule needs human '
                      'approval before any wet-lab run.',
         'claim_ids': [], 'evidence_level': 'none',
         'relevance': 'This is the protocol\'s main limitation and the reason its approval record lists '
                      'every quantity.'},
    ]
    if any(a.get('gene', '').upper() == 'BACH2' for a in gene_arms):
        insights.append(
            {'insight_id': 'I3',
             'statement': 'BACH2 restrains terminal differentiation and supports naive/stem-like maintenance '
                          'in peripheral T cells, and its effect is dose-dependent rather than monotonic. '
                          'Both arms therefore start on an identical schedule: a per-arm change made before '
                          'any measurement would confound the comparison.',
             'claim_ids': ['L03', 'L04', 'L05'], 'evidence_level': 'mechanistic_inference',
             'relevance': 'Justifies an empty arm_adjustments list at iteration 0.'})

    return {
        'schema_version': '2.0',
        'protocol_id': f'{rid}-it{iteration}',
        'request_id': rid,
        'iteration': iteration,
        'parent_protocol_id': None,
        'revision_brief_id': None,
        'changes_from_parent': [],
        'title': f'Designed iPSC -> {request["product"]["target_cell"]} schedule to day {harvest:g} '
                 f'({", ".join(a["arm_id"] for a in arms)}) (iteration {iteration})',
        'label': 'DESIGNED BASELINE, not an extracted protocol. Every quantity is a design choice; the '
                 'citations support directions only. Not clinical or manufacturing guidance.',
        'day_origin': 'iPSC seeding into the vessel',
        'evidence': {'handoff_paths': [], 'source_protocol_ids': ['designed_ipsc_tcell_baseline'],
                     'notes': 'Directional claims L01-L05; no numeric claim exists for this process.'},
        'insights': insights,
        'culture_system': {
            'format': request['product'].get('culture_format') or 'suspension_aggregate',
            'vessel': 'single-use stirred vessel with a DLL4-presenting surface for the commitment stage',
            'working_volume': _q(WORKING_VOLUME, 'mL',
                                 'Working volume chosen here for a bench-scale run. Not a retrieved value.'),
            'parameters': {
                'feed_fraction': _q(FEED_FRACTION, 'fraction',
                                    'Half-volume fed-batch addition when the culture approaches its density '
                                    'ceiling. Chosen here.'),
                'temperature': _q(37.0, 'degC', 'Standard culture temperature.'),
                'dissolved_oxygen': _q(21.0, '%', 'Atmospheric oxygen, chosen here; a hypoxic early phase is '
                                                  'a plausible alternative this schedule does not test.'),
            }},
        'stages': stages,
        'genotype_arms': [{'arm_id': a['arm_id'], 'genotype': a['genotype'], 'gene': a.get('gene'),
                           'modification': a.get('modification'), 'zygosity': a.get('zygosity'),
                           'line_id': a.get('line_id'), 'description': a.get('description', '')}
                          for a in arms],
        'genotype_effects': effects,
        'arm_adjustments': [],
        'measurement_plan': {
            'mode': request['measurement_mode'],
            'sampling_days': [d for d in (3, 8, 20, 30, harvest) if d <= harvest],
            'harvest_day': harvest,
            'qc_tests_planned': ['sterility', 'mycoplasma', 'endotoxin', 'karyotype',
                                 'residual pluripotency'],
            'replicates_per_arm': 1,
        },
        'expected_outcomes': [],
        'open_questions': [
            'No retrieved value exists for any dose, duration or density in this schedule, so the whole '
            'protocol is a starting point rather than a transfer of a published process.',
            'The DLL4 surface density that commits best is unknown here; only the direction of the lever '
            'is cited.',
            'Whether the TCR agonist pulse length chosen here matures the population without over-driving '
            'differentiation is untested.',
        ],
        'risks': [
            'One replicate per arm makes every genotype comparison directional only.',
            'Residual undifferentiated iPSC is a tumorigenicity risk; the limit and assay sensitivity are '
            'product-specific and must be set by the requester.',
        ],
        'limitations': [
            'Designed, not extracted. No quantity is attributed to a publication.',
            'A designed schedule is not an optimised or validated schedule.',
        ],
        'approval': None,
    }
