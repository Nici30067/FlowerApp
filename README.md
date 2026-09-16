# OSM Travel Companion

Collaborative day-trip planning with four specialist agents, OpenStreetMap data, deterministic constraint
validation, and reviewable itinerary revisions. The specialists run as a **Flower AgentApp** on **Flower
SuperGrid** and use the **Flower Endeavor 1.0** model (`flower-endeavor-v1.0`). The map application on your
machine submits each planning job to SuperGrid and streams the agents' collaboration back into the browser.

Built for the Flower Collaborative Agents Hackathon (Berlin, 16 September 2026).

## What it does

1. You describe one day: city, time window, budget, walking limit, interests.
2. Four specialists collaborate on a shared itinerary state:
   `Discovery -> Conditions -> Discovery (indoor alternatives on request) -> Mobility -> Budget & Pace -> Coordinator`.
3. Application code, not the model, builds the schedule (bounded beam search) and validates it against every
   constraint: opening hours, travel time, continuous walking, breaks, budget, weather, locked reservations.
4. Every later change (rain, tighter budget, less walking, a closed place, a completed stop) produces a proposed
   revision that you inspect and apply or reject. Completed stops, locked reservations and actual spending are
   preserved.

Models rank candidates and exchange structured requests. They cannot invent places, prices, or routes, and there
are no booking, payment, or cancellation tools.

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
 Providers (travel_agent/providers/services.py): bundled fixture | live Overpass + OpenRouteService + Open-Meteo
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
report, every agent-to-agent request, model call timings, and the deterministic validation result.

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
| `TRAVEL_FLOWER_RUN_TIMEOUT_S` | `900` | per SuperGrid run; the run is stopped afterwards |
| `TRAVEL_DATA_MODE` | `fixture` | `fixture` (bundled Berlin scenario) or `live` (local backend only) |
| `TRAVEL_AGENT_MODE` | `rules` | local backend: `rules` replay or `model` via `TRAVEL_MODEL_BASE_URL` |
| `TRAVEL_API_TOKEN` | empty | required before binding to a non-loopback interface |

`.env.example` lists every variable, including the live-provider settings.

Run-config keys of the AgentApp (`pyproject.toml`): `agent.input`, `agent.model`, `agent.max-tool-turns`,
`agent.reasoning-effort`, `agent.model-timeout-s`, `agent.wall-time-s`, `agent.model-candidates`,
`agent.public-web`, `travel.data-mode`, and the bridge-only `travel.api-url`, `travel.job-id`, `travel.job-token`.

## Data modes

- **Fixture** (default): a synthetic central-Berlin catalog with labeled prices, hours and direct-line routes. Every
  record carries `status: fixture`. This is what runs on SuperGrid.
- **Live** (local backend): Overpass (OpenStreetMap), OpenRouteService and Open-Meteo. Requires `TRAVEL_CONTACT`,
  `ORS_API_KEY`, and either your own Overpass instance or `TRAVEL_ALLOW_PUBLIC_OVERPASS=true`. Prices and hours
  stay `estimated` or `unknown` when OSM tags do not verify them; nothing is fabricated.

## Safety properties

- Each specialist has a distinct prompt, context, output schema, and read-only tool allowlist. Tool names and
  arguments are validated against a schema; unknown tools return an error to the model.
- Model, tool, and wall-time budgets are bounded per planning job. Provider responses are size-limited and
  time-limited. Model JSON is parsed tolerantly (prose or code fences are stripped) with one repair turn.
- Place IDs, roles, and message addresses in model output are checked against the evidence catalog.
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
travel_agent/providers/         fixture and live providers, opening-hours parser
travel_agent/flower_backend.py  SuperGrid submission through the Flower Control API
travel_agent/jobcodec.py        bounded job/proposal transport
travel_agent/api.py, store.py   HTTP API and SQLite persistence
travel_agent/web/               map interface (Leaflet + OpenStreetMap tiles, loaded on request)
docs/                           architecture, demo script, limitations
tests/                          117 tests (no network)
```

## Known limitations

- SuperGrid workers plan on the bundled fixture; live providers are configured for the local backend only.
- Endeavor answers take roughly 20 to 90 seconds per specialist call, so a full plan takes a few minutes.
- The fixture uses direct-line distances, not walking directions; live mode uses provider geometry.
- Conversation state persists inside one `flwr chat` series; a plain `flwr run` starts from a fresh state, which
  is why scripted scenarios exist.

## License

MIT. Map data © OpenStreetMap contributors.
