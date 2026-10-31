"""Model parameters, each with a value, a plausible range and an honesty tag.

Tags: S = sourced, D = derived from sourced numbers, A = assumption.
Ranges on A/D parameters drive the epistemic tier of every interval and the
Morris/Sobol sensitivity analysis. See docs/RESEARCH.md for sources.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass


@dataclass(frozen=True)
class Param:
    value: float
    lo: float
    hi: float
    tag: str
    note: str


PARAMS: dict[str, Param] = {
    # power
    "repair_median_min": Param(240, 150, 360, "A", "transformer fault: bus reconfiguration / mobile unit, median (CAIDI ~110 min is the all-cause baseline, EIA)"),
    "load_factor": Param(1.25, 1.1, 1.35, "A", "heat-dome evening load relative to design load (shrinks transfer headroom)"),
    "transfer_remote_min": Param(8, 4, 15, "A", "tie transfer via SCADA, median"),
    "transfer_manual_min": Param(40, 25, 70, "A", "tie transfer by field crew, median"),
    "transfer_flisr_min": Param(2, 1, 4, "S", "FLISR automated restoration (DOE 2014)"),
    # hospitals and backup power
    "p_fts": Param(0.06, 0.015, 0.12, "S", "generator failure-to-start under stress (ASHE: 10/172 Sandy subset)"),
    "p_fts_maintained": Param(0.01, 0.005, 0.02, "S", "with test & maintenance programme"),
    "gen_restart_median_min": Param(25, 10, 45, "A", "manual restart after failure-to-start"),
    "p_restart": Param(0.7, 0.5, 0.9, "A", "manual restart succeeds"),
    "gen_transfer_min": Param(0.17, 0.17, 0.17, "S", "NFPA 110 Type 10 (10 s)"),
    "water_diversion_delay_min": Param(45, 30, 90, "A", "hospital diverts after losing water pressure"),
    # telecom, dispatch
    "telecom_penalty_median_min": Param(3.0, 1.5, 6.0, "A", "call delay when the serving tower is down"),
    "failover_min": Param(8, 4, 15, "A", "dispatch failover to backup centre"),
    "backup_dispatch_penalty_min": Param(1.0, 0.5, 2.0, "A", "per-call overhead at backup centre"),
    # traffic
    "dark_cap": Param(0.40, 0.30, 0.60, "D", "capacity retained at a dark (all-way-stop) intersection, NCHRP"),
    "spillback": Param(0.35, 0.1, 0.6, "A", "extra v/c on arcs feeding a dark intersection (queue spillback)"),
    "dark_delay": Param(0.12, 0.05, 0.25, "A", "stop delay at a dark intersection (min)"),
    "bpr_alpha": Param(0.15, 0.15, 0.15, "S", "BPR alpha"),
    "bpr_beta": Param(4.0, 4.0, 4.0, "S", "BPR beta"),
    "vc_cap": Param(2.5, 2.5, 2.5, "A", "v/c cap inside BPR (gridlock ceiling)"),
    "ev_gain": Param(0.40, 0.30, 0.60, "D", "lights-and-sirens advantage at free flow (PMC4288957)"),
    # EMS demand and service
    "calls_per_1000_day": Param(0.45, 0.35, 0.53, "S", "EMS calls per 1000 residents per day (FDNY, county studies)"),
    "heat_mult": Param(1.2, 1.05, 1.3, "A", "extreme-heat EMS demand multiplier (heat-health literature: +10-25%)"),
    "evening_mult": Param(1.3, 1.2, 1.4, "A", "17:00-21:00 demand relative to daily mean"),
    "outage_surge": Param(0.35, 0.2, 0.58, "D", "immediate extra demand in blacked-out population (Prezant 2005: +58% total)"),
    "resp_surge": Param(0.9, 0.5, 1.9, "D", "device-dependent respiratory surge after onset (Prezant: resp x2.9)"),
    "resp_onset_min": Param(120, 60, 240, "A", "onset of respiratory surge after power loss"),
    "crash_rate_dark_per_h": Param(0.05, 0.0, 0.12, "A", "extra crashes per dark signal-hour (mixed evidence)"),
    "callproc_median_min": Param(1.1, 0.8, 1.5, "S", "call processing (NFPA 1221: 64 s)"),
    "turnout_median_min": Param(1.0, 0.8, 1.4, "S", "turnout (NFPA 1710: 60 s)"),
    "scene_median_min": Param(16, 12, 20, "S", "on-scene time (NEMSIS)"),
    "offload_median_min": Param(27, 20, 43, "S", "ED offload (California EMSA)"),
    "p_transport_hi": Param(0.85, 0.75, 0.95, "A", "P1/P2 transported"),
    "p_transport_lo": Param(0.5, 0.35, 0.65, "A", "P3 transported"),
    "unpowered_offload_extra_min": Param(15, 5, 30, "A", "extra offload at a hospital on failed backup"),
    "target_response_min": Param(8, 8, 8, "S", "NFPA 1710 ALS response target"),
}

SPREAD = {"callproc": 0.35, "turnout": 0.3, "scene": 0.35, "offload": 0.4, "repair": 0.3,
          "transfer": 0.5, "restart": 0.4, "telecom": 0.4, "battery": 0.2, "buffer": 0.3,
          "ups": 0.15}


def defaults() -> dict[str, float]:
    return {k: p.value for k, p in PARAMS.items()}


def uncertain() -> list[str]:
    """Parameters with a real range and a non-sourced tag (the epistemic tier)."""
    return [k for k, p in PARAMS.items() if p.hi > p.lo and p.tag in ("A", "D")]


def digest(params: dict) -> str:
    return hashlib.blake2b(json.dumps(sorted(params.items())).encode(), digest_size=8).hexdigest()


def table() -> list[dict]:
    return [{"name": k, "value": p.value, "range": [p.lo, p.hi], "tag": p.tag, "note": p.note}
            for k, p in PARAMS.items()]
