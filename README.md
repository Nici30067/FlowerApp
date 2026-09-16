# OSM Travel Companion

Collaborative day-trip planning with four specialist agents, live OpenStreetMap data, deterministic constraint
validation, and reviewable itinerary revisions. The specialists run as a **Flower AgentApp** on **Flower
SuperGrid** and use the **Flower Endeavor 1.0** model (`flower-endeavor-v1.0`). Describe your day to the AgentApp
in one sentence (`flwr run`, Flower Chat), or use the map application on your machine, which submits each
planning job to SuperGrid and streams the agents' collaboration back into the browser.

Built for the Flower Collaborative Agents Hackathon (Berlin, 16 September 2026).

## What it does

1. You describe one day: city (any city; the geocoder resolves it), date, time window, budget, walking limit,
   interests. To the AgentApp that is one sentence, for example
   `Plan a day in Tokyo tomorrow, art and coffee, budget 60`; in the browser it is the form plus **Locate**.
2. Four specialists collaborate on a shared itinerary state:
   `Discovery || Conditions (Conditions first when the forecast is wet) -> Discovery (indoor alternatives on request)
   -> Mobility -> Budget & Pace -> Coordinator`.
3. Application code, not the model, builds the schedule (bounded beam search) and validates it against every
   constraint: opening hours, travel time, continuous walking, breaks, budget, weather, locked reservations.
4. Every later change (rain, tighter budget, less walking, a closed place, a completed stop) produces a proposed
   revision that you inspect and apply or reject. Completed stops, locked reservations and actual spending are
   preserved.

Places, opening hours, prices and routes come from OpenStreetMap (Overpass), OSRM and Open-Meteo without any API
key; a bundled central-Berlin fixture remains available for offline runs. Models rank candidates and exchange
structured requests. They cannot invent places, prices, or routes, and there are no booking, payment, or
cancellation tools.

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
 Flower Chat / flwr run                      Browser (OpenStreetMap map, timeline, agent activity, review panel)
 one sentence, then follow-ups                   |  HTTPS/JSON + Server-Sent Events
        |  run-config agent.input                v
        |                                FastAPI server (travel_agent/api.py) ---- SQLite store: trips, proposals, jobs, events
        |                                        |
        |                   TRAVEL_EXECUTION_BACKEND=local            TRAVEL_EXECUTION_BACKEND=flower
        |                   (rules replay or a local model URL)       (Flower Control API, same login as `flwr login`)
        v                                        |                                     v
 Flower AgentApp (travel_agent/agent_app.py)     |                     SuperGrid run of this AgentApp (job mode:
   intake (travel_agent/intake.py):              |                     the server hands over the trip snapshot)
   sentence -> TripBrief -> geocoder             |                                     |
   -> TripRequest; follow-up commands            |                                     |
   FLWR_RUNTIME_BASE_URL -> OpenAI Responses API -> flower-endeavor-v1.0 (fallback: openai/gpt-5.6-sol)
        |                                        |                                     |
        v                                        v                                     v
 Coordinator (travel_agent/coordinator.py): shared itinerary state, specialist sequencing, proposal
        v
 Specialists (travel_agent/agents.py): discovery | conditions | mobility | budget_pace
   each has its own instructions, context, output contract, and read-only tool allowlist
        v
 Planning engine (travel_agent/planning/engine.py): beam search + independent validation
        v
 Providers (travel_agent/providers/services.py)
   live (default inside a Flower run): Overpass (OpenStreetMap) + OSRM or OpenRouteService + Open-Meteo
   fixture: bundled central-Berlin catalog
   configured by the travel.* run-config keys inside a Flower run, by TRAVEL_* variables in the local backend
 Geocoder (same module): Open-Meteo geocoding, used by the intake and by GET /api/geocode in every data mode
