"""Synthetic stand-in for the wet-lab bioreactor.

Maps a ProductionProtocol 2.0 onto the iPSC -> monocyte simulator and returns a
BioreactorRun 2.0 record with source = "synthetic_standin". It exists so the
loop (protocol -> run -> analysis -> orchestrator) can be exercised offline.
Its numbers are workflow demonstrations, never biological evidence.

The stand-in understands only what the simulator models: four stages
(expansion, mesoderm, hemato, myeloid), seeding density, agitation, dissolved
oxygen, feeding, and BMP4 / VEGF / M-CSF / IL-3. Every other protocol element
is listed in the run's notes as "not simulated".

Engineered genotypes are hidden truth, supplied separately (a "truth" file the
agents never read), exactly like the clonal variant in the simulator.
"""
from __future__ import annotations

import copy
from dataclasses import asdict
from typing import Dict, List, Optional

import numpy as np

from .simulator import (Bioreactor, LineState, SensorState, Setpoints,
                        introduce_sensor_fault, introduce_variant)

SIM_STAGES = [s for s, _ in Bioreactor.STAGES]
FACTOR_ALIASES = {
    "bmp4": "bmp4", "bmp-4": "bmp4",
    "vegf": "vegf", "vegf-a": "vegf", "vegfa": "vegf",
    "m-csf": "mcsf", "mcsf": "mcsf", "csf1": "mcsf", "csf-1": "mcsf",
    "il-3": "il3", "il3": "il3",
}
PARAM_MAP = {  # protocol culture parameter -> (Setpoints field, accepted units -> factor)
    "agitation_rpm": ("agitation_rpm", {"rpm": 1.0}),
    "dissolved_oxygen": ("do_setpoint", {"fraction": 1.0, "%air_saturation": 0.01, "%": 0.01}),
    "feed_fraction": ("feed_fraction", {"fraction": 1.0, "%": 0.01}),
    "feed_interval": ("feed_interval_h", {"h": 1.0, "day": 24.0}),
    "rock_inhibitor_exposure": ("rocki_hours", {"h": 1.0, "day": 24.0}),
}
SCENARIOS = ("clean", "clonal", "sensor_fault", "stain_fault")


def _num(q):
    if not isinstance(q, dict) or q.get("provenance") == "gap":
        return None
    v = q.get("value")
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def arm_steps(protocol: Dict, arm_id: str) -> List[Dict]:
    """Base steps with this arm's adjustments applied (same rule as biosense.production.protocol)."""
    adj = {}
    for a in protocol.get("arm_adjustments", []):
        if a["arm_id"] == arm_id:
            adj.setdefault(a["step_id"], []).append(a)
    out = []
    for st in protocol["stages"]:
        for step in st["steps"]:
            s = copy.deepcopy(step)
            s["stage_id"] = st["stage_id"]
            for a in adj.get(step["step_id"], []):
                if a.get("omit"):
                    s = None
                    break
                if "quantity" in a:
                    s["quantity"] = a["quantity"]
                if a.get("day_shift"):
                    s["day"] = s["day"] + a["day_shift"]
                    if s.get("end_day") is not None:
                        s["end_day"] = s["end_day"] + a["day_shift"]
            if s is not None:
                out.append(s)
    return out


