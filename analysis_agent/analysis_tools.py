"""
Tools for the Analysis Agent -- integrated-machine edition.

Every signal below comes from a module that physically exists in the closed
machine. No flow cytometer is required in the loop.

  M1  vessel sensor suite    pH + DO optical patches, capacitance biomass,
                             thermal jacket, gas mixer, impeller torque
  M2  fluidics               peristaltic pumps + gravimetric balances
  M3a metabolite analyser    glucose, lactate, ammonia (enzymatic / Raman)
  M3b in-situ microscope     aggregate diameter, count, morphology
  M3c image cytometer        AO/PI -> viable cell density + viability
  M3d microfluidic impedance cytometer   label-free, continuous, zero reagent
  M3e mini 3-colour cytometer            no-wash surface panel, scheduled
  M4  harvest counter        in-line count + cell-diameter histogram
  M5  ddPCR cartridge        20q11.21 copy number  (the confirmatory test)

Cytometry is now IN the loop, in two tiers:
  M3d runs continuously and free, but is label-free, so it reports a
      biophysical cluster, not an antigen.
  M3e runs on a schedule and costs reagent, and reports real surface markers.
Purity is the inverse-variance FUSION of the two, with the direct measurement's
precision decaying with age. Full offline flow stays available for periodic
re-calibration and release testing only.
"""
from __future__ import annotations

import math
from typing import Dict, List, Optional

import numpy as np

# ---------------------------------------------------------------------------
# Release spec -- only attributes the machine can actually measure
# ---------------------------------------------------------------------------
CQA_SPEC = {
    "final_viability_pct":          {"min": 70.0, "device": "M3c image cytometer"},
    "harvest_yield_per_input_ipsc": {"min": 15.0, "device": "M4 harvest counter"},
    "purity_estimate_pct":          {"min": 85.0, "device": "fused M3d + M3e"},
    "monocyte_diam_mean_um":        {"min": 12.0, "max": 17.0, "device": "M4 harvest counter"},
    "residual_pluripotency_pct":    {"max": 0.50, "device": "M3e TRA-1-60 (surface)"},
}

# Channels that MUST co-move because they share a physical source.
# Breaking one of these means an INSTRUMENT problem, not a biological one.
COHERENT_PAIRS = [
    ("vcd_e6_per_ml", "capacitance_pf_cm", +1, "M3c vs M1 biomass probe"),
    ("vcd_e6_per_ml", "oxygen_uptake_rate", +1, "M3c vs M1 gas balance"),
    ("lactate_mM", "ph", -1, "M3a vs M1 pH patch"),
    ("lactate_mM", "base_addition_ml", +1, "M3a vs M2 base pump"),
    ("viability_pct", "ldh_u_per_l", -1, "M3c vs M3a"),
]


# ---------------------------------------------------------------------------
# 1. Derived kinetics
# ---------------------------------------------------------------------------

