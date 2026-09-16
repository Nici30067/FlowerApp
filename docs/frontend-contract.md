# Frontend interaction contract

The contract between the browser UI (`travel_agent/web/`) and the backend in this repository (`travel_agent/api.py`,
`store.py`, `coordinator.py`, `agents.py`, `flower_backend.py`, `schemas.py`, `intake.py`). Every shape below was
read from that code and checked against a live `TestClient` run in fixture mode on 16 September 2026; the JSON
examples are real responses with only IDs shortened.

The planner builds **one day**. There is no `days` field anywhere in the API any more (see the last section).

Conventions: distances are meters, durations are seconds, money is minor units (cents) in the request currency,
timestamps are ISO-8601 with a UTC offset in the trip's timezone. Every request and response model is
`extra="forbid"`: an unknown key in a request body is a `422`.

---

## 1. HTTP endpoints

All `/api/*` routes are JSON. Error bodies are `{"detail": "<text>"}`; FastAPI validation errors are
`{"detail": [{"type", "loc", "msg", "input", ...}]}` with status `422`.

| Method | Path | Purpose | Success | Errors |
| --- | --- | --- | --- | --- |
| GET | `/health` | liveness | `{"status": "ok", "version": "0.3.1"}` | |
| GET | `/api/config` | server capabilities and defaults | object, section 6 | |
| POST | `/api/login` | exchange the operator token for a session cookie | `{"ok": true}` | 401 |
| GET | `/api/geocode?q=` | resolve a place name | object, section 7 | 400, 404, 503 |
| POST | `/api/intake` | one conversational intake turn | object, section 8 | 422 |
| POST | `/api/trips` | create a trip and queue the initial plan | `{"trip_id", "job"}` | 400, 422 |
| GET | `/api/trips` | 30 newest trips | `[{"id", "revision", "title"}]` | |
| GET | `/api/trips/{trip_id}` | committed trip plus pending proposals | `{"trip", "proposals"}` | 404 |
| GET | `/api/trips/{trip_id}/export` | download the committed `TripSnapshot` | JSON attachment | 404 |
| POST | `/api/trips/import` | validate and store a `TripSnapshot` | `{"trip_id"}` | 400, 422 |
| POST | `/api/trips/{trip_id}/events` | request a revision | `{"trip_id", "job"}` | 400, 404, 409, 422 |
| GET | `/api/jobs/{job_id}` | job status | job object, section 3 | 404 |
| GET | `/api/jobs/{job_id}/events` | Server-Sent Events for one job | `text/event-stream`, section 4 | 404 |
| POST | `/api/proposals/{proposal_id}/accept` | commit a pending proposal | the new committed `TripSnapshot` | 404, 409 |
| POST | `/api/proposals/{proposal_id}/reject` | discard a pending proposal | `{"ok": true}` | 404, 409 |
| POST | `/internal/jobs/{job_id}/claim`, `/events`, `/result`, `/failure` | bridge-mode SuperGrid workers only | | 403, 409 |
| GET | `/`, `/static/*` | the web UI | | |

### Authentication and transport rules (middleware, every request)

* **Loopback only unless a token is set.** Without `TRAVEL_API_TOKEN` any request whose `Host` is not
  `127.0.0.1`, `localhost` or `::1` gets `403 {"detail": "Configure TRAVEL_API_TOKEN before remote access"}`.
* **Token mode** (`/api/config` reports `"auth_required": true`): every `/api/*` route except `/api/config` and
  `/api/login` needs `Authorization: Bearer <token>` or the `travel_session` cookie, otherwise
  `401 {"detail": "API authentication required"}`. `POST /api/login {"token": "..."}` sets the cookie
  (`HttpOnly`, `SameSite=Strict`, 8 hours; `Secure` on HTTPS). The UI opens its login dialog on any 401.
* **Writes** (`POST`, `PUT`, `PATCH`, `DELETE`): an `Origin` header that differs from `Host` is
  `403 "Cross-origin writes are disabled"`; a body over 1 MB is `413`.
* **CSP**: `default-src 'self'; script-src 'self' https://unpkg.com; style-src 'self' 'unsafe-inline'
  https://unpkg.com; img-src 'self' data: https:; connect-src 'self'`. The frontend may only `fetch` and open
  `EventSource` against its own origin; Leaflet may come from unpkg; map tiles may come from any HTTPS host.

### Trip creation

`POST /api/trips` body: `{"request": TripRequest}`. `request` is optional and every field has a default (the
Berlin demonstration), so `{}` creates the default trip. The response is the new trip ID and the queued job:

```json
{"trip_id": "0cc7c9b21b2e4c30a9bb920f2de8571a",
 "job": {"id": "0cce1f058ee74a9e8e4302c3a1b95340", "trip_id": "0cc7c9b21b2e4c30a9bb920f2de8571a",
         "base": 0, "status": "queued", "error": null, "proposal_id": null, "external": 0}}
```

A request that violates a bound is `422`, for example an end before the start:
`{"detail": [{"type": "value_error", "loc": ["body", "request"], "msg": "Value error, Use one planning period of
more than zero and at most 18 hours", ...}]}`.

`TripRequest` fields and bounds (`schemas.py`):

| Field | Type / bound | Default |
| --- | --- | --- |
| `title` | 1..120 chars | `"A day in Berlin"` |
| `city` | up to 80 chars | `"Berlin"` |
| `timezone` | IANA name (validated) | `"Europe/Berlin"` |
| `start`, `end` | tz-aware; `0 < end - start <= 18 h` | `2026-09-16T10:00:00+02:00`, `17:00` |
| `origin`, `destination` | `{"lat": -85..85, "lon": -180..180}` | central Berlin `52.5225, 13.4024` |
| `interests` | list of tags: `art architecture parks coffee history food books shopping` | `["art","architecture","parks","coffee"]` |
| `currency` | `EUR USD GBP SEK` | `EUR` |
| `budget_minor` | 0..1 000 000 | 6000 |
| `max_walking_m` | 0..50 000 | 5000 |
| `max_continuous_walking_s` | 60..14 400 | 1800 |
| `break_duration_s` | 300..7200 | 900 |
| `min_stops` / `target_stops` | 0..8 / 1..8, `min_stops <= target_stops` | 2 / 5 |
| `rain_threshold_pct` | 0..100 | 60 |
| `avoid_rain_outdoor_visits` | bool | true |
| `transport_mode` | `walking` or `cycling` | `walking` |
| `require_verified_facts` | bool; turns the "unknown/unverified" warnings into errors | false |
| `required_place_ids` | up to 8, disjoint from excluded | `[]` |
| `excluded_place_ids` | up to 100 | `[]` |
| `reservations` | up to 8 `{place_id, start, duration_s 300..14400}`, unique per place | `[]` |
| `max_candidates` | 3..16 | 12 |

