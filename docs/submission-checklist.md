# Submission checklist status (16 September 2026)

## P0: Submission blockers

- [x] Root `README.md` and `LICENSE` (MIT).
- [x] `publisher = "tauska67"` matches the signed-in Flower account.
- [x] `uv sync` and `uv run flwr build` succeed (`tauska67.osm-travel-companion.0-1-0.<hash>.fab`).
- [x] `uv run flwr login supergrid` completed; `uv run flwr run . supergrid --stream` executes the AgentApp.
- [x] Complete AgentApp execution path verified on SuperGrid (see `docs/verification.md`).
- [x] Published to Flower Hub on 16 September 2026 (withdrawn afterwards: version 0.3.0 is an empty placeholder whose README says the app is retired; version 0.2.2 stays visible under Versions because Hub apps can only be deleted by Flower administrators): https://flower.ai/apps/tauska67/osm-travel-companion/ (`flwr new @tauska67/osm-travel-companion`). The Hub filter skips `travel_agent/web/*` (js, html, css) and `uv.lock`; the full tree lives in the Git repository.
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
- [x] Environment preconfigured by `scripts/serve_supergrid.sh`; timing plan in `docs/demo.md`.

## P1 highlights

- [x] 222 offline tests, including bundle configuration, Endeavor-style prose output handling, job codec bounds,
      SuperGrid backend with a fake grid, tampered-result rejection, stale-revision and concurrency checks.
- [x] CI workflow (`.github/workflows/ci.yml`): ruff, pytest, `flwr build` on Python 3.11 and 3.13.
- [x] Model, tool, wall-time and per-step model-call budgets; provider size and time limits.
- [ ] Live-adapter contract tests and browser-level smoke test (manual browser verification done).

## Credits

Sign-up grant 3000. Charged: 1 + 7 + 303 + 1128 (stopped baseline) + 500 (Endeavor plan-plus-rain) + about 56 (two
fallback-model browser runs). Balance 1005 on 16 September 2026, enough for two Endeavor plan-plus-rain cycles.
Check https://flower.ai/settings/billing before the demo.
