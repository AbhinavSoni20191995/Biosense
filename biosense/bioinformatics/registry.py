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
from .toolkit.bulk import gene_set_score
from .toolkit.bulk import pathway_enrichment
from .toolkit.proteomics import abundance
from .toolkit.chromatin import peak_overlap
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
    # Which file types this tool can actually open. The readiness check asks the
    # tool rather than assuming CSV, so adding a modality does not mean editing
    # a hardcoded list somewhere else.
    file_types: tuple = ('csv', 'tsv')
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
        d['file_types'] = list(self.file_types)
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


register(ToolSpec(
    name=gene_set_score.NAME, label='Gene-set score between two conditions',
    version=gene_set_score.VERSION,
    modalities=gene_set_score.MODALITIES,
    analysis_types=gene_set_score.ANALYSIS_TYPES,
    required_inputs=('a long-format table with a feature column, a sample column and a '
                     'normalised expression column',),
    required_metadata=gene_set_score.REQUIRED_METADATA,
    parameters={'readouts': 'the gene set, by symbol (2 to 200 genes); named by the planner',
                'value_column': 'auto-detected from a known set, or named in the plan'},
    outputs=('the set score difference with CI, p', 'each member gene with BH-q across the set',
             'which genes were missing'),
    runner=gene_set_score.run,
    notes='For a question about a module (identity genes, a pathway the evidence names), which a '
          'genome-wide top-genes list does not answer. Not a background-controlled enrichment.'))

register(ToolSpec(
    name=pathway_enrichment.NAME, label='Pathway enrichment (which programmes moved)',
    version=pathway_enrichment.VERSION,
    modalities=pathway_enrichment.MODALITIES,
    analysis_types=pathway_enrichment.ANALYSIS_TYPES,
    required_inputs=('a long-format table with a feature column and a normalised expression '
                     'column', 'a gene-set library fetched with `datasets fetch-genesets`'),
    required_metadata=pathway_enrichment.REQUIRED_METADATA,
    parameters={'library': 'reactome or go_bp (option library=…), fetched beforehand',
                'q': 'gene q-value for the over-representation list (option, default 0.05)'},
    outputs=('per pathway: rank-shift and over-representation statistics with BH-q',
             'the genes behind each pathway', 'the library, its version and checksum'),
    runner=pathway_enrichment.run,
    notes='Openly licensed libraries only (Reactome CC0, Gene Ontology CC BY). The background '
          'is the genes this table measured, never the genome.'))

register(ToolSpec(
    name=abundance.NAME, label='Protein abundance comparison (proteomics, secretome)',
    version=abundance.VERSION,
    modalities=abundance.MODALITIES,
    analysis_types=abundance.ANALYSIS_TYPES,
    required_inputs=('a processed protein or analyte table: long (protein, sample, condition, '
                     'value) or one row per sample with one column per analyte',),
    required_metadata=abundance.REQUIRED_METADATA,
    parameters={'min_valid': 'values needed in each group to test a protein (option; '
                             'default 2)',
                'scale': 'auto, log or linear (option); linear values are log2-transformed '
                         'once'},
    outputs=('per protein: difference in log2 abundance with BH-q',
             'proteins detected in one condition only, listed rather than imputed'),
    runner=abundance.run,
    notes='Processed tables only (MaxQuant, DIA-NN, Olink, Luminex exports). Missing values '
          'are never imputed: a protein seen in one condition only is reported as such.'))

register(ToolSpec(
    name=peak_overlap.NAME, label='Chromatin peak overlap and peak-to-gene association',
    version=peak_overlap.VERSION,
    modalities=peak_overlap.MODALITIES,
    analysis_types=peak_overlap.ANALYSIS_TYPES,
    required_inputs=('two processed peak files (BED / narrowPeak / broadPeak), one per condition',),
    required_metadata=peak_overlap.REQUIRED_METADATA,
    optional_inputs=('gene_anchors: a TSV of gene, chrom, tss, strand for annotation',),
    parameters={'peak_files': 'modality_detail mapping condition -> file; never guessed from '
                              'a filename'},
    outputs=('overlap in both directions', 'peak width and q-value comparison',
             'peak-to-gene annotation of condition-specific peaks'),
    file_types=('bed', 'narrowPeak', 'broadPeak'),
    runner=peak_overlap.run,
    notes='Processed peaks only. Calling peaks from signal is outside what BioSense does: run '
          'MACS or an established pipeline and register the result. This compares peak SETS, '
          'not replicated samples, and says so in its QC.'))