### Revisions: `POST /api/trips/{trip_id}/events`

Body: `{"base_revision": <int>, "event": TripEvent}` where `TripEvent` is
`{"id": "^[A-Za-z0-9_-]{1,100}$", "kind": ..., "payload": {...}, "simulated": false}`.

* `base_revision` must equal the trip's current `revision`, otherwise
  `409 "Refresh the current trip revision before requesting changes"`.
* The event is applied to a copy of the trip before queueing. A payload the coordinator rejects is `422` with the
  reason as `detail` (for example `"Unknown place ID"`, `"Rain scenario injection must be labeled simulated"`).
* **Idempotent by `event.id`.** Re-posting the same id with the same content returns the original job again
  (status `200`, whatever state that job is in). The same id with different content is
  `409 "An event ID was reused with different content"`. The UI generates a fresh id per click.
* The backend does not refuse a second event while a proposal is pending; the UI must gate this itself
  (the pending proposal would become stale once anything else commits).

| `kind` | `payload` | Notes |
| --- | --- | --- |
| `rain` | `{}` | `simulated` must be `true`; installs a 95 % rain override from now to the end of the day |
| `weather_updated` | `{}` (anything else is 422) | clears the override and re-reads the forecast provider |
| `pace_changed` | `{"max_walking_m": int, "max_continuous_walking_s"?: int}` | |
| `budget_changed` | `{"budget_minor": int}` | |
| `place_unavailable` | `{"place_id": str}` | must be a known place; appended to `closed_place_ids` |
| `lock_stop` / `unlock_stop` | `{"place_id": str}` | the stop must be in the current itinerary; lock becomes a reservation |
| `stop_completed` | `{"place_id", "actual_spent_minor": int >= 0, "completed_at": ISO tz-aware}` | must be the next uncompleted stop, time inside the day and not before `progress.now` |
| `user_running_late` | `{"now": ISO tz-aware, "location": {"lat", "lon"}}` | `now` must move forward and stay inside the day |
| `preferences_changed` | any subset of `interests max_walking_m budget_minor max_continuous_walking_s break_duration_s target_stops min_stops rain_threshold_pct avoid_rain_outdoor_visits transport_mode origin destination required_place_ids excluded_place_ids` | other keys are 422 |

Changing `city`, `timezone`, `start` or `end` is not an event: create a new trip.

### Review: accept and reject

`POST /api/proposals/{id}/accept` commits `proposal.proposed` as the new trip revision and returns that
`TripSnapshot`. It re-validates independently first; an infeasible proposal is
`409 "An infeasible proposal cannot be committed"`, a proposal that is no longer pending is
`409 "Proposal is no longer pending"`, and one whose base revision no longer matches is
`409 "Stale proposal: refresh the current trip before retrying"`. `reject` returns `{"ok": true}` or the same
"no longer pending" 409. Both move the job to `completed`.

### Read, export, import

`GET /api/trips/{id}` returns `{"trip": TripSnapshot, "proposals": [Proposal, ...]}` where `proposals` holds only
**pending** proposals (newest first, at most 10). A trip whose initial plan was auto-accepted therefore has an
empty list. `GET /api/trips/{id}/export` returns the same `TripSnapshot` with
`Content-Disposition: attachment; filename="itinerary-<first 12 chars of id>.json"`. `POST /api/trips/import`
accepts a `TripSnapshot` body (at most 24 places and 8 stops, itinerary must validate) and assigns a new id.

---

## 2. Fixture, live, local, SuperGrid

The server runs in one of these combinations, reported by `/api/config`:

| `data_mode` | `agent_mode` | `execution_backend` | Meaning |
| --- | --- | --- | --- |
| `fixture` | `rules` | `local` | offline demo: bundled central-Berlin catalog, synthetic weather and direct-line routes, deterministic specialists, zero model calls |
| `fixture` or `live` | `model` | `local` | four model specialists through `TRAVEL_MODEL_BASE_URL` (Responses API or the Chat Completions adapter) |
| `live` | either | `local` | any geocodable city: Overpass places, Open-Meteo forecast, OSRM or OpenRouteService routing |
| `fixture` | `model` | `flower` | the job runs as a Flower AgentApp on SuperGrid (`flower_mode: "control"`); live data is never used there |

Provenance is carried on the data itself, never inferred from the mode: every `Evidence.status`,
`Leg.source_status`, `Place.cost_status` and `Place.opening_status` says where a fact came from.

---

## 3. Jobs and their lifecycle

Job object (`GET /api/jobs/{id}` and the `job` in create/event responses):

```json
{"id": "f6e68daa6021470880302cda96bff7f1", "trip_id": "0cc7c9b21b2e4c30a9bb920f2de8571a", "base": 1,
 "status": "awaiting_review", "error": null, "proposal_id": "731c060cf5d545c4abe3f5c67ca97453", "external": 0}
```

* `base` is the trip revision the job plans from. `external` is `1` only for bridge-mode SuperGrid jobs; the
  creation response then also carries `flower_command` (a `uv run flwr run ...` line with a private, 30-minute
  job credential) and `capability_notice`.
* `error` is a short, redacted message (at most 350 characters) when `status` is `failed`.

```text
queued ──► running ──► awaiting_review ──► completed        (accept or reject)
                  └──► failed                                (error set, job.failed event)
```

* The **initial plan** (`base` 0, no event) is auto-accepted when its itinerary is valid: the job goes
  `awaiting_review -> completed` immediately and the trip becomes revision 1. If the initial plan is infeasible
  the job stays `awaiting_review`, the trip stays at revision 0 with `itinerary: null`, and the proposal is listed
  under `proposals` so the UI can show why (its `validation.issues`); `accept` would be a 409.
