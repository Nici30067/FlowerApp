# Submission checklist status (16 September 2026)

## P0: Submission blockers

- [x] Root `README.md` and `LICENSE` (MIT).
- [x] `publisher = "tauska67"` matches the signed-in Flower account.
- [x] `uv sync` and `uv run flwr build` succeed (`tauska67.osm-travel-companion.<version>.<hash>.fab`).
- [x] `uv run flwr login supergrid` completed; `uv run flwr run . supergrid --stream` executes the AgentApp
      (fixture data, 16 September 2026, see `docs/verification.md`).
- [x] Complete AgentApp execution path verified on SuperGrid (see `docs/verification.md`).
- [ ] `uv run flwr run . supergrid --run-config 'agent.input="Plan a day in Tokyo tomorrow, art and coffee, budget 60"' --stream`
      with live OpenStreetMap data on SuperGrid. Blocked by the credit balance (0); the same command, code and
      run-config keys were exercised on the local SuperLink `local-agent` (Gemini behind the Open Responses proxy).
      Also confirms worker egress to `overpass-api.de`, `routing.openstreetmap.de`, `api.open-meteo.com`,
      `geocoding-api.open-meteo.com`.
- [x] Published to Flower Hub on 16 September 2026 (withdrawn afterwards: version 0.3.0 is an empty placeholder whose README says the app is retired; version 0.2.2 stays visible under Versions because Hub apps can only be deleted by Flower administrators): https://flower.ai/apps/tauska67/osm-travel-companion/ (`flwr new @tauska67/osm-travel-companion`). The Hub filter skips `travel_agent/web/*` (js, html, css) and `uv.lock`; the full tree lives in the Git repository.
- [ ] Republish the live-data version to Flower Hub: bump `version` in `pyproject.toml` above the 0.3.0
      placeholder, run `uv run flwr ls supergrid` first (fresh access token), then `uv run flwr app publish .`.
      Only on an explicit go-ahead from the account owner.
- [ ] Public Git repository and tag of the submission commit.

## P0: Flower Endeavor integration

- [x] Model identifier verified on SuperGrid: `flower-endeavor-v1.0` (`endeavor-1.0` is rejected).
- [x] Endeavor is the default `agent.model`; `TRAVEL_MODEL` / `--run-config` switch models without code changes.
- [x] Complete planning request and dynamic replanning (`plan then rain`) executed with Endeavor.
- [x] Structured outputs from all four specialists validated (`AgentReport` schema, tolerant JSON extraction, one repair turn).
- [x] Final demo configuration recorded in `docs/verification.md`.

## P0: Collaborative agent execution

- [x] Distinct role prompts, contexts, output contracts and tool allowlists (`travel_agent/agents.py`).
- [x] Interaction path Discovery -> Conditions -> Discovery -> Mobility -> Budget & Pace -> Coordinator.
- [x] Structured hand-off (`AgentReport`, `AgentMessage`) into later specialists' context.
- [x] Agent-to-agent requests, model call timings, evidence counts and validation shown in the UI feed.
- [x] Proposal review shows the trigger, the requests behind the change, added/removed/preserved stops.

## P0: End-to-end demo path

- [x] One action: **Build itinerary** -> SuperGrid run -> specialist collaboration -> validation -> OSM itinerary.
- [x] No copied Flower command: the server submits runs through the Control API with the CLI login.
- [x] Environment preconfigured by `scripts/serve_supergrid.sh`; timing plan in `docs/demo.md` (variant A).
- [x] Hub-only path: one natural-language sentence to the AgentApp (`agent.input`), live OpenStreetMap data by
      default (`[tool.flwr.app.config.travel]`: `data-mode = "live"`, contact string, public-Overpass opt-in, OSRM),
      follow-ups (`rain`, `budget 30`, `walk 2 km`, `refresh`, `approve`, `reject`, `status`, `export`) in the
      same run series; script in `docs/demo.md` (variant B). Verified on the local Flower runtime; SuperGrid
      verification pending credits (see above).
- [x] Fixture fallback per run: `--run-config 'travel.data-mode="fixture"'`; model fallback:
      `--run-config 'agent.model="openai/gpt-5.6-sol"'`.

## P1 highlights

- [x] Offline test suite (`uv run pytest -q`, no network), including bundle configuration, Endeavor-style prose
      output handling, job codec bounds, SuperGrid backend with a fake grid, tampered-result rejection,
      stale-revision and concurrency checks, the intake parser and its questions (`tests/test_intake*.py`), and
      the run-config provider settings.
- [x] CI workflow (`.github/workflows/ci.yml`): ruff, pytest, `flwr build` on Python 3.11 and 3.13.
- [x] Model, tool, wall-time and per-step model-call budgets; provider size and time limits.
- [x] Every `travel.*` and `agent.*` run-config key documented with its default in the README.
- [ ] Live-adapter contract tests and browser-level smoke test (manual browser verification done).

## Credits

Sign-up grant 3000. Charged on 16 September 2026: 1 + 7 + 303 + 1128 (stopped baseline) + 500 (Endeavor
plan-plus-rain) + about 56 (two fallback-model browser runs), then further Endeavor runs from the demo servers and
Hub reproductions until the balance reached 0 (about 11:45 UTC). SuperGrid refuses new runs until the federation
owner has a positive balance, which is what blocks the live-data verification above. Check
https://flower.ai/settings/billing before the demo; one Endeavor plan-plus-rain cycle costs about 500, one
fallback-model cycle about 60.
