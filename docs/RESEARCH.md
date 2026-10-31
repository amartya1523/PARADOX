# Research Notes and Model Parameters

These notes condense the literature review behind PARADOX. Each entry pairs an idea with what was actually built from it.

> **Honesty tags used in the parameter tables:**
> - **[S]** sourced from the reference given;
> - **[D]** derived from sourced numbers;
> - **[A]** assumption made for this prototype.
>
> Several primary documents (the NFPA standards, some INL reports) were only reachable through secondary sources or abstracts. Treat those numbers as order-of-magnitude anchors, not calibration.

---

## A. Causal theory

| Idea | Used in PARADOX as | Reference |
|---|---|---|
| Ladder of causation; abduction → action → prediction | Branch semantics. Abduction is exact for simulated noise (we own the exogenous tape) and becomes **clamping** for observed facts. | Pearl, *Causal inference in statistics: an overview*, Stat. Surveys 2009. https://doi.org/10.1214/09-SS057 |
| Event-keyed counter-based RNG, giving common random numbers that survive path changes | `paradox.sim.noise.NoiseTape`: every draw is addressed by `(seed, entity, mechanism, ordinal)` and hashed with BLAKE2b. Uniforms are stored and inverse-CDF transformed at use time. | Buffalo, Pearson & Klein, *Realizing Common Random Numbers: Event-Keyed Hashing…* https://arxiv.org/abs/2603.11084 (abstract read) |
| Counterfactually-stable discrete choice (Gumbel-max) | Ambulance tie-breaking uses per-option Gumbel noise. | Oberst & Sontag, ICML 2019. https://proceedings.mlr.press/v97/oberst19a.html |
| Actual causation (modified HP); responsibility = 1/(k+1) | `paradox.causal.actual`: minimal flipping sets up to size 3, responsibility scores. | Halpern, *Cause, Responsibility, and Blame* https://arxiv.org/abs/1412.2985 ; Chockler & Halpern https://arxiv.org/abs/cs/0312038 |
| Shapley attribution of a continuous impact | `ATTRIBUTE`: exact over ≤ 7 root events, permutation sampling above that. | Standard cooperative game theory |
| Falsification, not validation, of twins | The `VALIDATE` harness runs an oracle world with a hidden SCM and reports error plus interval coverage. Every answer carries a disclaimer. | Cornish, Taufiq, Doucet & Holmes, *Causal Falsification of Digital Twins*, JMLR 27 (2026). https://www.jmlr.org/papers/v27/24-1752.html |
| Counterfactual coupling is not identifiable | The *coupling-sensitivity* check re-draws downstream noise and reports the spread. | Laudy, *The Digital Twin Counterfactual Framework* https://arxiv.org/abs/2604.01325 (preprint, abstract read) |

## B. Uncertainty and search

| Idea | Used as | Reference |
|---|---|---|
| Paired CRN Monte Carlo with bootstrap CI | `paradox.uncertainty.ensemble`. Reports Δ, the 95% CI and the achieved variance-reduction factor. | Standard; `scipy.stats.bootstrap` |
| Morris elementary-effects screening | `SENSITIVITY` (μ*, σ per parameter) | Morris 1991; Saltelli et al. 2010 https://doi.org/10.1016/j.cpc.2009.09.018 |
| Sobol first and total indices (Saltelli and Jansen estimators) | `SENSITIVITY ... METHOD SOBOL` on the top parameters | Saltelli et al. 2010 |
| Lazy greedy (CELF) with cost-benefit ratio, plus prune to an irreducible set | `paradox.planner` | Leskovec et al., KDD 2007 https://www.cs.cmu.edu/~jure/pubs/detect-kdd07.pdf |
| Network interdiction / min-cut | Candidate generation (dependency min node cut) | Smith & Song, EJOR 2020 https://doi.org/10.1016/j.ejor.2019.06.024 |

## C. Database systems

| Idea | Used as | Reference |
|---|---|---|
| Bitemporal: valid time and transaction time, append-only, corrections as new tx | `ChronoStore` events carry `t` (valid) and `tx`; `retract`/`correct`; `as_of(t, known_at=tx)` | XTDB, *Time in XTDB* https://docs.xtdb.com/about/time-in-xtdb.html ; Kulkarni & Michels, SQL:2011 temporal https://sigmodrecord.org/2012/09/30/temporal-features-in-sql2011/ |
| Content-addressed structural sharing (prolly trees, delta layers) | Events are stored once in a content-addressed object store. Branches are overlays: parent's prefix below `fork_t`, plus their own segment. | Dolt prolly trees https://docs.dolthub.com/architecture/storage-engine/prolly-tree ; TerminusDB delta layers |
| Historical graph snapshot retrieval | `TemporalGraph`: interval-labelled edges, checkpoints every K deltas plus replay. K ≈ √N is benchmarked. | Khurana & Deshpande, ICDE 2013 https://arxiv.org/abs/1207.5777 |
| Change propagation with early cutoff | Timeline SCM recompute in topological order. If a recomputed node's timeline digest equals the baseline, its subtree is skipped. | Hammer et al., *Adapton*, PLDI 2014 ; McSherry et al., *Differential Dataflow*, CIDR 2013 |
| Provenance semirings; W3C PROV | Every event stores `causes` + `rule` (why-provenance). Answers are issued as `Receipt`s pinned to branch Merkle heads. | Green, Karvounarakis & Tannen, PODS 2007 https://www.cs.ucdavis.edu/~green/papers/pods07.pdf ; W3C PROV-DM https://www.w3.org/TR/prov-dm/ |
| Tamper-evident logging | Per-branch hash chain whose genesis is the parent's head at the fork | Crosby & Wallach, USENIX Security 2009 |
| Urban digital twin open challenges | Motivation: cross-domain integration, dynamic graphs, explainability | Lei et al., Automation in Construction 2023 https://ual.sg/publication/2023-autcon-dt-challenges/ |