* **Every later revision** waits in `awaiting_review` until the user accepts or rejects it.
* A job is `failed` when the coordinator raised (provider outage, budget exhausted, model returned no valid
  report, SuperGrid rejected the run). The trip is unchanged.

The frontend should treat `queued` and `running` as "busy" and everything else as terminal.

---

## 4. The event stream: `GET /api/jobs/{job_id}/events`

`Content-Type: text/event-stream`, `Cache-Control: no-cache`. Open it with `EventSource` right after the job is
created; it works for finished jobs too (the stored history is replayed, then `done`).

### Framing

```text
id: 5
data: {"seq": 5, "type": "agent.completed", "data": {...}}

: keepalive

event: done
data: {"status": "completed"}
```

* Planning events are **unnamed** (`onmessage`). `id:` equals `data.seq`, a 1-based sequence per job. The JSON
  envelope is always `{"seq": int, "type": string, "data": object}`.
* The server polls the store every 200 ms, sends up to 200 stored events per poll, then a `: keepalive` comment.
* When the job leaves `queued`/`running` the server sends one **named** event `done` with
  `{"status": "awaiting_review" | "completed" | "failed"}` and closes the stream. `done.status` is a snapshot
  taken between polls: for the auto-accepted initial plan it may read `awaiting_review` a few milliseconds before
  `completed`, and `job.finished` / `job.failed` may not have been streamed yet. **After `done`, always
  `GET /api/jobs/{id}` for the final `status`, `proposal_id` and `error`, and `GET /api/trips/{trip_id}` for the
  trip and pending proposals.** That is what `watchJob()` does today.
* **Resume**: the server starts after `max(?since=N, Last-Event-ID)`. `EventSource` reconnects automatically and
  sends `Last-Event-ID`, so a dropped connection resumes without duplicates. The server also closes a stream
  after roughly 15 minutes (4500 polls) without `done`; the browser reconnects with the cursor and nothing is lost.
* `onerror` fires on any close, including the normal one after `done`; the UI must check the job status before
  treating it as a failure.

### Event catalogue

Events are emitted in this order for one planning step. The SuperGrid path relays the same kinds (unwrapped from
`travel.*` run events) plus the `flower.*` kinds. `data` keys are listed exhaustively.

| `type` | `data` | Emitted |
| --- | --- | --- |
| `flower.submitting` | `fab_hash` (8 hex), `model`, `federation` | SuperGrid only, before the run is billed |
| `flower.run.started` | `run_id` (string), `federation`, `model`, `note` | SuperGrid only |
| `coordinator.started` | `base_revision`, `data_mode`, `agent_mode`, `trigger` (`initial_plan` or the event kind), `simulated` | first event of every step |
| `evidence.loaded` | `place_count`, `weather_status` (`fixture live cached unknown`), `data_mode` | after places and forecast are fetched |
| `agent.started` | `role`, `engine` (`rules` or `model`), `model` (string or null) | once per specialist run |
| `tool.completed` | `role`, `name`, `engine` | per tool read (rules: exactly one per role) |
| `tool.failed` | `role`, `name`, `error` | model requested a disallowed or malformed tool call |
| `model.completed` | `role`, `requested_model`, `returned_model`, `elapsed_s`, `call_number`, `usage` (`{input_tokens, output_tokens, reasoning_tokens}` or null; each may be null) | every model call |
| `model.incomplete` | `role`, `call_number`, `max_output_tokens` | the answer was cut off at the output limit; the next call doubles it |
| `model.repair` | `role`, `reason` | the final answer was not a valid report; one bounded repair turn follows |
| `agent.completed` | `role`, `engine`, `model` (model runs only), `report` (`AgentReport`), `unknown_evidence_ids` | once per specialist run |
| `collaboration.message` | `AgentMessage`: `sender`, `recipient`, `kind` (`finding request proposal validation`), `summary`, `place_ids`, `evidence_ids` | each specialist request, and the coordinator's closing `proposal` to `user` |
| `planner.fallback` | `reason`, `candidate_count` | the specialists' ranking was empty or infeasible and the catalog order was used; reasons: `no specialist candidates`, `no unavoided specialist candidates`, `specialist ranking infeasible`, `avoided places reconsidered` |
| `planner.started` | `algorithm` (`bounded_beam_search`), `beam_width` (64), `candidate_count`, `avoided_count`, `ranking_source` (`specialists` or `catalog`) | before the search |
| `planner.repair` | `adopted` (bool), `reason`, `proposal_score`, `alternative_score` | only when Budget & pace asked for alternatives; reasons: `alternative adopted`, `alternative infeasible`, `alternative identical to the proposal`, `alternative scored lower` |
| `validation.completed` | `Validation`: `valid`, `status`, `issues[]` | after the final schedule is rebuilt on directions metrics |
| `proposal.ready` | `proposal_id`, `added[]`, `removed[]`, `preserved[]`, `metrics` | last coordinator event |
| `flower.run.finished` | `run_id`, `status` (`completed`), `relayed`, `metrics`, `listener_errors` | SuperGrid only |
| `flower.stream.reconnecting` | `attempt` (1..3), `delay_s` (2, 4, 6), `after_task_event_id`, `error` (exception class name) | SuperGrid only, the run-event stream dropped and is being resumed |
| `job.finished` | `job_id`, `proposal_id`, `metrics` | the job reached `awaiting_review` or `completed` |
| `job.failed` | `error` | the job failed; same text as the job's `error` |

`metrics` is `{"model_calls": int, "tool_calls": int, "provider_calls": int, "elapsed_s": float}`: model calls are
specialist requests to the model runtime, tool calls are model-initiated evidence reads (the rules runner counts
one per role), provider calls are the coordinator's own routing and weather requests. On SuperGrid the figures are
the AgentApp's own budget, relayed. `job.finished.metrics` always contains all three counters.

Specialist order in a step: when the forecast is already wet, `conditions` first and then `discovery`
(consuming the indoor request); otherwise `discovery` and `conditions` together, and `discovery` a second time
only if `conditions` sent it a request. Then `mobility`, then the search, then `budget_pace`.

### Worked stream (fixture, rules, initial plan, 19 events)

