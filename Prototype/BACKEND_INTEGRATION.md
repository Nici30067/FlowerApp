# Prototype: backend integration notes

How to make the Lovable/React mock in this directory talk to the real planner in `travel_agent/`, and what
each mocked structure corresponds to in the API. The canonical description of the API is
[`docs/frontend-contract.md`](../docs/frontend-contract.md); where this file and the contract disagree, the
contract wins, and both derive from `travel_agent/api.py` and `travel_agent/schemas.py`.

Units everywhere in the real API: **metres, seconds, minor currency units (cents), timezone-aware ISO-8601
datetimes**. The mock uses kilometres, minutes, whole euros and minutes-from-midnight; every mapping below is
also a unit conversion.

---

## 1. What the Prototype is today (audit, 2026-09-16)

| Question | Finding |
| --- | --- |
| Stack | TanStack Start 1.168 (SSR, file routes), Vite 8.1.5, React 19, Tailwind 4, Leaflet 1.9 (used directly; `react-leaflet` is installed but unused), lucide, zod. Wrapped by `@lovable.dev/vite-tanstack-config` 2.22. |
| Does it call the backend? | **No.** `grep -rn 'fetch(\|EventSource\|/api/' src` finds no request to `travel_agent`. The only outbound traffic at runtime is OpenStreetMap tiles (`MapView.tsx`) and Google Fonts (`__root.tsx`). |
| Any other network code? | `src/lib/travel/gateway.server.ts` posts to `https://ai.gateway.lovable.dev/v1/responses` using `process.env.LOVABLE_API_KEY`. It is **dead code**: nothing imports it. `lovable-error-reporting.ts` only calls `window.__lovableEvents` hooks that exist inside the Lovable editor. |
| Secrets | None committed. No `.env*` files; the only key reference is the unused `LOVABLE_API_KEY` env lookup. Keep it that way: the backend owns every model credential (`TRAVEL_MODEL_*`). |
| What is mocked | `src/lib/travel/data.ts`: 3 cities (Berlin, Paris, Lisbon) with invented hourly forecasts and ~40 places with invented EUR prices, hours and visit lengths. `src/lib/travel.functions.ts`: `intakeTurn` (regex parser, needs city + hours + budget + interests) and `runSpecialists` (a deterministic ranking that fabricates four "findings"). `src/lib/travel/planner.ts`: client-side scheduling (haversine, 4.5 km/h, rest break after 210 min). `src/lib/travel/diff.ts`: client-side diff. All state lives in React `useState`; a refresh loses everything. |
| Meant to replace `travel_agent/web`? | No. It is the **design reference** for the chat-thread layout that `travel_agent/web/` now implements (same bubbles, "C" avatar, intake chat, specialist cards, timeline, review/diff panel, sticky map). The root README already calls it a mock. The shipped UI stays `travel_agent/web/`, served by FastAPI at `/`. This document is the recipe if the Prototype is ever to go live. |
| Standalone status | `npm ci` **fails** with `npm error Invalid Version:` (npm/arborist bug: `package.json` has no `version` field and `overrides` is set). `npm install` works: 419 packages, ~345 MB in `node_modules`, ~2 min. `tsc --noEmit` is clean. `vite build` succeeds (~2 MB in `.output/`, nitro `cloudflare-module` preset by default; it also writes an untracked `.wrangler/` directory). Dev server: Vite's default port (5173) unless `vite.server.port` is set; 8080 is forced only inside the Lovable sandbox. |
| Dead weight | 34 shadcn/ui components under `src/components/ui/` are not imported by any travel screen; `react-leaflet`, `recharts`, `react-hook-form`, `embla-carousel`, `cmdk`, `vaul`, `input-otp` are unused. |

---

## 2. Pointing the Prototype at the real backend

### 2.1 Run a backend on a free port

```sh
# offline: bundled Berlin fixture, rule-based specialists, no network
TRAVEL_DB=runtime/proto.sqlite3 uv run python -m travel_agent.cli serve --port 8020

# any city: Open-Meteo geocoding, Overpass places, OSRM routing, model specialists
TRAVEL_CONTACT="you@example.org" GEMINI_API_KEY=... ./scripts/serve_live.sh 8021
```

