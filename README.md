# OSM Travel Companion

Collaborative day-trip planning with four specialist agents, OpenStreetMap data, deterministic constraint
validation, and reviewable itinerary revisions. The specialists run as a **Flower AgentApp** on **Flower
SuperGrid** and use the **Flower Endeavor 1.0** model (`flower-endeavor-v1.0`). The map application on your
machine submits each planning job to SuperGrid and streams the agents' collaboration back into the browser.

Built for the Flower Collaborative Agents Hackathon (Berlin, 16 September 2026).

## What it does

1. You describe one day: city (any city; **Locate** geocodes it), time window, budget, walking limit, interests.
2. Four specialists collaborate on a shared itinerary state:
   `Discovery || Conditions (Conditions first when the forecast is wet) -> Discovery (indoor alternatives on request)
   -> Mobility -> Budget & Pace -> Coordinator`.
3. Application code, not the model, builds the schedule (bounded beam search) and validates it against every
   constraint: opening hours, travel time, continuous walking, breaks, budget, weather, locked reservations.
4. Every later change (rain, tighter budget, less walking, a closed place, a completed stop) produces a proposed
   revision that you inspect and apply or reject. Completed stops, locked reservations and actual spending are
   preserved.

Models rank candidates and exchange structured requests. They cannot invent places, prices, or routes, and there
are no booking, payment, or cancellation tools.

### How specialists shape the plan

The specialists' output steers the schedule search rather than decorating it. The rankings returned by Discovery
and Mobility select the candidate set, and a place's position in that ranking carries a rank bonus equal to an
interest match. `avoid_ids` from Conditions exclude places from the candidate set unless the user required them
(a locked reservation or an explicit inclusion). The catalog order is only a labeled fallback: when no specialist
ranking is used the coordinator emits `planner.fallback` and the agent feed shows "Coordinator · catalog
fallback" with the reason. Budget & Pace may request one repair search, and the repaired schedule is compared
with the original on an equal baseline before it can replace it. Evidence IDs cited by a specialist are validated
against the evidence catalog; unknown IDs are dropped and reported as `unknown_evidence_ids`. The `refresh`
command (browser: **Refresh conditions**) clears a simulated rain override and reloads the forecast.

## Architecture

```text
 Browser (OpenStreetMap map, timeline, agent activity, review panel)
     |  HTTPS/JSON + Server-Sent Events
     v
 FastAPI server (travel_agent/api.py)  ---- SQLite store: trips, proposals, jobs, events
     |
     |  TRAVEL_EXECUTION_BACKEND=local          TRAVEL_EXECUTION_BACKEND=flower
     |  (rules replay or a local model URL)     (Flower Control API, same credentials as `flwr login`)
     v                                          v
 Coordinator (travel_agent/coordinator.py)   Flower SuperGrid run of this AgentApp
     |                                          |  FLWR_RUNTIME_BASE_URL -> OpenAI Responses API
     |                                          v
     |                                       flower-endeavor-v1.0  (fallback: openai/gpt-5.6-sol)
     v
 Specialists (travel_agent/agents.py): discovery | conditions | mobility | budget_pace
     each has its own instructions, context, output contract, and read-only tool allowlist
     |
     v
 Planning engine (travel_agent/planning/engine.py): beam search + independent validation
     |
     v
 Providers (travel_agent/providers/services.py): bundled fixture (central Berlin)
                                                | live Overpass + OSRM or OpenRouteService + Open-Meteo
 Geocoder (same module): Open-Meteo geocoding behind GET /api/geocode, available in both data modes
```

The AgentApp entry point is `travel_agent/agent_app.py`. In SuperGrid mode the server encodes the trip snapshot
and the triggering event into `agent.input`, starts the run, relays the run events (`travel.*`) to the browser,
and receives the proposal as one structured run event. The proposal is validated again locally before it can be
reviewed. No public callback URL, tunnel, or per-job secret is needed.

## 60-second local quick start (no Flower account needed)

```bash
uv sync --extra dev
uv run pytest -q
./run_demo.sh            # opens http://127.0.0.1:8000 with fixture data and rule-based specialists
```

