# Architecture

```text
React / TypeScript
  └─ POST /api/runs → SSE progress → itinerary / trace
FastAPI
  ├─ request lifecycle and session ownership
  ├─ deterministic fixed-v1 planner
  ├─ full-chain validator
  ├─ ReplayTools / LiveTools provider contract
  ├─ TfNSW route adapter and time-sensitive cache
  └─ SQLite document store
```

## Request lifecycle

A run moves through `queued → running → completed | search_exhausted | needs_input | failed | cancelled`. The database update that stores a finished result requires the current state to still be `running`; this compare-and-swap prevents a cancelled or superseded request from being overwritten by a late result. Startup marks abandoned `queued` and `running` rows as `interrupted`.

An itinerary independently has `verified`, `conditional`, or `infeasible` status. A search budget ending is a run status, not proof that the city has no solution.

## Planning and validation

The fixed policy ranks candidates by explicit preference tags and distance from the selected or browser-provided origin, chooses at most six candidate sets, and compares all orders for each small set. Coarse distance is only used to choose an order. Final feasibility uses provider legs for outbound, inter-stop and return travel.

The validator checks connection continuity, opening windows, event arrival buffer and full duration, minimum stays, return deadline, stop count, locks and exclusions, total walking, known/unknown cost, and evidence validity. Budget covers tickets and activities by default; transport joins that constraint only when explicitly selected. Any failed hard check makes the itinerary infeasible. Missing required evidence makes it conditional. The exception is `Request.venue_facts="advisory"`, used by the agent path: missing opening hours and venue evidence that does not cover the stay remain `unknown` checks marked `advisory`, are surfaced in `Itinerary.advisories`, and are excluded from the verdict, so the status reflects the time budget and known conflicts only.

## Evidence and caching

Each fact includes a source URL, fetch timestamp and validity interval. All fixture evidence has `synthetic: true`. Route cache keys include origin, destination, and exact departure instant. A changed time therefore cannot reuse an old departure-specific route.

The current cache is persisted inside the parent run and may be passed to a revision. Only exact keys with covering validity periods are reused. Live keys include the TfNSW adapter version, endpoints and departure rounded up to the provider's minute precision.

## State and privacy

The browser receives a random HTTP-only, SameSite=Strict session cookie. Every run and memory query requires this owner ID. Geolocation is requested only after the user clicks the location control. Accepted coordinates are limited to the supported Sydney area and are stored with the local run so revisions preserve their origin. There is no account system or cross-device sync. Users can inspect and delete their explicit memory records.

SQLite uses one JSON document table because the Replay domain is deliberately small. The composite index `(owner, kind, created_at)` matches history and memory queries. A Live implementation should move candidate/evidence relationships into normalized tables.

## Provider boundary

The planner consumes a small `PlannerTools` contract: retrieve candidates, route a leg, record trace evidence, enforce a call budget and expose a revision cache. `ReplayTools` implements it with synthetic fixtures. `LiveTools` implements it with an M0-curated venue allowlist and `TfNSWRouteProvider`.

The TfNSW adapter calls one fixed endpoint. It selects the fastest journey that departs at or after the requested minute, rejects school-bus-only options, uses planned times for validation, and maps authentication, rate-limit, timeout, schema and no-route failures to typed errors. It never treats unavailable Opal fare as zero. The Open-Meteo endpoint remains outside itinerary verification.

Each accepted itinerary includes a Google Maps Directions URL containing the origin, ordered stops, return destination and selected travel mode. This is a user handoff for viewing and navigation; it is not treated as machine-readable validation evidence.