```

The AgentApp entry point is `travel_agent/agent_app.py`. Inside a Flower run there are no environment variables:
the model credentials are injected by Flower (`FLWR_RUNTIME_*`) and everything the live OpenStreetMap stack
needs comes from the `travel.*` run-config keys declared in `pyproject.toml`
(`ProviderSettings.from_run_config` in `travel_agent/settings.py`). In SuperGrid mode the map application encodes
the trip snapshot and the triggering event into `agent.input`, starts the run, relays the run events (`travel.*`)
to the browser, and receives the proposal as one structured run event. The proposal is validated again locally
before it can be reviewed. No public callback URL, tunnel, or per-job secret is needed.

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
| Open-Meteo geocoding (`GEOCODER_URL`, `travel.geocoder-url`) | city name to coordinates, timezone and country; the most populous populated place with that name wins | none | non-commercial use, published fair-use limit about 10,000 calls a day; answers are cached |
| Overpass (`TRAVEL_OVERPASS_URL`, `travel.overpass-url`) | OpenStreetMap places with their tags (`name`, `name:en`, `opening_hours`, `fee`, `charge`, indoor hints) | none | the public instance only with `TRAVEL_ALLOW_PUBLIC_OVERPASS=true` / `travel.allow-public-overpass=true`; one bounded query per planning step with `[timeout:25]`; published fair use is roughly 10,000 queries and 1 GB a day per IP; run your own instance beyond a demo |
| Open-Meteo forecast (`OPEN_METEO_URL`, `travel.weather-url`) | hourly precipitation probability and temperature | none | same fair-use terms as the geocoder |
| OSRM, public FOSSGIS instance (`OSRM_URL_TEMPLATE`, `travel.osrm-url-template`) | walking (`routed-foot`) and cycling (`routed-bike`) travel-time matrix and route geometry | none | a demo server run for the OSM community: rate limited, no bulk requests, not for production; self-host OSRM or use OpenRouteService for anything else |
| OpenRouteService (`ORS_BASE_URL`, `TRAVEL_ROUTER=ors` / `travel.router="ors"`) | the same matrix and directions under a per-account quota | `ORS_API_KEY` / `travel.ors-api-key` (free account) | becomes the default router whenever `ORS_API_KEY` is set (local backend) |

Every live request carries `User-Agent: OSMTravelCompanion/0.2 (<contact>)`, so set `TRAVEL_CONTACT` (locally) or
`travel.contact` (inside a Flower run) to a real email address or URL that the operators can reach;
`serve_live.sh` refuses to start without it. Prices and hours come from OSM tags only: where a tag is missing the
value stays `unknown`, the validation status is `provisional`, and the review panel counts the unknown prices.
`flwr run . supergrid` plans from the same live stack by default (next section); the map application's SuperGrid
launcher `scripts/serve_supergrid.sh` still defaults to `TRAVEL_DATA_MODE=fixture`, so **Locate** shows the fixture
warning there for cities outside Berlin.

## Run on Flower Hub / SuperGrid with live data

This follows the layout of the official sample (`@flwrlabs/hackathon-collab-agent-recipe`): set up once, run with
`--stream`, override `agent.input` per run. Requirements: Python 3.11+, `uv`, a SuperGrid account with a positive
credit balance. The model credentials are injected by Flower's runtime; the OpenStreetMap stack needs no key.

### Setup

```bash
uv sync
uv run flwr build                          # validates the Flower App Bundle
uv run flwr login supergrid                # one-time browser login
```

### Run

```bash
uv run flwr run . supergrid --stream       # plans the default Berlin day, now from live OpenStreetMap data
```

Describe any day in one sentence; the city is geocoded, places come from Overpass, routes from OSRM, weather from
Open-Meteo:

```bash
uv run flwr run . supergrid \
  --run-config 'agent.input="Plan a day in Tokyo tomorrow, art and coffee, budget 60"' \
  --stream