Press **Build itinerary**, then **Simulate rain**, review the proposed revision, and apply it.

## Plan another city (no API keys)

The bundled fixture describes central Berlin only. Any other city is planned from keyless public services in
**live** data mode:

```bash
TRAVEL_CONTACT="you@example.org" GEMINI_API_KEY=... ./scripts/serve_live.sh        # http://127.0.0.1:8011
```

Type a city into **City**, press **Locate** (or Enter), read the status line, for example
`Located Tokyo, Japan · Asia/Tokyo · 35.6895, 139.6917`, then press **Build itinerary**. Locate moves the start
and finish to the city centre, sets the timezone and recentres the map; **Set start** and **Set finish** still
override the endpoints afterwards. Building with a city that was typed but not located geocodes it first, and a
name that does not resolve stops the build. An existing trip keeps its city: choose **New trip** to plan another.

`scripts/serve_live.sh [port]` runs the local backend with the Gemini Chat Completions adapter (`TRAVEL_MODEL`
default `gemini-3.8-flash`, `TRAVEL_MODEL_API=chat`, one tool turn), `TRAVEL_DATA_MODE=live`,
`TRAVEL_ALLOW_PUBLIC_OVERPASS=true`, `TRAVEL_ROUTER=osrm` and the database `runtime/travel-live.sqlite3` on port
8011. The only key it needs is `GEMINI_API_KEY`, for the model. The rule-based specialists plan on the same live
data without any key at all:
`TRAVEL_DATA_MODE=live TRAVEL_ALLOW_PUBLIC_OVERPASS=true TRAVEL_CONTACT=... uv run python -m travel_agent.cli serve`.
The mode pill then reads `Live data · OSRM` (rules) or `Live data · gemini-3.8-flash` (model), and the notice bar
names the router.

| Service | Provides | Key | Etiquette |
| --- | --- | --- | --- |
| Open-Meteo geocoding (`GEOCODER_URL`) | city name to coordinates, timezone and country; the most populous populated place with that name wins | none | non-commercial use, published fair-use limit about 10,000 calls a day; answers are cached |
| Overpass (`TRAVEL_OVERPASS_URL`) | OpenStreetMap places with their tags (`name`, `name:en`, `opening_hours`, `fee`, `charge`, indoor hints) | none | the public instance only with `TRAVEL_ALLOW_PUBLIC_OVERPASS=true`; one bounded query per planning step with `[timeout:25]`; published fair use is roughly 10,000 queries and 1 GB a day per IP; run your own instance beyond a demo |
| Open-Meteo forecast (`OPEN_METEO_URL`) | hourly precipitation probability and temperature | none | same fair-use terms as the geocoder |
| OSRM, public FOSSGIS instance (`OSRM_URL_TEMPLATE`) | walking (`routed-foot`) and cycling (`routed-bike`) travel-time matrix and route geometry | none | a demo server run for the OSM community: rate limited, no bulk requests, not for production; self-host OSRM or use OpenRouteService for anything else |
| OpenRouteService (`ORS_BASE_URL`, `TRAVEL_ROUTER=ors`) | the same matrix and directions under a per-account quota | `ORS_API_KEY` (free account) | becomes the default router whenever `ORS_API_KEY` is set |

Every live request carries `User-Agent: OSMTravelCompanion/0.2 (<TRAVEL_CONTACT>)`, so set `TRAVEL_CONTACT` to a
real email address or URL that the operators can reach; `serve_live.sh` refuses to start without it. Prices and
hours come from OSM tags only: where a tag is missing the value stays `unknown`, the validation status is
`provisional`, and the review panel counts the unknown prices. SuperGrid runs (`serve_supergrid.sh`,
`flwr run . supergrid`) keep planning the Berlin fixture: workers get no provider access and no contact string,
and locating another city there only shows the fixture warning.

## Run the specialists on Flower SuperGrid with Endeavor

```bash
uv sync
uv run flwr build                          # validates the Flower App Bundle
uv run flwr login supergrid                # one-time browser login
uv run flwr run . supergrid --stream       # plans the bundled Berlin day with flower-endeavor-v1.0
```

A scripted scenario in one run (initial plan, then a simulated rain update):

