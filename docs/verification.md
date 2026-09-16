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