**Key design insight from combining these.** An event's id hashes its content **and its causes' ids**, which makes the event log a *causal Merkle DAG*.
- Two branches share an event iff they share its whole causal past.
- The symmetric difference of two branches' id sets is therefore exactly the causal footprint of the intervention.
- That is the measured "cone of influence", and no separate diff algorithm is needed.

## D. Domain model parameters

### Power
| Parameter | Value | Tag | Source |
|---|---|---|---|
| Topology | substation → radial feeders → transformers → buildings; normally-open ties between feeders of different substations | [S] | IEEE PES test feeders https://cmte.ieee.org/pes-testfeeders/resources |
| Customers per substation, per feeder | ~2,200 and ~690 | [D] | TAMU combined T&D synthetic case |
| Tie-switch manual transfer delay | lognormal, median 35 min (5–90 min range) | [A] | — |
| Tie transfer feasibility | the neighbour substation must have headroom (N-1 firm capacity ≈ 50–67% loading) | [S/D] | EPRI workshop notes |
| Substation repair | 120–240 min for the scenario fault | [A] | CAIDI ≈ 110 min baseline (EIA) |

### Hospital backup power
| Parameter | Value | Tag | Source |
|---|---|---|---|
| Transfer time | 10 s (NFPA 110 Level 1, Type 10) | [S] | NFPA 110 via secondary sources |
| Generator failure-to-start (normal) | 1.5% | [S] | ASHE survey: 5/370 outages |
| Generator failure-to-start (stress) | 6% (heat/flood stress, used in the scenario) | [S] | ASHE survey (Sandy subset): 10/172 |
| Manual restart after FTS | lognormal, median 25 min; second-attempt success 70% | [A] | — |
| Fuel autonomy | 24–96 h (irrelevant for an evening scenario, but modelled) | [S/A] | Joint Commission fuel FAQ |
| Running failure rate | 1e-3 per hour | [A] | INL EDG data, used as an optimistic proxy |

### Traffic
| Parameter | Value | Tag | Source |
|---|---|---|---|
| Dark signal behaviour | becomes an all-way stop | [S] | e.g. California Vehicle Code |
| Capacity retained at a dark intersection | 0.40 (range 0.3–0.6) | [D/A] | NCHRP AWSC headways 4.0–7.6 s |
| Link travel time | BPR, t = t₀[1 + 0.15 (v/c)^4] | [S] | Standard BPR |
| Extra delay at a dark intersection | 6 s × (1 + v/c) | [A] | — |
| Signals with battery backup | ~15% of signals, 2–6 h runtime | [S/A] | San Rafael battery backup programme |
| Emergency vehicle speed advantage | k_EV ≈ 1.4, capped toward 1 as v/c → 1 | [D/A] | lights-and-sirens study, PMC4288957 |

### EMS
| Parameter | Value | Tag | Source |
|---|---|---|---|
| Call processing | lognormal, median 1.1 min (64 s NFPA target) | [S/A] | NFPA 1221 via DC FEMS |
| Turnout | lognormal, median 1.0 min | [S] | NFPA 1710 |
| Target for P1 travel / total | 8 min total response | [S] | NFPA 1710 (ALS 480 s) |
| Urban response distribution | median 6 min, p90 12 min (calibration sanity check) | [S] | JAMA Surgery 2017, PMC5831456 |
| Scene time | 15–20 min | [A] | — |
| Hospital handoff | 15–30 min | [A] | — |
| Diversion triggers | hospital unpowered (generator failed) or water pressure lost for > 45 min | [A] | Diversion review (Johns Hopkins) |
| Dispatch failover to the backup centre | +3 min per call while failing over | [A] | — |
| Telecom tower battery | 2–6 h; when exhausted, calls from its cell are delayed by +2–6 min | [A] | — |

### Water
| Parameter | Value | Tag | Source |
|---|---|---|---|
| Storage buffer after pump loss | zone-dependent, 3–14 h (elevated tanks); pumped-only zones lose pressure in < 20 min | [D/A] | Bentley; Ten States Standards; WA DOH |
| Minimum pressure | 20 psi | [S] | state rules |

### Synthetic city precedents
- **Micropolis and Mesopolis** (Brumbelow et al., 2007) are virtual cities for water and infrastructure research. They are the closest precedent. https://arxiv.org/pdf/1407.0424
- **TAMU ACTIVSg** synthetic grids: https://electricgrids.engr.tamu.edu/texas7k/
- **Dependency taxonomy:** Rinaldi, Peerenboom & Kelly, IEEE Control Systems 2001 (physical / cyber / geographic / logical). Used here as the edge-type enum.
- **Abrupt collapse of interdependent networks:** Buldyrev et al., Nature 2010. https://arxiv.org/pdf/0907.1182. Qualitative motivation only.
