# Architecture

## Components

| Component | File | Responsibility |
| --- | --- | --- |
| AgentApp | `travel_agent/agent_app.py` | Flower entry point. Parses `agent.input`, runs one or more planning steps, publishes run events, persists state in `Context` |
| Specialists | `travel_agent/agents.py` | Role instructions, read-only tool dispatcher with per-role allowlists, bounded model runner, deterministic rules runner |
| Coordinator | `travel_agent/coordinator.py` | Applies events, loads evidence, sequences the specialists, builds and validates the proposal |
| Planning engine | `travel_agent/planning/engine.py` | Schedule construction from an order, bounded beam search, independent validation |
| Providers | `travel_agent/providers/services.py` | Fixture catalog; live Overpass, OpenRouteService, Open-Meteo adapters with caching and bounds |
| Flower backend | `travel_agent/flower_backend.py` | Submits SuperGrid runs through the Flower Control API and relays run events |
| API and store | `travel_agent/api.py`, `travel_agent/store.py` | HTTP API, SSE event feed, compare-and-swap revisions, idempotent jobs |
| Web UI | `travel_agent/web/` | Map, timeline, agent activity, review panel |

## Specialist agents

| Role | Input context | Tools (read-only) | Output |
| --- | --- | --- | --- |
| discovery | request, places, shared reports/messages | `search_places`, `get_place_details` (+ optional `web_search`/`web_fetch`) | ranked `candidate_ids`, evidence refs |
| conditions | request, weather, places | `get_weather_forecast`, `get_place_details` | `avoid_ids`, request to discovery for indoor alternatives |
| mobility | discovery/conditions reports, route matrix | `get_route_matrix`, `get_place_details` | reachable `candidate_ids` preserving discovery order |
| budget_pace | proposed schedule, validation findings | `assess_costs`, `validate_itinerary`, `get_place_details` | review summary, optional repair request |

Every specialist returns an `AgentReport` (`role`, `summary`, `candidate_ids`, `avoid_ids`, `evidence_ids`,
`requests`). Requests are `AgentMessage` objects addressed to another role or the coordinator.

## Collaboration protocol

```text
Coordinator.plan(snapshot, event)
  apply_event -> refresh evidence (places, weather) -> reports/messages reset
  discovery || conditions            (concurrent in model mode; same frozen evidence)
  if conditions requested discovery  -> discovery re-runs with the request in shared_messages
  route matrix (provider)            -> mobility
  beam search over the ranking       -> proposed itinerary
  budget_pace                        -> optional one-shot repair search
  route geometry (provider)          -> rebuild schedule with directions metrics -> validate
  Proposal(base_revision, trigger, added/removed/preserved)
```

Specialist outputs are structured data in `TripSnapshot.reports` and `TripSnapshot.messages`; later specialists
receive them as `shared_reports` and `shared_messages`.

## Shared state schema (`travel_agent/schemas.py`)

- `TripRequest`: constraints (time window, budget in minor units, walking limits, breaks, interests, required and
  excluded places, reservations).
- `Place` with `Evidence` (`fixture | live | cached | user | unknown`), `cost_status`
  (`fixture | verified | estimated | unknown`), `opening_status`.
- `Weather` with `ForecastInterval`s. `weather_override` marks a simulated scenario.
- `Itinerary`: `Stop`s, `Leg`s (meters, seconds, geometry), `Break`s, totals, `Validation`.
- `Progress`: current time and location, spent amount, completed places.
- `TripSnapshot`: everything above plus `revision`, `reports`, `messages`, `data_mode`, `agent_mode`.
- `TripEvent` kinds: `rain`, `weather_updated`, `pace_changed`, `budget_changed`, `place_unavailable`,
  `stop_completed`, `lock_stop`, `unlock_stop`, `user_running_late`, `preferences_changed`.
- `Proposal`: `base_revision`, `trigger`, `proposed` snapshot, `added`, `removed`, `preserved`, `status`.

## SuperGrid execution path

```text
Browser "Build itinerary"
  -> POST /api/trips                       (server enqueues a job)
  -> FlowerBackend.run_job
       build FAB (cached)                  -> StartRun(fab, override_config{agent.input={"job": ...}})
       StreamRunEvents(run_id)             <- travel.agent.started, travel.collaboration.message, ...
                                           <- travel.result (compressed, integrity-checked proposal)
  -> Store.finish_job                      (re-validates constraints, progress, provenance)
  -> SSE /api/jobs/{id}/events             -> agent activity panel, review panel
```

The AgentApp never receives provider credentials or the API token. The run-config payload contains only the
trip snapshot and event. Both directions are gzip+base64 with a SHA-256 prefix and a decoded size bound.

## Flower Hub publication

```bash
uv run flwr build
uv run flwr login supergrid
uv run flwr app publish .
```

`publisher` in `pyproject.toml` must equal the signed-in account name.