Pick a port nothing else is using (in this workspace 8000-8011 belong to running servers; the examples use
8020/8021). `GET /health` answers `{"status":"ok","version":"0.3.1"}` when the server is up.

### 2.2 Why a plain `VITE_API_BASE` does not work

Three guards in `travel_agent/api.py` (`security` middleware) rule out calling the backend cross-origin from
the browser:

1. Every `POST/PUT/PATCH/DELETE` whose `Origin` netloc differs from the request `Host` is refused with
   `403 {"detail": "Cross-origin writes are disabled"}`.
2. The app sets no CORS headers, so even `GET` responses are unreadable from another origin.
3. Without `TRAVEL_API_TOKEN`, only requests whose `Host` is `localhost` / `127.0.0.1` / `::1` are served.

So the Prototype must be **same-origin** with the API: proxy `/api` through the Vite dev server (development)
or through one reverse proxy in front of both servers (production).

### 2.3 Dev proxy (`vite.config.ts`)

`@lovable.dev/vite-tanstack-config` merges an `options.vite` block with Vite's `mergeConfig` (verified in
2.22.0), so the standard Vite proxy works:

```ts
import { defineConfig } from "@lovable.dev/vite-tanstack-config";

const API = process.env["TRAVEL_API_URL"] ?? "http://127.0.0.1:8020";

export default defineConfig({
  tanstackStart: { server: { entry: "server" } },
  vite: {
    server: {
      proxy: {
        "/api": {
          target: API,
          changeOrigin: true, // Host becomes 127.0.0.1:8020, which the localhost guard accepts
          configure(proxy) {
            // The backend compares Origin with Host on every write; drop the browser's Origin so
            // the request looks like a same-origin call. Node's http-proxy streams SSE unbuffered.
            proxy.on("proxyReq", (req) => req.removeHeader("origin"));
          },
        },
        "/health": { target: API, changeOrigin: true },
      },
    },
  },
});
```

Then every call in the client is a relative URL: `fetch("/api/trips", ...)`, `new EventSource("/api/jobs/<id>/events")`.

### 2.4 Client helper

Replace the two `createServerFn` wrappers with one thin fetch helper (the reference implementation is
`api()`/`post()` at the top of `travel_agent/web/app.js`):