def _register_single_cell():
    """Registered only when the optional environment is present.

    A tool that is listed but cannot run is worse than one that is honestly
    absent: a planner would select it and fail at execution instead of at
    planning, which is the wrong end of the process to find out.
    """
    from ..data.readers import h5ad as H5
    if not H5.available():
        return None
    from .toolkit.single_cell import pseudobulk
    return register(ToolSpec(
        name=pseudobulk.NAME, label='Single-cell pseudobulk comparison',
        version=pseudobulk.VERSION,
        modalities=pseudobulk.MODALITIES,
        analysis_types=pseudobulk.ANALYSIS_TYPES,
        required_inputs=('an .h5ad matrix with a sample column in obs',),
        required_metadata=pseudobulk.REQUIRED_METADATA,
        optional_inputs=('cell_type column for composition', 'donor_column'),
        parameters={'readouts': 'genes to compare; default every gene in the matrix'},
        outputs=('per-gene pseudobulk comparison with BH-q', 'signature scores',
                 'cell composition per sample'),
        software='numpy + scipy + anndata, in process',
        file_types=('h5ad',),
        external_dependency='singlecell',
        runner=pseudobulk.run,
        notes='Pseudobulk by sample, because the replication unit is the sample and not the '
              'cell. No reclustering, re-annotation or UMAP: those change what the data says '
              'and only run when they resolve the named uncertainty.'))


_SINGLE_CELL = _register_single_cell()


def _register_identity_purity():
    """Per-sample identity and purity from single cells; needs the same optional reader."""
    from ..data.readers import h5ad as H5
    if not H5.available():
        return None
    from .toolkit.single_cell import identity_purity as IP
    return register(ToolSpec(
        name=IP.NAME, label='Single-cell identity and purity per sample',
        version=IP.VERSION,
        modalities=IP.MODALITIES,
        analysis_types=IP.ANALYSIS_TYPES,
        required_inputs=('an .h5ad matrix with a sample column in obs',
                         'the identity markers, named in the plan (--readouts)'),
        required_metadata=IP.REQUIRED_METADATA,
        parameters={'readouts': 'the identity marker set, by symbol (2 to 50)',
                    'min_markers': 'markers a cell must express to count as on-identity '
                                   '(option; default half the markers present, rounded up)',
                    'detect_threshold': 'a marker counts as expressed strictly above this '
                                        'value (option; default 0)',
                    'negative_markers': 'comma-separated symbols; a cell expressing any is '
                                        'off-identity (option)'},
        outputs=('per sample: the fraction of cells on-identity', 'that fraction compared '
                 'between conditions, n = samples', 'per-marker detection rates'),
        software='numpy + scipy + anndata, in process',
        file_types=('h5ad',),
        external_dependency='singlecell',
        runner=IP.run,
        notes='Purity is counted per cell and compared per sample: the sample is the unit of '
              'replication. A marker rule, stated in the result, not a clustering.'))


_IDENTITY_PURITY = _register_identity_purity()


def _register_deseq2():
    """Registered only where Rscript and DESeq2 actually are.

    Same reasoning as the single-cell tool, with a sharper edge: the alternative
    to DESeq2 is not a slower DESeq2, it is a Welch test over counts, which is
    the wrong answer rather than a cheaper one. So this is never silently
    substituted; without R the tool is absent and a plan naming it is refused.
    """
    from . import external as EXT
    from .toolkit.bulk import deseq2
    if not (EXT.available('deseq2') and deseq2.SCRIPT.is_file()):
        return None
    return register(ToolSpec(
        name=deseq2.NAME, label='DESeq2 count-level differential expression',
        version=deseq2.VERSION,
        modalities=deseq2.MODALITIES,
        analysis_types=deseq2.ANALYSIS_TYPES,
        required_inputs=('a long-format table of raw integer counts with a feature column, '
                         'a count column and a sample column',),
        required_metadata=deseq2.REQUIRED_METADATA,
        parameters={'feature_column': 'auto-detected from a known set, or named in the plan',
                    'count_column': 'auto-detected; must be raw counts, not normalised',
                    'design': 'always ~ condition; a covariate needs a stated reason'},
        outputs=('per-feature log2 fold change with lfcSE, Wald p and BH-q',
                 'the external execution record', 'QC checks'),
        software='DESeq2 (Bioconductor) via Rscript, as a subprocess',
        external_dependency='Rscript + DESeq2',
        resource='subprocess, seconds to minutes',
        status='phase_2',
        runner=deseq2.run,
        notes='Count-level dispersion modelling and shrinkage: what bulk.expression_comparison '
              'refuses raw counts in favour of. Runs out of process through the external-tool '
              'adapter, and every result carries the command, versions and checksums that '
              'produced it. A mock record is refused rather than reported as a result.'))


