"""
End-to-end demo: 9 runs on one iPSC line, a culture-adaptation clone appears
at passage 3, and the Analysis Agent has to notice.

    python run_demo.py            # variant arm
    python run_demo.py --clean    # control arm, no variant
"""
import argparse
import json
import os
import sys
from dataclasses import asdict

import numpy as np

# Runnable from anywhere: import the files as the analysis_agent package.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from analysis_agent.analysis_tools import (attribute_cause, check_cqa,  # noqa: E402
                                           derive_kinetics, recommend_next_action)
from analysis_agent.simulator import (Bioreactor, LineState, SensorState,  # noqa: E402
                                      Setpoints, introduce_sensor_fault, introduce_variant)


def build_history(variant_at=3, sensor_fault_at=None, fault_kind="capacitance",
                  n=9, seed=7):
    rng = np.random.default_rng(seed)
    line = LineState.sample(rng, "L01")
    sensors = SensorState()
    history = []
    for p in range(n):
        if variant_at is not None and p == variant_at:
            introduce_variant(line, 0.03)
        if sensor_fault_at is not None and p == sensor_fault_at:
            introduce_sensor_fault(sensors, fault_kind,
                                   0.22 if fault_kind == "capacitance" else 0.30)
        sp = Setpoints(agitation_rpm=float(58 + rng.normal(0, 1.5)))
        rec = Bioreactor(line, seed=100 + p, sensors=sensors).run(sp, run_id=f"R{p:02d}")
        history.append(asdict(rec))
    return history


def analyse(history, budget_remaining=100.0):
    """Exactly what the agent's tools return, assembled into its output schema."""
    run = history[-1]
    att = attribute_cause(run, history)
    cqa = check_cqa(run)
    kin = derive_kinetics(run)
    rec = recommend_next_action(att, cqa, budget_remaining)
    clo = att["clonal"]

    evidence = []
    if clo.get("testable"):
        evidence.append(
            f"Growth metrics vs passage (setpoint-adjusted): "
            f"{clo['growth_vs_passage_rho']}")
        evidence.append(
            f"Differentiation metrics vs passage (setpoint-adjusted): "
            f"{clo['differentiation_vs_passage_rho']}")
        evidence.append(
            f"Yield fell {clo['yield_drop_early_to_late']*100:.0f}% from early "
            f"to late runs; setpoints explain the change: "
            f"{att['process_fit'].get('setpoints_explain_change')}")
    evidence.append(
        f"Measurement coherence: {att['coherence']['incoherent_count']} "
        f"incoherent channel pairs of {len(att['coherence']['checks'])}")
    if att["sensor_health"]["flags"]:
        evidence.append(f"Sensor drift flags: {att['sensor_health']['flags']}")
    evidence.append(
        f"Carbon/oxygen balance closed: {att['mass_balance']['balance_closed']} "
        f"(Y_lac/glc {att['mass_balance']['y_lac_per_glc']}, "
        f"OUR/VCD {att['mass_balance']['our_per_vcd']})")
    if cqa["purity"].get("available"):
        ss = cqa["purity"]
        evidence.append(
            f"Fused purity {ss['purity_estimate_pct']}% (95% CI {ss['ci95']}, "
            f"sigma {ss['sigma_pct']}) from {'+'.join(ss['sources'])}; "
            f"label-free {ss['label_free'].get('estimate_pct')}% vs direct M3e "
            f"{ss['direct'].get('cd14_pct')}% at age "
            f"{ss['direct'].get('age_h')} h; calibration offset "
            f"{ss['calibration_offset_pct']}%")
    if cqa["staining"].get("testable"):
        st = cqa["staining"]
        evidence.append(
            f"Staining integrity: fluorescence viability {st['fluorescence_viability_pct']}% "
            f"vs label-free {st['impedance_viability_pct']}% "
            f"(delta {st['delta_pct']}); failure suspected="
            f"{st['staining_failure_suspected']}")

    decoupled = clo.get("decoupling_detected", False)
    weights = att["weights"]
    escalate = (weights["clonal_genetic_drift"] > 0.6
                or (cqa["status"] == "FAIL" and max(weights.values()) < 0.6)
                or not rec["affordable"])

    return {
        "run_id": run["run_id"],
        "line_id": run["line_id"],
        "passage_number": run["passage_number"],
        "derived_kinetics": kin,
        "cqa_verdict": {"status": cqa["status"], "failed": cqa["failed"],
                        "values": cqa["values"]},
        "attribution": {"weights": weights, "top_cause": att["top_cause"],
                        "clonal_drift_score": clo.get("clonal_drift_score")},
        "evidence": evidence,
        "assumption_challenged": {
            "flag": bool(decoupled),
            "statement": (
                "Earlier runs attributed falling yield to suboptimal agitation. "
                "Growth and viability are now IMPROVING with passage while "
                "differentiation degrades. A process deviation cannot produce "
                "that decoupling. The agitation hypothesis is not sufficient."
            ) if decoupled else "",
        },
        "confidence": ("high" if max(weights.values()) > 0.8
                       else "moderate" if max(weights.values()) > 0.5 else "low"),
        "recommended_next_action": rec,
        "next_decision_for_planner": (
            "HOLD parameter optimization. Every process-side experiment is "
            "confounded until the clonal hypothesis is resolved. Order the "
            "genomic assay, then re-baseline on an early-passage vial."
            if att["top_cause"] == "clonal_genetic_drift"
            else "Continue optimization; adjust agitation toward the "
                 "stage-specific diameter target."),
        "escalate_to_human": bool(escalate),
    }


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--clean", action="store_true", help="control arm, no variant")
    ap.add_argument("--sensor-fault", action="store_true",
                    help="instrument fault arm: no variant, fouled biomass probe")
    ap.add_argument("--stain-fault", action="store_true",
                    help="reagent fault arm: no variant, failed antibody cartridge")
    a = ap.parse_args()
    faulted = a.sensor_fault or a.stain_fault
    hist = build_history(
        variant_at=None if (a.clean or faulted) else 3,
        sensor_fault_at=5 if faulted else None,
        fault_kind="staining" if a.stain_fault else "capacitance")

    print(f"{'run':>4} {'pass':>5} {'VCD':>6} {'viab%':>6} "
          f"{'harvest':>8} {'purity':>7} {'+-':>5} {'CQA':>9}")
    for r in hist:
        o, c = r["outcome"], check_cqa(r)
        print(f"{r['run_id']:>4} {r['passage_number']:>5} "
              f"{o['final_vcd_e6_per_ml']:>6.2f} {o['final_viability_pct']:>6.1f} "
              f"{o['harvest_yield_per_input_ipsc']:>8.1f} "
              f"{(c['values']['purity_estimate_pct'] or 0):>7.1f} "
              f"{(c['purity'].get('sigma_pct') or 0):>5.1f} "
              f"{c['status']:>9}")

    print("\n--- Analysis Agent output ---")
    print(json.dumps(analyse(hist), indent=2))