def derive_kinetics(run: Dict) -> Dict:
    o = run["observations"]
    t = np.array([x["day"] for x in o]) * 24.0
    x = np.array([x["vcd_e6_per_ml"] for x in o])
    v = np.array([x["viability_pct"] for x in o])
    g = np.array([x["glucose_mM"] for x in o])
    l = np.array([x["lactate_mM"] for x in o])
    d = np.array([x["agg_diameter_mean_um"] for x in o])
    dsd = np.array([x["agg_diameter_sd_um"] for x in o])
    h = np.array([x["harvest_cells_e6_per_ml_day"] for x in o])

    grow = slice(0, max(3, len(t) // 3))
    mu = float(np.polyfit(t[grow], np.log(np.maximum(x[grow], 1e-6)), 1)[0])
    ivcd = float(np.trapezoid(x, t))
    dglc = float(-np.sum(np.diff(g)[np.diff(g) < 0]))
    dlac = float(np.sum(np.diff(l)[np.diff(l) > 0]))
    hp = h[h > 0]

    return {
        "mu_h": round(mu, 5),
        "doubling_time_h": round(math.log(2) / mu, 2) if mu > 0 else None,
        "fold_expansion": round(float(x[-1] / max(x[0], 1e-9)), 3),
        "integral_vcd": round(ivcd, 1),
        "q_glc_pmol_cell_h": round(dglc / max(ivcd, 1e-9), 4),
        "q_lac_pmol_cell_h": round(dlac / max(ivcd, 1e-9), 4),
        "y_lac_per_glc": round(dlac / max(dglc, 1e-9), 3),
        "viability_slope_pct_per_day": round(float(np.polyfit(t / 24, v, 1)[0]), 3),
        "diameter_max_um": round(float(d.max()), 1),
        "diameter_cv_pct": round(float(np.mean(dsd / np.maximum(d, 1e-9)) * 100), 2),
        "frac_time_over_300um": round(float(np.mean(d > 300)), 3),
        "min_ph": round(float(min(y["ph"] for y in o)), 3),
        "harvest_rate_mean_e6_per_ml_day": round(float(hp.mean()), 4) if hp.size else 0.0,
        "harvest_rate_slope": (round(float(np.polyfit(np.arange(hp.size), hp, 1)[0]), 5)
                               if hp.size > 2 else None),
    }


# ---------------------------------------------------------------------------
# 2. SOFT SENSOR -- purity without a flow cytometer
# ---------------------------------------------------------------------------
# Calibrated once against a small set of offline flow runs. Features are all
# in-line. It returns an INTERVAL, never a point estimate, and it flags when
# it is extrapolating outside its calibration range.

# ---- Tier 1: label-free model, M3d impedance + M4 sizing ---------------------
# Electrical diameter tracks cell volume and opacity tracks the membrane, so
# differentiation state is readable with no antibody at all. Calibrated once
# against offline flow.
LABEL_FREE = {
    "intercept": 12.0,
    "coef": {                        # % CD14+ per unit feature
        "imp_frac_monocyte_cluster": 62.0,
        "imp_opacity": 14.0,
        "harvest_cell_diam_mean_um": 0.6,
        "y_lac_per_glc": -3.0,
    },
    "rmse_pct": 5.5,
    "valid_range": {
        "imp_frac_monocyte_cluster": (0.40, 0.99),
        "imp_opacity": (1.10, 1.80),
        "harvest_cell_diam_mean_um": (10.0, 18.0),
        "y_lac_per_glc": (0.5, 2.6),
    },
    "n_calibration_runs": 12,
}

# ---- Tier 2: direct measurement, M3e --------------------------------------
DIRECT = {
    "sigma_pct": 2.2,        # 3-colour no-wash panel, ~10k events
    "ageing_tau_h": 48.0,    # precision decays as the culture moves on
    "panel": ["CD14-PE (identity)", "7-AAD (viability)",
              "TRA-1-60-APC (residual pluripotency)"],
    "constraint": ("Surface markers only. Intracellular OCT4 needs fixation "
                   "and permeabilisation, which cannot be automated in a "
                   "closed single-use flow path."),
}


def estimate_purity_label_free(run: Dict, kinetics: Optional[Dict] = None) -> Dict:
    """Tier 1. Continuous, zero reagent, M3d + M4 + M3a. No antigen measured."""
    o = run["observations"]
    kin = kinetics or derive_kinetics(run)
    tail = [x for x in o if x.get("imp_frac_monocyte_cluster", 0) > 0][-3:]
    if not tail:
        return {"available": False, "reason": "no impedance-cytometry data yet"}
    feats = {
        "imp_frac_monocyte_cluster": float(np.mean([x["imp_frac_monocyte_cluster"] for x in tail])),
        "imp_opacity": float(np.mean([x["imp_opacity"] for x in tail])),
        "harvest_cell_diam_mean_um": float(np.mean([x["harvest_cell_diam_mean_um"] for x in tail])),
        "y_lac_per_glc": float(kin["y_lac_per_glc"]),
    }
    oor = [k for k, (lo, hi) in LABEL_FREE["valid_range"].items()
           if not (lo <= feats[k] <= hi)]
    est = float(np.clip(LABEL_FREE["intercept"]
                        + sum(LABEL_FREE["coef"][k] * feats[k] for k in feats), 0, 100))
    sigma = LABEL_FREE["rmse_pct"] * (2.0 if oor else 1.0)
    return {"available": True, "tier": "label-free (M3d)", "estimate_pct": round(est, 2),
            "sigma_pct": round(sigma, 2), "features": {k: round(v, 4) for k, v in feats.items()},
            "extrapolating": bool(oor), "out_of_calibration_range": oor}


def latest_direct_cytometry(run: Dict) -> Dict:
    """Tier 2. Most recent M3e panel and how old it is."""
    o = run["observations"]
    panels = [(x["day"], x["minicyto"]) for x in o if x.get("minicyto")]
    if not panels:
        return {"available": False, "reason": "no M3e panel acquired in this run"}
    day, p = panels[-1]
    age_h = (o[-1]["day"] - day) * 24.0
    sigma = DIRECT["sigma_pct"] * math.sqrt(1.0 + age_h / DIRECT["ageing_tau_h"])
    return {"available": True, "tier": "direct (M3e)", "day": day, "age_h": round(age_h, 1),
            "cd14_pct": p["CD14_PE_pct"], "viability_7aad_pct": p["viability_7AAD_pct"],
            "residual_pluripotency_pct": p["TRA_1_60_APC_pct"],
            "events_acquired": p["events_acquired"],
            "sigma_pct": round(sigma, 2), "n_panels_this_run": len(panels)}


def fuse_purity(run: Dict, kinetics: Optional[Dict] = None) -> Dict:
    """
    Inverse-variance fusion of the continuous label-free estimate and the
    scheduled direct measurement. The direct value dominates when fresh and
    hands back to the label-free channel as it ages.

    The residual (direct - label-free) is the online calibration offset: it is
    what lets the loop need progressively less cytometry over time.
    """
    lf = estimate_purity_label_free(run, kinetics)
    dr = latest_direct_cytometry(run)
    if not lf.get("available") and not dr.get("available"):
        return {"available": False, "reason": "no cytometry data"}
    if not dr.get("available"):
        est, sig, src = lf["estimate_pct"], lf["sigma_pct"], ["label-free"]
        offset = None
    elif not lf.get("available"):
        est, sig, src = dr["cd14_pct"], dr["sigma_pct"], ["direct"]
        offset = None
    else:
        wl, wd = 1 / lf["sigma_pct"] ** 2, 1 / dr["sigma_pct"] ** 2
        est = (lf["estimate_pct"] * wl + dr["cd14_pct"] * wd) / (wl + wd)
        sig = math.sqrt(1 / (wl + wd))
        src = ["label-free", "direct"]
        offset = round(dr["cd14_pct"] - lf["estimate_pct"], 2)
    ci = 1.96 * sig
    return {"available": True, "purity_estimate_pct": round(float(est), 2),
            "sigma_pct": round(float(sig), 2),
            "ci95": [round(max(0.0, est - ci), 2), round(min(100.0, est + ci), 2)],
            "sources": src, "label_free": lf, "direct": dr,
            "calibration_offset_pct": offset,
            "direct_measurement_age_h": dr.get("age_h"),
            "extrapolating": bool(lf.get("extrapolating")),
            "note": ("Fused estimate. Direct M3e panel is a real surface-antigen "
                     "measurement; the label-free channel is inferred.")}


def check_staining_integrity(run: Dict) -> Dict:
    """
    A bad antibody lot or a failed no-wash step drops EVERY fluorescence channel
    together, viability dye included, while the label-free impedance channel is
    unaffected. That asymmetry identifies the fault unambiguously.
    """
    o = run["observations"]
    panels = [x for x in o if x.get("minicyto")]
    if not panels:
        return {"testable": False, "reason": "no M3e panel in this run"}
    last = panels[-1]
    fluor_viab = last["minicyto"]["viability_7AAD_pct"]
    imp_viab = last.get("imp_viability_pct") or next(
        (x["imp_viability_pct"] for x in reversed(o) if x["imp_viability_pct"] > 0), None)
    if not imp_viab:
        return {"testable": False, "reason": "no impedance viability to compare"}
    delta = fluor_viab - imp_viab
    failed = delta < -12.0
    return {"testable": True, "fluorescence_viability_pct": fluor_viab,
            "impedance_viability_pct": round(imp_viab, 2),
            "delta_pct": round(delta, 2),
            "staining_failure_suspected": bool(failed),
            "interpretation": (
                "All fluorescence channels low while label-free viability is "
                "normal: antibody lot or no-wash step failed. CD14 and TRA-1-60 "
                "from this panel are not usable." if failed
                else "Fluorescence and label-free viability agree; staining intact."),
            "device": "M3e reagent cartridge / no-wash step"}


# ---------------------------------------------------------------------------
# 3. CQA verdict
# ---------------------------------------------------------------------------

def check_cqa(run: Dict) -> Dict:
    o = run["observations"]
    soft = fuse_purity(run)
    stain = check_staining_integrity(run)
    tail = [x for x in o if x["harvest_cell_diam_mean_um"] > 0][-3:]
    direct = soft.get("direct", {}) if soft.get("available") else {}
    # a failed stain invalidates the antigen channels; do not score them
    stain_ok = not stain.get("staining_failure_suspected", False)
    vals = {
        "final_viability_pct": run["outcome"]["final_viability_pct"],
        "harvest_yield_per_input_ipsc": run["outcome"].get("harvest_yield_per_input_ipsc"),
        "purity_estimate_pct": soft.get("purity_estimate_pct"),
        "monocyte_diam_mean_um": (round(float(np.mean(
            [x["harvest_cell_diam_mean_um"] for x in tail])), 2) if tail else None),
        "residual_pluripotency_pct": (direct.get("residual_pluripotency_pct")
                                      if stain_ok else None),
    }
    failed, marginal = [], []
    for k, spec in CQA_SPEC.items():
        val = vals.get(k)
        if val is None:
            continue
        if "min" in spec and val < spec["min"]:
            failed.append({"cqa": k, "value": val, "limit": spec["min"],
                           "dir": "min", "device": spec["device"]})
        elif "min" in spec and val < spec["min"] * 1.1:
            marginal.append(k)
        if "max" in spec and val > spec["max"]:
            failed.append({"cqa": k, "value": val, "limit": spec["max"],
                           "dir": "max", "device": spec["device"]})

    # A soft-sensor CQA whose interval straddles the limit is MARGINAL,
    # never a hard FAIL: the instrument cannot support that call.
    if soft.get("available") and (soft["ci95"][0]
                                  < CQA_SPEC["purity_estimate_pct"]["min"]
                                  < soft["ci95"][1]):
        failed = [f for f in failed if f["cqa"] != "purity_estimate_pct"]
        marginal.append("purity_estimate_pct (CI straddles limit)")

    if not stain_ok:
        marginal.append("antigen CQAs not scored (staining failure)")
    return {
        "status": "FAIL" if failed else ("MARGINAL" if marginal else "PASS"),
        "values": vals, "failed": failed, "marginal": marginal,
        "purity": soft, "staining": stain,
        "offline_confirmation_required": bool(
            soft.get("available") and (soft["extrapolating"] or not stain_ok
                                       or soft.get("sigma_pct", 99) > 5.0)),
    }


# ---------------------------------------------------------------------------
# 4. SENSOR HEALTH -- the fourth cause, new in the machine edition
# ---------------------------------------------------------------------------

def check_sensor_health(run: Dict, history: List[Dict]) -> Dict:
    """
    Instruments drift: optical pH/DO patches photobleach, the dielectric probe
    fouls, the counter picks up a dilution bias. Each has a redundant partner,
    so a fault shows up as a RATIO shift between two channels that share one
    physical source, while the biology-side ratios stay put.
    """
    o = run["observations"]
    x = np.array([y["vcd_e6_per_ml"] for y in o], float)
    cap = np.array([y["capacitance_pf_cm"] for y in o], float)
    our = np.array([y["oxygen_uptake_rate"] for y in o], float)
    lac = np.array([y["lactate_mM"] for y in o], float)
    ph = np.array([y["ph"] for y in o], float)

    ratios = {
        "capacitance_per_vcd": float(np.median(cap / np.maximum(x, 1e-9))),
        "our_per_vcd": float(np.median(our / np.maximum(x, 1e-9))),
        "ph_at_zero_lactate": float(np.median(ph + 0.013 * lac)),
    }

    flags, baseline = [], {}
    if len(history) >= 4:
        # Reference window = the first runs after the last calibration, NOT the
        # whole history. A slow drift would otherwise redefine its own baseline.
        reference = history[:4]
        for key in ratios:
            vals = []
            for r in reference:
                oo = r["observations"]
                xx = np.array([y["vcd_e6_per_ml"] for y in oo], float)
                if key == "capacitance_per_vcd":
                    vv = np.array([y["capacitance_pf_cm"] for y in oo], float) / np.maximum(xx, 1e-9)
                elif key == "our_per_vcd":
                    vv = np.array([y["oxygen_uptake_rate"] for y in oo], float) / np.maximum(xx, 1e-9)
                else:
                    vv = (np.array([y["ph"] for y in oo], float)
                          + 0.013 * np.array([y["lactate_mM"] for y in oo], float))
                vals.append(float(np.median(vv)))
            mu_ = float(np.mean(vals))
            # n=4 underestimates sigma, so use the sample sd with Bessel's
            # correction and require a practical effect size as well as a
            # z-score. One borderline z on a 4-run reference is not a fault.
            sd_ = float(np.std(vals, ddof=1)) or 1e-6
            z = (ratios[key] - mu_) / sd_
            rel = (ratios[key] - mu_) / max(abs(mu_), 1e-9)
            baseline[key] = {"mean": round(mu_, 4), "sd": round(sd_, 4),
                             "z": round(z, 2), "relative_shift": round(rel, 4)}
            if abs(z) > 4.0 and abs(rel) > 0.08:
                flags.append({"ratio": key, "z": round(z, 2),
                              "relative_shift": round(rel, 4),
                              "device": _device_for(key),
                              "interpretation": "instrument drift, not biology"})

    return {"ratios": {k: round(v, 4) for k, v in ratios.items()},
            "baseline": baseline, "flags": flags,
            "reference_window": "runs 1-4 (post-calibration)",
            "sensor_fault_suspected": bool(flags),
            "testable": len(history) >= 4}


def _device_for(key: str) -> str:
    return {"capacitance_per_vcd": "M1 dielectric biomass probe (fouling) "
                                   "or M3c image cytometer (dilution bias)",
            "our_per_vcd": "M1 gas balance / DO patch",
            "ph_at_zero_lactate": "M1 pH optical patch (offset drift)"}[key]


def check_measurement_coherence(run: Dict) -> Dict:
    o = run["observations"]
    out = []
    for a, b, sign, dev in COHERENT_PAIRS:
        xa = np.array([x[a] for x in o], float)
        xb = np.array([x[b] for x in o], float)
        if xa.std() < 1e-9 or xb.std() < 1e-9 or len(xa) < 4:
            continue
        r = float(np.corrcoef(xa, xb)[0, 1])
        out.append({"pair": f"{a}~{b}", "devices": dev, "r": round(r, 3),
                    "coherent": bool((r * sign) > 0.3)})
    n_bad = sum(1 for c in out if not c["coherent"])
    return {"checks": out, "incoherent_count": n_bad,
            "measurement_artifact_suspected": n_bad >= 2}


# ---------------------------------------------------------------------------
# 5. Mass balance -- M2 balances vs M3a analyser
# ---------------------------------------------------------------------------

def reconcile_mass_balance(run: Dict, kinetics: Optional[Dict] = None) -> Dict:
    """
    Carbon consistency check across M2 (feed pumps + load cells), M3a (metabolite
    analyser) and M1 (gas balance).

    Daily sampling cannot see the feed transient, so do NOT try to reconcile the
    feed volume directly. Instead use stoichiometry, which is sampling-rate
    independent: glycolytic culture produces 1.0-2.2 mol lactate per mol glucose,
    and oxygen uptake must track biomass. Values outside those windows mean a
    pump, a line or an analyser is wrong -- not the cells.
    """
    kin = kinetics or derive_kinetics(run)
    o = run["observations"]
    y = float(kin["y_lac_per_glc"])
    x = np.array([v["vcd_e6_per_ml"] for v in o], float)
    our = np.array([v["oxygen_uptake_rate"] for v in o], float)
    our_per_cell = float(np.median(our / np.maximum(x, 1e-9)))

    y_ok = 0.8 <= y <= 2.4
    our_ok = 0.25 <= our_per_cell <= 0.70
    open_reasons = []
    if not y_ok:
        open_reasons.append(f"lactate/glucose yield {y} outside 0.8-2.4 "
                            "(M3a analyser or M2 feed pump)")
    if not our_ok:
        open_reasons.append(f"oxygen uptake per cell {our_per_cell:.3f} outside "
                            "0.25-0.70 (M1 gas balance or M3c counter)")
    return {"y_lac_per_glc": round(y, 3),
            "our_per_vcd": round(our_per_cell, 4),
            "balance_closed": bool(y_ok and our_ok),
            "open_reasons": open_reasons,
            "device_if_open": "M2 feed pump / load cell, M3a analyser, or M1 gas balance"}


# ---------------------------------------------------------------------------
# 6. Process attribution
# ---------------------------------------------------------------------------

PARAMS = ["seed_density", "agitation_rpm", "do_setpoint", "feed_fraction",
          "bmp4", "vegf", "mcsf", "il3"]


def _design_matrix(runs: List[Dict]) -> np.ndarray:
    return np.array([[r["setpoints"][p] for p in PARAMS] for r in runs], float)


def _std_design(runs: List[Dict]) -> np.ndarray:
    X = _design_matrix(runs)
    sd = X.std(0)
    X = (X - X.mean(0)) / np.where(sd < 1e-12, 1.0, sd)
    return np.c_[np.ones(len(X)), X]


def process_residual(history: List[Dict],
                     target: str = "harvest_yield_per_input_ipsc") -> Dict:
    if len(history) < 4:
        return {"fitted": False, "reason": "need >=4 runs"}
    y = np.array([r["outcome"][target] for r in history], float)
    X = _std_design(history)
    beta, *_ = np.linalg.lstsq(X[:-1], y[:-1], rcond=None)
    pred = float(X[-1] @ beta)
    resid = float(y[-1] - pred)
    sigma = float(np.std(y[:-1] - X[:-1] @ beta)) or 1e-6
    return {"fitted": True, "target": target, "observed": round(float(y[-1]), 4),
            "predicted_from_setpoints": round(pred, 4),
            "residual": round(resid, 4), "residual_z": round(resid / sigma, 2),
            "setpoints_explain_change": bool(abs(resid / sigma) < 2.0)}


# ---------------------------------------------------------------------------
# 7. Clonal / genetic drift -- works WITHOUT a flow cytometer
# ---------------------------------------------------------------------------
# Growth side  : M3c image cytometer (VCD, viability)
# Product side : M4 harvest counter (yield, gate fraction)
# Both are in-line, so the discriminating test survives removing flow.

GROWTH_METRICS = ["final_vcd_e6_per_ml", "final_viability_pct"]
DIFF_METRICS = ["harvest_yield_per_input_ipsc",
                "cumulative_differentiation_efficiency"]


def _spearman(a: np.ndarray, b: np.ndarray) -> float:
    ra, rb = a.argsort().argsort().astype(float), b.argsort().argsort().astype(float)
    if ra.std() < 1e-12 or rb.std() < 1e-12:
        return 0.0
    return float(np.corrcoef(ra, rb)[0, 1])


def _residual_series(history: List[Dict], metric: str) -> np.ndarray:
    y = np.array([r["outcome"][metric] for r in history], float)
    X = _std_design(history)
    if len(y) <= X.shape[1]:
        return y - y.mean()
    beta, *_ = np.linalg.lstsq(X, y, rcond=None)
    return y - X @ beta


def detect_clonal_drift(history: List[Dict]) -> Dict:
    if len(history) < 4:
        return {"testable": False, "reason": "need >=4 runs on the same line"}

    p = np.array([r["passage_number"] for r in history], float)
    growth = {m: _spearman(p, _residual_series(history, m)) for m in GROWTH_METRICS}
    diff = {m: _spearman(p, _residual_series(history, m)) for m in DIFF_METRICS}
    g_mean = float(np.mean(list(growth.values())))
    d_mean = float(np.mean(list(diff.values())))
    decoupled = (g_mean > 0.35) and (d_mean < -0.5)

    resid = process_residual(history)
    unexplained = bool(resid.get("fitted") and not resid["setpoints_explain_change"])

    k = max(2, len(history) // 3)
    early = np.mean([r["outcome"]["harvest_yield_per_input_ipsc"] for r in history[:k]])
    late = np.mean([r["outcome"]["harvest_yield_per_input_ipsc"] for r in history[-k:]])
    drop = float(1 - late / max(early, 1e-9))

    score = (0.45 * decoupled + 0.20 * unexplained
             + 0.20 * (abs(d_mean) > 0.7) + 0.15 * (drop > 0.25))
    return {"testable": True,
            "growth_vs_passage_rho": {k_: round(v, 3) for k_, v in growth.items()},
            "differentiation_vs_passage_rho": {k_: round(v, 3) for k_, v in diff.items()},
            "decoupling_detected": bool(decoupled),
            "unexplained_by_setpoints": unexplained,
            "yield_drop_early_to_late": round(drop, 3),
            "clonal_drift_score": round(float(score), 3),
            "verdict": ("LIKELY_CLONAL" if score >= 0.6
                        else "POSSIBLE_CLONAL" if score >= 0.35 else "NO_EVIDENCE"),
            "measured_by": "M3c image cytometer (growth) + M4 harvest counter (product)"}


# ---------------------------------------------------------------------------
# 8. Four-way attribution
# ---------------------------------------------------------------------------

CAUSES = ["process_parameters", "sensor_fault", "measurement_artifact",
          "clonal_genetic_drift"]


def attribute_cause(run: Dict, history: List[Dict]) -> Dict:
    sens = check_sensor_health(run, history)
    coh = check_measurement_coherence(run)
    mb = reconcile_mass_balance(run)
    stain = check_staining_integrity(run)
    res = process_residual(history)
    clo = detect_clonal_drift(history)

    w = dict.fromkeys(CAUSES, 0.25)
    if stain.get("staining_failure_suspected"):
        w = {"process_parameters": 0.06, "sensor_fault": 0.80,
             "measurement_artifact": 0.09, "clonal_genetic_drift": 0.05}
    elif sens["sensor_fault_suspected"] or not mb["balance_closed"]:
        # instrument faults are checked FIRST: a bad sensor makes every
        # downstream inference wrong, including the clonal test
        w = {"process_parameters": 0.10, "sensor_fault": 0.70,
             "measurement_artifact": 0.12, "clonal_genetic_drift": 0.08}
    elif coh["measurement_artifact_suspected"]:
        w = {"process_parameters": 0.12, "sensor_fault": 0.18,
             "measurement_artifact": 0.62, "clonal_genetic_drift": 0.08}
    elif clo.get("verdict") == "LIKELY_CLONAL":
        w = {"process_parameters": 0.08, "sensor_fault": 0.04,
             "measurement_artifact": 0.03, "clonal_genetic_drift": 0.85}
    elif clo.get("verdict") == "POSSIBLE_CLONAL":
        w = {"process_parameters": 0.32, "sensor_fault": 0.06,
             "measurement_artifact": 0.07, "clonal_genetic_drift": 0.55}
    elif res.get("fitted") and res["setpoints_explain_change"]:
        w = {"process_parameters": 0.72, "sensor_fault": 0.08,
             "measurement_artifact": 0.08, "clonal_genetic_drift": 0.12}

    return {"weights": w, "top_cause": max(w, key=w.get), "sensor_health": sens,
            "coherence": coh, "mass_balance": mb, "staining": stain,
            "process_fit": res, "clonal": clo}


# ---------------------------------------------------------------------------
# 9. What to do next -- a device action, not just an assay
# ---------------------------------------------------------------------------

ACTIONS = {
    "karyotype_ddpcr": (25.0, "M5 ddPCR cartridge"),
    "recalibrate_sensor": (2.0, "M1 patches / M3c counter -- 2-point recalibration"),
    "replace_reagent_cartridge": (2.0, "M3e antibody cartridge + re-acquire panel"),
    "repeat_measurement": (1.0, "M3a + M3c re-sample"),
    "acquire_direct_panel": (2.0, "M3e 3-colour no-wash panel (unscheduled)"),
    "offline_flow_confirmation": (8.0, "OFFLINE flow -- re-fits the label-free model"),
}


def recommend_next_action(attribution: Dict, cqa: Dict,
                          budget_remaining: float) -> Dict:
    top = attribution["top_cause"]
    if attribution.get("staining", {}).get("staining_failure_suspected"):
        a, why, gain = ("replace_reagent_cartridge",
                        "Every fluorescence channel is low while label-free "
                        "viability is normal. The antigen CQAs from this panel "
                        "are void until the cartridge is replaced.", 0.75)
    elif top == "sensor_fault":
        a, why, gain = ("recalibrate_sensor",
                        "Every downstream inference is suspect until the probe "
                        "is recalibrated. Cheapest and highest-value step.", 0.80)
    elif top == "measurement_artifact":
        a, why, gain = ("repeat_measurement",
                        "Re-sample before acting on an incoherent signal.", 0.45)
    elif top == "clonal_genetic_drift":
        a, why, gain = ("karyotype_ddpcr",
                        "Only the genomic assay separates clonal takeover from a "
                        "process effect. Until it runs, every process-side "
                        "experiment is confounded.", 0.85)
    elif (cqa.get("purity", {}).get("sigma_pct", 0) > 4.0
          or (cqa.get("purity", {}).get("direct_measurement_age_h") or 0) > 72):
        a, why, gain = ("acquire_direct_panel",
                        "The purity estimate is leaning on the label-free "
                        "channel. One M3e panel at cost 2 tightens the interval "
                        "and re-sets the calibration offset.", 0.65)
    elif cqa.get("offline_confirmation_required"):
        a, why, gain = ("offline_flow_confirmation",
                        "Label-free model is extrapolating; one offline run "
                        "both confirms and re-fits it.", 0.60)
    else:
        a, why, gain = ("acquire_direct_panel",
                        "Routine confirmation of the fused estimate.", 0.35)
    cost, device = ACTIONS[a]
    return {"action": a, "device": device, "cost": cost, "rationale": why,
            "expected_information_gain": gain,
            "affordable": bool(cost <= budget_remaining),
            "gain_per_cost": round(gain / cost, 4)}