```

The stream opens with what the intake understood, for example
`No time window given: planning 10:00 to 17:00 local time (say for example 9 to 18 to change it).` and
`Planning A day in Tokyo: 2026-09-17 10:00 to 17:00 (Asia/Tokyo); interests art, coffee; budget 60.00 EUR; ...
Located Tokyo, Japan via Open-Meteo geocoding.`, then
`Live data: OpenStreetMap via Overpass, OSRM routes, Open-Meteo weather (osrm)`. It narrates every specialist and
agent-to-agent request and ends with the proposed revision (real OSM place names, walking distance, accounted
cost, unknown-price count, validation status) and the prompt
`Type approve to commit this revision, or reject to keep the current itinerary.`

Other overrides (several keys fit into one `--run-config` string, separated by spaces):

```bash
# another city, a weekday, a walking limit
uv run flwr run . supergrid \
  --run-config 'agent.input="A day in Lisbon next Saturday, 9am to 5pm, history and food, walk 6 km"' --stream

# fixture fallback: the bundled central-Berlin catalog, no outbound requests from the worker
uv run flwr run . supergrid --run-config 'travel.data-mode="fixture"' --stream

# model fallback (faster, cheaper, identical code path); Endeavor stays the default
uv run flwr run . supergrid \
  --run-config 'agent.input="Berlin tomorrow, 10 to 17, art and history" agent.model="openai/gpt-5.6-sol"' --stream

# your own contact string (please do this when you run the app yourself) and your own Overpass instance
uv run flwr run . supergrid \
  --run-config 'travel.contact="you@example.org" travel.overpass-url="https://overpass.example.org/api/interpreter"' \
  --stream
```

The intake needs a city, a date and at least one interest (vocabulary: art, architecture, parks, coffee, history,
food, books, shopping). A sentence without a time window is planned 10:00 to 17:00 local time, and the run says
so; add `9 to 18` or `9am to 5pm` to choose your own. Optional details: `budget 60` or `60 euros` or `cheap`,
`walk 6 km`, `by bike`, `4 stops`, `2026-10-03` or `next Saturday`. When the city, the date or the interests are
missing, the run answers with a single question plus a line such as
`Still needed: date. So far: city Tokyo; interests art, coffee.` and remembers the partial brief for the next run
of the same series (see "Follow-ups and the run series"). A plain `flwr run` starts every invocation from an
empty state, so give it the complete sentence. A city name the geocoder does not know is asked again; in fixture
mode a real city outside central Berlin is refused with a message that suggests
`--run-config 'travel.data-mode="live"'`.

### Run-config keys

Defaults live in `pyproject.toml` under `[tool.flwr.app.config.agent]` and `[tool.flwr.app.config.travel]`.
An empty string means the default shown in the table.

| Key | Default | Meaning |
| --- | --- | --- |
| `agent.input` | `Plan the Berlin demonstration itinerary.` | one sentence describing the day, a follow-up command, a scripted scenario (`plan then rain`) or JSON (`{"request": ...}`, `{"event": ...}`) |
| `agent.model` | `flower-endeavor-v1.0` | runtime model ID; `openai/gpt-5.6-sol` is the verified fallback |
| `agent.model-candidates` | `""` | comma-separated model IDs probed by the `diagnose` input |
| `agent.max-tool-turns` | `0` | evidence tool rounds per specialist, 0 to 10; 0 = one bounded model call each |
| `agent.reasoning-effort` | `low` | Responses API reasoning effort; `""` omits the field (chat-style endpoints such as the local Gemini proxy) |
| `agent.model-timeout-s` | `120` | seconds per model call |
| `agent.wall-time-s` | `900` | wall-time budget per planning step |
| `agent.max-model-calls` | `0` | model-call cap per planning step; 0 = auto: 5 × (tool turns + 2) |
| `agent.max-output-tokens` | `2000` | output cap per model call; a truncated answer is reported as `model.incomplete` |
| `agent.public-web` | `false` | offer Flower's `web_search`/`web_fetch` connectors to Discovery; requires `agent.max-tool-turns` ≥ 1; SuperGrid only |
| `travel.data-mode` | `live` | `live` (OpenStreetMap stack) or `fixture` (bundled central-Berlin catalog) |
| `travel.contact` | `osm-travel-companion Flower Hub AgentApp` | goes into `User-Agent: OSMTravelCompanion/0.2 (<contact>)` on every live request; replace it with an address the service operators can reach |
| `travel.allow-public-overpass` | `true` | explicit opt-in for `overpass-api.de`; set `false` when `travel.overpass-url` points at your own instance |
| `travel.router` | `osrm` | `osrm` (public FOSSGIS instance, no key) or `ors` (OpenRouteService, needs `travel.ors-api-key`) |
| `travel.osrm-url-template` | `""` = `https://routing.openstreetmap.de/routed-{profile}/{service}/v1/driving` | HTTPS template; `{profile}` becomes `foot` or `bike`, `{service}` `table` or `route` |
| `travel.overpass-url` | `""` = `https://overpass-api.de/api/interpreter` | Overpass endpoint |
| `travel.ors-url` | `""` = `https://api.openrouteservice.org` | OpenRouteService endpoint |
| `travel.ors-api-key` | `""` | OpenRouteService key; only read when the router is `ors`. Never put a key into published defaults |
| `travel.weather-url` | `""` = `https://api.open-meteo.com/v1/forecast` | Open-Meteo forecast |
| `travel.geocoder-url` | `""` = `https://geocoding-api.open-meteo.com/v1/search` | Open-Meteo geocoding, used by the intake to resolve the city |
| `travel.api-url`, `travel.job-id`, `travel.job-token` | `""` | legacy bridge mode only (the map application uses the Control API instead) |

