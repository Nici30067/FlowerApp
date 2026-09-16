# Architecture

## Components

| Component | File | Responsibility |
| --- | --- | --- |
| AgentApp | `travel_agent/agent_app.py` | Flower entry point. Parses `agent.input` (a natural-language sentence through the intake, a follow-up command, a scripted scenario, JSON, or a job handed over by the map application), runs one or more planning steps, publishes run events, persists state in `Context` |
| Intake | `travel_agent/intake.py` | Rules-based conversational intake: parses free text onto a `TripBrief`, asks one question at a time, checks the city through the geocoder (fixture radius in fixture mode, any resolved place in live mode) and builds the single-day `TripRequest`; optional model enrichment with the rules result as the floor (local backend only) |
| Settings | `travel_agent/settings.py` | `ModelSettings` (model knobs) and `ProviderSettings` (data mode, contact, endpoints, router): `from_env()` for the local backend, `from_run_config()` for the `travel.*` keys of a Flower run |
| Specialists | `travel_agent/agents.py` | Role instructions, read-only tool dispatcher with per-role allowlists, bounded model runner, deterministic rules runner |
| Coordinator | `travel_agent/coordinator.py` | Applies events, loads evidence, sequences the specialists, selects the candidate set from their rankings and avoid lists, builds and validates the proposal |
| Planning engine | `travel_agent/planning/engine.py` | Schedule construction from an order, bounded beam search, independent validation |
| Providers | `travel_agent/providers/services.py` | Fixture catalog (central Berlin, `fixture_supported` within 10 km of its centre); live Overpass, OSRM or OpenRouteService, Open-Meteo forecast adapters with caching and bounds; `Geocoder` (Open-Meteo geocoding) shared by both data modes; `make_provider(mode, settings)` and `make_geocoder(settings)` build them from a `ProviderSettings` |
| Flower backend | `travel_agent/flower_backend.py` | Submits SuperGrid runs through the Flower Control API and relays run events |
| API and store | `travel_agent/api.py`, `travel_agent/store.py` | HTTP API (including `GET /api/geocode` and the conversational `POST /api/intake`), SSE event feed, compare-and-swap revisions, idempotent jobs |
| Web UI | `travel_agent/web/` | Map, timeline, agent activity, review panel, city **Locate** (moves start, finish and timezone) |

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

## AgentApp natural-language flow

`agent.input` is classified before any model call. Known commands, JSON and scripted scenarios keep their
meaning; everything else is a sentence for the intake.

```text
agent.input
  help | status | export | approve | reject | diagnose          -> answered from context.state, no planning
  plan | rain | refresh | budget N | walk N km                   -> one planning step (events need a committed snapshot)
  "<step> then <step> ..." or "<step>; <step>"                  -> scripted scenario, at most 6 steps, valid revisions auto-commit
  {"request": ...} | {"event": ...} | {"job": "<encoded>"}      -> explicit configuration or the map application's job
  first segment is no command (and not JSON)                    -> a description: converse()

converse(description [then <command> ...])
  the segments after the description are compiled as commands up front; if one of them is no command either,
  the whole prompt is one description
  brief = context.state["travel"]["brief"] (JSON) or an empty TripBrief
  brief = intake.parse(description, brief)       scalars overwrite, interests are unioned unless the sentence replaces them
  several days mentioned                         -> note: one day of the trip is planned
  only the time window missing                   -> 10:00 to 17:00 local time, with a note (DEFAULT_WINDOW)
  city, date or interests missing?
      yes -> publish intake.next_question(brief) + "Still needed: ... So far: ..." (needed_hint)
             persist the brief; scripted steps are skipped; return (no model call, no provider call)
      no  -> intake.resolve_request(brief, LazyGeocoder(settings), settings.data_mode, TripRequest())
               live mode:     geocoder.search(city)  -> GeoPlace (name, country, coordinate, timezone)
               fixture mode:  Berlin needs no lookup; any other place must pass fixture_supported, otherwise the
                              run names the place and suggests --run-config 'travel.data-mode="live"'
               unknown name:  "I couldn't find a place called ..." and the city is dropped from the brief, so the
                              next answer (even a bare lower-case one) is read as the city
               lookup outage: the name is kept and the run says how to retry
             request  = to_trip_request(brief, TripRequest(), place)   origin, destination, timezone, canonical name from the geocode
             "Planning <title>: <date> <start> to <end> (<timezone>); interests ...; budget ...; walking up to ... m;
              start <lat>, <lon>. Located <name>, <country> via Open-Meteo geocoding."
             snapshot = TripSnapshot(request, data_mode=settings.data_mode, agent_mode="model")
             "Live data: OpenStreetMap via Overpass, OSRM routes, Open-Meteo weather (<router>)"   (live mode only)
             run_steps([plan, *scripted steps]) -> summary per step -> persist snapshot + pending proposal; brief cleared
```

`context.state["travel"]` is a `ConfigRecord` with three string fields: `snapshot` (the committed
`TripSnapshot`), `proposal` (the pending `Proposal`) and `brief` (the partial `TripBrief`; `""` when empty).
Flower carries the state across the runs of one run series (Flower Chat), so a clarifying question is answered by
the next message and follow-up commands act on the committed snapshot; a standalone `flwr run` starts with an
empty record. `approve` and `reject` keep the brief, `status` prints it (`Conversation brief: city Tokyo; ...`)
together with the data mode and router. Once a plan has been built the brief is cleared, so the next sentence
starts a new brief rather than merging into the old one. The parser is deterministic and offline
(`tests/test_intake*.py`); the only network call of the intake is the geocode, which runs after the brief is
complete, and the fixture city never opens a connection (`LazyGeocoder`).

