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
