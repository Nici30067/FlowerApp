# Architecture

## Components

| Component | File | Responsibility |
| --- | --- | --- |
| AgentApp | `travel_agent/agent_app.py` | Flower entry point. Parses `agent.input`, runs one or more planning steps, publishes run events, persists state in `Context` |
| Specialists | `travel_agent/agents.py` | Role instructions, read-only tool dispatcher with per-role allowlists, bounded model runner, deterministic rules runner |
| Coordinator | `travel_agent/coordinator.py` | Applies events, loads evidence, sequences the specialists, selects the candidate set from their rankings and avoid lists, builds and validates the proposal |
| Planning engine | `travel_agent/planning/engine.py` | Schedule construction from an order, bounded beam search, independent validation |
| Providers | `travel_agent/providers/services.py` | Fixture catalog (central Berlin, `fixture_supported` within 10 km of its centre); live Overpass, OSRM or OpenRouteService (`TRAVEL_ROUTER`), Open-Meteo forecast adapters with caching and bounds; `Geocoder` (Open-Meteo geocoding) shared by both data modes |
| Flower backend | `travel_agent/flower_backend.py` | Submits SuperGrid runs through the Flower Control API and relays run events |
| API and store | `travel_agent/api.py`, `travel_agent/store.py` | HTTP API (including `GET /api/geocode` and the conversational `POST /api/intake`), SSE event feed, compare-and-swap revisions, idempotent jobs |
| Intake | `travel_agent/intake.py` | Rules-based chat intake: parses free text onto a `TripBrief`, asks one question at a time, checks the city through the geocoder (fixture radius in fixture mode, any resolved place in live mode) and builds the single-day `TripRequest`; optional model enrichment with the rules result as the floor |
| Web UI | `travel_agent/web/` | Chat-thread layout: conversational intake, preference form, map, timeline, agent activity, review panel |

## Specialist agents

| Role | Input context | Tools (read-only) | Output |
| --- | --- | --- | --- |
| discovery | request, places, shared reports/messages | `search_places`, `get_place_details` (+ optional `web_search`/`web_fetch`) | ranked `candidate_ids` (select the candidate set; rank position earns a bonus equal to one interest match), evidence refs |
| conditions | request, weather, places | `get_weather_forecast`, `get_place_details` | `avoid_ids` (consumed by the coordinator: excluded from the candidate set unless required), request to discovery for indoor alternatives |
| mobility | discovery/conditions reports, route matrix | `get_route_matrix`, `get_place_details` | reachable `candidate_ids` preserving discovery order (also select the candidate set) |
| budget_pace | proposed schedule, validation findings | `assess_costs`, `validate_itinerary`, `get_place_details` | review summary, optional repair request (the repair is compared on an equal baseline) |

Every specialist returns an `AgentReport` (`role`, `summary`, `candidate_ids`, `avoid_ids`, `evidence_ids`,
`requests`). Requests are `AgentMessage` objects addressed to another role or the coordinator. `evidence_ids`
are validated against the evidence catalog; unknown IDs are pruned and listed as `unknown_evidence_ids` on the
`agent.completed` event. Tools are offered to the model only when `max-tool-turns` is 1 or more; with the
default of 0 each specialist answers in one bounded call from the compact evidence in its context.

## Collaboration protocol

```text
Coordinator.plan(snapshot, event)
  apply_event -> refresh evidence (places, weather; provider calls) -> reports/messages reset
  wet forecast:  conditions -> discovery         (avoid_ids and the indoor request reach discovery first)
  otherwise:     discovery || conditions         (concurrent in model mode; same frozen evidence)
                 if conditions requested discovery -> discovery re-runs with the request in shared_messages
  route matrix (provider call)       -> mobility
  candidate set = discovery/mobility rankings minus avoid_ids (required places kept)
                                        rank position earns a bonus equal to one interest match
  beam search over the ranking       -> proposed itinerary        planner.started ranking_source=specialists
                                        (catalog order only as a labeled fallback: planner.fallback)
  budget_pace                        -> optional one-shot repair search, compared on an equal baseline
  route geometry (provider call)     -> rebuild schedule with directions metrics -> validate
  Proposal(base_revision, trigger, added/removed/preserved)
```

