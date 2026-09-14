export type Origin = 'townhall' | 'central' | 'circular' | 'current';
export interface PlanRequest {
  origin_id: Origin; origin_lat: number | null; origin_lon: number | null;
  departure: string; deadline: string; preference: string;
  max_walk_minutes: number; budget_aud: number | null; include_transport_cost: boolean; max_stops: number;
  locked_ids: string[]; excluded_ids: string[]; stay_minutes: Record<string, number>;
  mode: 'replay' | 'live';
  catalog: 'osm'; venue_facts: 'advisory';
}
export interface Evidence { id: string; field: string; value: string; source_url: string; fetched_at: string; valid_from: string; valid_until: string; synthetic: boolean }
export interface Candidate {id: string; name: string; category: string; description: string; lat: number; lon: number; tags: string[]; cost: number | null; evidence: Evidence[]; event: boolean}
export interface Stop { candidate: Candidate; arrival: string; start: string; end: string; stay_minutes: number; wait_minutes: number; locked: boolean; stay_basis: string }
export interface Leg {origin: string; destination: string; departure: string; arrival: string; minutes: number; walking_minutes: number; fare_aud: number | null; mode: 'walk' | 'transit'; evidence: Evidence}
export interface Check {name: string; status: 'pass' | 'fail' | 'unknown'; detail: string; evidence_ids: string[]}
export interface Itinerary {id: string; title: string; reason: string; stops: Stop[]; legs: Leg[]; checks: Check[]; status: 'verified' | 'conditional'; return_at: string; total_minutes: number; walking_minutes: number; known_cost: number; unknowns: string[]; advisories: string[]; map_url: string}
export interface Trace {sequence: number; node: string; action: string; summary: string; elapsed_ms: number; cache_hit: boolean}
export interface Result {request: PlanRequest; itineraries: Itinerary[]; trace: Trace[]; rejected: {candidates: string[]; reasons: string[]}[]; status: string; message: string; tool_calls: number; cache_hits: number; elapsed_ms: number; data_notice: string; strategy: string; agent?: AgentInfo}
export interface AgentInfo {anchor_id: string | null; memory_ids: string[]; evidence_ids: string[]; probe: {dimension: string | null; pole: number; mode: string; summary: string} | null; intent: {summary: string; inferred: string[]} | null; seed: number | null; model_calls: number; tokens: number}
export interface Run {id: string; status: string; result: Result | null}
export type Reason = 'want_sit' | 'want_move' | 'too_far' | 'want_farther' | 'too_obvious' | 'too_obscure' | 'not_this_kind' | 'never_here' | 'no_spend' | 'bad_time' | 'been_there' | 'other';
export interface Proposal {signature: string; action: 'add' | 'narrow' | 'retire'; text: string; evidence: number}
export interface MemoryItem {id: string; kind: 'dimension' | 'category' | 'place' | 'note'; text: string; context: string | null; source: 'user_stated' | 'agent_proposed'; evidence: number; stale: boolean; confirmed_at: string}
export interface Episode {id: string; event: 'reroll' | 'accept'; reason: string; context: string; counts: boolean; created_at: string}
export interface Taste {items: MemoryItem[]; proposals: Proposal[]; episodes: Episode[]; incognito: boolean; session: Record<string, number>}
export interface RerollResponse {id: string | null; status?: string; clarify: 'time' | null; message: string; proposal: Proposal | null}
export interface Session {now: string; today: string; tomorrow: string; origins: Record<Exclude<Origin, 'current'>, {name: string; lat: number; lon: number}>; mode: 'replay'; available_modes: Array<'replay' | 'live'>; agent_available: boolean}