```text
id: 1  coordinator.started   {"base_revision": 0, "data_mode": "fixture", "agent_mode": "rules", "trigger": "initial_plan", "simulated": false}
id: 2  evidence.loaded       {"place_count": 12, "weather_status": "fixture", "data_mode": "fixture"}
id: 3  agent.started         {"role": "discovery", "engine": "rules", "model": null}
id: 4  tool.completed        {"role": "discovery", "name": "search_places", "engine": "rules"}
id: 5  agent.completed       {"role": "discovery", "engine": "rules", "report": {...}, "unknown_evidence_ids": []}
id: 6-8   conditions (started, tool get_weather_forecast, completed)
id: 9-11  mobility   (started, tool get_route_matrix, completed)
id: 12 planner.started       {"algorithm": "bounded_beam_search", "beam_width": 64, "candidate_count": 12, "avoided_count": 0, "ranking_source": "specialists"}
id: 13-15 budget_pace (started, tool validate_itinerary, completed)
id: 16 validation.completed  {"valid": true, "status": "provisional", "issues": [{"code": "PRICE_UNVERIFIED", "severity": "warning", "message": "Cost is an estimate or fixture", "place_id": "demo-coffee"}, ..., {"code": "SYNTHETIC_ROUTES", "severity": "warning", "message": "Travel times and direct-line geometry are synthetic demonstration values", "place_id": null}]}
id: 17 collaboration.message {"sender": "coordinator", "recipient": "user", "kind": "proposal", "summary": "Proposed revision 1; 5 stops; status provisional.", "place_ids": ["demo-coffee", "demo-courtyards", "demo-bookshop", "demo-gallery", "demo-forum"], "evidence_ids": []}
id: 18 proposal.ready        {"proposal_id": "c6210b1a...", "added": ["demo-coffee", "demo-courtyards", "demo-bookshop", "demo-gallery", "demo-forum"], "removed": [], "preserved": [], "metrics": {"model_calls": 0, "tool_calls": 4, "provider_calls": 1, "elapsed_s": 0.112}}
id: 19 job.finished          {"job_id": "0cce1f05...", "proposal_id": "c6210b1a...", "metrics": {"model_calls": 0, "tool_calls": 4, "provider_calls": 1, "elapsed_s": 0.116}}
event: done                  {"status": "completed"}
```

A full `agent.completed` payload:

```json
{"seq": 5, "type": "agent.completed", "data": {
  "role": "discovery", "engine": "rules",
  "report": {"role": "discovery",
             "summary": "Ranked 12 catalog candidates against stated interests.",
             "candidate_ids": ["demo-bookshop", "demo-coffee", "demo-courtyards", "demo-forum", "demo-gallery", "..."],
             "avoid_ids": [],
             "evidence_ids": ["fixture-demo-monbijou", "fixture-demo-museum-walk", "..."],
             "requests": []},
  "unknown_evidence_ids": []}}
```

A rain revision shows the handoff. `conditions` runs first, lists the outdoor places in `avoid_ids`, and asks
`discovery` for indoor alternatives; the request is echoed as a `collaboration.message`; `discovery` reports that
it consumed it; `planner.started` then shows `candidate_count: 7, avoided_count: 5`:

```json
{"seq": 6, "type": "collaboration.message", "data": {
  "sender": "conditions", "recipient": "discovery", "kind": "request",
  "summary": "Return indoor alternatives matching the affected activities' interests.",
  "place_ids": ["demo-monbijou", "demo-museum-walk", "demo-courtyards", "demo-bebelplatz", "demo-alexanderplatz"],
  "evidence_ids": ["scenario-rain-1"]}}
```

Model-mode extras, in the order they appear inside one specialist run:

```json
{"type": "model.completed", "data": {"role": "discovery", "requested_model": "gemini-2.5-flash", "returned_model": "gemini-2.5-flash",
                                     "elapsed_s": 4.21, "call_number": 1,
                                     "usage": {"input_tokens": 3120, "output_tokens": 410, "reasoning_tokens": null}}}
{"type": "model.incomplete", "data": {"role": "discovery", "call_number": 1, "max_output_tokens": 2000}}
{"type": "model.repair",     "data": {"role": "discovery", "reason": "final output was truncated at 2000 output tokens"}}
{"type": "tool.failed",      "data": {"role": "discovery", "name": "get_route_matrix", "error": "Requested tool is outside this specialist's allowlist"}}
{"type": "planner.repair",   "data": {"adopted": false, "reason": "alternative scored lower", "proposal_score": 16.661, "alternative_score": 15.02}}
```

SuperGrid extras:

```json
{"type": "flower.submitting",          "data": {"fab_hash": "3f9a1c2b", "model": "flower-endeavor-v1.0", "federation": "account default"}}
{"type": "flower.run.started",         "data": {"run_id": "184213", "federation": "@tauska67/default", "model": "flower-endeavor-v1.0", "note": ""}}
{"type": "flower.stream.reconnecting", "data": {"attempt": 1, "delay_s": 2, "after_task_event_id": 41, "error": "RpcError"}}
{"type": "flower.run.finished",        "data": {"run_id": "184213", "status": "completed", "relayed": 63,
                                                "metrics": {"model_calls": 8, "tool_calls": 0, "provider_calls": 3, "elapsed_s": 142.7}, "listener_errors": 0}}
```

Failure:

```json
{"type": "job.failed", "data": {"error": "ProviderError: routing.openstreetmap.de request failed (503)"}}
```

---

## 5. Trip, proposal and itinerary JSON

### `TripSnapshot` (the `trip` in `GET /api/trips/{id}`, the export, and `proposal.proposed`)

```json
{"id": "0cc7c9b21b2e4c30a9bb920f2de8571a", "revision": 1,
 "request": {TripRequest},
 "places": [Place, ...],
 "weather": Weather,
 "itinerary": Itinerary,
 "progress": {"now": null, "location": null, "spent_minor": 0, "completed_place_ids": []},
 "closed_place_ids": [],
 "weather_override": null,
 "reports": [AgentReport, ...],
 "messages": [AgentMessage, ...],
 "data_mode": "fixture", "agent_mode": "rules"}
```