_DESEQ2 = _register_deseq2()


# Declared, not implemented. Listed so an interface and a planner can see the
# shape of what is coming without anything pretending it can already run.
PLANNED = (
    {'name': 'single_cell.pseudobulk_comparison',
     'label': 'Single-cell pseudobulk comparison',
     'modalities': ['single_cell_rna'],
     'status': 'optional_environment' if _SINGLE_CELL is None else 'phase_2',
     'notes': 'Needs biosense[singlecell] (anndata + h5py). Install it with '
              '`uv sync --extra singlecell`; until then it is not registered, so a plan is '
              'refused at planning rather than at execution.'},
    {'name': 'cytometry.gating', 'label': 'Automated gating / FlowSOM / UMAP',
     'modalities': ['flow_cytometry'], 'status': 'planned',
     'notes': 'Needs an FCS reader and a clustering stack. fcsparser pins numpy below this '
              'project\'s version and cannot resolve, so raw FCS stays deferred rather than '
              'pinning the whole project backwards.'},
    {'name': 'external.deseq2', 'label': 'DESeq2 via the external-tool adapter',
     'modalities': ['bulk_rna'],
     'status': 'external_binary' if _DESEQ2 is None else 'phase_2',
     'notes': 'Needs Rscript with the DESeq2 Bioconductor package. The adapter, the R script and '
              'the counts handoff ship now and are tested without R; where R is absent the tool '
              'is not registered, so a plan is refused at planning rather than part-way through. '
              'It never runs in-process, and ordinary CI never needs it.'},
)


#: statuses a PLANNED entry may carry, and what each one is waiting on.
#: 'planned' is work nobody has written. The others are written and gated on
#: something the reader can install, which is why the capabilities diagram may
#: draw them as existing even where they cannot run. They are kept apart because
#: the refusals read differently: one has no remedy, the others name one.
WAITING_ON = {
    'planned': 'is declared but not implemented',
    'optional_environment': 'needs an optional environment',
    'external_binary': 'needs an external program that is not on this machine',
    'phase_2': None,
}
IMPLEMENTED_STATUSES = ('optional_environment', 'external_binary', 'phase_2')


def is_implemented(name):
    """Whether the code for this tool exists, regardless of this install.

    Distinct from ``name in TOOLS``, which answers whether it can run *here*.
    Documents describe the project and must use this; a plan must use TOOLS.
    """
    if name in TOOLS:
        return True
    planned = next((p for p in PLANNED if p['name'] == name), None)
    return bool(planned) and planned['status'] in IMPLEMENTED_STATUSES


def get(name):
    if name not in TOOLS:
        planned = next((p for p in PLANNED if p['name'] == name), None)
        if planned:
            kind = WAITING_ON.get(planned['status']) or 'is declared but not implemented'
            raise K.ContractError(
                f'{name!r} {kind} ({planned["notes"]}). '
                f'Available now: {", ".join(sorted(TOOLS))}.')
        raise K.ContractError(f'unknown tool {name!r}; available: {", ".join(sorted(TOOLS))}')
    return TOOLS[name]


def for_analysis(analysis_type, modality):
    """Tools that can run this analysis on this modality."""
    return [t for t in TOOLS.values()
            if analysis_type in t.analysis_types and modality in t.modalities]


def describe():
    """What can run here, and what cannot yet.

    A tool whose optional extra is installed is registered, so it is reported as
    implemented and dropped from the planned list: listing it as both would tell a
    reader it is unavailable while a plan using it runs.
    """
    return {'implemented': [t.to_dict() for t in TOOLS.values()],
            'planned': [dict(p) for p in PLANNED if p['name'] not in TOOLS]}
