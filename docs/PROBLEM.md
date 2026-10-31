# PARADOX — Problem Analysis

> *"A database that does not only remember what happened, but can be interrogated about why, and argued with about what if."*

This document is the first hour of the project: before writing code, what exactly is the problem, why is it hard, and which parts of it can a prototype honestly solve?

---

## 1. Restating the problem precisely

A conventional database answers **rung-1 questions** (Pearl's ladder of causation):

| Rung | Question | Example | Conventional DB? |
|---|---|---|---|
| 1. Association | *What happened? What co-occurred?* | "Which hospitals reported outages at 18:30?" | Yes |
| 2. Intervention | *What happens if we do X?* | "If we add batteries to 40 traffic signals, how does average EMS response change?" | No |
| 3. Counterfactual | *Given what actually happened, what would have happened had X been different?* | "**This** evening, with **these** incidents, **these** generators, would patient #42 have been reached in under 8 minutes if substation S3 had not failed?" | No |

PARADOX's job is to make rungs 2 and 3 **queryable**, while also being **traceable** (every answer has evidence) and **explicit about uncertainty**. Rung 3 is the stronger and stranger claim. It is not "simulate a world without the failure". It is "simulate *the same* world, with the same luck, the same callers and the same coin-flips, except for the failure". That distinction is the backbone of the architecture.

## 2. Why this is a 10/10 problem: the eight hard sub-problems

### H1. Counterfactual coherence: "the same luck"
A stochastic simulator draws random numbers. If the alternate branch takes a different execution path, a single sequential RNG stream drifts. Every later draw is consumed by a different decision, and the two timelines differ by noise rather than by the intervention. Comparing them then measures **randomness, not causation**.

**Requirement:** exogenous noise must be addressable by *what it is for*, not by *when it was drawn*. Every random draw is keyed by `(seed, entity, mechanism, ordinal)` through a counter-based hash. A branch reuses every draw whose address it reaches, and only genuinely new situations consume fresh noise. This is the computational form of Pearl's abduction → action → prediction, and of the "twin network" construction.

### H2. Branching without corrupting reality
The original timeline must be **immutable**. A what-if must never edit it, and many what-ifs must coexist cheaply, like git branches over history. Naively copying a million-event log per branch is too expensive; sharing structure needs content addressing.

**Requirement:** a content-addressed, hash-chained event store in which an event's identity is a hash of its content **and of its causes**. Two branches agree on an event iff they agree on its entire causal past. The diff between two timelines is then exactly the *causal cone of the intervention*, with no heuristics.

### H3. Recomputing only what changed
Replaying a whole city for every question is too slow for interactive use. It is also far too slow for the hundreds of runs that uncertainty quantification and attribution need. But "recompute only the affected portion" has to be **provably equal** to a full recompute, or it is a bug generator.

**Requirement:**
- Structure the simulator as a **structural causal model over timelines**: each component's state history is a function of its parents' histories plus its own noise.
- Recompute in topological order from the intervened nodes, and **cut off early** when a node's recomputed timeline hashes equal to the baseline (Adapton/Salsa-style change propagation).
- Verify this by tests against full replay.

### H4. Dependencies are not static, and not all are physical
A tie switch closes at 18:41 and a feeder is now powered from a different substation. A dispatch centre fails over. A hospital goes on diversion and ambulances are routed elsewhere. So the dependency graph **changes over time**, and "what depended on what" is only meaningful **as of a timestamp**.

Some couplings are not edges in any infrastructure map. Two incidents compete for the same ambulance (**resource coupling**). A dark intersection causes a crash that consumes an ambulance (**induced demand**).

**Requirement:**
- A temporal graph with interval-valued edges, plus checkpoint+replay as-of reconstruction.
- A record of *dynamic* causal links (resource contention, induced events) as provenance, not as static topology.

### H5. Causation versus correlation inside the database
Across an ensemble, "hospital H5 diverted" and "signal I-07-12 dark" are strongly correlated. Neither causes the other: both descend from substation S3. A dashboard would happily show the correlation. A causal database must:
- distinguish **modelled mechanism** from **observed co-occurrence**;
- identify the **confounder**;
- rank actual causes with a formal definition: but-for, Halpern–Pearl actual causation, Chockler–Halpern responsibility, and Shapley attribution for continuous impacts.

### H6. Credibility: counterfactuals cannot be directly validated
This is the deepest problem, and the research flags it clearly (Cornish et al., *Causal Falsification of Digital Twins*, JMLR 2026).
- Rung-3 quantities depend on the joint coupling of factual and counterfactual noise, and that coupling is not identifiable from data, even infinite data.
- Observational data can **falsify** a twin but never **certify** it.

A prototype that prints "Emergency response: 14 min" with no qualifiers is making a claim it cannot support.

**Requirement:**
- Every answer carries an interval, a coupling assumption and a model-relative disclaimer.
- The system includes a **validation harness**. Because the city is synthetic, we can build a hidden "reality" model that differs from the twin (misspecified parameters, a dependency the twin doesn't know about) and measure how wrong the twin's counterfactuals are, and whether its intervals actually cover the truth.

### H7. Uncertainty, honestly decomposed
Uncertainty here has distinct kinds:
- **Aleatoric:** which calls come in, which generator coin-flips land.
- **Epistemic:** what is the true capacity of a dark intersection, or the generator failure-to-start rate?
- **Structural:** is the model even right? This kind can only be bounded by H6.

Paired Monte Carlo with common random numbers makes the *effect* estimate dramatically tighter than the two marginal estimates. Global sensitivity analysis (Morris, Sobol) says which assumptions the answer hinges on.

### H8. Searching for the smallest fix
"What is the smallest set of changes that would have prevented this?" is a combinatorial search: interdiction, hitting set, budgeted coverage. Each evaluation is itself a Monte Carlo counterfactual. Interacting interventions make greedy methods non-optimal: two redundant fixes are worth less together than the sum of each, which is supermodularity.

**Requirement:**
- Restrict candidate actions to the **causal cone** of the harm.
- Run cost-aware lazy greedy (CELF), with exact enumeration for small sets.
- Prune to an **irreducible** set.
- Re-evaluate the final set on **fresh seeds**, to defeat the optimiser's curse.

## 3. What PARADOX deliberately does **not** claim

- **It is not a validated model of any real city.** All infrastructure and all numbers are synthetic. Parameters are drawn from the literature where possible (see [`RESEARCH.md`](RESEARCH.md)) and flagged as assumptions elsewhere.
- **Counterfactuals are model-relative.** "Had S3 not failed, p90 response would have been 14 min" means *within this structural model and this noise coupling*.
- **Intervals cover Monte Carlo and parameter uncertainty, not structural error.** Structural error is probed by the oracle validation harness, and the harness reports it separately.

## 4. Design stance: one engine, many faces

The brief suggests a polyglot stack: PostgreSQL+PostGIS, Neo4j, TimescaleDB, an event store, a scenario store and a provenance store. That is a reasonable *production* deployment, but for a research prototype it hides the interesting part. The interesting part is the **contract between the stores**:
- event identity;
- branch semantics;
- as-of semantics;
- provenance pinning.

PARADOX therefore implements a single embedded **causal-temporal engine** (`ChronoStore`) whose layers each correspond to one of those systems:

| Brief's layer | PARADOX layer | Core idea |
|---|---|---|
| PostgreSQL + PostGIS | `city` registry + spatial grid index | entities, geometry, static attributes |
| Neo4j | `TemporalGraph` | typed dependency edges with validity intervals; as-of reconstruction via checkpoints + replay |
| TimescaleDB | `Hypertable` | time-chunked columnar telemetry, zone-map pruning, continuous rollups |
| Event store | `EventLog` | content-addressed, causally-hashed, hash-chained, bitemporal |
| Scenario store | `Branch` overlays | copy-on-write timelines forked at a time, sharing all unaffected events |
| Provenance store | `Receipt` ledger + causal links | every answer pinned to branch heads, seeds, model version; replayable |

An exporter maps the embedded engine onto the production stack (SQL DDL for PostGIS/Timescale, Cypher for Neo4j), so the design stays honest about how it would scale out.

## 5. The scenario used throughout

**"Heat Dome, 18:15"** is a synthetic city on a summer evening.
- **The city:**
  - roads: about 2,000 road segments on a perturbed lattice with arterials and a river with three bridges;
  - power: 10 substations, about 60 feeders with normally-open tie switches, and about 400 distribution transformers, giving about 500 electrical components;
  - buildings: 50,000;
  - facilities: 12 hospitals with backup generators, 18 EMS stations, 2 dispatch centres, 20 water pumps with storage tanks, and 30 telecom towers with batteries, giving about 100 critical facilities.
- **The failure:** at 18:15 a transformer fault takes substation **S3** offline, and the cascade begins:
  - feeders drop;
  - unbacked traffic signals go dark, and dark intersections become all-way stops;
  - hospital generators get a start attempt;
  - pumps stop, and storage tanks begin to drain;
  - telecom batteries count down;
  - crashes at dark intersections add incidents and consume ambulances.

The questions PARADOX must answer on this scenario:

1. `AS OF 18:40 SHOW DEPENDENTS OF substation:S3`: reconstruct the dependency graph as it was.
2. `WHY incident:I-0042`: why did this patient wait 27 minutes?
3. `WHAT IF substation:S3 NEVER FAILS COMPARE ems.p90_response`: actual against alternate, with a paired CI.
4. `WHAT IF generator:H04 STARTS AFTER 0`: "the backup activated earlier".
5. `ATTRIBUTE ems.p90_response`: how much of the harm is due to each root event.
6. `PLAN REDUCE ems.p90_response BELOW 12 BUDGET 4`: the smallest fix set.
7. `SENSITIVITY ems.p90_response`: which assumptions the answer depends on.
8. `VALIDATE`: how wrong are we, against a hidden reality?