* `revision` starts at 0; `itinerary` is `null` until a plan is committed.
* `weather_override` is set by a `rain` event (source `"provider": "User-injected weather scenario"`,
  `"note": "Simulated event, not an observed forecast update."`) and cleared by `weather_updated`. Its presence
  means "simulated scenario"; the UI shows that label.
* `reports` and `messages` are the specialist outputs of the step that produced this revision; they are what the
  review panel reads after a reload (the SSE feed is gone by then).
* `progress` advances only through `stop_completed` and `user_running_late`.

### `Itinerary` (single day)

```json
{"stops": [Stop, ...], "legs": [Leg, ...], "breaks": [Break, ...],
 "end_arrival": "2026-09-17T14:27:21+02:00",
 "walking_m": 2327, "travel_duration_s": 1941, "cost_minor": 900, "unknown_cost_count": 0,
 "validation": {"valid": true, "status": "provisional", "issues": [Issue, ...]},
 "score": 16.661}
```

* `legs.length == stops.length + 1`: `legs[i]` leads into `stops[i]` (`legs[0].from_id` is `"origin"`), the last
  leg goes to `"destination"`. `end_arrival` is arrival at the destination.
* `breaks` are inserted after every second activity; `location_id` is the place where the break happens and it
  starts at or after that stop's `end`.
* `cost_minor` counts known prices plus `progress.spent_minor`; `unknown_cost_count` stops have `cost_minor: null`.
* An infeasible result is `{"stops": [], "legs": [], "breaks": [], "end_arrival": <start>, ..., "validation":
  {"valid": false, "status": "infeasible", "issues": [{"code": "NO_FEASIBLE_PLAN", ...}]}}`.

```json
Stop  {"place_id": "demo-coffee", "name": "Hackescher Markt coffee stop",
       "arrival": "2026-09-17T10:00:46+02:00", "start": "2026-09-17T10:00:46+02:00", "end": "2026-09-17T10:30:46+02:00",
       "cost_minor": 900, "cost_status": "fixture", "locked": false, "completed": false, "reason": "Matches coffee"}
Leg   {"from_id": "origin", "to_id": "demo-coffee", "distance_m": 55, "duration_s": 46, "mode": "walking",
       "geometry": [[13.4024, 52.5225], [13.402, 52.5228]], "source_status": "fixture", "evidence_id": "fixture-route-matrix"}
Break {"location_id": "demo-courtyards", "start": "2026-09-17T11:18:10+02:00", "end": "2026-09-17T11:33:10+02:00",
       "reason": "Scheduled break after two activities"}
Issue {"code": "PRICE_UNVERIFIED", "severity": "warning", "message": "Cost is an estimate or fixture", "place_id": "demo-coffee"}
```

`Leg.geometry` is GeoJSON order `[lon, lat]`. Fixture legs are two-point straight lines with
`source_status: "fixture"`; live legs carry full OSRM/ORS geometry with `osrm-route` / `ors-directions` evidence
(matrix-only legs, `osrm-table` / `ors-matrix`, have `geometry: []` and the `GEOMETRY_MISSING` warning).

### `Place`

```json
{"id": "demo-monbijou", "name": "Monbijou Park", "coordinate": {"lat": 52.5232, "lon": 13.3967},
 "categories": ["parks", "architecture"], "indoor": false, "visit_duration_s": 2400,
 "cost_minor": 0, "cost_status": "fixture", "currency": "EUR",
 "opening_hours": "08:00-20:00", "opening_intervals": [{"start": "...", "end": "..."}], "opening_status": "fixture",
 "source": {"id": "fixture-demo-monbijou", "provider": "Bundled synthetic fixture", "status": "fixture",
            "retrieved_at": "2026-09-17T10:00:00+02:00", "reference": "fixtures/berlin.json",
            "note": "Synthetic demonstration data. ..."},
 "osm_type": null, "osm_id": null, "website": null, "description": "Outdoor riverside visit in the demonstration."}
```

* `indoor` is `true`, `false` or `null` (unknown exposure). `cost_minor: null` means unknown price;
  `cost_status` is `fixture verified estimated unknown`; `opening_status` is `fixture tag unknown`, with
  `opening_intervals: null` when hours are unknown.
* Live places have ids `osm-<type>-<id>`, `source.provider: "OpenStreetMap via Overpass"`, `source.status`
  `live` or `cached`, and `source.reference` pointing at `https://www.openstreetmap.org/<type>/<id>`.
* `Evidence.status` is one of `fixture live cached user unknown`.

### `Weather`

`{"intervals": [{"start", "end", "precipitation_probability_pct": 10.0, "temperature_c": 21.0}, ...], "source": Evidence}`.
Fixture: hourly 10 % from `Synthetic weather`. Live: Open-Meteo hourly, `source.status` `live`/`cached`; a date
outside the 15-day horizon returns `intervals: []` with `status: "unknown"` (and `FORECAST_UNKNOWN` warnings).

### `AgentReport` and `AgentMessage`

```json
{"role": "conditions", "summary": "Reviewed forecast coverage and activity exposure; 5 candidates need rain-aware scheduling.",
 "candidate_ids": ["demo-bookshop", "..."], "avoid_ids": ["demo-monbijou", "demo-museum-walk", "demo-courtyards", "demo-bebelplatz", "demo-alexanderplatz"],
 "evidence_ids": ["scenario-rain-1"],
 "requests": [{"sender": "conditions", "recipient": "discovery", "kind": "request",
               "summary": "Return indoor alternatives matching the affected activities' interests.",
               "place_ids": ["demo-monbijou", "..."], "evidence_ids": ["scenario-rain-1"]}]}
```

`role` is `discovery conditions mobility budget_pace`; a message's `recipient` may also be `coordinator` or
`user`. Rankings drive the plan: the search only considers `mobility` + `discovery` `candidate_ids` minus every
report's `avoid_ids` (required, reserved and completed places are never avoided).

### `Proposal`

```json
{"id": "731c060cf5d545c4abe3f5c67ca97453", "trip_id": "0cc7c9b2...", "base_revision": 1, "trigger": "rain",
 "proposed": {TripSnapshot with revision 2},
 "added": ["demo-nationalgalerie"], "removed": ["demo-courtyards"],
 "preserved": ["demo-coffee", "demo-bookshop", "demo-gallery", "demo-forum"],
 "status": "pending"}
```