def protocol_to_setpoints(protocol: Dict, arm_id: str):
    """Return (Setpoints, stage_days, notes). Raises ValueError if the stages do not map."""
    stages = protocol["stages"]
    if len(stages) != len(SIM_STAGES):
        raise ValueError(f"stand-in models exactly {len(SIM_STAGES)} stages {SIM_STAGES}; "
                         f"protocol has {len(stages)}")
    stage_map = {st["stage_id"]: SIM_STAGES[i] for i, st in enumerate(stages)}
    stage_days = {stage_map[st["stage_id"]]: float(st["end_day"] - st["start_day"]) for st in stages}
    sp, notes = Setpoints(), []
    volume = _num(protocol["culture_system"]["working_volume"])
    if volume is None or protocol["culture_system"]["working_volume"]["unit"] != "mL":
        raise ValueError("stand-in needs culture_system.working_volume in mL")
    for name, q in protocol["culture_system"]["parameters"].items():
        v = _num(q)
        if name not in PARAM_MAP:
            notes.append(f"culture parameter {name!r} not simulated")
            continue
        field_, units = PARAM_MAP[name]
        if v is None or q["unit"] not in units:
            notes.append(f"{name}: gap or unsupported unit {q.get('unit')!r}; simulator default used")
            continue
        setattr(sp, field_, v * units[q["unit"]])
    for s in arm_steps(protocol, arm_id):
        q = s.get("quantity")
        if s["action"] == "seed":
            v = _num(q)
            if v is None or q["unit"] != "cells/mL":
                notes.append("seed density gap or not in cells/mL; simulator default used")
            else:
                sp.seed_density = v / 1e6
        elif s["action"] == "add_factor":
            key = FACTOR_ALIASES.get((s.get("factor") or "").strip().lower())
            v = _num(q)
            if key is None:
                notes.append(f"factor {s.get('factor')!r} ({s['step_id']}) not simulated")
            elif v is None or q["unit"] != "ng/mL":
                notes.append(f"{s['step_id']}: {s.get('factor')} gap or not in ng/mL; simulator default used")
            else:
                setattr(sp, key, v)
                sim_stage = stage_map[s["stage_id"]]
                expected = {"bmp4": "mesoderm", "vegf": "mesoderm", "mcsf": "myeloid", "il3": "myeloid"}[key]
                if sim_stage != expected:
                    notes.append(f"{s['step_id']}: {s.get('factor')} acts only in the simulator's {expected} stage")
        elif s["action"] not in ("sample", "harvest", "start_harvest"):
            notes.append(f"step {s['step_id']} ({s['action']}) not simulated")
    return sp, stage_days, volume, sorted(set(notes))


def _apply_truth(line: LineState, arm: Dict, truth: Optional[Dict]):
    t = (truth or {}).get("arms", {}).get(arm["arm_id"])
    if not t:
        return
    line.genotype_mu_ratio = float(t.get("genotype_mu_ratio", 1.0))
    line.genotype_diff_ratio = float(t.get("genotype_diff_ratio", 1.0))
    line.factor_optima.update({k: float(v) for k, v in t.get("factor_optima", {}).items()})


def _harvest(rec: Dict, volume_ml: float, seed_e6: float, day: float) -> Dict:
    obs, out = rec["observations"], rec["outcome"]
    panels = [o["minicyto"] for o in obs if o.get("minicyto")]
    onset = next((o["day"] for o in obs if o["harvest_cells_e6_per_ml_day"] > 0), None)
    if panels:
        marker, resid, method = panels[-1]["CD14_PE_pct"], panels[-1]["TRA_1_60_APC_pct"], "M3e no-wash panel CD14-PE (synthetic)"
    else:
        tail = [o for o in obs if o["harvest_frac_in_monocyte_gate"] > 0][-1:]
        marker = round(100 * tail[0]["harvest_frac_in_monocyte_gate"], 2) if tail else 0.0
        resid, method = None, "M4 monocyte gate fraction (synthetic; no antigen panel)"
    return {
        "day": round(day, 2),
        "viable_cells_total": round(out["harvest_total_e6_per_ml"] * 1e6 * volume_ml, 0),
        "viability_pct": out["final_viability_pct"],
        "target_marker_pct": marker,
        "marker_method": method,
        "residual_pluripotency_pct": resid,
        "residual_pluripotency_marker": "TRA-1-60" if resid is not None else None,
        "harvest_onset_day": onset,
    }


