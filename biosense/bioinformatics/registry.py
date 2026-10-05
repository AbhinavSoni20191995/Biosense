"""The tool registry: what can execute, over what, needing what.

The agent selects a tool by name. Code executes it. The point of the registry is
that the selection is checkable: a plan naming an unknown tool is refused, a plan
whose modality the tool does not accept is refused, and a plan whose dataset does
not carry the metadata the tool declares it needs is refused *and told which
field is missing*. None of that depends on a prompt being followed.

Adding a modality means adding a ToolSpec, not editing a prompt or a schema.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .. import contracts as K
from .toolkit.bulk import basic_de
from .toolkit.cytometry import population_stats


@dataclass(frozen=True)
class ToolSpec:
    name: str
    label: str
    version: str
    modalities: tuple
    analysis_types: tuple
    required_inputs: tuple
    required_metadata: tuple
    optional_inputs: tuple = ()
    parameters: dict = field(default_factory=dict)
    outputs: tuple = ()
    software: str = 'numpy + scipy, in process'
    external_dependency: str = None
    resource: str = 'in-process, seconds'
    status: str = 'phase_1'
    runner: object = None
    notes: str = ''

    def to_dict(self):
        d = {k: v for k, v in self.__dict__.items() if k != 'runner'}
        d['modalities'] = list(self.modalities)
        d['analysis_types'] = list(self.analysis_types)
        d['required_inputs'] = list(self.required_inputs)
        d['required_metadata'] = list(self.required_metadata)
        d['optional_inputs'] = list(self.optional_inputs)
        d['outputs'] = list(self.outputs)
        return d


TOOLS = {}


def register(spec):
    TOOLS[spec.name] = spec
    return spec


register(ToolSpec(
    name=population_stats.NAME, label='Flow-cytometry population comparison',
    version=population_stats.VERSION,
    modalities=population_stats.MODALITIES,
    analysis_types=population_stats.ANALYSIS_TYPES,
    required_inputs=('a processed, gated population table (csv/tsv), one row per sample',),
    required_metadata=population_stats.REQUIRED_METADATA,
    optional_inputs=('replicate_column', 'donor_column', 'batch_column'),
    parameters={'paired': 'bool, default from the manifest',
                'readouts': 'list of columns; default every numeric non-design column'},
    outputs=('per-readout effect, CI, p and BH-q', 'QC checks', 'candidate process parameters'),
    runner=population_stats.run,
    notes='Processed tables only. Raw FCS, automated gating, FlowSOM and UMAP are Phase 2 and '
          'are refused rather than approximated.'))

register(ToolSpec(
    name=basic_de.NAME, label='Bulk expression comparison (screening)',
    version=basic_de.VERSION,
    modalities=basic_de.MODALITIES,
    analysis_types=basic_de.ANALYSIS_TYPES,
    required_inputs=('a long-format table with a feature column and a normalised expression column',),
    required_metadata=basic_de.REQUIRED_METADATA,
    parameters={'feature_column': 'auto-detected from a known set, or named in the plan',
                'value_column': 'auto-detected from a known set, or named in the plan'},
    outputs=('per-feature difference in log2 expression with BH-q', 'QC checks'),
    runner=basic_de.run,
    notes='A screening comparison, not DESeq2/edgeR. Raw counts are refused: they need dispersion '
          'modelling, which belongs in the external-tool adapter.'))


# Declared, not implemented. Listed so an interface and a planner can see the
# shape of what is coming without anything pretending it can already run.
PLANNED = (
    {'name': 'single_cell.pseudobulk_de', 'label': 'Single-cell pseudobulk differential expression',
     'modalities': ['single_cell_rna'], 'status': 'planned',
     'notes': 'Needs an h5ad reader (h5py) and Scanpy; deliberately out of Phase 1.'},
    {'name': 'chromatin.peak_overlap', 'label': 'Peak overlap and peak-to-gene association',
     'modalities': ['chip_seq', 'atac_seq'], 'status': 'planned',
     'notes': 'Needs BED/narrowPeak handling and an annotation source.'},
    {'name': 'cytometry.gating', 'label': 'Automated gating / FlowSOM / UMAP',
     'modalities': ['flow_cytometry'], 'status': 'planned',
     'notes': 'Needs an FCS reader and a clustering stack.'},
    {'name': 'external.deseq2', 'label': 'DESeq2 via the external-tool adapter',
     'modalities': ['bulk_rna'], 'status': 'planned',
     'notes': 'Contract and a mock ship in Phase 1; running real R is out of scope and must stay '
              'optional so ordinary CI needs no Bioconductor.'},
)


def get(name):
    if name not in TOOLS:
        planned = next((p for p in PLANNED if p['name'] == name), None)
        if planned:
            raise K.ContractError(
                f'{name!r} is declared but not implemented ({planned["notes"]}). '
                f'Available now: {", ".join(sorted(TOOLS))}.')
        raise K.ContractError(f'unknown tool {name!r}; available: {", ".join(sorted(TOOLS))}')
    return TOOLS[name]


def for_analysis(analysis_type, modality):
    """Tools that can run this analysis on this modality."""
    return [t for t in TOOLS.values()
            if analysis_type in t.analysis_types and modality in t.modalities]


def describe():
    return {'implemented': [t.to_dict() for t in TOOLS.values()], 'planned': list(PLANNED)}