`trigger` is `initial_plan` or the event kind. `added`/`removed`/`preserved` compare stop place ids against the
committed itinerary. `status` is `pending accepted rejected`; only `pending` ones are listed on the trip.

### Validation issue codes

`Validation.status` is `valid` (no issues), `provisional` (warnings only, still committable) or `infeasible`
(at least one error; `valid: false`; cannot be accepted). `place_id` is set on per-stop findings.

| Code | Severity | Meaning |
| --- | --- | --- |
| `NO_FEASIBLE_PLAN` | error | the bounded search found nothing under the constraints |
| `DIRECTIONS_CONFLICT` | error | final directions metrics no longer fit the schedule |
| `DUPLICATE_STOP`, `MISSING_REQUIRED`, `TOO_FEW_STOPS`, `END_TIME`, `ROUTE_COUNT`, `COMPLETED_CHANGED`, `UNKNOWN_PLACE`, `UNAVAILABLE`, `TIME_ORDER`, `VISIT_END`, `ROUTE_LINK`, `TRAVEL_TIME`, `CLOSED`, `CURRENCY`, `COST_CHANGED`, `WEATHER_CONFLICT`, `RESERVATION_CHANGED`, `ENDPOINT`, `END_TRAVEL`, `TOTALS_MISMATCH`, `WALKING_LIMIT`, `BUDGET_LIMIT`, `TRANSPORT_MODE`, `CONTINUOUS_WALK` | error | hard constraint or consistency failures (messages are self-explanatory) |
| `HOURS_UNKNOWN`, `PRICE_UNKNOWN`, `PRICE_UNVERIFIED`, `FORECAST_UNKNOWN` | warning, **error when `require_verified_facts`** | unverified facts |
| `EXPOSURE_UNKNOWN` | warning | `indoor` is null |
| `GEOMETRY_MISSING` | warning | a leg has no route geometry |
| `SYNTHETIC_ROUTES` | warning | any leg is a fixture: times and lines are synthetic |
| `OUTDOOR_TRANSFERS` | warning | rain above the threshold somewhere in the day; walking between stops may be wet |

The UI groups issues by code and shows the messages; `SYNTHETIC_ROUTES` and the `PRICE_*`/`HOURS_UNKNOWN`
warnings are the honest-labelling signals and must stay visible.

---

## 6. `GET /api/config`

```json
{"data_mode": "fixture", "agent_mode": "rules", "execution_backend": "local",
 "flower_mode": null, "flower": null, "auth_required": false,
 "tile_url": "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
 "default_request": {TripRequest defaults, section 1},
 "fixture_notice": "Synthetic Berlin scenario. Prices, hours, weather and direct-line routes are unverified fixtures.",
 "live_notice": "Live OpenStreetMap data. Prices and hours stay unknown unless OSM tags verify them; routes via the configured router.",
 "geocoding": true, "router": null,
 "model": null, "model_api": null, "model_settings": null,
 "intake": true, "supported_cities": ["Berlin"]}
```

| Key | Values | Use in the UI |
| --- | --- | --- |
| `data_mode` | `fixture` / `live` | banner text (`fixture_notice` vs `live_notice`), map route label, whether "complete stop" times are simulated |
| `agent_mode` | `rules` / `model` (always `model` on the flower backend) | engine label ("0 model calls" vs the model name) |
| `execution_backend` | `local` / `flower` | show the SuperGrid panel and `flower.*` events |
| `flower_mode` | `control` / `bridge` / null | bridge shows the private `flower_command` |
| `flower` | null or `{connection, federation, model, run_timeout_s, max_tool_turns, reasoning_effort, max_model_calls, max_output_tokens, wall_time_s}` | SuperGrid details |
| `auth_required` | bool | probe `/api/trips` on boot and open the login dialog on 401 |
| `tile_url` | template | Leaflet tile layer |
| `default_request` | `TripRequest` | initial form values and the base every request is built on |
| `fixture_notice`, `live_notice` | text | the top banner; `live_notice` names the router (`OSRM` / `OpenRouteService`) |
| `geocoding` | always `true` | `/api/geocode` is available in both data modes |
| `router` | null / `osrm` / `ors` | live routing provider (null in fixture mode) |
| `model` | string or null | model ID for the label (`flower-endeavor-v1.0`, a Gemini ID, ...) |
| `model_api` | `responses` / `chat` / null | local model path only; informational |
| `model_settings` | null or `{max_tool_turns, reasoning_effort, max_model_calls, max_output_tokens, wall_time_s}` | bounds of one planning step; `wall_time_s` is the longest a job can run |
| `intake` | always `true` | `/api/intake` is available |
| `supported_cities` | `["Berlin"]` in fixture mode, **`null` in live mode** (any geocodable city) | intake hint text; never treat null as "no cities" |

---

## 7. `GET /api/geocode?q=<name>`

Resolves a typed place name through Open-Meteo geocoding (populated places first, then largest population). Works
in both data modes; results are cached for a day.

```json
{"query": "Lisbon", "name": "Lisbon", "country": "Portugal", "admin1": "Lisbon",
 "lat": 38.7167, "lon": -9.1333, "timezone": "Europe/Lisbon", "population": 517802,
 "fixture_supported": false,
 "source": {"provider": "Open-Meteo geocoding", "status": "live"}}
```

| Status | Body | When |
| --- | --- | --- |
| 200 | above | a match; `fixture_supported` is true within 10 km of central Berlin (the only place the fixture server can plan) |
| 400 | `{"detail": "Enter a place name of 2 to 80 characters"}` | query length |
| 404 | `{"detail": "No place matched that name"}` | no usable result; never a guessed location |
| 503 | `{"detail": "Geocoding service unavailable"}` | upstream failure or malformed record |

Use `name`, `timezone`, and `lat`/`lon` (as both `origin` and `destination`) when a user changes the city in the
form; the intake does this itself. Never put anything but the place name in `q`.

---

## 8. `POST /api/intake`

One conversational turn. The server is stateless: the client sends the brief back each turn.

Request (`IntakeTurn`):