```bash
uv run flwr run . supergrid --run-config 'agent.input="plan then rain"' --stream
```

Inspect a run's structured events (the same stream the map application consumes):

```bash
uv run python scripts/run_events.py <run-id> --seconds 30
```

Other inputs: `budget 30`, `walk 2 km`, `status`, `approve`, `reject`, `export`, `diagnose` (probes model IDs),
or JSON such as `{"request": {...}, "events": [...]}`. Configuration keys under `[tool.flwr.app.config.agent]`
can be overridden per run, for example `--run-config 'agent.model="openai/gpt-5.6-sol"'`.

### Map application driven by SuperGrid

```bash
uv run flwr login supergrid
./scripts/serve_supergrid.sh               # http://127.0.0.1:8000, backend=flower, model=flower-endeavor-v1.0
```

**Build itinerary** is the only action. The right-hand panel shows the SuperGrid run ID, each specialist's
report, every agent-to-agent request, model call timings (with token usage when the runtime reports it), a
metrics line in the form `N model calls · N tool calls · N provider calls · Ns`, and the deterministic
validation result.

## Other model providers (local backend)

The specialists speak the OpenAI Responses API. Providers that only offer Chat Completions, such as Google
Gemini's OpenAI-compatible endpoint, work through the adapter in `travel_agent/model_adapter.py`:

```bash
TRAVEL_EXECUTION_BACKEND=local TRAVEL_AGENT_MODE=model TRAVEL_MODEL_API=chat \
TRAVEL_MODEL_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai/ \
TRAVEL_MODEL=gemini-3.8-flash TRAVEL_MODEL_API_KEY=... TRAVEL_MAX_TOOL_TURNS=1 \
uv run python -m travel_agent.cli serve
```

This is useful for free development and rehearsal: a full plan on `gemini-3.8-flash` took 29 s for 9 model calls
and 6 tool calls (16 September 2026). `scripts/serve_gemini.sh` wraps the configuration on fixture data;
`scripts/serve_live.sh` combines it with live data for any city. SuperGrid runs always use Flower's model
catalog, and the Flower connectors are only available there.

## Zero-credit local run of the SuperGrid path

A local Flower SuperLink can execute this AgentApp with Google Gemini as the model provider, so the exact code that
runs on SuperGrid can be rehearsed without credits. The `gemini-responses-proxy` folder next to this project exposes
an Open Responses endpoint in front of Gemini:

```bash
(cd ../gemini-responses-proxy && GEMINI_API_KEY=... ./run_local_stack.sh)   # proxy on 9100, SuperLink on 9091
uv run flwr run . local-agent --run-config 'agent.input="plan then rain"' --stream
```

