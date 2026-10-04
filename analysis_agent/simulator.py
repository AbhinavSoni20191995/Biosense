"""
iPSC -> macrophage bioreactor simulator.

Design rule: every observable is DERIVED, never sampled independently.
The dependency chain is:

  L0  setpoints (agent-controlled)
  L1  hidden line state (line genetics, clonal composition)   <- never observable
  L2  hydrodynamics + mass transfer (shear, kLa, aggregate size)
  L3  biology (growth, death, metabolism, stage transitions)
  L4  observables (online / at-line / imaging / flow / function), each with
      its own noise model, cost and latency

This is a mechanistic model calibrated to published protocol ranges.
It is NOT a validated digital twin. Swap `run_stage` for hardware later.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional

import numpy as np

# ----------------------------------------------------------------------------
# L0  SETPOINTS -- what the planner agent is allowed to choose
# ----------------------------------------------------------------------------

@dataclass
class Setpoints:
    seed_density: float = 0.5        # 1e6 cells/mL
    agitation_rpm: float = 60.0      # rpm
    do_setpoint: float = 0.20        # fraction (0.05-0.40)
    feed_fraction: float = 0.5       # fraction of volume exchanged per feed
    feed_interval_h: float = 24.0    # h
    rocki_hours: float = 24.0        # ROCK inhibitor exposure
    passage_trigger: str = "day"     # "day" | "diameter"
    passage_day: float = 4.0
    passage_diameter_um: float = 250.0
    bmp4: float = 25.0               # ng/mL, mesoderm stage
    vegf: float = 50.0               # ng/mL, mesoderm stage
    mcsf: float = 50.0               # ng/mL, myeloid stage
    il3: float = 25.0                # ng/mL, myeloid stage


# ----------------------------------------------------------------------------
# L1  HIDDEN LINE STATE -- the agent can never read this directly
# ----------------------------------------------------------------------------

@dataclass
class SensorState:
    """Instrument faults. Separate from biology on purpose: the agent has to
    tell a drifting probe apart from a changing culture."""
    ph_offset: float = 0.0            # pH units, optical patch drift
    do_patch_gain: float = 1.0        # photobleaching of the DO spot
    capacitance_fouling: float = 1.0  # biofilm on the dielectric probe
    counter_bias: float = 1.0         # image-cytometer dilution bias
    stain_efficiency: float = 1.0     # antibody lot / no-wash staining failure


@dataclass
class LineState:
    """Per-iPSC-line biology. `variant_fraction` is the clonal takeover."""
    line_id: str = "L01"
    mu_max: float = 0.030            # 1/h, wild-type
    death_base: float = 0.0035       # 1/h
    q_glc: float = 0.35e-10          # mmol/cell/h
    y_lac_glc: float = 1.7           # mol lactate per mol glucose (glycolytic)
    opt_shift: Dict[str, float] = field(default_factory=dict)  # per-stage optima offsets
    passage_number: int = 20

    # --- engineered genotype (WT vs knockout etc.); defaults = wild type ---
    # Cytokine optima (ng/mL) for the stage-efficiency bells. A knockout that
    # changes a factor requirement shifts its optimum, so the "right" dose for
    # a KO arm differs from WT. Hidden from agents like the clonal variant.
    factor_optima: Dict[str, float] = field(default_factory=lambda: {
        "bmp4": 25.0, "vegf": 50.0, "mcsf": 50.0, "il3": 25.0})
    genotype_mu_ratio: float = 1.0      # growth effect of the engineered edit
    genotype_diff_ratio: float = 1.0    # differentiation effect of the edit

    # --- culture-adaptation variant (modelled on 20q11.21 gain / BCL2L1) ---
    variant_fraction: float = 0.0    # fraction of cells carrying the gain
    variant_death_ratio: float = 0.55   # anti-apoptotic: lower death
    variant_mu_ratio: float = 1.04      # mild proliferative edge
    variant_diff_ratio: float = 0.45    # IMPAIRED differentiation
    variant_selection_s: float = 0.90   # per-passage selection coefficient

    @staticmethod
    def sample(rng: np.random.Generator, line_id: str) -> "LineState":
        return LineState(
            line_id=line_id,
            mu_max=0.030 * rng.normal(1.0, 0.10),
            death_base=0.0035 * rng.normal(1.0, 0.15),
            q_glc=0.35e-10 * rng.normal(1.0, 0.10),
            opt_shift={
                "expansion": rng.normal(0, 0.08),
                "mesoderm": rng.normal(0, 0.10),
                "hemato": rng.normal(0, 0.10),
                "myeloid": rng.normal(0, 0.10),
            },
        )

    def advance_passage(self, rng: np.random.Generator) -> None:
        """Clonal selection: once present, the variant takes over the culture."""
        self.passage_number += 1
        if self.variant_fraction > 0:
            f = self.variant_fraction
            f = f * (1 + self.variant_selection_s) / (1 + f * self.variant_selection_s)
            self.variant_fraction = float(np.clip(f * rng.normal(1.0, 0.05), 0, 1))


# ----------------------------------------------------------------------------
# L2  HYDRODYNAMICS AND MASS TRANSFER
# ----------------------------------------------------------------------------

def shear_stress(rpm: float) -> float:
    """dyne/cm^2. Anchored to ~6e-2 dyne/cm^2 at 75 rpm (CSTR, Nature 2019)."""
    return 6.0e-2 * (rpm / 75.0) ** 1.6


def kla(rpm: float) -> float:
    """1/h volumetric oxygen transfer coefficient, rises with agitation."""
    return 4.0 * (rpm / 60.0) ** 1.4


def aggregate_dynamics(d_um: float, mu_eff: float, rpm: float, dt_h: float,
                       rng: np.random.Generator) -> float:
    """
    Aggregate diameter = growth by cell division MINUS breakage by shear.
    Breakage scales with D^2 (bigger aggregates are more fragile), so the
    balance gives a stable equilibrium diameter set by rpm:
        dD/dt = D*mu/3  -  k_break * tau * D^2 / D_REF
    """
    tau = shear_stress(rpm)
    k_break, D_REF = 0.242, 300.0
    dD = d_um * mu_eff / 3.0 - k_break * tau * d_um * d_um / D_REF
    return max(40.0, d_um + dD * dt_h + rng.normal(0, 0.4) * math.sqrt(dt_h))


def necrotic_fraction(d_um: float, do: float) -> float:
    """
    Oxygen penetration depth sets a viable shell. >300 um cores go hypoxic
    and necrotic (Kim 2021 / Jeske 2019). Penetration scales with sqrt(DO).
    """
    pen = 110.0 * math.sqrt(max(do, 1e-3) / 0.20)        # um of viable shell
    r = d_um / 2.0
    if r <= pen:
        return 0.0
    core = (r - pen) / r
    return float(np.clip(core ** 2, 0.0, 0.92))


# ----------------------------------------------------------------------------
# L3  BIOLOGY
# ----------------------------------------------------------------------------

KS_GLC = 0.4        # mM, Monod half-saturation
LAC_INH = 22.0      # mM, half-inhibition
AMM_INH = 4.5       # mM, half-inhibition
X_MAX = 6.0         # 1e6 cells/mL, density ceiling of the vessel
FEED_GLC = 20.0     # mM glucose in fresh medium

# Stage-specific optima. Note the diameter optimum CHANGES per stage:
# expansion wants <300 um; myeloid priming wants large 300-500 um aggregates.
STAGE_OPTIMA = {
    "expansion": dict(d_um=150.0, d_tol=65.0, do=0.20, do_tol=0.12),
    "mesoderm":  dict(d_um=200.0, d_tol=80.0, do=0.10, do_tol=0.07),
    "hemato":    dict(d_um=300.0, d_tol=110.0, do=0.12, do_tol=0.08),
    "myeloid":   dict(d_um=400.0, d_tol=140.0, do=0.18, do_tol=0.10),
}


def _bell(x: float, opt: float, tol: float) -> float:
    return float(math.exp(-0.5 * ((x - opt) / tol) ** 2))


def _inhib(c: float, k: float) -> float:
    return float(k / (k + max(c, 0.0)))


# ----------------------------------------------------------------------------
# L4  OBSERVABLES -- noise, cost and latency live here
# ----------------------------------------------------------------------------

# Each entry maps to ONE module of the integrated machine.
ASSAYS = {
    # name:              (cost, latency_h, CV, device module)
    "online":            (0.0, 0.0, 0.02, "M1 vessel sensor suite"),
    "metabolite":        (1.0, 0.25, 0.05, "M3a metabolite analyser"),
    "insitu_imaging":    (0.0, 0.0, 0.08, "M3b in-situ microscope"),
    "image_cytometer":   (1.0, 0.3, 0.06, "M3c image cytometer (AO/PI)"),
    "harvest_counter":   (0.0, 0.0, 0.07, "M4 in-line harvest counter"),
    "impedance_cyto":    (0.0, 0.05, 0.07, "M3d microfluidic impedance cytometer"),
    "mini_cytometer":    (2.0, 0.5, 0.04, "M3e 3-colour no-wash cytometer"),
    "flow":              (8.0, 24.0, 0.12, "OFFLINE - not in the machine"),
    "function":          (15.0, 48.0, 0.18, "OFFLINE - not in the machine"),
    "karyotype_ddpcr":   (25.0, 72.0, 0.05, "M5 ddPCR cartridge"),
}


@dataclass
class Observation:
    stage: str
    day: float
    # --- online (continuous, cheap) ---
    ph: float = 0.0
    do_measured: float = 0.0
    agitation_rpm: float = 0.0
    temperature_c: float = 37.0
    capacitance_pf_cm: float = 0.0
    oxygen_uptake_rate: float = 0.0
    base_addition_ml: float = 0.0
    # --- at-line (daily) ---
    vcd_e6_per_ml: float = 0.0
    viability_pct: float = 0.0
    glucose_mM: float = 0.0
    lactate_mM: float = 0.0
    ammonia_mM: float = 0.0
    osmolality_mOsm: float = 0.0
    ldh_u_per_l: float = 0.0
    # --- M4 harvest stream (continuous, free) ---
    harvest_cells_e6_per_ml_day: float = 0.0
    harvest_cum_e6_per_ml: float = 0.0
    harvest_cell_diam_mean_um: float = 0.0
    harvest_frac_in_monocyte_gate: float = 0.0
    # --- M3d impedance cytometer (label-free, continuous, free) ---
    imp_electrical_diameter_um: float = 0.0
    imp_opacity: float = 0.0              # |Z|(2 MHz)/|Z|(0.5 MHz); membrane state
    imp_frac_monocyte_cluster: float = 0.0
    imp_viability_pct: float = 0.0         # label-free: lysed cells lose low-f signal
    # --- M3e mini fluorescence cytometer (at-line, scheduled) ---
    minicyto: Optional[Dict[str, float]] = None
    # --- imaging (daily) ---
    agg_diameter_mean_um: float = 0.0
    agg_diameter_sd_um: float = 0.0
    agg_frac_over_300um: float = 0.0
    agg_count_per_ml: float = 0.0
    # --- flow (milestone only; None unless purchased) ---
    flow: Optional[Dict[str, float]] = None
    # --- function (terminal only) ---
    function: Optional[Dict[str, float]] = None
    # --- genetic assay (only when purchased) ---
    genomic: Optional[Dict[str, float]] = None
    # --- bookkeeping ---
    assay_cost: float = 0.0


@dataclass
class RunRecord:
    run_id: str
    line_id: str
    passage_number: int
    setpoints: Dict
    observations: List[Dict]
    outcome: Dict
    total_assay_cost: float
    ground_truth: Dict          # withheld from agents; for scoring only


# ----------------------------------------------------------------------------
# THE REACTOR
# ----------------------------------------------------------------------------

class Bioreactor:
    """One differentiation run: expansion -> mesoderm -> hemato -> myeloid."""

    STAGES = [("expansion", 4.0), ("mesoderm", 3.0), ("hemato", 4.0), ("myeloid", 14.0)]

    def __init__(self, line: LineState, seed: int = 0,
                 sensors: Optional[SensorState] = None):
        self.line = line
        self.sensors = sensors or SensorState()
        self.rng = np.random.default_rng(seed)

    # -- helper: population-weighted trait given clonal mix ------------------
    def _mix(self, wt: float, ratio: float) -> float:
        f = self.line.variant_fraction
        return wt * (1 - f) + wt * ratio * f

    def run(self, sp: Setpoints, run_id: str,
            flow_at_stage_end: bool = False,
            buy_genomic: bool = False,
            minicyto_every_days: int = 4,
            stage_days: Optional[Dict[str, float]] = None) -> RunRecord:
        """stage_days overrides the default stage durations (days) by stage name."""
        rng = self.rng
        L = self.line

        # ---- initial state -------------------------------------------------
        X = sp.seed_density                 # 1e6 cells/mL viable
        viab = 0.95
        glc, lac, amm = 17.5, 0.5, 0.2
        d_um = 60.0
        d_sd = 12.0
        day = 0.0
        obs: List[Dict] = []
        cost = 0.0
        harvest_cum = 0.0
        S = self.sensors
        stage_yield = {}
        carried = 1.0                        # cumulative transition efficiency

        dt = 2.0                             # h
        stages = [(s, float((stage_days or {}).get(s, d))) for s, d in self.STAGES]
        for stage, dur_d in stages:
            opt = STAGE_OPTIMA[stage]
            d_opt = opt["d_um"] * (1 + L.opt_shift.get(stage, 0.0))
            do_opt = opt["do"] * (1 + L.opt_shift.get(stage, 0.0))
            steps = int(dur_d * 24 / dt)
            eff_acc, eff_n = 0.0, 0

            for i in range(steps):
                # ---- L2: mass transfer ------------------------------------
                our = 0.42 * X                                  # mmol/L/h
                do = sp.do_setpoint - our / max(kla(sp.agitation_rpm), 1e-6) * 0.05
                do = float(np.clip(do, 0.01, 0.45))
                nec = necrotic_fraction(d_um, do)

                # ---- L3: growth -------------------------------------------
                mu_max = self._mix(L.mu_max, L.variant_mu_ratio) * L.genotype_mu_ratio
                rocki = 1.0 if day * 24 < sp.rocki_hours else 0.85 if day < 0.5 else 1.0
                mu = (mu_max
                      * (glc / (KS_GLC + glc))
                      * _inhib(lac, LAC_INH)
                      * _inhib(amm, AMM_INH)
                      * _bell(do, do_opt, opt["do_tol"])
                      * (1 - nec)
                      * max(0.0, 1 - X / X_MAX)
                      * rocki)

                tau = shear_stress(sp.agitation_rpm)
                # BCL-xL-type variant is anti-apoptotic under ALL stress,
                # so the protection applies to the whole death term
                death = (L.death_base
                         + 0.055 * max(tau - 0.07, 0.0) / 0.07
                         + 0.090 * nec
                         + 0.030 * max(amm - 3.0, 0.0))
                death = self._mix(death, L.variant_death_ratio)

                X += (mu - death) * X * dt
                X = max(X, 1e-4)
                target_v = float(np.clip(1.0 - 25.0 * death, 0.05, 0.985))
                viab = float(np.clip(viab + 0.08 * (target_v - viab) * dt, 0.05, 0.985))

                # ---- L3: metabolism ---------------------------------------
                dglc = -L.q_glc * X * 1e6 * 1000 * dt
                glc = max(glc + dglc, 0.0)
                lac += -dglc * L.y_lac_glc
                amm += 0.11 * (-dglc) * 0.25
                if (day * 24) % sp.feed_interval_h < dt:
                    glc = glc * (1 - sp.feed_fraction) + FEED_GLC * sp.feed_fraction
                    lac *= (1 - sp.feed_fraction)
                    amm *= (1 - sp.feed_fraction)

                # ---- L2: aggregate size -----------------------------------
                # aggregates grow by division INSIDE the aggregate, so they are
                # not limited by the vessel density ceiling
                mu_agg = mu_max * (glc / (KS_GLC + glc)) * (1 - nec) * rocki
                d_um = aggregate_dynamics(d_um, mu_agg, sp.agitation_rpm, dt, rng)
                d_sd = 0.18 * d_um + 6.0

                # ---- L3: stage transition efficiency ----------------------
                cyto = 1.0
                if stage == "mesoderm":
                    cyto = (_bell(sp.bmp4, L.factor_optima["bmp4"], 12.0)
                            * _bell(sp.vegf, L.factor_optima["vegf"], 25.0))
                elif stage == "myeloid":
                    cyto = (_bell(sp.mcsf, L.factor_optima["mcsf"], 25.0)
                            * _bell(sp.il3, L.factor_optima["il3"], 15.0))
                eff = (_bell(d_um, d_opt, opt["d_tol"])
                       * _bell(do, do_opt, opt["do_tol"])
                       * _inhib(lac, LAC_INH * 1.5)
                       * cyto * viab)
                # the variant's signature: differentiation is impaired,
                # growth and survival are NOT
                eff *= self._mix(1.0, L.variant_diff_ratio) * L.genotype_diff_ratio
                eff_acc += eff
                eff_n += 1

                # ---- M4: continuous monocyte release (myeloid stage only) --
                if stage == "myeloid":
                    release = 0.028 * eff * X * dt       # 1e6 cells/mL per step
                    harvest_cum += release
                else:
                    release = 0.0

                day += dt / 24.0

                # ---- L4: daily observation --------------------------------
                if abs((day * 24) % 24) < dt:
                    o = Observation(stage=stage, day=round(day, 2))
                    n = lambda v, cv: float(v * rng.normal(1.0, cv))
                    o.ph = round(n(7.35 - 0.013 * lac + S.ph_offset, 0.004), 3)
                    o.do_measured = round(n(do * S.do_patch_gain, 0.03), 4)
                    o.agitation_rpm = sp.agitation_rpm
                    o.capacitance_pf_cm = round(n(X * 1.9 * S.capacitance_fouling, 0.03), 3)
                    o.oxygen_uptake_rate = round(n(our, 0.04), 3)
                    o.base_addition_ml = round(max(0.0, n(0.05 * lac, 0.08)), 3)
                    o.vcd_e6_per_ml = round(n(X * S.counter_bias, 0.06), 4)
                    o.viability_pct = round(min(99.0, n(viab * 100, 0.03)), 2)
                    o.glucose_mM = round(max(0.0, n(glc, 0.05)), 3)
                    o.lactate_mM = round(n(lac, 0.05), 3)
                    o.ammonia_mM = round(n(amm, 0.07), 3)
                    o.osmolality_mOsm = round(n(285 + 2.1 * lac, 0.01), 1)
                    o.ldh_u_per_l = round(n(120 + 900 * death, 0.10), 1)
                    # M4 harvest-stream channels
                    o.harvest_cells_e6_per_ml_day = round(n(release * 12, 0.07), 4)
                    o.harvest_cum_e6_per_ml = round(n(harvest_cum, 0.05), 4)
                    gate = float(np.clip(0.52 + 0.85 * mean_eff_running(eff_acc, eff_n),
                                         0, 0.99))
                    o.harvest_cell_diam_mean_um = round(n(11.0 + 4.5 * gate, 0.05), 2)
                    o.harvest_frac_in_monocyte_gate = round(n(gate, 0.06), 4)
                    # M3d: label-free impedance cytometry on the harvest stream.
                    # Electrical diameter tracks cell volume; opacity tracks the
                    # membrane, so differentiation state is readable without dyes.
                    if gate > 0:
                        o.imp_electrical_diameter_um = round(n(10.6 + 4.7 * gate, 0.05), 2)
                        o.imp_opacity = round(n(1.28 + 0.42 * gate, 0.06), 3)
                        o.imp_frac_monocyte_cluster = round(n(gate * 0.97, 0.07), 4)
                        o.imp_viability_pct = round(min(99.0, n(viab * 100, 0.035)), 2)
                    o.agg_diameter_mean_um = round(n(d_um, 0.05), 1)
                    o.agg_diameter_sd_um = round(n(d_sd, 0.08), 1)
                    o.agg_frac_over_300um = round(float(np.clip(
                        1 - 0.5 * (1 + math.erf((300 - d_um) / (d_sd * math.sqrt(2)))), 0, 1)), 3)
                    cells_per_agg = max((d_um / 15.0) ** 3, 1.0)
                    o.agg_count_per_ml = round(n(X * 1e6 / cells_per_agg, 0.09), 1)
                    # M3e: scheduled 3-colour panel. Surface markers ONLY --
                    # intracellular OCT4 needs fix/perm, which cannot be
                    # automated in a closed loop, so TRA-1-60 is the
                    # pluripotency marker instead.
                    if stage == "myeloid" and int(round(day)) % minicyto_every_days == 0:
                        st = S.stain_efficiency
                        o.minicyto = {
                            "CD14_PE_pct": round(float(np.clip(
                                n((0.55 + 0.90 * mean_eff_running(eff_acc, eff_n))
                                  * 100 * st, 0.04), 0, 100)), 2),
                            "TRA_1_60_APC_pct": round(float(max(0.0, n(
                                0.09 * st, 0.25))), 3),
                            "viability_7AAD_pct": round(float(np.clip(
                                n(viab * 100 * st, 0.03), 0, 99)), 2),
                            "events_acquired": int(n(9500, 0.08)),
                        }
                        o.assay_cost += ASSAYS["mini_cytometer"][0]
                        cost += ASSAYS["mini_cytometer"][0]
                    o.assay_cost = o.assay_cost + (ASSAYS["metabolite"][0]
                                    + ASSAYS["image_cytometer"][0]
                                    + ASSAYS["insitu_imaging"][0]
                                    + ASSAYS["harvest_counter"][0]
                                    + ASSAYS["impedance_cyto"][0])
                    cost += (ASSAYS["metabolite"][0] + ASSAYS["image_cytometer"][0]
                             + ASSAYS["insitu_imaging"][0] + ASSAYS["harvest_counter"][0]
                             + ASSAYS["impedance_cyto"][0])
                    obs.append(asdict(o))

            mean_eff = eff_acc / max(eff_n, 1)
            carried *= mean_eff
            stage_yield[stage] = round(mean_eff, 4)

            # ---- L4: milestone flow panel -----------------------------------
            if flow_at_stage_end and obs:
                cv = ASSAYS["flow"][2]
                # marker purity tracks THIS stage's transition efficiency, not the
                # cumulative yield (a low-yield run can still be pure)
                purity = float(np.clip((0.55 + 0.90 * mean_eff) * rng.normal(1.0, cv), 0, 1))
                panel = {
                    "expansion": {"OCT4_pct": round(96 * purity + 2, 2),
                                  "SSEA4_pct": round(95 * purity + 2, 2)},
                    "mesoderm":  {"KDR_pct": round(70 * purity, 2),
                                  "CD56_pct": round(55 * purity, 2)},
                    "hemato":    {"CD34_pct": round(62 * purity, 2),
                                  "CD43_pct": round(48 * purity, 2)},
                    "myeloid":   {"CD14_pct": round(88 * purity, 2),
                                  "CD11b_pct": round(90 * purity, 2),
                                  "residual_OCT4_pct": round(
                                      max(0.0, rng.normal(0.08, 0.05)), 3)},
                }[stage]
                panel["live_pct"] = round(min(99.0, viab * 100 * rng.normal(1, cv / 2)), 2)
                obs[-1]["flow"] = panel
                obs[-1]["assay_cost"] += ASSAYS["flow"][0]
                cost += ASSAYS["flow"][0]

            if stage == "expansion":
                L.advance_passage(rng)

        # ---- terminal functional assay ---------------------------------------
        cv = ASSAYS["function"][2]
        func = {
            "phagocytosis_index": round(float(np.clip(
                0.72 * self._mix(1.0, 0.85) * rng.normal(1, cv), 0, 1)), 3),
            "tnfa_pg_per_ml_lps": round(float(1850 * self._mix(1.0, 0.9) * rng.normal(1, cv)), 1),
            "il10_pg_per_ml_il4": round(float(420 * rng.normal(1, cv)), 1),
        }
        obs[-1]["function"] = func
        obs[-1]["assay_cost"] += ASSAYS["function"][0]
        cost += ASSAYS["function"][0]

        # ---- optional genomic assay (the confirmatory test) -------------------
        if buy_genomic:
            obs[-1]["genomic"] = {
                "cnv_20q11_21_copies": round(float(
                    2 + 1.0 * L.variant_fraction * rng.normal(1, 0.05)), 3),
                "variant_allele_fraction": round(float(
                    np.clip(L.variant_fraction * rng.normal(1, 0.08), 0, 1)), 4),
            }
            obs[-1]["assay_cost"] += ASSAYS["karyotype_ddpcr"][0]
            cost += ASSAYS["karyotype_ddpcr"][0]

        yield_per_ipsc = round(float(carried * X / max(sp.seed_density, 1e-6)), 4)
        outcome = {
            "monocyte_yield_per_input_ipsc": yield_per_ipsc,
            "harvest_total_e6_per_ml": round(harvest_cum, 4),
            "harvest_yield_per_input_ipsc": round(
                float(harvest_cum / max(sp.seed_density, 1e-6)), 4),
            "final_vcd_e6_per_ml": round(X, 4),
            "final_viability_pct": round(viab * 100, 2),
            "stage_efficiency": stage_yield,
            "cumulative_differentiation_efficiency": round(carried, 4),
        }
        return RunRecord(
            run_id=run_id,
            line_id=L.line_id,
            passage_number=L.passage_number,
            setpoints=asdict(sp),
            observations=obs,
            outcome=outcome,
            total_assay_cost=round(cost, 2),
            ground_truth={"variant_fraction": round(L.variant_fraction, 4),
                          "mu_max": round(L.mu_max, 5),
                          "death_base": round(L.death_base, 5)},
        )


def mean_eff_running(acc: float, n: int) -> float:
    return acc / max(n, 1)


def introduce_variant(line: LineState, initial_fraction: float = 0.02) -> None:
    """Seed the culture-adaptation clone (20q11.21 gain / BCL2L1 model)."""
    line.variant_fraction = initial_fraction


def introduce_sensor_fault(sensors: SensorState, kind: str = "capacitance",
                           severity: float = 0.18) -> None:
    """Instrument fault, NOT biology. The agent must not confuse the two."""
    if kind == "capacitance":
        sensors.capacitance_fouling = 1.0 - severity
    elif kind == "ph":
        sensors.ph_offset = -severity
    elif kind == "do":
        sensors.do_patch_gain = 1.0 - severity
    elif kind == "counter":
        sensors.counter_bias = 1.0 - severity
    elif kind == "staining":
        # bad antibody lot / no-wash step failed: ALL fluorescence channels
        # drop together, including the viability dye. Impedance is unaffected.
        sensors.stain_efficiency = 1.0 - severity
