# Verification record (Flower SuperGrid, 16 September 2026)

Account `@tauska67`, federation `@tauska67/workspace` (simulation runtime), Flower 1.37.0 CLI, runtime Python 3.13.

| Run ID | Purpose | Configuration | Result |
| --- | --- | --- | --- |
| 5396692141164549271 | first submission | model `flower-endeavor-v1.0`, 25 s client timeout | FAB installed and AgentApp loaded; first model call exceeded the 25 s timeout |
| 17573414323352673273 | `diagnose` model IDs | candidates `flower-endeavor-v1.0`, `flower-endeavor`, `endeavor-1.0`, `openai/gpt-5.6-sol` | `flower-endeavor-v1.0` ok 7.2 s; `flower-endeavor` ok 11.9 s (resolves to v1.0); `endeavor-1.0` rejected ("not a valid model ID"); `openai/gpt-5.6-sol` ok 3.7 s |
| 10353767203542452834 | full plan | Endeavor, 120 s timeout, strict JSON parser | model answered with prose around a fenced JSON block; parser made tolerant afterwards |
| 10099255000141560608 | `plan then rain` baseline | Endeavor, 1 tool round per specialist, default reasoning | step 1: 8 calls (28 to 91 s each), revision 1 with 5 stops, provisional; step 2 reached Conditions with a `discovery -> conditions` rain request; stopped manually after 11 calls to save credits (1128 credits charged) |
| 8395419302626907313 | `plan then rain` optimized | Endeavor, 0 tool rounds, reasoning effort low, compact context, discovery and conditions concurrent | completed in 6 min 58 s, 500 credits. Step 1: 4 calls (61 s and 62 s concurrent, 34 s, 67 s) -> revision 1, 5 stops, provisional. Step 2 (rain): 5 calls (69 s and 71 s concurrent, 66 s, 76 s, 40 s); `conditions -> discovery` request, discovery re-run, revision 2 with five indoor stops (Humboldt Forum, Neues Museum, James-Simon-Galerie, Alte Nationalgalerie, coffee stop), provisional, no errors |
| 17786761010864221019 | browser **Build itinerary** through the API server | `openai/gpt-5.6-sol`, same AgentApp job path | 4 calls of 9 to 12 s, 31 s on SuperGrid, revision 1 committed automatically, about 28 credits |
| 27774342408883161 | browser **Simulate rain** through the API server | `openai/gpt-5.6-sol` | 5 calls of 6 to 13 s, 41 s on SuperGrid, proposal awaiting review, applied as revision 2 in the browser, about 28 credits |

Structured outputs: every specialist report parsed into `AgentReport` (role, summary, ordered candidate IDs,
avoid IDs, evidence IDs, addressed requests). Observed requests: `discovery -> conditions`,
`discovery -> coordinator`, `budget_pace -> coordinator`, `conditions -> discovery`.

Credits after these runs: 1005 of the 3000 sign-up grant (16 September 2026, 10:40 UTC). One Endeavor plan-plus-rain
cycle costs about 500; one fallback-model cycle about 60.

Final demo configuration: `agent.model="flower-endeavor-v1.0"`, `agent.max-tool-turns=0`,
`agent.reasoning-effort="low"`, `agent.model-timeout-s=120`, `travel.data-mode="fixture"`.
Fallback: `agent.model="openai/gpt-5.6-sol"` with identical code.

## Google Gemini through the Chat Completions adapter (local backend, 16 September 2026)

`gemini-3.8-flash` via `https://generativelanguage.googleapis.com/v1beta/openai/` with `TRAVEL_MODEL_API=chat` and one
evidence tool round per specialist. Gemini 3.x requires its opaque `thought_signature` to be echoed back with every
replayed tool call; the adapter carries the provider's raw tool-call object through the round trip for that reason.