### Follow-ups and the run series

Once a plan exists, the same inputs the map application offers work as text:

| Input | Effect |
| --- | --- |
| `rain` | simulated rain: Conditions runs first and asks Discovery for indoor alternatives; a proposed revision follows |
| `refresh` | clears the simulated override and reloads the live forecast |
| `budget 30` | tighter budget in major units; a proposed revision follows |
| `walk 2 km` | walking limit; a proposed revision follows |
| `approve` / `reject` | commit or discard the pending proposal (locked stops and completed stops are preserved) |
| `status` | committed revision, pending proposal, the conversation brief collected so far, and the data mode with its router |
| `export` | the committed snapshot as JSON |
| `help` | the natural-language form first, then the commands above |
| `diagnose` | probes the model IDs in `agent.model-candidates`; no planning |

In Flower Chat (the interactive chat for Hub apps; `uv run flwr chat` in the terminal, which uses the default
connection in `~/.flwr/config.toml`) every message becomes a new run in the same **run series**, and the
AgentApp's `context.state` travels with the series. It holds the committed snapshot, the pending proposal and the
intake brief, so a clarifying question can be answered in the next message, `rain` acts on the itinerary you just
approved, and `status` or `export` read the committed revision. A plain `flwr run` is a series of one: chain steps
with `then` instead. The description may open the chain
(`--run-config 'agent.input="Plan a day in Tokyo tomorrow, art and coffee, budget 60 then rain then budget 30"'`),
or `plan` builds the default central-Berlin day (`agent.input="plan then rain"`); valid revisions auto-commit
between steps, at most 6 steps per run, and a description that still has gaps asks its question and skips the
scripted steps.

### Network access from SuperGrid workers

Live mode needs outbound HTTPS from the worker to `overpass-api.de`, `routing.openstreetmap.de`,
`api.open-meteo.com` and `geocoding-api.open-meteo.com` (or to the endpoints you configure). The live path was
verified on the local Flower runtime (see "Zero-credit local run"); verification of SuperGrid worker egress is
pending a positive credit balance on the account. A blocked host surfaces as a provider error in the run output;
nothing falls back silently. `travel.data-mode="fixture"` needs no outbound access at all.

### Debugging a run

```bash
uv run flwr list supergrid                              # run IDs, status, elapsed time
uv run flwr log <run-id> supergrid --show               # the text stream of a finished run
uv run python scripts/run_events.py <run-id> --seconds 30   # the structured travel.* events the map application consumes
```

