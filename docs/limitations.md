# Known limitations

## Live OpenStreetMap data on SuperGrid

- Outbound HTTPS from SuperGrid workers to `overpass-api.de`, `routing.openstreetmap.de`, `api.open-meteo.com`
  and `geocoding-api.open-meteo.com` has not been verified: the account's credit balance was 0 when the live
  AgentApp path was finished, and SuperGrid refuses runs without a positive balance. The live path was exercised
  on the local Flower runtime (`local-agent`), whose worker has ordinary outbound access. A blocked host shows up
  as a provider error in the run output; nothing falls back silently. `travel.data-mode="fixture"` needs no
  egress.
- Public service etiquette. The public Overpass instance, the FOSSGIS OSRM demo servers and Open-Meteo are shared,
  rate-limited services run for the community (published fair use: roughly 10,000 Overpass queries and 1 GB a
  day per IP; OSRM "no bulk requests, not for production"; Open-Meteo about 10,000 calls a day, non-commercial).
  SuperGrid workers share their egress addresses with every other app on the platform, so those per-IP limits
  are shared too, and a `429` or a timeout fails the planning step (provider errors are reported, never
  papered over). The app keeps its footprint small: one bounded Overpass query per planning step with
  `[timeout:25]`, one forecast call, one OSRM table and one route call, size- and time-limited responses, and
  a `User-Agent` of `OSMTravelCompanion/0.2 (<travel.contact>)` on every request. The bundled default contact
  string names the app, not a person: replace `travel.contact` with an address the operators can reach when you
  run the app yourself, and point `travel.overpass-url` / `travel.osrm-url-template` at your own instances (or
  `travel.router="ors"` with your own OpenRouteService key) for anything beyond a demo. The in-process TTL cache
  does not survive between runs, so every run of a series re-queries the services.
- `travel.ors-api-key` travels through the run config. Do not put a key into the published defaults of a Hub
  app; pass it per run on your own SuperLink, or keep OSRM.

## Intake and state

- The intake is a rules parser, not a model: it understands the documented shapes (a city after `in`/`to`/at the
  start of the sentence or as a bare answer, `today`/`tomorrow`/`next Saturday`/`5 January`/ISO dates, `10 to 18`
  or `9am to 5pm`, `budget 60`/`60 euros`/`cheap`, `walk 6 km`, `by bike`, `4 stops`, the interest vocabulary
  art, architecture, parks, coffee, history, food, books, shopping) and asks one question for a missing city,
  date or interest, with a `Still needed: ... So far: ...` line. A missing time window is not asked for: the day
  is planned 10:00 to 17:00 local time and the run says so. The geocoder is the final judge of a city name; a
  name it does not know is dropped from the brief and asked again, a lookup outage keeps the name and says how
  to retry. Ambiguous names resolve to the most populous match (no country filter): the `Planning ... Located
  <name>, <country>` line shows what was chosen. The model-based enrichment of the brief exists only in the
  local backend's `POST /api/intake`.
- Brief persistence per run series. The partial brief lives in `context.state["travel"]["brief"]`, so a clarifying
  question can only be answered inside the same run series (Flower Chat). A standalone `flwr run` starts from an
  empty state: give it the complete sentence in one go; the description may open a `then` chain
  (`... budget 60 then rain then budget 30`), and a description with gaps asks its question and skips the chained
  steps. Once a plan has been built the brief is cleared; the next sentence starts a new brief. A new sentence in the middle of an intake
  merges onto the stored brief (scalars overwrite, interests are unioned unless the sentence signals a
  replacement such as `actually` or `just`).
- The planner builds one day. A sentence asking for several days plans one day and says so.
- `flwr run` starts from an empty `Context`; multi-step conversations persist only inside a run series (Flower
  Chat, `flwr chat`) or via scripted scenarios such as `plan then rain`.

## Data quality

- Live mode plans are usually `provisional`: prices and opening hours come only from OSM tags (`fee`, `charge`,
  `opening_hours`), which most places lack, so their cost stays `unknown` and the budget check cannot account
  for them. The review panel and the run summary list the unknown-price count with every revision.
- Place names fall back to the local script when OSM has no `name:en` tag, so a Tokyo plan mixes English and
  Japanese names.
- A place whose indoor status is unknown counts as exposed: with **Avoid outdoor visits in rain** on, it is
  treated like an outdoor place during rainy intervals. Live data infers indoor status from the category
  (museums, galleries, cafes, restaurants, bookshops and malls count as sheltered unless tagged `indoor=no` or
  `outdoor_seating=only`; parks count as exposed); attractions and memorials stay unknown unless tagged.
- Geocoding takes the most populous populated place matching the typed name; there is no country filter, so
  check the status line (`Located <name>, <country> · <timezone> · <lat>, <lon>` in the browser, the city and
  country named in the run summary) before building.
- Fixture routes are direct lines scaled by 1.28, not walking directions. The fixture covers central Berlin
  only: in fixture mode a city more than 10 km from the fixture centre is refused with a warning (browser
  **Locate**) or a message suggesting `travel.data-mode="live"` (AgentApp).
- The opening-hours parser covers common patterns (24/7, day ranges, multiple intervals, `off`). Public holidays,
  sunrise/sunset and month rules are reported as unknown.

## Model execution and credits

- Endeavor latency: 20 to 90 seconds per specialist call measured on 16 September 2026 (fixture data). Discovery
  and Conditions run concurrently when the forecast is dry; when it is wet, Conditions runs first so its avoid
  list reaches Discovery, which serialises those two calls. The default configuration makes one call per
  specialist. Live data adds the provider calls before the first model call; on SuperGrid they are not timed yet.
- SuperGrid credits: Endeavor calls are charged per token. A three-call run with 15 KB tool outputs cost 303 credits
  on 16 September 2026, which is why the default configuration uses no tool rounds, a compact context, low
  reasoning effort, and a model-call cap of 5 × (tool turns + 2) per planning step (10 with the default 0 tool
  turns; `max-model-calls = 0` selects this automatic value). With those defaults one Endeavor plan-plus-rain
  scenario cost 500 credits; the same scenario on `openai/gpt-5.6-sol` cost about 60. The balance reached 0 later
  that day.
- Tool turns default to 0 (`TRAVEL_MAX_TOOL_TURNS`, `agent.max-tool-turns`). Each specialist then makes exactly one
  bounded model call with the compact evidence already in its context: the model is offered no tools, so
  `search_places`, `get_place_details`, `get_weather_forecast`, `get_route_matrix`, `assess_costs`,
  `validate_itinerary` and the optional `web_search`/`web_fetch` connectors are never invoked by a model, the
  tool-name and argument validation and the tool-call budget are not exercised, and the `tool calls` metric stays
  at 0 (provider calls such as the route matrix are counted separately as `provider calls`). Set the value to 1 or
  more to enable evidence tool rounds at the cost of additional model calls.
- Token usage (`N in / N out tokens` in the agent feed) appears only when the runtime returns `response.usage`;
  otherwise the feed shows timings only.
- The local Flower runtime (`local-agent`, Gemini behind an Open Responses proxy) has no web connectors, so
  `agent.public-web=true` only works on SuperGrid.

## Map application

- The map application's SuperGrid launcher (`scripts/serve_supergrid.sh`) defaults to `TRAVEL_DATA_MODE=fixture`;
  that is the configuration verified on SuperGrid. **Locate** works in every mode, but in fixture mode a city more
  than 10 km from the fixture centre is refused with a warning that points to `scripts/serve_live.sh`.
- The legacy bridge mode requires a public HTTPS origin; control mode replaces it for the demo.