```json
{"message": "tomorrow 10am to 5pm",
 "brief": {"city": "Berlin", "date": null, "start_time": null, "end_time": null, "timezone": null, "budget_minor": null,
           "interests": [], "max_walking_m": null, "transport_mode": null, "target_stops": null,
           "avoid_rain_outdoor_visits": null, "title": null},
 "history": [{"role": "user", "text": "Plan a day in Berlin"}, {"role": "assistant", "text": "What date would you like to travel?"}]}
```

* `message` at most 600 characters, `history` at most 20 turns of at most 600 characters each; `brief` may be `{}`
  on the first turn. Anything else is `422`.
* `TripBrief` fields: `city` (<= 80), `date` (`YYYY-MM-DD`), `start_time`/`end_time` (`HH:MM`), `timezone`,
  `budget_minor` (0..1 000 000), `interests` (<= 16 tags), `max_walking_m` (<= 50 000), `transport_mode`,
  `target_stops` (1..8), `avoid_rain_outdoor_visits`, `title`. No `days`.

Response:

```json
{"reply": "What are you interested in: art, history, parks, food, coffee, books, shopping, architecture?",
 "brief": {"city": "Berlin", "date": "2026-09-17", "start_time": "10:00", "end_time": "17:00", "timezone": null,
           "budget_minor": null, "interests": [], "max_walking_m": null, "transport_mode": null,
           "target_stops": null, "avoid_rain_outdoor_visits": null, "title": null},
 "missing": ["interests"], "ready": false, "request": null, "engine": "rules", "notes": []}
```

| Key | Meaning |
| --- | --- |
| `reply` | the assistant's next line: exactly one clarifying question, a city correction, or the "building now" confirmation. May be `""` only in edge cases; render nothing then. |
| `brief` | the updated brief; send it back verbatim next turn. The city is rewritten to the geocoder's canonical spelling (`"lisbon"` becomes `"Lisbon"`) and cleared to `null` when the name could not be found. |
| `missing` | ordered subset of `city`, `date`, `time_window`, `interests` |
| `ready` | `true` when the brief is complete **and** the city is plannable on this server and the day validates |
| `request` | a full `TripRequest` when `ready`, otherwise `null`. Post it as `{"request": ...}` to `/api/trips` unchanged. It already carries the geocoded `timezone`, `origin` and `destination` (in live mode) and layers the brief onto `default_request`. |
| `engine` | `rules`, or `model` when the local model enrichment changed the brief (local model mode only; SuperGrid and rules servers always say `rules`) |
| `notes` | zero or more user-facing explanations to show alongside the reply, each a complete sentence (see below). Show all of them, not just the last. |

Notes the server produces:

* Multi-day request: `"This planner builds one day at a time, so I will plan a single day of your 3-day trip."`
  (the brief has no day count; the day parsed is the one it plans).
* Fixture server, other city: `"Lisbon, Portugal is outside the bundled Berlin scenario, which this server plans
  from fixture data. Start scripts/serve_live.sh to plan other cities from live OpenStreetMap data."` with the
  reply `"I can only plan Berlin on this server. Want a Berlin day instead?"`.
* Unknown name: `"I couldn't find a place called 'Zzzzqqq'. Check the spelling or name the nearest larger city."`
  with the reply `"Which city did you mean? I couldn't find 'Zzzzqqq'."`.
* Lookup outage: `"I couldn't look up 'Porto' right now: the place lookup is unavailable. Try again in a moment,
  or say Berlin, which needs no lookup."`; once everything else is known the reply becomes
  `"I have everything except a confirmed location for Porto. Send any message to retry the lookup, or name
  another city."`.

A complete turn:

```json
{"reply": "Great, I have everything I need. Building your day in Berlin now.",
 "brief": {"city": "Berlin", "date": "2026-09-17", "start_time": "10:00", "end_time": "17:00", "timezone": null,
           "budget_minor": 6000, "interests": ["art", "coffee"], "max_walking_m": null, "transport_mode": null,
           "target_stops": null, "avoid_rain_outdoor_visits": null, "title": null},
 "missing": [], "ready": true,
 "request": {"title": "A day in Berlin", "city": "Berlin", "timezone": "Europe/Berlin",
             "start": "2026-09-17T10:00:00+02:00", "end": "2026-09-17T17:00:00+02:00",
             "origin": {"lat": 52.5225, "lon": 13.4024}, "destination": {"lat": 52.5225, "lon": 13.4024},
             "interests": ["art", "coffee"], "currency": "EUR", "budget_minor": 6000, "max_walking_m": 5000,
             "max_continuous_walking_s": 1800, "break_duration_s": 900, "min_stops": 2, "target_stops": 5,
             "rain_threshold_pct": 60, "avoid_rain_outdoor_visits": true, "transport_mode": "walking",
             "require_verified_facts": false, "required_place_ids": [], "excluded_place_ids": [],
             "reservations": [], "max_candidates": 12},
 "engine": "rules", "notes": []}
```

What the rules parser understands (so the UI can hint at it): a city after `to/in/at/around/visit/explore` or at
the start of the message, or a bare one-to-three-word answer; `today`, `tomorrow`, `next friday`, `on monday`,
`2026-10-03`, `5 jan`, `january 5th 2027`; `10am to 5pm`, `9-17`, `10:30 until 16:00`, and a bare `3pm` as a
start correction once a window exists; `60 euros`, `€60`, `budget 60`, `cheap`; the interest words (`museums`
maps to `art`, `cafes` to `coffee`, ...), with `actually/instead/just/only/rather` replacing instead of adding;
`3 km`, `no long walks`; `4 stops`; `bike/cycling`; `don't care about rain`.

---

## 9. What the frontend must show

These are the backend facts that must be visible to the user; how they are laid out is the UI's business.

1. **System status while a job runs.** `queued`/`running` versus terminal, with the four specialists lighting up
   on `agent.started`/`agent.completed` and each report's summary, `candidate_ids` count, `avoid_ids` count and
   evidence count. On SuperGrid, the run id and federation from `flower.run.started`, reconnect attempts, and the
   final `flower.run.finished` metrics. Model mode: each `model.completed` with elapsed time, and
   `model.incomplete`/`model.repair` so a slow or truncated model is not a silent stall. `model_settings.wall_time_s`
   is the upper bound to communicate ("up to 15 minutes").
