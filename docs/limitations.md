# Known limitations

- SuperGrid runs use the bundled Berlin fixture. Live providers run in the local backend only
  (`scripts/serve_live.sh`); workers receive neither provider access nor `TRAVEL_CONTACT`. **Locate** works in
  every mode, but in fixture mode a city more than 10 km from the fixture centre is refused with a warning.
- The public OSRM demo servers (`routing.openstreetmap.de`, FOSSGIS) and the public Overpass instance are shared
  and rate limited. They are suited to a rehearsal, not to production; self-host them or switch to
  OpenRouteService (`TRAVEL_ROUTER=ors`) for anything more.
- Live mode plans are usually `provisional`: prices and opening hours come only from OSM tags (`fee`, `charge`,
  `opening_hours`), which most places lack, so their cost stays `unknown` and the budget check cannot account
  for them. The review panel lists the unknown-price count with every revision.
- Place names fall back to the local script when OSM has no `name:en` tag, so a Tokyo plan mixes English and
  Japanese names.
- A place whose indoor status is unknown counts as exposed: with **Avoid outdoor visits in rain** on, it is
  treated like an outdoor place during rainy intervals. Live data infers indoor status from the category
  (museums, galleries, cafes, restaurants, bookshops and malls count as sheltered unless tagged `indoor=no` or
  `outdoor_seating=only`; parks count as exposed); attractions and memorials stay unknown unless tagged.
- Geocoding takes the most populous populated place matching the typed name; there is no country filter, so
  check the status line (`Located <name>, <country> · <timezone> · <lat>, <lon>`) before building.
- Endeavor latency: 20 to 90 seconds per specialist call measured on 16 September 2026. Discovery and Conditions
  run concurrently when the forecast is dry; when it is wet, Conditions runs first so its avoid list reaches
  Discovery, which serialises those two calls. The default configuration makes one call per specialist.
- Fixture routes are direct lines scaled by 1.28, not walking directions.
- The opening-hours parser covers common patterns (24/7, day ranges, multiple intervals, `off`). Public holidays,
  sunrise/sunset and month rules are reported as unknown.
- `flwr run` starts from an empty `Context`; multi-step conversations persist only inside a `flwr chat` series or
  via scripted scenarios such as `plan then rain`.
- The legacy bridge mode requires a public HTTPS origin; control mode replaces it for the demo.
- SuperGrid credits: Endeavor calls are charged per token. A three-call run with 15 KB tool outputs cost 303 credits on
  16 September 2026, which is why the default configuration uses no tool rounds, a compact context, low reasoning
  effort, and a model-call cap of 5 × (tool turns + 2) per planning step (10 with the default 0 tool turns;
  `max-model-calls = 0` selects this automatic value). With those defaults one Endeavor plan-plus-rain scenario
  cost 500 credits; the same scenario on `openai/gpt-5.6-sol` cost about 60.
- Tool turns default to 0 (`TRAVEL_MAX_TOOL_TURNS`, `agent.max-tool-turns`). Each specialist then makes exactly one
  bounded model call with the compact evidence already in its context: the model is offered no tools, so
  `search_places`, `get_place_details`, `get_weather_forecast`, `get_route_matrix`, `assess_costs`,
  `validate_itinerary` and the optional `web_search`/`web_fetch` connectors are never invoked by a model, the
  tool-name and argument validation and the tool-call budget are not exercised, and the `tool calls` metric stays
  at 0 (provider calls such as the route matrix are counted separately as `provider calls`). Set the value to 1 or
  more to enable evidence tool rounds at the cost of additional model calls.
- Token usage (`N in / N out tokens` in the agent feed) appears only when the runtime returns `response.usage`;
  otherwise the feed shows timings only.
