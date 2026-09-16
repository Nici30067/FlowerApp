# Known limitations

- SuperGrid runs use the bundled Berlin fixture. Live providers need operator credentials that are configured
  locally only.
- Endeavor latency: 20 to 90 seconds per specialist call measured on 16 September 2026. Discovery and Conditions
  run concurrently; the default configuration makes one call per specialist.
- Fixture routes are direct lines scaled by 1.28, not walking directions.
- The opening-hours parser covers common patterns (24/7, day ranges, multiple intervals, `off`). Public holidays,
  sunrise/sunset and month rules are reported as unknown.
- `flwr run` starts from an empty `Context`; multi-step conversations persist only inside a `flwr chat` series or
  via scripted scenarios such as `plan then rain`.
- The legacy bridge mode requires a public HTTPS origin; control mode replaces it for the demo.
- SuperGrid credits: Endeavor calls are charged per token. A three-call run with 15 KB tool outputs cost 303 credits on
  16 September 2026, which is why the default configuration uses no tool rounds, a compact context, low reasoning
  effort, and a hard cap of 12 model calls per planning step. With those defaults one Endeavor plan-plus-rain
  scenario cost 500 credits; the same scenario on `openai/gpt-5.6-sol` cost about 60.
