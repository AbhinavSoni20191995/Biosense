"""Product-specific synthetic stand-ins for the wet lab.

A stand-in reads an approved ProductionProtocol 2.0, turns its literature-derived
values into model inputs, and returns a BioreactorRun 2.0 record labelled
`synthetic_standin`. Output is a workflow demonstration, never biological
evidence, and no stand-in is a validated digital twin.

Each arm's engineered genotype is HIDDEN TRUTH supplied in a separate file that
the agents never read, so a knockout can genuinely need different conditions
than the wild-type control and the loop has to discover that from measurements.

  tcell       CAR-T style process: activation -> transduction -> expansion.
              Minimal measurement mode only.
  ipsc_tcell  iPSC -> T-lineage process in five stages: mesoderm, hemogenic
              endothelium, Notch-driven T commitment, maturation, expansion.
              Reports residual pluripotency. Minimal measurement mode only.
  monocyte    iPSC -> monocyte process, in analysis_agent/protocol_runner.py,
              which also supports max mode via the integrated-machine simulator.
"""
from . import ipsc_tcell, tcell

STANDINS = {'tcell': tcell, 'ipsc_tcell': ipsc_tcell}


def get_standin(name):
    if name == 'monocyte':
        from analysis_agent import protocol_runner
        return protocol_runner
    if name not in STANDINS:
        raise ValueError(f'unknown stand-in {name!r}; available: '
                         f'{", ".join(sorted(STANDINS))}, monocyte')
    return STANDINS[name]
