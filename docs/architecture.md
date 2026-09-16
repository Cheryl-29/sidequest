# Architecture

```text
React / TypeScript SPA
  └─ POST /api/runs · reroll · accept → SSE progress → quest card / trace / preferences drawer
FastAPI (api.py)
  ├─ run lifecycle, owner-scoped sessions, compare-and-swap writes      storage.py
  ├─ agent round (LLM decides WHERE)                                    agent.py, llm.py, moment.py
  │    infer_intent → choose_probe → search_places → rank + seeded draw → narrate
  ├─ taste session and long-term typed memory                           taste.py, memory.py
  ├─ executor (no LLM decides IF / HOW)                                 planner.py
  │    candidate catalog → ≤6 combinations × permutations → full-chain validator
  ├─ providers: OSM place index, TfNSW trips, synthetic replay          places.py, providers.py, fixtures.py
  └─ evaluation: frozen scenarios, brute-force reference, simulated users   reference.py, personas.py, harness.py
```

## The boundary that matters

The model never produces a time, a cost or a feasibility verdict. Every model decision is a JSON-schema structured reply with a fixed vocabulary (form/travel poles, one of 18 place kinds). The executor turns those decisions into a ranked list of real candidates from the place index, locks one of them into a request, and hands it to the deterministic planner. Whatever the model says, the worst outcome is a dull quest, never an unreachable one.

| The agent decides | The executor guarantees |
|---|---|
| Structured intent from one sentence and the clock | Schema validation; inferred fields are marked as inferred |
| Whether to probe a taste dimension or exploit | Only dimensions that passed `check_dimensions.py` on the pool; a probe only reorders candidates that already passed hard constraints |
| Which place kinds to search | Centre, radius and endpoints come from the request; a filter that removes everything the user asked for is undone (`keep_what_the_user_asked_for`) |
| Title and hook of the quest | Cited evidence ids must exist; claims about what the user said are stripped unless confirmed memory backs the round |
| How to map free-text feedback | It may not map onto a permanent ban; unmapped text stays session context |
| — | Route legs, stay windows, return deadline, walking and budget arithmetic, verdict |

Randomness lives in the executor: a seeded weighted draw among the top five candidates within one axis hit of the best. The seed is traced and fixed in tests and evals, so variety never trades away a dimension match and every round is reproducible.

A model error, a timeout (`AGENT_TIMEOUT`, 45 s inside the 60 s server deadline) or a round where every bet failed validation degrades to the fixed planner with a `degrade` trace step the UI shows.

## Request lifecycle

A run moves through `queued → running → completed | search_exhausted | needs_input | failed | cancelled | interrupted`. The update that stores a finished result requires the current state to still be `running`; this compare-and-swap prevents a cancelled or superseded request from being overwritten by a late result. Agent steps are streamed as they happen with the same guard, and an abandoned round aborts at its next step instead of writing. Startup marks abandoned `queued` and `running` rows as `interrupted`.

An itinerary independently has `verified`, `conditional`, or `infeasible` status. A search budget ending is a run status, not proof that the city has no solution.

## Planning and validation

The fixed policy ranks candidates by explicit preference tags and distance from the origin, chooses at most six candidate sets, and compares all orders for each small set. Coarse distance only chooses an order; feasibility uses provider legs for outbound, inter-stop and return travel.

The validator checks connection continuity, opening windows, event arrival buffer and full duration, minimum stays, return deadline, stop count, locks and exclusions, total walking, known/unknown cost, and evidence validity. Any failed check makes the itinerary infeasible; missing required evidence makes it conditional. Under `Request.venue_facts="advisory"`, used by the agent path, missing opening hours and venue evidence that does not cover the stay remain `unknown` checks marked `advisory`, listed in `Itinerary.advisories` and excluded from the verdict; a known closing time still fails.

`scripts/check_reference.py` enumerates every ordered combination through the same validator to measure whether the fixed planner misses feasible solutions. It measures search recall; it cannot catch validator bugs, which is why `evals/scenarios.json` asserts observable outcomes independently.

## Place discovery