## Provider settings from run-config

Inside a Flower run there are no environment variables, so `ProviderSettings` has two constructors that produce
the same frozen dataclass:

| Field | Local backend (`from_env`) | Flower run (`from_run_config`) | Default when empty |
| --- | --- | --- | --- |
| `data_mode` | `TRAVEL_DATA_MODE` | `travel.data-mode` | `fixture` (env), `live` (`pyproject.toml`) |
| `contact` | `TRAVEL_CONTACT` | `travel.contact` | empty (env), the app's contact string (`pyproject.toml`) |
| `allow_public_overpass` | `TRAVEL_ALLOW_PUBLIC_OVERPASS` | `travel.allow-public-overpass` | `false` (env), `true` (`pyproject.toml`) |
| `router` | `TRAVEL_ROUTER` (else `ors` when `ORS_API_KEY` is set, else `osrm`) | `travel.router` | `osrm` |
| `osrm_url_template` | `OSRM_URL_TEMPLATE` | `travel.osrm-url-template` | `https://routing.openstreetmap.de/routed-{profile}/{service}/v1/driving` |
| `overpass_url` | `TRAVEL_OVERPASS_URL` | `travel.overpass-url` | `https://overpass-api.de/api/interpreter` |
| `ors_url` | `ORS_BASE_URL` | `travel.ors-url` | `https://api.openrouteservice.org` |
| `ors_key` | `ORS_API_KEY` | `travel.ors-api-key` | empty |
| `weather_url` | `OPEN_METEO_URL` | `travel.weather-url` | `https://api.open-meteo.com/v1/forecast` |
| `geocoder_url` | `GEOCODER_URL` | `travel.geocoder-url` | `https://geocoding-api.open-meteo.com/v1/search` |

`make_provider(mode=None, settings=None)` and `make_geocoder(settings=None)` accept a `ProviderSettings`; without
one they read the environment exactly as before, and an explicit `mode` overrides `settings.data_mode` (the job
path keeps the data mode of the snapshot handed over by the map application). Validation of the combination
(contact present in live mode, public Overpass allowed, `ors` with a key, HTTPS endpoints) happens in
`LiveProvider`, so a misconfiguration fails the same way on every path and before any model call. The AgentApp
builds its settings once per run from `context.run_config` and prints the live-data line at the start of a live
plan so the transcript names the data source and the router.

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

The event set is unchanged by the natural-language flow; the only addition is the text line
`Live data: OpenStreetMap via Overpass, OSRM routes, Open-Meteo weather (<router>)` at the start of a live plan
(`OpenRouteService routes` when the router is `ors`), preceded by the `Planning ...` request line when the plan
came from a description.

## Shared state schema (`travel_agent/schemas.py`)

- `TripRequest`: constraints (time window, budget in minor units, walking limits, breaks, interests, required and
  excluded places, reservations).
- `TripBrief`: the partial, conversationally built mirror of the planning-relevant `TripRequest` fields (city,
  date, start and end time, timezone, budget, interests, walking limit, transport mode, target stops, rain
  preference, title); every field optional, no day count. `IntakeMessage` and `IntakeTurn` carry one chat turn
  through `POST /api/intake`.
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
  `source` Evidence). It is returned by `GET /api/geocode` together with `fixture_supported`, and the intake
  takes origin, destination, timezone and the canonical city name from it.

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

The AgentApp never receives the map application's API token. The job payload contains only the trip snapshot
and event; provider endpoints and the contact string come from the `travel.*` run config, and
`travel.ors-api-key` is the only provider secret that can reach a worker, and only when the router is `ors`.
Both directions are gzip+base64 with a SHA-256 prefix and a decoded size bound. The job keeps the data mode of
the snapshot; the map application's SuperGrid launcher (`scripts/serve_supergrid.sh`) defaults to the fixture.

## Data modes: run-config inside Flower, environment locally

```text
Flower run (SuperGrid or a local SuperLink)                Local backend (TRAVEL_EXECUTION_BACKEND=local)
  ProviderSettings.from_run_config(context.run_config)       ProviderSettings.from_env()
  travel.data-mode = "live" by default                       TRAVEL_DATA_MODE=fixture by default
  travel.contact in every User-Agent                         TRAVEL_CONTACT required for live mode
  Overpass  ([timeout:25], 30 s, name:en preferred over name), one bounded query per planning step
  Open-Meteo forecast
  OSRM table + route  (evidence ids osrm-table, osrm-route), or OpenRouteService when router = ors with a key
  travel.data-mode = "fixture": central Berlin catalog,      TRAVEL_DATA_MODE=live: provider built eagerly at
  no outbound requests                                       startup (bad router or env fails fast)
```

Geocoding sits outside this split. The intake calls the `Geocoder` once the brief is complete (live mode; in
fixture mode only for cities other than Berlin, to apply the 10 km radius), and `GET /api/geocode?q=<text>`
serves the browser in every mode with the `GeoPlace` fields plus `fixture_supported`. The browser uses that flag:
in fixture mode a city the fixture does not cover is refused before a job is created, in live mode the geocoded
coordinates become the start, finish and timezone of the new trip. `/api/config` reports `geocoding`, `router`
(null unless the data mode is live), `live_notice`, `intake` and `supported_cities` (`["Berlin"]` in fixture
mode, `null` in live mode), and the mode pill and notice bar follow them.

## Flower Hub publication

```bash
uv run flwr build
uv run flwr login supergrid
uv run flwr app publish .
```

`publisher` in `pyproject.toml` must equal the signed-in account name, and the version must be higher than every
version already listed on the Hub page. Published versions cannot be removed by the author; see
`docs/submission-checklist.md` for the state of the listing.