Catalog model IDs such as `flower-endeavor-v1.0` are mapped to Gemini by the proxy. Flower's built-in web
connectors are not available on the local runtime.

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `TRAVEL_EXECUTION_BACKEND` | `local` | `local` plans in-process; `flower` submits SuperGrid runs |
| `TRAVEL_FLOWER_MODE` | `control` | `control` uses the Flower Control API; `bridge` is the legacy public-URL callback |
| `TRAVEL_FLOWER_CONNECTION` | `supergrid` | SuperLink connection name from `~/.flwr/config.toml` |
| `TRAVEL_FLOWER_FEDERATION` | account default | for example `@your-name/workspace` |
| `TRAVEL_MODEL` | `flower-endeavor-v1.0` | runtime model ID; `openai/gpt-5.6-sol` is a verified fallback |
| `TRAVEL_MAX_TOOL_TURNS` | `0` | evidence tool rounds per specialist (0 = one bounded call each) |
| `TRAVEL_REASONING_EFFORT` | `low` | Responses API reasoning effort |
| `TRAVEL_MODEL_TIMEOUT_S` | `120` | per model call |
| `TRAVEL_MAX_MODEL_CALLS` | `0` | model calls per planning job; `0` = auto: 5 × (tool turns + 2), i.e. 10 with 0 tool turns |
| `TRAVEL_WALL_TIME_S` | `900` | wall-time budget per planning job (local backend) |
| `TRAVEL_MAX_OUTPUT_TOKENS` | `2000` | output cap per model call; a truncated answer is reported as `model.incomplete` |
| `TRAVEL_FLOWER_RUN_TIMEOUT_S` | `900` | per SuperGrid run; the run is stopped afterwards |
| `TRAVEL_DATA_MODE` | `fixture` | `fixture` (bundled Berlin scenario) or `live` (local backend only) |
| `TRAVEL_AGENT_MODE` | `rules` | local backend: `rules` replay or `model` via `TRAVEL_MODEL_BASE_URL` |
| `TRAVEL_MODEL_API` | `responses` | local model mode: `responses` or `chat` (Gemini and other Chat Completions providers) |
| `TRAVEL_ROUTER` | `osrm`, or `ors` when `ORS_API_KEY` is set | live mode router: `osrm` (public FOSSGIS instance, no key) or `ors` (OpenRouteService, requires `ORS_API_KEY`) |
| `OSRM_URL_TEMPLATE` | `https://routing.openstreetmap.de/routed-{profile}/{service}/v1/driving` | OSRM endpoint; `{profile}` becomes `foot` or `bike`, `{service}` `table` or `route`; https only |
| `GEOCODER_URL` | `https://geocoding-api.open-meteo.com/v1/search` | geocoder behind `GET /api/geocode`, used in every data mode |
| `TRAVEL_CONTACT` | empty | contact (email or URL) in the `User-Agent` of every live request; `serve_live.sh` requires it |
| `TRAVEL_API_TOKEN` | empty | required before binding to a non-loopback interface |

`.env.example` lists every variable, including the live-provider settings.

Run-config keys of the AgentApp (`pyproject.toml`): `agent.input`, `agent.model`, `agent.max-tool-turns`,
`agent.max-model-calls` (`0` = auto: 5 × (tool turns + 2)), `agent.max-output-tokens` (`2000`),
`agent.reasoning-effort`, `agent.model-timeout-s`, `agent.wall-time-s`, `agent.model-candidates`,
`agent.public-web`, `travel.data-mode`, and the bridge-only `travel.api-url`, `travel.job-id`, `travel.job-token`.

## Data modes

- **Fixture** (default): a synthetic central-Berlin catalog with labeled prices, hours and direct-line routes. Every
  record carries `status: fixture`. This is what runs on SuperGrid. **Locate** works here too, but a city more
  than 10 km from the fixture centre is refused with a warning that points to `scripts/serve_live.sh`.
- **Live** (local backend): Overpass (OpenStreetMap), Open-Meteo, and OSRM (`TRAVEL_ROUTER=osrm`, no key) or
  OpenRouteService (`TRAVEL_ROUTER=ors`, `ORS_API_KEY`). Requires `TRAVEL_CONTACT` and either your own Overpass
  instance or `TRAVEL_ALLOW_PUBLIC_OVERPASS=true`. Prices and hours stay `estimated` or `unknown` when OSM tags
  do not verify them; nothing is fabricated. See "Plan another city (no API keys)" above.

## Safety properties

- Each specialist has a distinct prompt, context, output schema, and read-only tool allowlist. When
  `max-tool-turns` is 1 or more, tool names and arguments are validated against a schema and unknown tools return
  an error to the model. With the default of 0 tool turns no tool is offered: each specialist makes one bounded
  model call with the compact evidence already in its context.
- Model, tool, provider, and wall-time budgets are bounded per planning job. The model-call cap defaults to
  5 × (tool turns + 2) per planning step (10 with 0 tool turns); provider calls (places, forecast, route matrix,
  geometry) are counted separately from model-initiated tool calls. Each answer is capped at `max-output-tokens`
  (2000) and a truncated answer is reported as `model.incomplete`. Provider responses are size-limited and
  time-limited. Model JSON is parsed tolerantly (prose or code fences are stripped) with one repair turn.
- Place IDs, roles, message addresses, and evidence IDs in model output are checked against the evidence
  catalog; unknown evidence IDs are dropped and reported as `unknown_evidence_ids`.
- Results from SuperGrid are re-validated locally; a worker cannot change user constraints, progress, or data
  provenance.
