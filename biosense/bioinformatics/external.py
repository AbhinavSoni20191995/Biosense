"""The external-tool adapter: how DESeq2, Scanpy or MACS would run, and what must
come back before BioSense will believe them.

Phase 1 ships the **contract** and a mock. Nothing in the base install depends on
R, Bioconductor or a container runtime, and ordinary CI runs none of this.

The contract matters more than the execution. An imported number with no record
of what produced it is unfalsifiable, so `record()` refuses a result that cannot
name its tool version, and every field below is required before a result is
accepted:

    command, tool, tool_version, parameters,
    input_checksums, output_checksums,
    exit_status, duration_s, environment,
    stdout_tail, stderr_tail

`run_mock` returns a structurally complete record marked `mock: true`, so the
downstream contract can be exercised and no reader can mistake it for a run that
happened.
"""
from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path

from .. import contracts as K

REQUIRED = ('command', 'tool', 'tool_version', 'parameters', 'input_checksums',
            'output_checksums', 'exit_status', 'duration_s', 'environment')

# Declared adapters. None is executed in Phase 1.
ADAPTERS = {
    'deseq2': {'binary': 'Rscript', 'label': 'DESeq2 (Bioconductor)',
               'version_cmd': ['Rscript', '-e', 'cat(as.character(packageVersion("DESeq2")))'],
               'note': 'Count-level differential expression with dispersion modelling and '
                       'shrinkage. The reason the in-process bulk tool refuses raw counts.'},
    'edger': {'binary': 'Rscript', 'label': 'edgeR (Bioconductor)',
              'version_cmd': ['Rscript', '-e', 'cat(as.character(packageVersion("edgeR")))'],
              'note': 'Alternative count-level differential expression.'},
    'scanpy': {'binary': 'python', 'label': 'Scanpy',
               'version_cmd': ['python', '-c', 'import scanpy; print(scanpy.__version__)'],
               'note': 'Single-cell processing. Needs h5py/anndata; Phase 2.'},
    'macs': {'binary': 'macs3', 'label': 'MACS3', 'version_cmd': ['macs3', '--version'],
             'note': 'Peak calling for ChIP/ATAC; Phase 2.'},
}


def available(name):
    """Whether the adapter's binary is on PATH. Never installs anything."""
    a = ADAPTERS.get(name)
    return bool(a) and shutil.which(a['binary']) is not None


def describe():
    return {k: {**v, 'available_here': available(k)} for k, v in ADAPTERS.items()}


def record(*, command, tool, tool_version, parameters, inputs, outputs, exit_status,
           duration_s, environment, stdout_tail='', stderr_tail='', mock=False):
    """Build the execution record. Refuses anything that cannot be reproduced."""
    if not tool_version or str(tool_version).lower() in ('unknown', 'none', ''):
        raise K.ContractError(
            f'refusing to record a run of {tool!r} with no tool version. A number whose software '
            f'version is unknown cannot be reproduced or disputed, which makes it an assertion '
            f'rather than a measurement.')
    rec = {
        'command': command, 'tool': tool, 'tool_version': str(tool_version),
        'parameters': dict(parameters or {}),
        'input_checksums': [K.sha256_file(p) for p in inputs],
        'output_checksums': [K.sha256_file(p) for p in outputs if Path(p).is_file()],
        'exit_status': int(exit_status), 'duration_s': round(float(duration_s), 3),
        'environment': environment, 'stdout_tail': (stdout_tail or '')[-2000:],
        'stderr_tail': (stderr_tail or '')[-2000:],
        'mock': bool(mock), 'recorded_at': K.now_iso(),
    }
    missing = [f for f in REQUIRED if rec.get(f) in (None, '')]
    if missing:
        raise K.ContractError(f'external execution record is missing {", ".join(missing)}')
    return rec


def run_mock(name, *, inputs, parameters=None, note=''):
    """A structurally complete record of a run that did not happen.

    Exists so the AnalysisResult contract can be exercised end to end without R
    or a container. `mock: true` is in the record and in its note, and the
    executor refuses to treat a mock as evidence.
    """
    a = ADAPTERS.get(name)
    if a is None:
        raise K.ContractError(f'unknown external adapter {name!r}; '
                              f'declared: {", ".join(sorted(ADAPTERS))}')
    t0 = time.time()
    return record(
        command=f'[MOCK] {a["binary"]} <{name} script> ' + ' '.join(str(i) for i in inputs),
        tool=name, tool_version='mock-0', parameters=parameters or {},
        inputs=list(inputs), outputs=[], exit_status=0, duration_s=time.time() - t0,
        environment={'kind': 'mock', 'available_here': available(name),
                     'why': 'Phase 1 ships the adapter contract, not the execution. '
                            'No external process ran.'},
        stdout_tail=note or f'{a["label"]}: {a["note"]}', mock=True)


def run_external(name, argv, *, inputs, outputs, parameters=None, timeout=900,
                 i_have_execution_permission=False):  # pragma: no cover - needs the binary
    """Run a real external tool. Not exercised in CI and not required by it."""
    if not i_have_execution_permission:
        raise K.ContractError('running an external tool needs i_have_execution_permission=True')
    a = ADAPTERS.get(name)
    if a is None or not available(name):
        raise K.ContractError(f'{name!r} is not available on this machine '
                              f'({(a or {}).get("binary")!r} is not on PATH)')
    ver = subprocess.run(a['version_cmd'], capture_output=True, text=True, timeout=60)
    t0 = time.time()
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    return record(command=' '.join(argv), tool=name,
                  tool_version=(ver.stdout or ver.stderr).strip() or None,
                  parameters=parameters or {}, inputs=inputs, outputs=outputs,
                  exit_status=proc.returncode, duration_s=time.time() - t0,
                  environment={'kind': 'subprocess', 'binary': shutil.which(a['binary'])},
                  stdout_tail=proc.stdout, stderr_tail=proc.stderr)