| Step | Result |
| --- | --- |
| Initial plan (browser, `Build itinerary`) | 9 model calls, 6 tool calls, 29.3 s total; calls of 1.6 to 7.6 s; revision 1 with 5 stops, provisional; one automatic repair turn (Mobility's first final answer was not a report object) |
| Rain replan (browser, `Simulate rain`) | 12 model calls, 7 tool calls, 40.1 s; `conditions -> discovery` request, Discovery re-run; proposed revision 2 with five indoor stops (Humboldt Forum, Neues Museum, James-Simon-Galerie, Mitte bookshop, coffee stop); applied in the browser |
| Tools used | `get_weather_forecast`, `search_places`, `get_route_matrix`, `assess_costs`, `validate_itinerary` |
| Repair turns | Gemini's first final answer for Mobility (and once for Conditions) was not a report object; the bounded repair turn recovered every time |

No SuperGrid credits are consumed in this mode. The Flower connectors and the Endeavor bonus apply only to SuperGrid runs.

Local SuperLink (zero credits): the same AgentApp ran `plan then rain` on a local `flower-superlink` whose model provider was the
Gemini Open Responses proxy (`../gemini-responses-proxy`), run 15607922627542377881, model calls of 4 to 14 s. The chat adapter now adds
reasoning-token headroom to `max_tokens` so Gemini's thinking does not truncate answers.

## Post-fix rehearsal on Gemini (16 September 2026, local backend, `scripts/serve_gemini.sh 8010`)

Model `gemini-3.8-flash` through the Chat Completions adapter, one tool turn, fixture data, after the review fixes
(174 -> 180 offline tests).

| Step | Calls | Time | Notes |
| --- | --- | --- | --- |
| Build itinerary | 7 model, 4 tool, 1 provider | 34 s | `planner.started` ranking_source `specialists`, candidate_count 12; every specialist cited real evidence ids (`unknown_evidence_ids` empty); usage reported per call (3.0k to 9.8k input tokens) |
| Simulate rain | 8 model, 5 tool, 1 provider | 54 s | Conditions ran first and asked Discovery for indoor alternatives; `planner.started` candidate_count 7, avoided_count 5; proposal all indoor (coffee, Alte Nationalgalerie, James-Simon-Galerie, Neues Museum, Humboldt Forum), provisional, override flagged |

One earlier rain attempt failed with an HTTP 400 from Gemini during Mobility's second call and succeeded on retry;
job failures are now logged server-side with the redacted cause so intermittent provider errors can be inspected.

## Any-city live planning on Gemini (16 September 2026, `scripts/serve_live.sh 8011`)

Zero-key live stack: Open-Meteo geocoding, public Overpass (with contact string), OSRM FOSSGIS routing, Open-Meteo
forecast; model `gemini-3.8-flash` through the Chat Completions adapter, one tool turn.

| Step | Result | Time |
| --- | --- | --- |
| GET /api/geocode?q=Tokyo | Tokyo, Japan, 35.6895 / 139.6917, Asia/Tokyo, `fixture_supported=false` | < 1 s |
| Build itinerary (Tokyo, art/coffee/parks/architecture) | 5 real OSM stops (Sompo Museum of Art, Bunka Gakuen Costume Museum, two cafes, a memorial museum), OSRM walking legs with polylines, 2969 m, provisional (HOURS_UNKNOWN, PRICE_UNKNOWN, OUTDOOR_TRANSFERS warnings) | 87 s |
| Simulate rain | Conditions asked Discovery for indoor alternatives; all five stops indoor; proposal awaiting review | 66 s |

The real Tokyo forecast that day was 100 percent precipitation, so the initial plan already avoided the four parks
(`planner.started` avoided_count 4) and the rain replan preserved every stop.

## Flower Hub environment (16 September 2026, local Flower runtime; SuperGrid egress pending credits)

Verified on a local insecure `flower-superlink` (127.0.0.1:9091, connection `local-agent`) whose model provider was
the Gemini Open Responses proxy, model `gemini-3.8-flash`. The SuperGrid account had 0 credits, so none of these
runs touched `supergrid`; egress from Flower's hosted runtime to Overpass, OSRM and Open-Meteo is still to be
confirmed once credits are available. Everything below used the same FAB the Hub would receive
(`tauska67.osm-travel-companion.0-2-2.21572ac8.fab`, 283191 bytes, installed as `@tauska67/osm-travel-companion==0.2.2`)
and the run-config defaults from `pyproject.toml` (`travel.data-mode="live"`, `travel.router="osrm"`, public Overpass with
the contact string). Credentials come from Flower (`FLWR_RUNTIME_*`); no provider environment variables were set.

| Run ID | Purpose | Configuration | Result |
| --- | --- | --- | --- |
| 16389144267729390984 | live natural-language plan | `agent.input="Plan a day in Kyoto tomorrow from 10:00 to 17:00, art and coffee, budget 60"`, 1 tool turn, live data | Intake completed in one message; "Located Kyoto, Japan via Open-Meteo geocoding" (35.0211 / 135.7538, Asia/Tokyo); text line "Live data: OpenStreetMap via Overpass, OSRM routes, Open-Meteo weather (osrm)"; `evidence.loaded` place_count 12, weather_status live; 7 model calls of 4.2 to 13.1 s, 4 tool calls (`get_weather_forecast`, `search_places`, `assess_costs`, `validate_itinerary`), coordinator elapsed 61.0 s; proposed revision 1 with five OSM stops: 中信美術館 10:02 to 10:47, 蒸しまん＆カフェ まんまん堂 10:53 to 11:23, Morpho cafe 11:40 to 12:10, レインボウ 12:10 to 12:40, 樂美術館 13:00 to 13:45 (`osm-node-14161661601`, `-2684562849`, `-2684583206`, `-2684529110`, `-1422966440`); walking 1721 m; provisional (PRICE_UNKNOWN, HOURS_UNKNOWN); no traceback, exit 0 |
| 1967189214739825181, 218357287088548199 | same command, first two attempts | identical | reached the "Live data" line, then `ProviderError: overpass-api.de request failed (504)` (Overpass: "server is probably too busy"); the app published the provider-failure text, the Flower task ended with Exit Code 800, the `flwr run` CLI still exited 0 |
| 16879081083787617208 | clarifying question | `agent.input="Plan a day trip for me"`, 0 tool turns | 2 s (15:27:36Z to 15:27:38Z); exactly three events (user message, one `response.output_text.delta`, `response.completed`); asked "Which city are you visiting? Any city works: I look it up for you." followed by "Still needed: city, date, time window, interests."; zero model calls (no `travel.model.completed` events, no model-call log lines, no `POST /v1/runtime/responses` on the SuperLink during the task) |
| 7087680461104305825 | `plan then rain` on the Berlin fixture | `travel.data-mode="fixture"`, 0 tool turns | 59 s wall, finished 15:28:24Z. Step 1: 4 calls (11.1, 7.1, 9.8, 6.3 s), coordinator 27.3 s, revision 1 (Museum Island promenade, Neues Museum, Monbijou Park, Hackesche Hoefe courtyards, Hackescher Markt coffee stop) committed to continue the scenario. Step 2 (rain, simulated): 4 calls (6.3, 7.8, 6.3, 6.3 s), coordinator 26.8 s, revision 2 all indoor: Hackescher Markt coffee stop 10:00, Mitte bookshop 10:34, James-Simon-Galerie 11:26, Neues Museum 12:08, Humboldt Forum 13:39; added `demo-bookshop`, `demo-gallery`, `demo-forum`, removed `demo-museum-walk`, `demo-monbijou`, `demo-courtyards`; both `coordinator.started` events report `data_mode: fixture` |
| 136881638298839129 | `status` after the Kyoto plan | live defaults | "Committed revision: none. Pending proposal: none. Conversation brief: none. Data mode: live (osrm)." Each CLI `flwr run` opens a new run series on this SuperLink, so the follow-up did not see the earlier proposal |

Bundle: `uv run flwr build` packs 23 entries including `travel_agent/intake.py`, `settings.py`, `providers/services.py`,
`providers/opening_hours.py`, `fixtures/berlin.json`, `LICENSE` and `README.md`; no `.env`, key, sqlite or runtime files.
The packed `pyproject.toml` declares 23 run-config keys (`agent.*`, `travel.*`) and the code reads exactly those 23. The
packed tree imports on its own (`ProviderSettings.from_run_config` -> live, osrm; `make_provider`, `make_geocoder` present).

Routing: the same code path as the AgentApp (`ProviderSettings.from_run_config` -> `make_provider` -> `LiveProvider`)
against `https://routing.openstreetmap.de/routed-{profile}/{service}/v1/driving` returned a matrix leg (`osrm-table`,
1046 m, 837 s) and a route leg (`osrm-route`, 1046 m, 837 s, 28 geometry points). Proposal events carry place ids only,
so leg provenance was checked here rather than in the run events.

Open items for the SuperGrid pass:

- Public Overpass reliability: the identical Kyoto command failed 2 of 3 times with a 504 on the `nwr` cafe block
  (13.6 s and 14.4 s to the timeout; the museum and gallery blocks alone answer in 2.5 s, a node-only variant in 2.1 s,
  the same shape for Tokyo in 3.0 s). `CachedHTTP.request` makes a single attempt with no retry, backoff or alternate
  endpoint. Suggested: retry with backoff on 504/429 and a lighter cafe query, so a busy instance does not abort the plan.
- The `flwr run` CLI exits 0 even when the AgentApp task ends with Exit Code 800; scripts must inspect the log or events.
- Same-series follow-ups (rain, budget 30, walk 2 km, refresh, approve, reject, export) were not exercised from the CLI:
  `flwr run` exposes no series option, so each invocation starts a fresh series. They remain covered by the offline tests
  and need the Hub UI or `flwr chat` for a live check.
- `flwr run --stream` shows only process stdout (the `[role] model call N ... took Xs` lines); the narrative and the
  `travel.*` events are read from the run-series events (Hub UI, `flwr chat`, Control API).

Final gate on the delivered tree: `uv run pytest -q` 326 passed, 0 failed; `uv run ruff check .` clean;
`uv run flwr build` succeeded (283191 bytes) and the FAB was removed afterwards.