2. **Agent handoffs.** Every `collaboration.message` with `kind: "request"` is a specialist asking another
   specialist (sender, recipient, summary, the places concerned). After a reload the same messages are in
   `trip.messages` / `proposal.proposed.messages`. `planner.started.ranking_source`, `planner.fallback` and
   `planner.repair` say whether the specialists' ranking, the catalog fallback, or a reviewer-requested
   alternative produced the plan; they should be in the activity feed.
3. **Why a proposal exists.** `proposal.trigger` (event kind or `initial_plan`), whether it was a simulated
   scenario (`proposed.weather_override` present, or `coordinator.started.simulated`), and the diff
   `added / removed / preserved` with place names resolved from both the committed and the proposed `places`.
   Locked reservations that were protected (`request.reservations`).
4. **Review is a decision.** A pending proposal blocks nothing on the server, so the UI must present Apply /
   Keep current explicitly, disable Apply when `validation.valid` is false, and explain the 409s ("Proposal is no
   longer pending", "Stale proposal") by reloading the trip. The initial plan needs no review (auto-accepted)
   unless it is infeasible, in which case the pending proposal's issues explain why there is no itinerary.
5. **Evidence and provenance on every fact.** Per place: `source.provider`, `source.status`, `source.reference`
   (a link for OSM records), `source.note`, `cost_status`, `opening_status`, `indoor` unknown state. Per leg:
   `source_status` (dashed line and "synthetic route" for fixtures). Weather source and status. Validation
   findings grouped by code with the warning/error distinction. `unknown_evidence_ids` on a model report means the
   model cited something outside the supplied evidence and those citations were dropped.
6. **Fixture versus live labels.** The banner from `fixture_notice`/`live_notice`; `data_mode` on the map label
   and in the engine pill; `supported_cities` in the intake hint (`["Berlin"]` or "any city"); the intake `notes`
   when a city is outside the fixture. Nothing in the UI may imply a booking, payment, verified price or verified
   opening time.
7. **Errors with a next step.** `job.failed.error` / `job.error` as the message; 409 on events as "reload and
   retry"; 422 as a form problem; 401 as login; geocode 404 as "check the spelling"; 503 as "try again".
8. **Minimal steps.** Intake `ready: true` with a `request` should lead straight to `POST /api/trips` with that
   request (one action, no re-typing); event buttons need one click plus the review.

---

## 10. Changes since the partner's main (`3465439`)

The backend in this branch is the reviewed local backend; the partner's multi-day and static-city-list code was
replaced. Frontend code that still references the removed shapes must change.

### Removed

* **Multi-day trips.** `TripRequest.days`, `TripBrief.days`, `DayPlan`, `Itinerary.days[]` and its derived
  `stops/legs/breaks/end_arrival` properties, `validate_trip`, the `DAY_COUNT_MISMATCH`, `DAY_DATE_MISMATCH`,
  `DUPLICATE_STOP_ACROSS_DAYS`, `TRIP_BUDGET_LIMIT`, `TRIP_TOTALS_MISMATCH` issue codes, and the
  `days`-scaled import limits. `Itinerary` is flat again: `stops`, `legs`, `breaks`, `end_arrival`, `walking_m`,
  `travel_duration_s`, `cost_minor`, `unknown_cost_count`, `validation`, `score`. Sending `days` in a request is a
  `422` (unknown field). In the current `app.js`: `buildRequest()`/`briefToRequestFields()` still copy `days`,
  `render()`, `renderDayTabs()`, `renderTimeline()`, `selectedDayPlan()` and the map read `itinerary.days` (so an
  itinerary renders as "No feasible schedule" and 0 stops), and the chat placeholder still advertises "Two days".
* `store.finish_job(auto_accept=)`: auto-acceptance of the valid initial plan is now done by the API after
  `finish_job` (same observable result; job `completed`, trip revision 1).
* `intake.SUPPORTED_CITIES` / `is_supported_city(city)` as a static list. City support is now `resolve_city`
  through the geocoder. `config.supported_cities` is `["Berlin"]` in fixture mode and `null` in live mode instead
  of always a list.
* The intake's old note text (`'X' isn't supported yet — only Berlin is available right now.`) and the old
  confirmation (`Building your itinerary now.`).

### Added

* `GET /api/geocode` and the `GeoPlace` schema; the intake geocodes any city, rewrites it to the canonical
  name, and in live mode fills `timezone`, `origin` and `destination` from the result.
* `/api/config` keys `live_notice`, `geocoding`, `router`, `model_api`, `model_settings`, `intake`.
* Intake `notes` for multi-day mentions and the four city outcomes (fixture, unresolved, unavailable,
  outside fixture); `engine` stays `rules` unless the model actually changed the brief.
* Events `planner.fallback`, `planner.repair`, `model.incomplete`, `flower.stream.reconnecting`;
  `planner.started.ranking_source` and `avoided_count`; `model.completed.usage`;
  `agent.completed.unknown_evidence_ids`; `metrics.provider_calls` everywhere (model / tool / provider split) and
  `job.finished.metrics` guaranteed to hold all three counters.
* Specialist rankings and `avoid_ids` drive the candidate set (catalog order is a labelled fallback); the
  Budget & pace repair is compared on an equal baseline; tolerant JSON extraction and one repair turn for model
  reports; per-step model, tool and provider budgets with `wall_time_s` (default 900 s local, reported in
  `model_settings`).
* `travel_agent/settings.py` (one `ModelSettings` for the local model path and the SuperGrid launcher),
  `travel_agent/model_adapter.py` (Chat Completions endpoints such as Gemini behind `TRAVEL_MODEL_API=chat`),
  `scripts/serve_live.sh` (any-city live planning), `scripts/serve_gemini.sh`.
* API version `0.3.1` (`/health` and the FastAPI title).

### Unchanged and still relied on by the UI

`POST /api/trips`, `POST /api/trips/{id}/events` with `base_revision`, the SSE framing and `done` event,
`GET /api/trips/{id}` returning `{trip, proposals}`, accept/reject, export/import, login, the `TripEvent` kinds and
payloads, the `Place`/`Leg`/`Stop`/`Break`/`Validation` shapes, `flower.submitting` / `flower.run.started` /
`flower.run.finished`, and the bridge-mode `flower_command`.