def simulate_protocol(protocol: Dict, protocol_sha256: str, mode: str = "minimal", seed: int = 5,
                      scenario: str = "clean", truth: Optional[Dict] = None, replicates: int = 1,
                      release_tests: Optional[bool] = None, run_id: Optional[str] = None) -> Dict:
    """Run every genotype arm of a protocol through the simulator."""
    if mode not in ("minimal", "max"):
        raise ValueError("mode must be minimal or max")
    if scenario not in SCENARIOS:
        raise ValueError(f"scenario must be one of {SCENARIOS}")
    release_tests = (mode == "max") if release_tests is None else release_tests
    rng = np.random.default_rng(seed)
    base_line = LineState.sample(rng, "L01")
    arms, notes_all = [], set()
    for ai, arm in enumerate(protocol["genotype_arms"]):
        sp, stage_days, volume, notes = protocol_to_setpoints(protocol, arm["arm_id"])
        notes_all.update(notes)
        for rep in range(1, replicates + 1):
            line = copy.deepcopy(base_line)
            line.line_id = arm.get("line_id") or f"L01-{arm['arm_id']}"
            _apply_truth(line, arm, truth)
            sensors = SensorState()
            history = []
            if mode == "max":
                # four prior runs of this line at the base setpoints (drift reference window)
                for p in range(4):
                    if scenario == "clonal" and p == 1:
                        introduce_variant(line, 0.2)
                    prev = Bioreactor(line, seed=seed * 100 + ai * 10 + p, sensors=sensors).run(
                        Setpoints(agitation_rpm=float(58 + rng.normal(0, 1.5))), run_id=f"hist-{arm['arm_id']}-{p}")
                    history.append(asdict(prev))
            if scenario == "sensor_fault":
                introduce_sensor_fault(sensors, "capacitance", 0.22)
            elif scenario == "stain_fault":
                introduce_sensor_fault(sensors, "staining", 0.30)
            elif scenario == "clonal" and mode == "minimal":
                introduce_variant(line, 0.6)
            rec = asdict(Bioreactor(line, seed=seed * 1000 + ai * 10 + rep, sensors=sensors).run(
                sp, run_id=f"{arm['arm_id']}-r{rep}", stage_days=stage_days,
                buy_genomic=release_tests))
            day = sum(stage_days.values())
            a = {"arm_id": arm["arm_id"], "replicate": rep, "line_id": rec["line_id"],
                 "passage_number": rec["passage_number"],
                 "input_cells": round(sp.seed_density * 1e6 * volume, 0),
                 "deviations": [],
                 "timepoints": [{"day": o["day"], "viable_cells_total": round(o["vcd_e6_per_ml"] * 1e6 * volume, 0),
                                 "viability_pct": o["viability_pct"]}
                                for o in rec["observations"] if abs(o["day"] - round(o["day"])) < 1e-6
                                and round(o["day"]) % 5 == 0],
                 "harvest": _harvest(rec, volume, sp.seed_density, day)}
            if release_tests:
                g = rec["observations"][-1].get("genomic") or {}
                cnv = g.get("cnv_20q11_21_copies", 2.0)
                a["qc_tests"] = {"sterility": "no_growth", "mycoplasma": "not_detected",
                                 "endotoxin_EU_per_mL": 0.05,
                                 "karyotype": "abnormal" if cnv > 2.15 else "normal",
                                 "karyotype_detail": f"20q11.21 copies {cnv} (synthetic ddPCR)"}
            if mode == "max":
                a["observations"] = rec["observations"]
                a["setpoints"] = rec["setpoints"]
                a["outcome"] = rec["outcome"]
                a["history"] = history
            arms.append(a)
    return {
        "schema_version": "2.0",
        "run_id": run_id or f"standin-{protocol['protocol_id']}-{mode}-{scenario}-s{seed}",
        "protocol_id": protocol["protocol_id"],
        "protocol_sha256": protocol_sha256,
        "mode": mode,
        "source": "synthetic_standin",
        "label": f"SYNTHETIC STAND-IN ({scenario}); workflow demonstration, not biological evidence",
        "operator": None,
        "started_at": None,
        "notes": "; ".join(sorted(notes_all)) or "all protocol elements mapped to the simulator",
        "arms": arms,
    }
