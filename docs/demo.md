# Two-minute demo script

Measured on 16 September 2026: with `flower-endeavor-v1.0` one planning step takes about 2 to 2.5 minutes on
SuperGrid (Discovery and Conditions run concurrently on a dry forecast, then Mobility, then Budget & Pace). A rain
replan runs Conditions before Discovery, the same number of sequential calls as the Discovery re-run it replaces,
about 3 minutes. Prepare the slow parts before the judges arrive and keep the live part short.

## Preparation (10 minutes before)

1. `uv run flwr login supergrid` on the demo machine (once).
2. `./scripts/serve_supergrid.sh`, open http://127.0.0.1:8000, press **Load street map**.
3. Press **Build itinerary** and wait for revision 1 (SuperGrid, Endeavor).
4. Press **Simulate rain** and wait for the proposed revision. Leave it pending: the agent activity feed, the
   agent-to-agent requests, the SuperGrid run ID and the review panel all stay on screen.

## Live (about 120 seconds)

| Time | Action | What to say |
| --- | --- | --- |
| 0:00 | Map with revision 1 and the pending rain proposal | One day in a city is a constraint problem: opening hours, budget, walking, weather. Four specialist agents plan it together as a Flower AgentApp on SuperGrid, using Flower Endeavor. |
| 0:20 | Point at the agent activity feed and the metrics line | Discovery ranked places, Conditions checked the forecast, Mobility checked routes, Budget & Pace reviewed the schedule. Application code built and validated the schedule; the models cannot invent places or prices. The line under the specialists reads `N model calls · N tool calls · N provider calls · Ns`: provider calls are the read-only place, forecast and route lookups, counted apart from model calls. |
| 0:45 | Point at the handoff entries | When rain arrived, Conditions ran first and asked Discovery for indoor alternatives. This is the request, this is the answer, and every model call is timed and bounded; a truncated answer or a catalog fallback would be labeled here too. |
| 1:10 | Review panel | The proposal lists what was added, removed and preserved, who triggered it, and the deterministic validation result. The locked reservation is untouched. |
| 1:30 | Press **Apply revision** | Revision 2 is committed. The SuperGrid run ID is on screen; `flwr run . supergrid --stream` reproduces it from the terminal. |
| 1:50 | Flower Hub page and repository | Open source, MIT, 222 offline tests, published on Flower Hub. |

## Alternatives

- Live replanning within one minute: start the server with `TRAVEL_MODEL=openai/gpt-5.6-sol` (same code path, same
  Flower runtime; Endeavor stays documented as the primary model).
- No network at all: `./run_demo.sh` shows the identical collaboration with the deterministic specialist replay in
  under two seconds, labeled "Fixture replay · 0 model calls".
- Any city, live data (optional): `TRAVEL_CONTACT=... GEMINI_API_KEY=... ./scripts/serve_live.sh` (port 8011), type a
  city, press **Locate**, then **Build itinerary**; OpenStreetMap places, Open-Meteo weather and OSRM routes,
  labeled "Live data". Rehearse it once beforehand: the public OSRM and Overpass instances are shared demo servers.
