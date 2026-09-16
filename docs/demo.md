# Two-minute demo script

Measured on 16 September 2026: with `flower-endeavor-v1.0` one planning step takes about 2 to 2.5 minutes on
SuperGrid (Discovery and Conditions run concurrently on a dry forecast, then Mobility, then Budget & Pace). A rain
replan runs Conditions before Discovery, the same number of sequential calls as the Discovery re-run it replaces,
about 3 minutes. Those timings were taken on the fixture data; live OpenStreetMap data adds the provider calls
(geocode, Overpass, forecast, OSRM table and route), which took well under the model time in the local-backend
rehearsal (`docs/verification.md`). Prepare the slow parts before the judges arrive and keep the live part short.

Two variants follow: the map application driven by SuperGrid (variant A, the verified path) and the AgentApp
alone on Flower Hub / SuperGrid with live data (variant B, one sentence plus follow-ups).

## Variant A: map application driven by SuperGrid

### Preparation (10 minutes before)

1. `uv run flwr login supergrid` on the demo machine (once).
2. `./scripts/serve_supergrid.sh`, open http://127.0.0.1:8000, press **Load OSM map**.
3. Press **Build itinerary** and wait for revision 1 (SuperGrid, Endeavor).
4. Press **Simulate rain** and wait for the proposed revision. Leave it pending: the agent activity feed, the
   agent-to-agent requests, the SuperGrid run ID and the review panel all stay on screen.

### Live (about 120 seconds)

| Time | Action | What to say |
| --- | --- | --- |
| 0:00 | Map with revision 1 and the pending rain proposal | One day in a city is a constraint problem: opening hours, budget, walking, weather. Four specialist agents plan it together as a Flower AgentApp on SuperGrid, using Flower Endeavor. |
| 0:20 | Point at the agent activity feed and the metrics line | Discovery ranked places, Conditions checked the forecast, Mobility checked routes, Budget & Pace reviewed the schedule. Application code built and validated the schedule; the models cannot invent places or prices. The line under the specialists reads `N model calls · N tool calls · N provider calls · Ns`: provider calls are the read-only place, forecast and route lookups, counted apart from model calls. |
| 0:45 | Point at the handoff entries | When rain arrived, Conditions ran first and asked Discovery for indoor alternatives. This is the request, this is the answer, and every model call is timed and bounded; a truncated answer or a catalog fallback would be labeled here too. |
| 1:10 | Review panel | The proposal lists what was added, removed and preserved, who triggered it, and the deterministic validation result. The locked reservation is untouched. |
| 1:30 | Press **Apply revision** | Revision 2 is committed. The SuperGrid run ID is on screen; `flwr run . supergrid --stream` reproduces it from the terminal. |
| 1:50 | Flower Hub page and repository | Open source, MIT, offline test suite, published on Flower Hub. |

## Variant B: Flower Hub / SuperGrid, one sentence and follow-ups (live OpenStreetMap data)

This is the path a judge can reproduce with nothing but the Hub listing: `flwr run . supergrid` or Flower Chat,
no map application, no keys. It plans any city from live OpenStreetMap data (`travel.data-mode = "live"` is the
default in `pyproject.toml`). Use Flower Chat for the live part so that the follow-ups share one run series and
act on the plan you just approved; a plain `flwr run` starts every invocation from an empty state.

### Preparation (15 minutes before)

1. Check the credit balance at https://flower.ai/settings/billing (SuperGrid refuses runs at 0) and rehearse the
   exact sentence once, so the Overpass and OSRM answers for that city are known to arrive; the public instances
   are shared demo servers.
2. `uv sync && uv run flwr build && uv run flwr login supergrid`.
3. Open a Flower Chat session for the app (or a terminal with `uv run flwr chat`; it uses the default connection in
   `~/.flwr/config.toml`) and send the sentence:
   `Plan a day in Tokyo tomorrow, art and coffee, budget 60` (add `10 to 18` to choose the window; without it the
   day runs 10:00 to 17:00 and the run says so). Wait for the proposed revision 1: it starts with
   `Planning A day in Tokyo: ... Located Tokyo, Japan via Open-Meteo geocoding.` and
   `Live data: OpenStreetMap via Overpass, OSRM routes, Open-Meteo weather (osrm)`, and ends with the
   approve/reject prompt. Send `approve`.