`scripts/fetch_places.py` makes one Overpass batch request and `scripts/build_places.py` turns it into a read-only SQLite index. Retrieval is centred on the origin with a radius derived from the free window and capped per kind per half-radius ring, so both near and far options survive. OSM facts stay out of the verdict: `opening_hours` is used only to drop places a fully parsed tag shows shut for the whole window, and `fee` never becomes a cost. Kinds whose default stay cannot fit the window are dropped at retrieval.

## Taste and memory

Feedback is typed. Every `Reason` belongs to exactly one of seven scopes, enforced by an import-time assertion:

| Scope | Reasons | Effect |
|---|---|---|
| taste | want_sit / want_move / too_far / want_farther | moves one D1/D2 belief; evidence for memory |
| local | too_obvious / too_obscure | one-shot re-sort of that kind for the next round, then cleared |
| kind | not_this_kind | penalises a category for the session |
| stated | never_here | a user statement the caller confirms into a hard skip |
| constraint | no_spend / bad_time | patches the request |
| dedup | been_there | marks consumed; expresses no taste |
| session | other | model-mapped, or kept as session context |

Long-term memory is a list of typed `MemoryItem`s (`dimension`, `category`, `place`, `note`), each optionally scoped to a `Context` computed from the request clock (slot × window span). Every reroll and accept becomes an `Episode`; `Memory` only counts them:

- only `confirm()` creates an active item;
- a proposal needs weighted support in one context across at least two sessions with no contradiction there; a contradiction in another context is not a contradiction;
- a global item needs every context with evidence to be clean, and either two contexts ready on their own or pooled support with a reroll in at least two contexts (accepts alone never pool, because they mostly echo the ranker's defaults);
- a context item beats a global one on the same key; memory adjustments are capped at one dimension hit, so memory never overrules what the user says now;
- proposals are recomputed server-side and answered by signature, so a client cannot author one; forgetting is immediate and cascades to derived items.

Target priority in `agent.targets`: session feedback > what the user said now > recalled memory > inferred intent.

## Evidence and caching

Each fact includes a source URL, fetch timestamp, validity interval and a `synthetic` flag. Route cache keys include origin, destination and the exact departure instant; live keys also include the TfNSW adapter version and the departure rounded up to the provider's minute precision. A changed time therefore cannot reuse an old departure-specific route. The cache is persisted inside the parent run and may be passed to a revision.

The hook's "why now" comes from `moment.py`: computed, citable facts (sunset relative to the stops, the free window, each leg, and memory items the pick agrees with), not encyclopedia text.

## Provider boundary

The planner consumes a small `PlannerTools` contract: retrieve candidates, route a leg, record trace evidence, enforce a call budget and expose a revision cache. `ReplayTools` implements it with synthetic fixtures; `LiveTools` with `TfNSWRouteProvider`. `planner.catalog()` is the single switch for the candidate source, shared by planner and agent so a locked id is always found again.

The TfNSW adapter calls one fixed endpoint, selects the fastest journey departing at or after the requested minute, rejects school-bus-only options, and maps authentication, rate-limit, timeout, schema and no-route failures to typed errors. It never treats an unavailable Opal fare as zero. `llm.py` is the only file that knows the model provider: JSON-schema structured output over `httpx`, a per-round decision budget, and typed errors mirroring the providers.

## Evaluation

1. **Frozen scenarios** (`evals/scenarios.json`, 16 dev + 8 holdout) run in CI with expectations outside validator internals.
2. **Reference planner** measures the fixed planner's search recall.
3. **Simulated users** (`evals/personas.json`, 12 personas, 5 holdout): hidden structured taste, a deterministic judge, and multi-session runs where only memory carries over. Six groups (`fixed`, `no_memory`, `last_feedback`, `flat_profile`, `typed_no_context`, `typed_context`) differ only in what they may store. `RuleModel` stands in for the LLM for offline, deterministic runs; reports carry raw numerators and denominators.

## State and privacy

The browser receives a random HTTP-only, SameSite=Strict session cookie. Every run, taste session and memory read or write is owner-scoped. Geolocation is requested only after the user clicks the location control and is clamped to the Sydney area. Episodes store a context bucket and candidate ids, not coordinates. Users can inspect and delete memory items and episodes, or pause recording. API keys live in the git-ignored `.env` and only ever go into the `Authorization` header.