### Map application driven by SuperGrid

```bash
uv run flwr login supergrid
./scripts/serve_supergrid.sh               # http://127.0.0.1:8000, backend=flower, model=flower-endeavor-v1.0
```

**Build itinerary** is the only action. The right-hand panel shows the SuperGrid run ID, each specialist's
report, every agent-to-agent request, model call timings (with token usage when the runtime reports it), a
metrics line in the form `N model calls · N tool calls · N provider calls · Ns`, and the deterministic
validation result. This path was verified with the fixture data mode.

### Publish to Flower Hub

```bash
uv run flwr build
uv run flwr login supergrid
uv run flwr app publish .
```

`publisher` in `pyproject.toml` must equal the signed-in account name, and the version must be higher than any
version already on the Hub page (see `docs/submission-checklist.md` for the current state of the listing).

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
uv run flwr run . local-agent \
  --run-config 'agent.input="Plan a day in Tokyo tomorrow, art and coffee, budget 60" agent.model="gemini-3.8-flash" agent.max-tool-turns=1 agent.reasoning-effort=""' \
  --stream
```

`local-agent` is the connection name of that SuperLink in `~/.flwr/config.toml`. Catalog model IDs such as
`flower-endeavor-v1.0` or `openai/gpt-5.6-sol` are mapped to Gemini by the proxy, so the default `agent.model`
works there too. The worker runs on your machine and has ordinary outbound HTTPS, so the live OpenStreetMap
stack, the same `travel.*` run-config keys and the follow-up commands behave as they do on SuperGrid. Flower's
built-in web connectors are not available on the local runtime.

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
| `TRAVEL_DATA_MODE` | `fixture` | `fixture` (bundled Berlin scenario) or `live`, for the local backend; inside a Flower run `travel.data-mode` applies instead |
| `TRAVEL_AGENT_MODE` | `rules` | local backend: `rules` replay or `model` via `TRAVEL_MODEL_BASE_URL` |
| `TRAVEL_MODEL_API` | `responses` | local model mode: `responses` or `chat` (Gemini and other Chat Completions providers) |
| `TRAVEL_ROUTER` | `osrm`, or `ors` when `ORS_API_KEY` is set | live mode router: `osrm` (public FOSSGIS instance, no key) or `ors` (OpenRouteService, requires `ORS_API_KEY`) |
| `TRAVEL_ALLOW_PUBLIC_OVERPASS` | `false` | explicit opt-in for the public `overpass-api.de` instance |
| `OSRM_URL_TEMPLATE` | `https://routing.openstreetmap.de/routed-{profile}/{service}/v1/driving` | OSRM endpoint; `{profile}` becomes `foot` or `bike`, `{service}` `table` or `route`; https only |
| `GEOCODER_URL` | `https://geocoding-api.open-meteo.com/v1/search` | geocoder behind `GET /api/geocode`, used in every data mode |
| `TRAVEL_CONTACT` | empty | contact (email or URL) in the `User-Agent` of every live request; `serve_live.sh` requires it |
| `TRAVEL_API_TOKEN` | empty | required before binding to a non-loopback interface |

`.env.example` lists every variable, including the live-provider settings, and shows which `travel.*` run-config
key replaces each of them inside a Flower run. The run-config keys themselves (`agent.*`, `travel.*`) are listed
under "Run on Flower Hub / SuperGrid with live data".

## Data modes

- **Fixture**: a synthetic central-Berlin catalog with labeled prices, hours and direct-line routes. Every record
  carries `status: fixture`. It is the default of the local backend (`TRAVEL_DATA_MODE=fixture`) and the fallback
  of the AgentApp (`travel.data-mode="fixture"`). **Locate** and the intake work here too, but a city more than
  10 km from the fixture centre is refused with a warning that points to live mode (`scripts/serve_live.sh` in the
  browser, `travel.data-mode="live"` in a Flower run).
