"""Local entry points. Run `python -m travel_agent.cli serve` for the map application."""
import argparse
import json
import os
from pathlib import Path
from uuid import uuid4


def main():
    parser = argparse.ArgumentParser(description="OSM Travel Companion")
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve", help="Start the map interface and API")
    serve.add_argument("--host", default=os.getenv("TRAVEL_HOST", "127.0.0.1"))
    serve.add_argument("--port", type=int, default=int(os.getenv("TRAVEL_PORT", "8000")))
    replay = sub.add_parser("replay", help="Run a deterministic baseline and rain-replanning scenario")
    replay.add_argument("--output", default="runtime/replay.json")
    args = parser.parse_args()
    if args.command == "serve":
        import uvicorn

        from travel_agent.api import create_app
        if args.host not in ("127.0.0.1", "localhost", "::1") and not os.getenv("TRAVEL_API_TOKEN"):
            parser.error("Set TRAVEL_API_TOKEN before binding to a public interface")
        uvicorn.run(create_app(), host=args.host, port=args.port, log_level="info")
    else:
        from travel_agent.agents import ExecutionBudget, RulesRunner
        from travel_agent.coordinator import Coordinator
        from travel_agent.providers.services import FixtureProvider
        from travel_agent.schemas import TripEvent, TripRequest, TripSnapshot
        events = []
        emit = lambda typ, value: events.append({"type": typ, "data": value})
        budget = ExecutionBudget()
        coordinator = Coordinator(FixtureProvider(), RulesRunner(emit, budget), emit, budget)
        baseline = coordinator.plan(TripSnapshot(id=uuid4().hex, request=TripRequest()))
        revised = coordinator.plan(baseline.proposed, TripEvent(id="replay-rain", kind="rain", simulated=True))
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps({"notice": "Fixture data and rule-based specialists; no model calls.",
            "baseline": baseline.model_dump(mode="json"), "rain": revised.model_dump(mode="json"),
            "events": events, "metrics": budget.metrics()}, indent=2))
        print(f"Baseline: {baseline.proposed.itinerary.validation.status}; rain: {revised.proposed.itinerary.validation.status}")
        print(f"Removed {len(revised.removed)} activities; added {len(revised.added)}. Saved {output}")


if __name__ == "__main__":
    main()