```ts
export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const res = await fetch(path, {
    ...init,
    headers: { "Content-Type": "application/json", ...(init.headers ?? {}) },
  });
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`;
    try { detail = (await res.json()).detail ?? detail; } catch { /* keep status text */ }
    throw new Error(detail); // 400/404/409/422 all carry a human-readable `detail`
  }
  return res.json() as Promise<T>;
}
```

Errors worth surfacing verbatim: `409 Refresh the current trip revision before requesting changes` (stale
`base_revision`), `409 Proposal is no longer pending`, `409 An infeasible proposal cannot be committed`,
`422 <validation message>` for a bad event payload, `404 No place matched that name` from geocoding.

### 2.5 Authentication (only when `TRAVEL_API_TOKEN` is set)

`GET /api/config` reports `auth_required: true`. Every other `/api/*` route then needs either
`Authorization: Bearer <token>` or the `travel_session` cookie that `POST /api/login {"token": "..."}` sets
(httpOnly, SameSite=Strict, 8 h). Through the proxy the cookie is same-origin and just works, so the UI only
needs a login dialog on a 401. Never bake the token into the bundle.

### 2.6 Production

- Keep `travel_agent/web/` as the shipped UI (recommended for the hackathon), or
- `vite build` with `nitro: { preset: "node-server" }` in `defineConfig`, run the Node server, and put one
  reverse proxy in front that routes `/api/*` and `/health` to FastAPI (stripping `Origin`) and everything
  else to Node. A static SPA served by FastAPI is not an option without further work: TanStack Start renders
  on the server and `api.py` only mounts `travel_agent/web/`.

### 2.7 Files to delete once the backend is wired

`src/lib/travel.functions.ts`, `src/lib/travel/gateway.server.ts`, `src/lib/travel/data.ts`,
`src/lib/travel/planner.ts` (keep a time formatter, rewritten for ISO datetimes, see 5.4),
most of `src/lib/travel/diff.ts` (the server supplies added/removed/preserved; only "retimed" stays client
side). `src/lib/travel/types.ts` becomes the API types below. The components themselves survive with prop
changes.

---

## 3. Endpoints the flow uses

| Method and path | Body | Returns | Used by |
| --- | --- | --- | --- |
| `GET /api/config` | - | `data_mode`, `agent_mode`, `execution_backend`, `model`, `auth_required`, `tile_url`, `default_request` (a full `TripRequest`), `fixture_notice`, `live_notice`, `geocoding`, `intake: true`, `supported_cities` (`["Berlin"]` in fixture mode, `null` = any city in live mode) | header mode pill, example chips, map tiles |
| `GET /api/geocode?q=<text>` | - | `{query, name, country, admin1, lat, lon, timezone, population, fixture_supported, source}`; 400 (2-80 chars), 404 no match, 503 provider down | map centre before a trip exists; the intake already calls it server-side |
| `POST /api/intake` | `{message (<=600 chars), brief: TripBrief, history: [{role, text}] (<=20)}` | `{reply, brief, missing[], ready, request \| null, engine: "rules"\|"model", notes[]}` | `IntakeChat` |
| `POST /api/trips` | `{request: TripRequest}` (use the `request` the intake returned) | `{trip_id, job}` | first plan |
| `GET /api/jobs/{job_id}/events?since=N` | - | SSE stream, see section 4 | specialist panel, progress |
| `GET /api/jobs/{job_id}` | - | `{id, trip_id, base, status, error, proposal_id, external}` (+ `flower_command`, `capability_notice` in SuperGrid bridge mode). `status` is `queued` -> `running` -> `awaiting_review` (proposal produced) -> `completed` (accepted or rejected), or `failed`; anything other than `queued`/`running` is terminal for the stream | finish sequence, `onerror` fallback |
| `GET /api/trips/{trip_id}` | - | `{trip: TripSnapshot, proposals: Proposal[]}` (pending proposals only, newest first) | everything that renders |
| `POST /api/trips/{trip_id}/events` | `{base_revision, event: {id, kind, payload, simulated}}` | `{trip_id, job}`; 409 if `base_revision != trip.revision`; 422 on a bad payload | trigger chips, lock, done |
| `POST /api/proposals/{id}/accept` | - | the new `TripSnapshot`; 409 if not pending or infeasible | `DiffPanel` Accept |
| `POST /api/proposals/{id}/reject` | - | `{ok: true}` | `DiffPanel` Keep current |
| `GET /api/trips/{trip_id}/export` | - | the `TripSnapshot` as a JSON download | optional export button |
| `POST /api/trips/import` | a `TripSnapshot` (<=1 MB, <=24 places, <=8 stops) | `{trip_id}` | optional import |
| `GET /api/trips` | - | `[{id, revision, title}]` (last 30) | optional "recent trips" |

Event `id` must match `^[A-Za-z0-9_-]+$` (<=100 chars); `crypto.randomUUID().replaceAll("-", "")` is what
the web client uses. Re-posting the same event id with the same payload returns the existing job (idempotent);
the same id with a different payload is rejected.

---

## 4. The SSE job feed

`GET /api/jobs/{job_id}/events` is `text/event-stream`. Each message is

```
id: <seq>
data: {"seq": <int>, "type": "<event type>", "data": {...}}
```

with `: keepalive` comment lines every 200 ms, resumption via `?since=<seq>` or the `Last-Event-ID` header
(EventSource sends it on reconnect), and a terminal

```
event: done
data: {"status": "completed" | "failed"}
```

The server stops streaming after about 15 minutes regardless. Finish sequence, copied from `watchJob()` in
`travel_agent/web/app.js`:

1. On `done` (or on `onerror` when `GET /api/jobs/{id}` says the status is no longer `queued`/`running`):
   close the source.
2. `GET /api/trips/{trip_id}` and `GET /api/jobs/{job_id}` in parallel.
3. `trip` is the live state; the pending proposal is `proposals.find(p => p.id === job.proposal_id)`.
4. If `job.status === "failed"`, show `job.error`.

The **first** plan is auto-accepted server-side when its validation is `valid` (`revision` 0 -> 1, the
itinerary appears on `trip.itinerary`, `proposals` is empty). If the first plan is infeasible it stays a
pending proposal with `validation.valid === false`; Accept would 409, so show the issues and offer a new trip.
Every later revision is a pending proposal until the user accepts or rejects it, and only one can be pending
at a time (the web client refuses new events while one is pending).

Event types and payloads (`data` field):

| type | data | UI use |
| --- | --- | --- |
| `coordinator.started` | `{base_revision, data_mode, agent_mode, trigger, simulated}` | start the "planning" state; `trigger` is `initial_plan` or the event kind |
| `evidence.loaded` | `{place_count, weather_status, data_mode}` | "Loaded N places" line |
| `agent.started` | `{role, engine: "rules"\|"model", model}` | mark that specialist card as working |
| `tool.completed` / `tool.failed` | `{role, name, engine}` / `{role, name, error}` | per-card activity line |
| `model.completed` | `{role, requested_model, returned_model, elapsed_s, call_number, usage}` | latency and token badge |
| `model.incomplete` / `model.repair` | `{role, call_number, max_output_tokens}` / `{role, reason}` | "answer was cut off, retrying" note |
| `agent.completed` | `{role, engine, model?, report: AgentReport, unknown_evidence_ids}` | fill the card (section 5.6) |
| `collaboration.message` | `AgentMessage {sender, recipient, kind, summary, place_ids, evidence_ids}` | "Mobility asked Discovery: ..." rows |
| `planner.fallback` | `{reason, candidate_count}` | notice that the catalogue order was used |
| `planner.started` | `{algorithm, beam_width, candidate_count, avoided_count, ranking_source}` | progress line |
| `planner.repair` | `{adopted, reason, proposal_score, alternative_score}` | note when the budget specialist's alternative won |
| `validation.completed` | `Validation {valid, status, issues[]}` | preview of warnings before the proposal loads |
| `proposal.ready` | `{proposal_id, added[], removed[], preserved[], metrics}` | can pre-render the diff |
| `job.finished` | `{job_id, proposal_id, metrics: {model_calls, tool_calls, provider_calls, ...}}` | run metrics |
| `job.failed` | `{error}` | error banner |
| `flower.submitting` / `flower.run.started` / `flower.run.finished` | SuperGrid backend only | optional panel |

Specialist `role` values are `discovery`, `conditions`, `mobility`, `budget_pace` (the mock says `budget`).

---

## 5. Component-by-component mapping

### 5.1 `IntakeChat` and `intakeTurn` -> `POST /api/intake`

| Mock | Real | Notes |
| --- | --- | --- |
| `messages: ChatMessage[]` `{role, content}` | `history: IntakeMessage[]` `{role, text}` | send the last 20; the current user line goes in `message`, not in `history` |
| `Brief.cityId` (`"berlin"`) | `TripBrief.city` (`"Berlin"`) | the backend returns the geocoder's canonical spelling; an unknown name comes back as `null` with a `reply` asking again |
| - | `TripBrief.date` (`"YYYY-MM-DD"`) | **required by the real intake, absent in the mock.** Parsed from `today`, `tomorrow`, `next saturday`, `on saturday`, `12 October`, ISO dates. A bare "Saturday" is not a date, so the example chip "Saturday in Berlin, 10 to 5, ..." leaves `missing = ["date"]` and the assistant asks for it. Add a Date row to the facts card. |
| `startMin` / `endMin` (minutes) | `start_time` / `end_time` (`"HH:MM"`) | `end <= start` rolls to the next day; total window <= 18 h |
| `budgetEur` (euros, required) | `budget_minor` (cents, **optional**) | display `budget_minor / 100` with `request.currency`; the real brief is complete without it (default 6000) |
| `interests: string[]` | `interests: string[]` | required in both |
| `maxWalkKm` | `max_walking_m` | display `/ 1000`; optional (default 5000) |
| - | `transport_mode`, `target_stops`, `avoid_rain_outdoor_visits`, `title`, `timezone` | optional extras the parser may fill; show `title` as the trip name |
| `IntakeResult.complete` | `ready` | when `ready && request`, `request` is the exact body for `POST /api/trips {request}` |
| `IntakeResult.reply` | `reply` | same |
| - | `missing: string[]` (`city`, `date`, `time_window`, `interests`) | highlight the unfilled rows instead of showing "unknown" for all of them (error prevention) |
| - | `notes: string[]` | e.g. "This planner builds one day at a time, so I will plan a single day of your 3-day trip." Render as a quiet system line |
| - | `engine` | "rules" or "model": small badge for visibility of what parsed the message |
| `EXAMPLES` (Berlin, Lisbon, Paris) | `config.supported_cities` | in fixture mode only Berlin plans; other cities get "I can only plan Berlin on this server". Generate the chips from `supported_cities`, or show non-Berlin examples only when `config.data_mode === "live"`, and put a real date word in them |

The mock auto-plans as soon as the brief completes; the web client shows a "Build itinerary" chip instead.
Either is fine; whichever is chosen, disable the input while the job runs and re-enable it afterwards (the
mock disables the input forever once an itinerary exists, which blocks free-text follow-ups the backend
supports through `preferences_changed`).

### 5.2 `CITIES` / `getCity` -> geocode, request and weather

| Mock `City` | Real | Notes |
| --- | --- | --- |
| `id`, `name`, `country` | `trip.request.city`; before a trip exists `GET /api/geocode` `{name, country, admin1}` | |
| `center {lat, lng}` | `trip.request.origin {lat, lon}` (also `destination`); geocode `{lat, lon}` | note `lon`, not `lng` |
| `forecast: HourForecast[] {hour, sky, tempC}` | `trip.weather.intervals[]` `ForecastInterval {start, end, precipitation_probability_pct, temperature_c}` plus `trip.weather.source {provider, status, note}` | derive `sky`: `pct >= request.rain_threshold_pct` -> rain, else cloud/sun by your own cut-off. `trip.weather_override` is set after a simulated rain event; label it "simulated scenario" |
| `summary` | none | drop, or use `config.fixture_notice` / `config.live_notice` |

### 5.3 `PLACES` / `getPlace` -> `trip.places[]`

| Mock `Place` | Real `Place` | Notes |
| --- | --- | --- |
| `id` | `id` | stop and proposal ids refer to these |
| `name`, `blurb` | `name`, `description` | |
| `lat`, `lng` | `coordinate.lat`, `coordinate.lon` | |
| `category`, `tags` | `categories: string[]` | |
| `priceEur: number \| null` | `cost_minor: int \| null`, `cost_status: fixture\|verified\|estimated\|unknown`, `currency` | `null` stays "unknown"; never invent a price. Show `cost_status` when it is `estimated` |
| `durationMin` | `visit_duration_s` | |
| `outdoor: boolean` | `indoor: true \| false \| null` | three states; `null` renders as "exposure unknown" |
| `hours: {open, close} \| null` | `opening_hours` (OSM string) and `opening_intervals[] {start, end}` \| null, `opening_status` | |
| - | `source {provider, status, reference, note}`, `website`, `osm_type`, `osm_id` | evidence line in the stop details |

### 5.4 `planner.ts` / `Itinerary` / `Stop` -> `trip.itinerary`

Delete `buildItinerary`; the backend owns times, distances, breaks and totals.

| Mock | Real | Notes |
| --- | --- | --- |
| `Itinerary.revision` | `trip.revision` | header "v{n}" |
| `Itinerary.cityId` | `trip.request.city` | |
| `stops` (places and `kind: "break"` entries interleaved) | `itinerary.stops[]` plus `itinerary.breaks[]` `{location_id, start, end, reason}` | interleave by `start`; a break's `location_id` is the previous node (`"origin"` or a place id) |
| `totalWalkKm` | `itinerary.walking_m / 1000` | also `travel_duration_s` for "N min on foot" |
| `totalCostEur`, `unknownCostCount` | `itinerary.cost_minor / 100`, `itinerary.unknown_cost_count` | keep the "+" suffix when `unknown_cost_count > 0` |
| `endsAtMin` | `itinerary.end_arrival` (arrival back at `destination`) | |
| `warnings: string[]` | `itinerary.validation {valid, status: valid\|provisional\|infeasible, issues[]}` | issues with `place_id === null` are itinerary-level; codes include `BUDGET_LIMIT`, `WALKING_LIMIT`, `CONTINUOUS_WALK`, `END_TIME`, `TOO_FEW_STOPS`, `SYNTHETIC_ROUTES`, `NO_FEASIBLE_PLAN` |
| `Stop.placeId`, `name`, `reason` | `place_id`, `name`, `reason` | `reason` can be empty (rules mode); fall back to the place description |
| `Stop.kind` | stops vs `breaks` | |
| `arriveMin`, `leaveMin` | `arrival`, `start`, `end` | `arrival < start` means waiting for opening (the mock's "Opens at ... waiting on arrival") |
| `walkKm`, `walkMin` | `itinerary.legs[i]` for `stops[i]`: `distance_m / 1000`, `ceil(duration_s / 60)`, `mode` | legs run `origin -> stops[0] -> ... -> destination`; the final leg is the return the web client shows as "N min walking to finish" |
| `costEur` | `cost_minor`, `cost_status` | |
| `locked` | `locked` (derived from `request.reservations`) | |
| `done` | `completed` (also `trip.progress.completed_place_ids`) | |
| `warnings: string[]` per stop | `validation.issues.filter(i => i.place_id === stop.place_id)` | codes such as `HOURS_UNKNOWN`, `PRICE_UNKNOWN`, `PRICE_UNVERIFIED`, `EXPOSURE_UNKNOWN`, `CLOSED`, `WEATHER_CONFLICT`, `OUTDOOR_TRANSFERS`; `severity` is `warning` or `error` |
| `lat`, `lng`, `outdoor` | look up `trip.places` by `place_id` | |
| `fmtTime(min)` | format an ISO datetime **in the trip's zone**: `new Intl.DateTimeFormat(undefined, {hour: "2-digit", minute: "2-digit", hour12: false, timeZone: trip.request.timezone}).format(new Date(iso))` | never format in the browser's zone |

### 5.5 `Timeline` lock / done buttons -> trip events

The mock flips local state instantly. Against the backend each click is an event that schedules a job and
yields a proposal to review, so the button needs a pending state and the itinerary must not change until
Accept.

| Button | `POST /api/trips/{id}/events` body (`base_revision: trip.revision`) | Constraints |
| --- | --- | --- |
| Lock | `event: {id, kind: "lock_stop", payload: {place_id}}` | the stop must be in the current itinerary; becomes a `reservation` at its current start/duration |
| Unlock | `kind: "unlock_stop", payload: {place_id}` | |
| Done | `kind: "stop_completed", payload: {place_id, actual_spent_minor, completed_at}, simulated` | only the **next** uncompleted stop can be completed, in order; `completed_at` is a tz-aware ISO time between `stop.start` and `request.end`. In fixture mode the web client sends `simulated: true` and `completed_at = stop.end`; live mode sends `new Date().toISOString()` |

### 5.6 `AgentPanels` / `AgentFinding` -> SSE `agent.*` events and `trip.reports[]`

| Mock | Real | Notes |
| --- | --- | --- |
| `agent: discovery\|conditions\|mobility\|budget` | `role: discovery\|conditions\|mobility\|budget_pace` | rename the `budget` key |
| `running` (whole grid pulses) | per role: working between `agent.started` and `agent.completed` | real status instead of a fake pulse |
| `headline` | `report.summary` (<=1800 chars; first sentence as headline) | |
| `notes[]` | `report.candidate_ids.length`, `report.avoid_ids.length`, `report.requests[].summary`, `evidence_ids.length`; `tool.completed` names; `model.completed` `elapsed_s` and `usage` | |
| - | `engine` and `model` from `agent.started` | badge: "rules" or the model id |
| after a reload | `trip.reports[]` is the same `AgentReport` list for the committed revision | the web client replays them as synthetic `agent.completed` events |

### 5.7 `DiffPanel` and `diff.ts` -> `Proposal`

| Mock | Real | Notes |
| --- | --- | --- |
| `Proposal.itinerary` | `proposal.proposed` (a full `TripSnapshot`; itinerary at `proposed.itinerary`, places at `proposed.places`) | |
| `rows` computed client-side | `proposal.added[]`, `removed[]`, `preserved[]` (place ids) | names via a map over `trip.places` and `proposed.places`. "Retimed" is still client-side: same `place_id` in both itineraries with a different `start` |
| `reason` | `proposal.trigger` (`initial_plan` or the event kind) plus "simulated scenario" when `proposed.weather_override` is set | |
| `revision + 1` label | `proposal.proposed.revision` | |
| `onAccept` | `POST /api/proposals/{id}/accept` then `GET /api/trips/{id}` | disable when `proposed.itinerary.validation.valid === false` (the server would 409) |
| `onDiscard` | `POST /api/proposals/{id}/reject` then `GET /api/trips/{id}` | |
| "Locked - should not be removed" row | `proposed.request.reservations.length` | the backend never drops a reservation; show "N locked reservation(s) protected" |
| - | `proposed.messages.filter(m => m.kind === "request")` | the inter-agent requests the web client lists above the diff |

### 5.8 Trigger chips in `routes/index.tsx` -> event kinds

| Chip | Event | Payload |
| --- | --- | --- |
| Rain this afternoon | `rain` | `{}` with `simulated: true` (the server refuses an unlabelled rain injection); it overrides the forecast from `progress.now ?? request.start` to `request.end` with 95 % precipitation |
| Too much walking | `pace_changed` | `{max_walking_m: Math.max(300, Math.round(itinerary.walking_m * 0.6))}` (optionally `max_continuous_walking_s`) |
| Running 45 min late | `user_running_late` | `{now: <ISO, tz-aware>, location: {lat, lon}}`; `now` must be after `progress.now ?? request.start` and before `request.end`; `location` is where the traveller is (use the last completed stop or `request.origin`) |
| Re-plan | `weather_updated` (`{}`: refresh the live forecast) or `preferences_changed` (`{interests, budget_minor, max_walking_m, transport_mode, target_stops, avoid_rain_outdoor_visits, ...}`) | `preferences_changed` accepts only the whitelisted keys listed in `coordinator.apply_event` |
| (new) Drop a stop | `place_unavailable` | `{place_id}` |
| (new) Budget change | `budget_changed` | `{budget_minor}` |

All chips must be disabled while a job is running or a proposal is pending; every one changes the itinerary
only after Accept.

### 5.9 `MapView`

| Mock | Real | Notes |
| --- | --- | --- |
| `city.center` | `trip.request.origin` (`lon` -> Leaflet `lng`) | |
| `stops[].lat/lng` | `trip.places` by `place_id` | |
| polyline through stop points | concatenate `itinerary.legs[].geometry` | **GeoJSON order `[lon, lat]`**; swap to `[lat, lng]` for Leaflet. Fixture legs are straight lines (`SYNTHETIC_ROUTES` issue); OSRM/ORS legs are real paths |
| `ghostStops` | `proposal.proposed.itinerary.stops` not present in `trip.itinerary.stops`, coordinates from `proposed.places` | |
| hard-coded OSM tile URL | `config.tile_url` (default `https://tile.openstreetmap.org/{z}/{x}/{y}.png`) | keep the OpenStreetMap attribution |
| - | `trip.progress.location` | current position marker after `stop_completed` / `user_running_late` |

### 5.10 Header metrics and revision history

- Revision `trip.revision`; walking `itinerary.walking_m / 1000`; cost `itinerary.cost_minor / 100` in
  `request.currency` (use `Intl.NumberFormat`); ends `itinerary.end_arrival` in the trip zone.
- There is no revision-history endpoint (`GET /api/trips` lists trips, not revisions). Keep the history
  bubble client-side (push the previous `trip` before accepting) or drop it. `GET /api/trips/{id}/export`
  gives a downloadable snapshot instead.

### 5.11 `/api/config` -> mode pill and notices

`execution_backend` (`local` / `flower`), `agent_mode` (`rules` / `model`), `data_mode` (`fixture` / `live`),
`model`, and the matching `fixture_notice` or `live_notice`. The web client renders these as the pill under the
title; the Prototype's static subtitle should show the same so a viewer knows whether prices and routes are
fixtures.

---

## 6. Behavioural differences the UI has to absorb

1. **Everything that changes the day is asynchronous and reviewable.** Lock, done, weather, pace, budget: each
   is a job (seconds in rules mode, minutes with a model) that produces a proposal. Show job progress from the
   SSE feed, keep the live itinerary untouched, and route every change through Accept / Keep current.
2. **One pending proposal at a time**, and `base_revision` must equal `trip.revision`; on a 409, reload the
   trip and tell the user what happened.
3. **Unknown stays unknown.** `cost_minor: null`, `indoor: null`, `opening_hours: null` and the
   `*_UNKNOWN` issue codes are first-class states; the mock's `null` handling already matches this, keep it.
4. **Fixture mode plans Berlin only**; the intake says so. Live mode plans any geocodable city.
5. **The intake needs a date and does not need a budget**; the mock had this the other way round.
6. **Datetimes are zone-aware ISO strings**; format in `request.timezone`, compare as `Date`, never parse
   "HH:MM" out of them.
7. **State is server-side.** Persist `trip_id` in `localStorage` (the web client uses the key
   `travel-trip`) and rebuild the screen from `GET /api/trips/{id}` on load; the mock loses everything on
   refresh.
8. **Multi-day is out of scope.** The backend plans one day; `notes[]` from the intake explains that when
   a user asks for several days.

---

## 7. Verification recipe (no model, no network)

```sh
TRAVEL_DB=runtime/proto.sqlite3 uv run python -m travel_agent.cli serve --port 8020 &
B=http://127.0.0.1:8020

curl -s $B/api/config | python -m json.tool | head -20
curl -s -X POST $B/api/intake -H 'Content-Type: application/json' \
  -d '{"message":"Tomorrow in Berlin from 10 to 17, art and parks, budget 60","brief":{},"history":[]}'
#   -> ready: true, request: {...}          (fixture mode; try "Lisbon" to see the Berlin-only reply)

REQ=$(curl -s -X POST $B/api/intake -H 'Content-Type: application/json' \
  -d '{"message":"Tomorrow in Berlin from 10 to 17, art and parks","brief":{},"history":[]}' \
  | python -c 'import json,sys; print(json.dumps({"request": json.load(sys.stdin)["request"]}))')
OUT=$(curl -s -X POST $B/api/trips -H 'Content-Type: application/json' -d "$REQ")
TRIP=$(echo "$OUT" | python -c 'import json,sys; print(json.load(sys.stdin)["trip_id"])')
JOB=$(echo "$OUT" | python -c 'import json,sys; print(json.load(sys.stdin)["job"]["id"])')

curl -sN --max-time 20 "$B/api/jobs/$JOB/events"     # agent.started ... proposal.ready, job.finished, event: done
curl -s $B/api/trips/$TRIP | python -c 'import json,sys; d=json.load(sys.stdin); print(d["trip"]["revision"], len(d["trip"]["itinerary"]["stops"]), len(d["proposals"]))'
#   -> 1 <stops> 0        (first plan auto-accepted)

curl -s -X POST $B/api/trips/$TRIP/events -H 'Content-Type: application/json' \
  -d '{"base_revision":1,"event":{"id":"rain1","kind":"rain","payload":{},"simulated":true}}'
#   -> a new job; after it finishes GET /api/trips/$TRIP lists one pending proposal to accept or reject
```

Then start the Prototype with the proxy from 2.3 (`TRAVEL_API_URL=http://127.0.0.1:8020 npm run dev`) and
confirm in the browser's network panel that `/api/config` returns 200 through the Vite origin and that a
`POST /api/intake` is not answered with 403 "Cross-origin writes are disabled".