- **Live**: Overpass (OpenStreetMap), Open-Meteo, and OSRM (router `osrm`, no key) or OpenRouteService (router
  `ors` with an API key). It is the default of the AgentApp inside a Flower run (`[tool.flwr.app.config.travel]`
  declares `data-mode = "live"`, a contact string and the public-Overpass opt-in) and is selected locally with
  `TRAVEL_DATA_MODE=live`, `TRAVEL_CONTACT` and either your own Overpass instance or
  `TRAVEL_ALLOW_PUBLIC_OVERPASS=true`. Prices and hours stay `estimated` or `unknown` when OSM tags do not verify
  them; nothing is fabricated. See "Plan another city (no API keys)" above.

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
- The intake is a deterministic parser: free text becomes a `TripBrief`, the city is checked against the geocoder,
  and the resulting `TripRequest` is validated like any other request before a model is called.
- Place IDs, roles, message addresses, and evidence IDs in model output are checked against the evidence
  catalog; unknown evidence IDs are dropped and reported as `unknown_evidence_ids`.
- Results from SuperGrid are re-validated locally; a worker cannot change user constraints, progress, or data
  provenance.
- Runtime credentials stay in the AgentApp process; job payloads are integrity-checked and size-bounded. Provider
  endpoints from the run config must be HTTPS (OSRM template, geocoder), the public Overpass instance needs the
  explicit opt-in, and every live request carries the contact string. The AgentApp never receives the map
  application's API token; `travel.ors-api-key` is the only provider secret that can reach a worker, and only
  when you select the `ors` router yourself.
- OSM and web text are treated as untrusted data and escaped in the browser.

## Repository map

```text
travel_agent/agent_app.py       Flower AgentApp entry point (natural-language intake, commands, scenarios, SuperGrid job mode)
travel_agent/intake.py          deterministic conversational intake: sentence -> TripBrief -> TripRequest (also behind POST /api/intake)
travel_agent/agents.py          specialist roles, tool dispatcher, bounded model runner
travel_agent/coordinator.py     shared-state collaboration and proposal generation
travel_agent/planning/engine.py schedule search and independent validation
travel_agent/providers/         fixture and live providers (Overpass, OSRM or OpenRouteService, Open-Meteo, geocoder), opening-hours parser
travel_agent/settings.py        ModelSettings and ProviderSettings (from_env for the local backend, from_run_config for Flower runs)
travel_agent/flower_backend.py  SuperGrid submission through the Flower Control API
travel_agent/jobcodec.py        bounded job/proposal transport
travel_agent/api.py, store.py   HTTP API and SQLite persistence
travel_agent/web/               map interface (Leaflet + OpenStreetMap tiles, loaded on request; city Locate)
docs/                           architecture, demo script, limitations, submission checklist, verification record
tests/                          offline test suite (no network): uv run pytest -q
```

## Known limitations

- The public OSRM and Overpass instances are shared demo servers with rate limits, and SuperGrid workers share
  their egress. They are fine for a rehearsal and unsuited to production; configure your own instances through
  `travel.overpass-url` / `travel.osrm-url-template` (or `travel.router="ors"`) for anything more.
- Live plans are usually `provisional`: most OSM places carry no `fee`, `charge` or `opening_hours` tag, so their
  cost and hours stay `unknown`.
- Endeavor answers take roughly 20 to 90 seconds per specialist call, so a full plan takes a few minutes.
- The fixture uses direct-line distances, not walking directions; live mode uses provider geometry.
- Conversation state, including a half-filled intake brief, persists inside one run series (Flower Chat); a plain
  `flwr run` starts from a fresh state, which is why the sentence should name city, date and interests in one go
  and why `then` chains exist.
- Outbound HTTPS from SuperGrid workers to the OpenStreetMap services is not yet verified (credit balance 0 at the
  time of writing); the local Flower runtime is the verified path.

See `docs/limitations.md` for the full list.

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