4. Send `rain` and wait for the proposed revision. Leave it pending on screen.

The equivalent one-shot terminal command for a rehearsal, with the same code path:

```bash
uv run flwr run . supergrid \
  --run-config 'agent.input="Plan a day in Tokyo tomorrow, art and coffee, budget 60"' --stream
```

The description may also open a scripted chain for a one-run rehearsal of the whole story:
`agent.input="Plan a day in Tokyo tomorrow, art and coffee, budget 60 then rain then budget 30"` (valid revisions
auto-commit between steps).

### Live (about 120 seconds)

| Time | Action | What to say |
| --- | --- | --- |
| 0:00 | Scroll to the top of the transcript: the sentence, the `Planning ... Located Tokyo, Japan via Open-Meteo geocoding.` line and the `Live data:` line | One sentence: city, date, interests, budget. A deterministic intake turned it into a validated request; the city was geocoded, and every place, route and forecast comes from OpenStreetMap, OSRM and Open-Meteo, live, with no API key. |
| 0:20 | The specialist lines of revision 1 | Discovery ranked real OSM places, Conditions read the real forecast, Mobility used OSRM walking times, Budget & Pace reviewed the schedule. Application code built and validated the schedule; the models cannot invent places or prices, and unknown prices are counted, not guessed. |
| 0:45 | The `conditions -> discovery` line of the rain replan and the pending proposal | Rain: Conditions ran first and asked Discovery for indoor alternatives. Added and removed stops are listed; anything locked or already completed is preserved. |
| 1:10 | Send `approve` | Revision 2 is committed in this run series. |
| 1:25 | Send `budget 30` (or `walk 2 km`) | A tighter budget is just another event: same specialists, a new proposed revision against the committed plan. Do not wait for it unless the fallback model is configured (next section); say what will happen and move on. |
| 1:40 | Send `status`, then `export` | The committed revision and the pending proposal are state of the run series; `export` prints the whole itinerary as JSON, the same object the map application renders. |
| 1:50 | Flower Hub page and repository | Open source, MIT, offline test suite, run-config keys documented in the README, published on Flower Hub. |

### Notes for variant B

- Latency: Endeavor answers took 20 to 90 seconds per specialist call on the fixture (16 September 2026); the live
  path on SuperGrid has not been timed yet because the account had no credits left. Pre-run steps 3 and 4 and
  only send the cheap follow-ups live.
- Incomplete sentence: when the city, the date or the interests are missing the app asks one question, adds
  `Still needed: ... So far: ...`, and remembers the partial brief for the next message of the series (a missing
  time window is not asked for; the day runs 10:00 to 17:00 with a note). That is a fine thing to show if there
  is time, but not in the first 30 seconds.
- Fixture fallback for a room without egress to the OpenStreetMap services:
  `--run-config 'agent.input="Berlin tomorrow, 10 to 17, art and history" travel.data-mode="fixture"'`.

## Alternatives

- Live replanning within one minute: `TRAVEL_MODEL=openai/gpt-5.6-sol` for variant A, or
  `--run-config 'agent.model="openai/gpt-5.6-sol"'` for variant B (same code path, same Flower runtime; Endeavor
  stays documented as the primary model).
- No network at all: `./run_demo.sh` shows the identical collaboration with the deterministic specialist replay in
  under two seconds, labeled "Fixture replay · 0 model calls".
- Any city, live data, in the browser: `TRAVEL_CONTACT=... GEMINI_API_KEY=... ./scripts/serve_live.sh` (port 8011),
  type a city, press **Locate**, then **Build itinerary**; OpenStreetMap places, Open-Meteo weather and OSRM routes,
  labeled "Live data". Rehearse it once beforehand: the public OSRM and Overpass instances are shared demo servers.
- Zero credits: the same AgentApp and the same sentence on the local SuperLink
  (`uv run flwr run . local-agent --run-config '... agent.model="gemini-3.8-flash" agent.max-tool-turns=1 agent.reasoning-effort=""' --stream`,
  see the README). Live OpenStreetMap data works there; Flower's web connectors do not.
