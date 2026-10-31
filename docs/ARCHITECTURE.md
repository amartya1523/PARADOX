# PARADOX: Solution Architecture

This is the design that came out of [`ANALYSIS.md`](ANALYSIS.md). Decision tags such as *(D3)* or *(A3-#6)* point back to the analysis that forced them.

```
                        ┌──────────────────────────────────────────────────────────┐
   PQL / REST / UI ───▶ │  paradox.engine.Paradox  (query planner + receipts)        │
                        └───────┬───────────────┬───────────────┬──────────────────┘
                                │               │               │
             ┌──────────────────▼──┐   ┌────────▼────────┐  ┌───▼──────────────────┐
             │ causal/             │   │ uncertainty/    │  │ planner/             │
             │  why · actual-cause │   │  paired CRN     │  │  cone candidates     │
             │  shapley · evidence │   │  morris · sobol │  │  CELF · prune        │
             │  correlate · audit  │   │  oracle validate│  │  held-out evaluation │
             └──────────┬──────────┘   └────────┬────────┘  └───┬──────────────────┘
                        └───────────────┬───────┴───────────────┘
                              ┌─────────▼──────────────────────────────┐
                              │ sim/   world = f(city, scenario, noise) │
                              │  timeline SCM (infra) ─▶ EMS block      │
                              │  addressed noise · abduction posterior  │
                              │  early cutoff · τ* checkpoint resume    │
                              └─────────┬──────────────────────────────┘
                                        │ events (causally hashed)
   ┌────────────────────────────────────▼─────────────────────────────────────────┐
   │ store/  ChronoStore                                                            │
   │  EventLog (content+cause addressed, hash-chained, bitemporal intervals)        │
   │  Branch overlays (fork_t, fork_tx) · TemporalGraph (as-of, checkpoints)        │
   │  Hypertable (chunked columnar telemetry) · Receipts (provenance ledger)        │
   └───────────────────────────────────────────────────────────────────────────────┘
```

## 1. The world function

The whole system rests on one pure function:

```
run(city, scenario, interventions, noise, params)  →  World(timelines, incidents, events, kpis)
```

- **`city`** is a static synthetic registry produced by `generate_city(seed)`:
  - roads, signals, blocks and buildings;
  - substations, feeders, tie switches and distribution transformers;
  - hospitals, fuel pumps, EMS stations, dispatch centres, telecom towers, water pumps and pressure zones.
- **`scenario`** holds the initiating exogenous faults, e.g. `substation:S3` at 18:15 with a repair duration drawn from noise.
- **`interventions`** are do-operators (§4).
- **`noise`** is an addressed noise source (§3): the explicit exogenous variables U.
- **`params`** are model parameters, each tagged [S]/[D]/[A].

Same inputs give a bit-identical output, down to the Merkle head of the emitted event log. That property is tested.

## 2. Simulation layers

### 2.1 Infrastructure layer: an SCM over timelines

Every component's condition is a **Timeline**: a normalised set of disjoint "bad" intervals `[a, b)` inside the horizon (16:00–24:00). Each interval endpoint carries a **cause tag**, the id of the event that started or ended it, plus the **root**, the id of the initiating exogenous fault. Each node's structural equation maps its parents' timelines and its own noise to a timeline and a list of events.

| Node | Structural equation (simplified) | Exogenous noise (address) |
|---|---|---|
| `substation:S` | faults from the scenario, minus `NEVER FAILS`, plus `FAILS AT` | repair duration `(S, repair, fault_id)` |
| `grid` (block over all feeders) | feeder down = substation down, except that a tie transfer restores it after delay *d* if the neighbouring substation is up and its **transfer arbiter** still has headroom; *d* is remote (SCADA up) or manual (SCADA down) *(D5)* | `(feeder, transfer, root)` |
| `tower:K` | down = feeder down, with onset delayed by battery life *(D3)* | `(K, battery, root)` |
| `signal:I` | dark = feeder down, delayed by UPS hours if it has a UPS | `(I, ups, root)` |
| `pump:P` → `zone:Z` | zone depressurised = pump down, delayed by tank buffer | `(Z, buffer, root)` |
| `fuelpump:H` | twin: on the hospital emergency bus. Oracle: may sit on another feeder *(D2)* | — |
| `hospital:H` | grid loss = all of its feeds down; generator start / restart; day tank runs dry if the fuel pump is unpowered; unpowered = grid loss ∩ generator unavailable; diversion = unpowered ∪ (water lost, delayed 45 min) | `(H, gen_start, root)`, `(H, gen_restart, root)`, `(H, restart_delay, root)` |
| `dispatch:D1` | down = feeder down, delayed by UPS, ∩ generator unavailable; failover to D2 takes 8 min | `(D1, gen_start, root)` |
| `block:B` | power down = feeder down (drives the outage-induced demand surge, *D4*) | — |

**Why episodic noise is keyed by root fault (A3-#3).** Keying a tower's battery draw by its outage ordinal would shift every later draw whenever an intervention removed an earlier outage. Keying by the *exogenous root fault* that caused the episode keeps the draw attached to *what it is about*.

**Incremental recompute (D1, A10).**
- Nodes run in topological order.
- A node is recomputed only if it is directly touched by an intervention, or if any parent's output digest differs from the baseline. Otherwise its baseline timeline and events are reused. This is change propagation with early cutoff.
- The digest covers the full output, endpoints and cause tags included, so nothing a child reads can hide from it.
- Coupled feeders are a single `grid` node, which keeps the arbiter's shared headroom inside one block.

### 2.2 EMS layer: one coupled block (A3-#6, A10)

Incidents compete for ambulances, so per-incident early cutoff is **unsound**. The EMS layer is therefore a single block that is simulated in time order.

1. **Exogenous candidate tape.** Each of 64 demand cells runs a *dominating* Poisson process at rate λ_max. For candidate `k`:
   - the gap comes from `(cell, gap, k)`;
   - acceptance comes from `(cell, acc, k)`;
   - marks (block, priority, transport) come from `(cell, mark, k)`.
2. **Thinning.** A candidate is accepted when `u < λ(t)/λ_max`, where λ(t) = base · time-of-day · (1 + outage surge). The surge decomposition labels each accepted incident as **base** (independent of the outage) or **induced** (caused by a specific `power.lost` event).
3. **Crashes.** Crash candidates are generated per signal with addresses `(signal, crash, k)`, and are accepted only while the signal is dark.
4. **Incident ids** have the form `I-<cell>-<k>`. They are stable across all worlds, so base incidents form a **fixed matched population** (A3-#7).
5. **Per-incident draws** are keyed by incident id: `(iid, callproc)`, `(iid, turnout)`, `(iid, scene)`, `(iid, offload)`, plus Gumbel tie-breaks `(iid, unit, choice)` (Oberst & Sontag).
6. **Response** = call processing + telecom penalty + dispatch (failover queue) + queue wait + turnout + travel.
   - Travel is a scipy Dijkstra on a road graph whose edge weights depend on the **road-state epoch** (the set of dark signals plus a 15-minute time bin), using BPR with dark-intersection capacity factors.
   - Distance arrays are cached per (epoch, node, direction) and shared across branches and replicates *(D7)*.
7. **Dynamic provenance.**
   - A dispatch event cites the incident, the event that freed the unit, any dark-signal events on the route, and failover or telecom events.
   - If the nearest unit was busy, it also cites that unit's holding assignment (`rule = resource:held_by`). This is how absence causation becomes explicit.
8. **Checkpoint resume.** Unit and queue state are checkpointed every N incidents. A counterfactual computes τ\*, the earliest time at which any EMS input timeline differs from the baseline. It then restores the last checkpoint at or before τ\* and simulates from there. This is tested against a full replay.

## 3. Addressed noise and abduction

- `NoiseTape.u(*address)` hashes a **typed, length-prefixed** encoding of `(seed, address)` with BLAKE2b (A3-#12).
- Threshold draws (`bernoulli`, thinning acceptance) record a **constraint interval**: the outcome tells you `u ∈ (0, p)` or `u ∈ [p, 1)`. Continuous draws record a **point**.
- **Abduction** (`Posterior(tape, seed_r)`), for replicate r:
  - a point stays a point;
  - an interval is re-sampled uniformly *inside* the interval using replicate-specific noise (the truncated posterior);
  - an address that was never observed is drawn fresh.
- **Consistency for free.** The factual arm reproduces the observed world exactly (Galles–Pearl consistency, which is tested). The counterfactual arm varies only where it touches unobserved or partially observed noise.
- **Worked example.** The factual world observed "the generator at H04 failed to start", so u ∈ (0, 0.06). Under the intervention "generator maintenance programme" (p_fts → 0.01), the counterfactual start fails with probability 0.01/0.06 ≈ 17%. That is the *probability of necessity*-style answer, and it falls out of the posterior with no special-casing.
- **Coupling sensitivity (A3-#1).** An alternative coupling re-draws all per-incident service noise independently in the counterfactual arm. Results that change under the alternative coupling are flagged *coupling-sensitive*.

## 4. Interventions (do-operators)

| PQL | Effect |
|---|---|
| `substation:S NEVER FAILS` / `FAILS AT hh:mm FOR m` / `REPAIRS IN m` | edit the exogenous fault list or the repair override |
| `hospital:H GENERATOR STARTS [AFTER m]` | force generator start success, with the given start delay |
| `hospital:H GETS FUEL BACKUP` | put the fuel pump on the emergency bus (removes the hidden dependency) |
| `hospital:H GETS MAINTENANCE` | p_fts → 0.01 for that site (a parametric do, so it uses the posterior) |
| `feeder:F GETS FLISR` / `ALL FEEDERS GET FLISR` | automated transfer in about 2 min with no SCADA dependency |
| `signal:I GETS UPS h` / `ALL SIGNALS GET UPS h` | battery backup |
| `tower:K GETS BATTERY h` | battery extension |
| `dispatch:D1 GENERATOR STARTS` | force start |
| `station:E ADD n AMBULANCES` | extra units |
| `SET param = value` | parametric intervention |

## 5. ChronoStore contracts

| Contract | Mechanism |
|---|---|
| **Event identity** | `eid = BLAKE2b-128(canonical(t, kind, entity, attrs, sorted(causes), rule))`. The canonical encoder rejects unknown types and coerces numpy values. Re-using an id asserts content equality. |
| **Causal Merkle DAG** | Because causes are part of the id, two branches share an event ⇔ they share its causal past. `diff(main, b)` = the intervention's causal footprint. |
| **Bitemporal visibility** | Per branch and per event, a list of `(assert_tx, retract_tx)` intervals. `known_at=tx` evaluates visibility at that transaction. `correct()` is one atomic transaction (retract + assert). |
| **Branch overlay** | A branch reads `parent[t < fork_t, known_at ≤ fork_tx] ∪ own`. Branches are O(1) to create, and objects are deduplicated across branches. |
| **Hash chain** | genesis = H(parent name, parent chain length at fork, parent head at fork, fork_t). `verify()` walks the lineage. |
| **Temporal graph** | A materialised view: static edges from the city, dynamic edges from `topo.open/close` events, deltas from `state` attributes, ties broken by a causal sequence number, checkpoints every √N deltas. |
| **Hypertable** | Telemetry in 60-min chunks, columnar arrays sorted by (entity, metric, t), with chunk pruning by id range and zone map, and incrementally maintained rollups. |
| **Receipts** | `{query, branches → (head, known_at), model/code/param digests, seeds, n, coupling, queries_explored, result_digest}`. `VERIFY` re-executes the query and compares digests. |

## 6. Analysis engines

- **WHY `incident:X`.** The answer has three parts:
  - a response-time decomposition, compared against the same incident in the *root-free* counterfactual world;
  - a provenance tree from lineage, with each edge carrying its dependency class (physical, cyber, geographic or logical) and evidence grade;
  - but-for tests and minimal flipping sets (|S| ≤ 3) over the root exogenous events, giving each root event a Chockler–Halpern responsibility of 1/|S|.

  The answer also reports *chain support*: the fraction of posterior replicates in which the same chain reproduces (A3-#10).
- **ATTRIBUTE metric.** Exact Shapley over root events, v(S) = KPI(roots in S active) − KPI(no roots). Each coalition is an incremental counterfactual. The interaction between dependent roots is disclosed.
- **COMPARE / WHAT IF.** Paired runs: rung-3 posterior replicates by default, or rung-2 fresh evenings with `MODE INTERVENTIONAL`. Outputs:
  - Δ, a 95% bootstrap CI, P(Δ < 0), quantiles, and the CRN variance-reduction factor;
  - matched versus induced populations, zone-stratified tables, and a reversal flag;
  - the coupling band.
- **SENSITIVITY.** Morris over [A] parameters (r trajectories), then Sobol (Saltelli A/B/AB design, Jansen total index) over the top k.
- **PLAN.**
  - Candidate actions are drawn from the causal cone of the harm.
  - CELF lazy greedy runs by Δ/cost and also by plain Δ, and keeps the better result.
  - Complementary actions are offered as bundles.
  - The plan is pruned to an irreducible set.
  - It is evaluated on **held-out seeds**, and the output is a Pareto curve plus P(meet target).
- **CORRELATE / AUDIT.**
  - CORRELATE: association across a randomised-fault ensemble, then a modelled-path / common-ancestor / d-separator check, then a stratified conditional-independence test.
  - AUDIT: co-occurrence screen, then knockout confirmation, then evidence grades A–X.
- **VALIDATE.** The oracle world is the twin plus hidden structure: a Bellevue-style fuel pump, a higher true p_fts, a lower true dark capacity and a higher crash rate. Outputs:
  - the twin's counterfactual error;
  - empirical coverage of its 90% intervals;
  - whether AUDIT recovers the hidden dependency.

## 7. Module map

```
paradox/
  store/      events.py  tgraph.py  timeseries.py  receipts.py  derive.py
  city/       generator.py  model.py  roads.py
  sim/        noise.py  timeline.py  infra.py  ems.py  world.py  interventions.py  params.py  telemetry.py
  causal/     why.py  actual.py  shapley.py  evidence.py
  uncertainty/ ensemble.py  sensitivity.py  validate.py
  planner/    actions.py  celf.py
  pql/        lexer.py  parser.py  executor.py
  engine.py   export.py  cli.py
  api/        app.py
  web/        index.html  app.js  style.css
```
