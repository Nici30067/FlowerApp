"""Print the structured run events of a SuperGrid run (the same stream the map application consumes).

Usage: uv run python scripts/run_events.py <run-id> [--seconds 30] [--connection supergrid]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("run_id", type=int)
    parser.add_argument("--seconds", type=float, default=30, help="stop after this many seconds")
    parser.add_argument("--connection", default="supergrid")
    args = parser.parse_args()

    from flwr.cli.flower_config import read_superlink_connection
    from flwr.cli.utils import init_http_client_from_connection
    from flwr.proto.control_pb2 import StreamRunEventsRequest

    client = init_http_client_from_connection(read_superlink_connection(args.connection))
    counts: dict[str, int] = {}
    text: list[str] = []

    def consume() -> None:
        for item in client.StreamRunEvents(StreamRunEventsRequest(run_id=args.run_id)):
            event = item.task_event
            try:
                data = json.loads(event.data)
            except ValueError:
                data = {}
            kind = event.event or data.get("type", "")
            counts[kind] = counts.get(kind, 0) + 1
            if kind == "response.output_text.delta":
                text.append(str(data.get("delta", "")))
            elif kind.startswith("travel.") and kind != "travel.result":
                payload = data.get("data", {})
                brief = json.dumps(payload, ensure_ascii=True)[:160]
                print(f"{event.timestamp} {kind}: {brief}", flush=True)
            elif kind == "travel.result":
                print(f"{event.timestamp} travel.result: {len(str(data.get('proposal', '')))} chars, metrics {data.get('metrics')}", flush=True)
            elif kind in ("response.completed", "error", "response.failed", "travel.failed"):
                print(f"{event.timestamp} {kind}: {json.dumps(data)[:200]}", flush=True)

    worker = threading.Thread(target=consume, daemon=True)
    worker.start()
    worker.join(args.seconds)
    print("\n--- event counts ---")
    for kind, count in sorted(counts.items()):
        print(f"{count:4d}  {kind}")
    if text:
        print("\n--- assistant text ---")
        print("".join(text)[-3000:])
    client.close()
    sys.stdout.flush()
    os._exit(0)


if __name__ == "__main__":
    main()