- Runtime credentials stay in the AgentApp process; job payloads are integrity-checked and size-bounded.
- OSM and web text are treated as untrusted data and escaped in the browser.

## Repository map

```text
travel_agent/agent_app.py       Flower AgentApp entry point (commands, scenarios, SuperGrid job mode)
travel_agent/agents.py          specialist roles, tool dispatcher, bounded model runner
travel_agent/coordinator.py     shared-state collaboration and proposal generation
travel_agent/planning/engine.py schedule search and independent validation
travel_agent/providers/         fixture and live providers (Overpass, OSRM or OpenRouteService, Open-Meteo, geocoder), opening-hours parser
travel_agent/flower_backend.py  SuperGrid submission through the Flower Control API
travel_agent/jobcodec.py        bounded job/proposal transport
travel_agent/api.py, store.py   HTTP API and SQLite persistence
travel_agent/intake.py          rules-based conversational intake behind POST /api/intake (optional model enrichment)
travel_agent/web/               chat-thread map interface (Leaflet + OpenStreetMap tiles, loaded on request)
Prototype/                      Lovable/React mock of the layout (no backend calls)
docs/                           architecture, demo script, limitations
tests/                          288 tests (no network)
```

## Known limitations

- SuperGrid workers plan on the bundled fixture; live providers are configured for the local backend only, so
  cities other than Berlin need `scripts/serve_live.sh`.
- The public OSRM and Overpass instances are shared demo servers with rate limits; they are fine for a rehearsal
  and unsuited to production.
- Endeavor answers take roughly 20 to 90 seconds per specialist call, so a full plan takes a few minutes.
- The fixture uses direct-line distances, not walking directions; live mode uses provider geometry.
- Conversation state persists inside one `flwr chat` series; a plain `flwr run` starts from a fresh state, which
  is why scripted scenarios exist.

## Chat UI and Prototype

Two front-end pieces were added on top of the API by the second team member:

- **Chat-thread web app** (`travel_agent/web/`, served at `/`): the map interface restyled as a thread of
  "bubbles". The first bubble, *Let's plan your day*, is a conversational intake: each message goes to
  `POST /api/intake`, the returned brief fills the preference form underneath (trip name, city, date, time window,
  timezone, budget, walking limit, transport, target stops, interests, rain avoidance), and a **Build itinerary**
  chip appears as soon as the brief is complete. The remaining bubbles are the SuperGrid panel, the agent
  collaboration feed, the itinerary with its metrics and scenario buttons, the review panel, and the map.
- **Prototype/**: a Lovable-generated React mock of the same layout (TanStack Start, Vite, Tailwind CSS,
  react-leaflet, lucide icons). It renders static sample data and makes no backend calls. Run it separately with
  `cd Prototype && npm i && npm run dev`; it is not part of the Flower App Bundle or the Python test suite.

The backend in this repository is the single-day planner described above; multi-day trips are not supported.

### Conversational intake (`POST /api/intake`)

The chat sends `{message, brief, history}` and receives `{reply, brief, missing, ready, request, engine, notes}`.
A deterministic parser (`travel_agent/intake.py`) extracts the city, date, time window, budget, interests,
walking limit, target stops, transport and rain preference, asks one question at a time for whatever is still
missing, and builds the `request` for `POST /api/trips` once the brief is complete. In local model mode the
same endpoint as the specialists (`TRAVEL_MODEL_BASE_URL`, `TRAVEL_MODEL_API` `responses` or `chat`) is asked
to fill the brief as well, with a 20 s timeout; the rules result is the floor and `engine` reports whether the
model contributed. The city goes through the geocoder: in fixture mode only places within 10 km of central
Berlin are accepted (Berlin itself needs no lookup, so the offline demo works without a network) and any other
city is answered with a pointer to `scripts/serve_live.sh`; in live mode any city the geocoder resolves is
accepted, and its coordinates and timezone become the request's start, finish and timezone. `/api/config`
reports `intake: true` and `supported_cities` (`["Berlin"]` in fixture mode, `null` in live mode). A request
for several days is answered with a note: the planner builds one day.

## License

MIT. Map data © OpenStreetMap contributors.