Specialist outputs are structured data in `TripSnapshot.reports` and `TripSnapshot.messages`; later specialists
receive them as `shared_reports` and `shared_messages`. The `refresh` command (`weather_updated` event) clears a
simulated rain override before the evidence is refreshed.

## Execution events

Every planning step emits structured events (`travel.*` run events on SuperGrid, Server-Sent Events from the local
API). The browser feed and `scripts/run_events.py` consume the same stream.

| Kind | Data |
| --- | --- |
| `agent.started` / `agent.completed` | `role`, `engine`, `report`; `agent.completed` adds `unknown_evidence_ids` (pruned evidence refs) |
| `collaboration.message` | an `AgentMessage` (`sender`, `recipient`, `kind`, `summary`, `place_ids`, `evidence_ids`) |
| `model.completed` | `role`, `call_number`, `requested_model`, `returned_model`, `elapsed_s`, `usage` (`input_tokens`, `output_tokens`, `reasoning_tokens`) or null |
| `model.incomplete` | `role`, `call_number`, `max_output_tokens`: the answer reached the output cap |
| `model.repair` | `role`, `reason`: one bounded repair turn follows |
| `tool.completed` / `tool.failed` | `role`, `name`, `engine` or `error`; only model-initiated tool executions count as tool calls |
| `planner.started` | `algorithm`, `beam_width`, `candidate_count`, `avoided_count`, `ranking_source` (`specialists` or `catalog`) |
| `planner.fallback` | `reason`, `candidate_count`: catalog order was used instead of a specialist ranking |
| `planner.repair` | `adopted`, `reason`, `proposal_score`, `alternative_score`: a Budget & Pace alternative was scored on the same baseline as the proposal |
| `validation.completed` | the `Validation` object (`valid`, `status`, `issues`) |
| `proposal.ready` / `job.finished` | proposal summary plus `metrics`: `model_calls`, `tool_calls`, `provider_calls`, `elapsed_s` |
| `flower.submitting` / `flower.run.started` / `flower.run.finished` / `flower.stream.reconnecting` | SuperGrid only: `fab_hash`, `run_id`, `federation`, `model`, `note`; `flower.run.finished` adds `metrics`, `relayed`, `listener_errors`; `flower.stream.reconnecting` carries `attempt`, `delay_s`, `after_task_event_id`, `error` |

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
- `GeoPlace`: one geocoder answer (`query`, `name`, `country`, `admin1`, `coordinate`, `timezone`, `population`,
  `source` Evidence). It is returned by `GET /api/geocode` together with `fixture_supported`.

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

## Data modes: SuperGrid fixture versus local live

```text
TRAVEL_EXECUTION_BACKEND=flower                  TRAVEL_EXECUTION_BACKEND=local, TRAVEL_DATA_MODE=live
  data mode is fixture, always                     provider built eagerly at startup (bad router or env fails fast)
  workers receive the trip snapshot only           Overpass  ([timeout:25], 30 s, name:en preferred over name)
  no provider access, no TRAVEL_CONTACT            Open-Meteo forecast
  central Berlin catalog                           OSRM table + route  (evidence ids osrm-table, osrm-route)
                                                   or OpenRouteService when TRAVEL_ROUTER=ors and ORS_API_KEY is set
```

Geocoding sits outside this split. `GET /api/geocode?q=<text>` calls Open-Meteo geocoding through a lazily
created `Geocoder` in every mode and answers with the `GeoPlace` fields plus `fixture_supported`. The browser
uses that flag: in fixture mode a city the fixture does not cover is refused before a job is created, in live
mode the geocoded coordinates become the start, finish and timezone of the new trip. `/api/config` reports
`geocoding`, `router` (null unless the data mode is live) and `live_notice`, and the mode pill and notice bar
follow them.

## Flower Hub publication

```bash
uv run flwr build
uv run flwr login supergrid
uv run flwr app publish .
```

`publisher` in `pyproject.toml` must equal the signed-in account name.
