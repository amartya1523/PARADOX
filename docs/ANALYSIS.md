# Deep Analysis — What Thirteen Research Streams Told Us

Before the solution was designed, the problem was attacked from thirteen independent angles: three initial literature passes, then ten parallel deep-dive analyses. This document is the synthesis. Each section ends with the concrete decisions it forced. Those decisions are numbered **D1…Dn** and are referenced from [`ARCHITECTURE.md`](ARCHITECTURE.md) and from the code.

| # | Stream | Question asked |
|---|---|---|
| R1 | Causal theory | How do you make rung-3 counterfactuals coherent inside a stochastic simulator? |
| R2 | Database systems | Bitemporality, branching, temporal graphs, incremental recompute, provenance |
| R3 | Domain modelling | Interdependency models; power, traffic, EMS and water parameters |
| A1 | DB prior art | What do HypeR, Mahif, Caravan, Tiresias, MCDB and SimSQL already solve? |
| A2 | Real cascades | 2003 NE, 2019 Manhattan, Sandy, Uri, 2011 SW, Ida, Iberia 2025 |
| A3 | Red team | How could PARADOX be confidently wrong, or misused? |
| A4 | Evidence | Causal discovery from event logs; confounders; evidence grading |
| A5 | V&V | How to verify a simulator *and* a counterfactual engine |
| A6 | Planning | Real interventions, their costs, and honest optimisation |
| A7 | Language & UX | Query grammar, sensemaking UI, uncertainty visualisation |
| A8 | Performance | Cost model and how to stay interactive in Python |
| A9 | EMS fidelity | Demand, outage surges, queueing nonlinearity, diversion |
| A10 | Design review | Line-level bugs in the draft engine and the interfaces for the rest |

---

## 1. Where PARADOX sits: the gap no system fills (A1)

Database research has attacked "what-if" from several directions. Each line of work stops short of the full problem:

| System | Gets right | Missing for our problem |
|---|---|---|
| **HypeR** (SIGMOD'22) | causal what-if / how-to SQL over a causal model | static snapshot; acyclic; population expectations only. The authors name *time-unrolling* as future work. |
| **HypDB / CauSumX** (SIGMOD'18/'24) | confounder-aware explanations of aggregates | static tables, no interventions over time |
| **Mahif / Ultraverse** (SIGMOD'22/'25) | historical what-if over transaction logs via reenactment + slicing | deterministic; effects only travel along SQL data-flow, never through physical mechanisms |
| **Caravan / FaDE** | pre-provisioned or bit-vector interventions; FaDE reports >1M what-ifs/s | deletions/scaling only; no causal propagation |
| **Tiresias** (SIGMOD'12) | how-to queries compiled to MIP | deterministic, no causality |
| **Meliou et al.** (PVLDB'10) | HP causality and responsibility for query answers | no interventions, no time |
| **MCDB / SimSQL** | stochastic tables, Markov-chain databases | no causal semantics, no abduction, no WHY |

**The PARADOX niche** combines what each of these leaves out:
- a **time-unrolled structural causal model** with feedback broken across time;
- **per-instance counterfactuals** reached by abduction on recorded history;
- **persistent, queryable branches**;
- **Monte Carlo uncertainty**;
- **responsibility-ranked WHY**;
- **how-to search over branches**.

In one line: *SimSQL-style stochastic execution, HypeR semantics, Meliou responsibility, Tiresias-style how-to, Mahif-style slicing, for coupled infrastructure.*

> **D1.** Borrow HypeR's clause discipline (scope / intervention / output / post-condition) for PQL, and Mahif's slicing for incremental recompute (only descendants of the intervened node, only after the fork time). Borrow MCDB's "bundle" idea by vectorising the Monte Carlo replicates.

## 2. What real cascades teach (A2)

Seven documented events share a small set of recurring patterns. A credible twin must be able to express each one:

| Pattern | Real instance | Modelling requirement |
|---|---|---|
| **Common-mode hidden dependency** | Sandy, Bellevue: generators on floor 13, *fuel pumps in the basement*. Manhattan 2019: primary and backup relays shared one defective connection. | Backup systems decompose into parts (generator, fuel pump, ATS) with their *own* dependencies. → the oracle world hides one of these. |
| **Depletable buffers, second-wave failures** | Iberia 2025: small cells died instantly, then macro sites failed in a sharp wave at 4–6 h on batteries. 2003: 911 failed when Verizon generators failed. | Batteries, tanks and fuel are clocks, not booleans. Failures arrive in waves. |
| **Monitoring is a dependency** | 2003 NE: FirstEnergy's alarm system failed silently ("no alarm failure detection") | Cyber/monitoring edges delay *operator response* without cutting power → tie-switch operations depend on SCADA/telecom. |
| **Water pressure feeds hospitals** | Uri 2021: St. David's lost water pressure, then its boiler, then transferred patients | Hospital diversion can be triggered by water as well as power. |
| **Device-dependent residents** | Iberia 2025: a home-ventilator patient died when the battery ran out. 2003 NYC: respiratory device visits rose from 0 to 65. | A delayed respiratory incident surge 2–6 h after power loss in affected blocks. |
| **Unregistered critical load** | Uri: gas facilities sat on rotating-outage circuits | "Critical-load registration" is a counterfactual lever. |
| **Human workarounds** | bucket brigades; civilians directing traffic | Out of scope for v0.1; noted as a limitation. |

> **D2.** The city model includes generator **fuel transfer pumps** as separate nodes. The twin assumes they share the hospital's feeder. The hidden "reality" used for validation wires one of them to a *different* feeder, a Bellevue-style common-mode trap.
>
> **D3.** Telecom towers carry batteries (2–6 h). Their depletion produces a *second wave* in which calls from affected cells are delayed, and dispatch/SCADA functions degrade.
>
> **D4.** Blocks that lose power generate delayed respiratory incidents. The onset is lognormal around 3 h and the multiplier is sourced from Prezant 2005.
>
> **D5.** Tie-switch transfer delay depends on SCADA/telecom being up. Without it, crews switch manually and take longer.

## 3. How PARADOX could be confidently wrong, and the countermeasures (A3, A10)

The red team ranked twelve failure modes by severity. Every one of them became a feature, a test, or a disclosed limitation.

| # | Failure mode | Severity | Countermeasure in PARADOX |
|---|---|---|---|
| 1 | Individual counterfactual verdicts depend on an **unidentifiable noise coupling** | critical | Every answer is tagged *coupling-invariant* or *coupling-sensitive*. A **coupling-sensitivity band** re-runs the query with downstream noise re-drawn, and individual verdicts that flip are flagged. Responsibility is assigned to *components*, never people. |
| 2 | **Precision theatre**: CI covers Monte Carlo noise only, while results hinge on [A] parameters | critical | **Three-tier intervals**: (a) MC, (b) + parameter uncertainty (Latin hypercube over [A] ranges), (c) structural error measured by the oracle. A credibility scorecard is printed with every answer. |
| 3 | **Noise misalignment** when an entity's draw count changes (an ambulance does one fewer run, so all its later draws shift) | high | Noise is keyed to the **exogenous unit** (the incident id from a pre-generated candidate tape), never to the resource serving it. Test: interventions in an unrelated zone leave distant incidents' draws identical. |
| 4 | **Nondeterminism** (float ulps, dict order, numpy types in hashes) produces fake divergence | high | Typed canonical encoder that rejects unknown types, with numpy values coerced. Time is rounded to fixed resolution *before* it enters any decision. Test: the null intervention gives an empty diff, and the head hash is stable across `PYTHONHASHSEED` values. |
| 5 | **Late corrections** break as-of queries and causal hashes | high | Visibility is a list of `(assert_tx, retract_tx)` intervals per event. `correct()` is one atomic transaction. `lineage()` respects visibility. Diffs and receipts pin `known_at` on both sides. |
| 6 | **Early cutoff is unsound** under resource coupling (shared ambulances, shared substation headroom) | high | The EMS layer is **one coupled block**. Its cutoff compares *input digests*. On mismatch it finds the earliest divergence time τ\* and **resumes from a checkpoint ≤ τ\***. Power transfers share headroom through a per-substation **arbiter**. Randomised differential test: incremental == full replay. |
| 7 | **Simpson's paradox / shifting populations**: p90 is computed over different incident sets in different branches | high | **Fixed-population KPIs** over exogenous incidents matched by id, with induced incidents reported separately. Zone- and priority-stratified tables, plus a reversal detector. |
| 8 | **Garden of forking paths** in interactive exploration | med-high | A session **query ledger**: every receipt states how many what-ifs were explored before it. A *confirmatory* mode re-runs the query on a sealed seed block. |
| 9 | **Optimiser's curse** in the planner | med-high | Optimise on training seeds and report on **held-out seeds**. Report P(meet target), not only the mean. |
| 10 | **Cherry-picked seed** drives the narrative | medium | The scenario seed is committed by hash. WHY answers report *how often this causal chain occurs across the ensemble*. |
| 11 | **Forgeable provenance** | medium | Full 128-bit ids, with a content-equality assertion on id reuse. Receipts include code, parameter and clamp digests. `VERIFY` replays a receipt and compares result digests. |
| 12 | **Key-encoding collisions** (`("a/b","c")` vs `("a","b/c")`) | medium | Length-prefixed typed encoding for noise addresses and clamp keys. Injectivity property test. |
| + | **Clamping is not abduction**: observing that a generator failed implies U ~ Uniform(0, p), not a single point | — | Observed facts become **truncation constraints** on the noise. Each replicate samples U from the truncated posterior. |
| + | **Absence causation** ("no ambulance was free") is not an event | — | Contention is recorded explicitly: the dispatch event cites the assignment that held the nearest unit (`resource:held_by`). |

The design review (A10) also found concrete bugs in the first draft of the store: single-interval visibility, non-atomic correction, a genesis hash that committed to parent events the branch never sees, DFS-truncated lineage, float32 boundary misses in the hypertable, and noise-key separator mismatch. All are fixed, and each has a regression test.

## 4. Telling causation from correlation (A4)

A simulator can *intervene*, and that is a stronger evidence source than any observational discovery method. PARADOX therefore uses observational discovery only as a **screen**, and confirms each candidate by **knockout**: a paired CRN ensemble with the edge disabled versus enabled.

**Screen.** A pooled multivariate **Hawkes process** with an exponential kernel, fitted by EM. Its excitation matrix is the Granger graph for event sequences (Xu et al., ICML 2016; topological Hawkes, Cai et al.).

**Confounder detector.** For each strongly associated pair (X, Y):
1. If a directed path X→Y exists in the modelled graph, the association is a modelled dependency.
2. Otherwise, compute the common ancestors CA = anc(X) ∩ anc(Y).
3. Find a minimal d-separator Z (networkx).
4. Test X ⟂ Y | Z empirically by stratifying on Z.
5. If X and Y become independent given Z, report *"explained by confounder Z"*. If they remain dependent, report *"hidden dependency or unmodelled confounder"* and run a knockout.

**Evidence grades for every edge on a causal path:**

| Grade | Meaning |
|---|---|
| **A** | modelled mechanism, and a knockout shows an effect whose CI excludes 0 |
| **B** | modelled, and observationally supported (stable Hawkes edge) |
| **C** | modelled but untested: data have no power |
| **D** | discovered and knockout-confirmed, but **not in the model** (a hidden dependency) |
| **E** | modelled, tested with power, no effect found |
| **X** | contradicted: the association vanishes given Z, or has the wrong sign or lag |

An answer's confidence is the *weakest* grade on its path, worded in ICD-203 style ("likely", "almost certain").

> **D6.** `CORRELATE a WITH b` runs the confounder detector. `AUDIT` runs Hawkes + knockouts and prints grades. In the oracle world, AUDIT should discover the hidden fuel-pump dependency (grade D). That is the system catching its own model error.

## 5. Verification and credibility (A5)

The weak spot is *data validity*: the city is synthetic. Credibility must therefore rest on five things:
- verified code;
- a verified counterfactual engine;
- analytic sub-model checks;
- metamorphic relations;
- an oracle.

PARADOX adopts this test plan, implemented in `tests/`:

| Group | Test | Property asserted |
|---|---|---|
| CF engine | replay determinism | same seed and interventions ⇒ identical Merkle head, across processes |
| | **incremental == full** | early-cutoff recompute is event-for-event identical to a from-scratch run |
| | null intervention | do(X = x_factual) ⇒ empty diff |
| | effectiveness | under do(X=x), X = x throughout the window |
| | composition | if do(X=x) gives W=w, then do(X=x, W=w) gives the same Y |
| | commutativity | intervention order is irrelevant |
| | cone locality | entities outside the time-bounded reachability set keep identical traces |
| | CRN stability and efficacy | inserting an event leaves unrelated draws unchanged; Var(paired Δ) ≪ Var(independent Δ) |
| Simulator | conservation | ambulances conserved; incidents = served + queued; time non-decreasing |
| | extreme conditions | no failure ⇒ no outage KPIs; infinite fleet ⇒ zero queueing delay |
| | **Erlang-C check** | EMS layer with outage and traffic effects off: P(wait) and mean wait match M/M/c within tolerance across ρ |
| | Little's law | λ · E[busy] / c = simulated unit-hour utilisation |
| Metamorphic | monotonicity | adding a failure never improves outcomes (first-order stochastic dominance), apart from whitelisted non-monotone effects |
| | relabelling | permuting ids leaves outcome distributions unchanged |
| Store | bitemporal | corrections invisible at older `known_at`; branch isolation; hash-chain tamper detection |

Every answer carries a **credibility scorecard**, a 0–4 scale in the spirit of NASA-STD-7009B / ASME V&V40. Its *input pedigree* row is capped at 1 permanently, because the city is synthetic and the card must never overclaim.

## 6. Uncertainty, decomposed and shown honestly (R1, A7)

- **Aleatoric** uncertainty uses paired CRN Monte Carlo. Each replicate r shares its seed across both arms, Δ_r = Y_cf − Y_f. The answer reports:
  - mean Δ with a bootstrap CI;
  - P(Δ < 0);
  - the 5/50/95 quantiles;
  - the **achieved variance-reduction factor** of pairing.
- **Epistemic** uncertainty uses a Latin hypercube over [A]-tagged parameter ranges. **Morris** screening ranks which assumptions matter; **Sobol** (Saltelli/Jansen) quantifies the top few.
- **Structural** uncertainty is the oracle-world error and interval coverage.
- **Display:** never a bare number. Paired differences are shown as **quantile dotplots** (Kay et al.: these produce less biased effect-size judgments than means with error bars). Timelines use **HOPs** (hypothetical outcome plots), animating replicate pairs. Each result carries the sentence *"P(Δp90 < 0) = 0.97 over 200 paired runs"*.

## 7. Making it fast enough to be interactive (A8)

The cost model was measured on the development machine:
- multi-source scipy Dijkstra on about 1k nodes takes 1.2 ms, against 24 ms for 100 separate sources;
- blake2b hashes about 2.2 M small events/s;
- per-event Python handlers cost 1–3 µs.

The dominant workloads are **Shapley** (2^k coalitions × replicates), **CELF's first pass** (|C| ensembles) and **Sobol** (N(d+2) evaluations).

> **D7.** Make the engine fast by caching rather than scale:
> - road travel matrices are cached per *road-state epoch*: the hash of the dark-signal set plus the time bin. These are shared across branches and replicates;
> - the power and infrastructure layer recomputes with early cutoff;
> - the EMS block resumes from its checkpoint at τ\*;
> - telemetry is vectorised in numpy;
> - ensembles run in a process pool designed for `spawn`, with each replicate's seed derived from its index and never from the worker.
>
> Report: ingestion rows/s, as-of latency vs checkpoint interval, incremental-vs-full speedup with reuse ratio, CRN variance reduction, and ensemble throughput.

## 8. Interventions that actually exist (A6, A9)

The planner's candidate catalogue is grounded in real resilience measures, with relative costs (1 CU ≈ $10k):
- FLISR self-healing feeders: restoration from 60–120 min to under 5 min;
- signal battery UPS: about 1 CU, 6–8 h;
- generator quick-connect docks and pooled portable units;
- generator test programmes: failure-to-start from 5% to 1%;
- dispatch-centre backup power;
- telecom battery extension;
- ambulance pre-positioning and extra units.

Two kinds of extra are complements (dock + generator, maintenance + generator) and are offered as **bundles**, so greedy search does not undervalue them.

**Honest reporting** means:
- optimise on training seeds, report on held-out seeds;
- give a Pareto curve of cost against impact, from greedy's nested solutions;
- prune the plan to an **irreducible** set;
- show P(meet target);
- watermark "synthetic, not decision-grade".

**EMS realism (A9) puts the system at the knee of the queueing curve.** The baseline city has about 600k residents at 0.45 calls per 1,000 residents per day, times 1.3 for evening. Busy time is 70–90 min per transport (scene 16 + transport 13 + offload ~27 + return). With 26 ambulances, utilisation is ρ ≈ 0.7. Erlang-C says P(wait) rises from 22% to 41% as ρ goes from 0.7 to 0.8. The 2003 blackout drove EMS demand up **58%**, and respiratory calls up 2.9×. This nonlinearity is the real mechanism that turns a substation fault into a response-time crisis, and it is why a small fix (two extra units, faster tie switching) can have a disproportionate effect.

## 9. Language and interface (A7)

- **PQL:**
  - time is a prefix clause (`AS OF t [KNOWN AT tx]`);
  - the world is a clause (`IN @branch` or `WHAT IF …`);
  - verbs: `SHOW / WHY / WHY NOT / COMPARE / ATTRIBUTE / PLAN / SENSITIVITY / CORRELATE / AUDIT / VALIDATE / VERIFY / DIFF`;
  - every result carries a header with world, as-of, seeds, n and receipt hash.
- **UI** follows the Pirolli–Card sensemaking loop (forage, then make sense) and Jigsaw's linked views:
  - the map, with a time scrubber and event ticks;
  - a causal-chain explorer, left to right on a time axis, with evidence grades on the edges;
  - an actual-vs-alternate view: synchronised split maps, an overlaid timeline, and a **difference lane** that marks the first divergence;
  - a quantile dotplot of paired Δ;
  - a planner with a Pareto plot;
  - a ledger of events, hashes and receipts;
  - a PQL console that shows the query behind every panel action.

## 10. Final scope decisions

| In scope (v0.1) | Out of scope (stated as limitations) |
|---|---|
| Synthetic city at brief scale (50k buildings, ~2k road segments, ~500 electrical components, ~100 facilities) | Any real city data |
| Power, signals, hospitals (+fuel pumps), EMS, dispatch, telecom, water | Gas–electric feedback, flooding hazard geometry, human workarounds |
| Exact model-relative counterfactuals with addressed noise; truncated-posterior abduction | Learning the structural model from data |
| Three-tier uncertainty; Morris/Sobol; oracle validation with coverage | Formal guarantees on structural error |
| Hawkes screen + knockouts for evidence grades | Full FCI / PCMCI |
| CELF planner with held-out evaluation | Bilevel / defender–attacker–defender optimisation |
| Embedded engine + exporters to SQL/Cypher | Running PostGIS / Neo4j / Timescale clusters |
