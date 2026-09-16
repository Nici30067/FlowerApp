"""Submit planning jobs to Flower SuperGrid through the Control API and stream their run events back.

The local API server is a run-event client, exactly like Flower Chat: no public callback URL, no tunnel, and no
per-job credential leaves this machine. The AgentApp receives the job inside `agent.input` and returns the
proposal as one structured run event. Flower CLI credentials from `flwr login supergrid` are reused.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
from collections.abc import Callable
from pathlib import Path

from travel_agent import jobcodec
from travel_agent.schemas import Proposal, TripEvent, TripSnapshot

PROJECT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_MODEL = "flower-endeavor-v1.0"
Listener = Callable[[str, dict], None]


class FlowerError(RuntimeError):
    """A SuperGrid submission or run failed. Messages are safe to show to the operator."""


def encode_job(snapshot: TripSnapshot, event: TripEvent | None) -> str:
    return jobcodec.encode({"snapshot": snapshot.model_dump(mode="json", exclude={"reports", "messages"}),
                            "event": event.model_dump(mode="json") if event else None})


def decode_result(payload: dict) -> Proposal:
    return Proposal.model_validate(jobcodec.decode(str(payload.get("proposal", "")))["proposal"])


def _payload(task_event) -> tuple[str, dict]:
    try:
        data = json.loads(task_event.data)
    except (TypeError, ValueError):
        data = {}
    data = data if isinstance(data, dict) else {}
    return task_event.event or str(data.get("type", "")), data


class FlowerBackend:
    """Runs one planning job per SuperGrid run and relays the specialist events to a listener."""

    def __init__(self, *, connection: str = "supergrid", federation: str = "", model: str = DEFAULT_MODEL,
                 max_tool_turns: int = 0, model_timeout_s: float = 120, run_timeout_s: float = 900,
                 reasoning_effort: str = "low", project_dir: Path = PROJECT_DIR):
        if not model.strip():
            raise ValueError("Set TRAVEL_MODEL to the runtime model ID")
        if federation and not federation.startswith("@"):
            raise ValueError("A Flower federation ID looks like @account/federation")
        self.connection, self.federation, self.model = connection, federation, model.strip()
        self.max_tool_turns, self.model_timeout_s, self.run_timeout_s = int(max_tool_turns), float(model_timeout_s), float(run_timeout_s)
        self.reasoning_effort = reasoning_effort
        self.project_dir = Path(project_dir)
        self._lock = threading.Lock()
        self._fab: tuple[str, bytes, float] | None = None

    @classmethod
    def from_env(cls) -> FlowerBackend:
        return cls(connection=os.getenv("TRAVEL_FLOWER_CONNECTION", "supergrid"),
                   federation=os.getenv("TRAVEL_FLOWER_FEDERATION", ""),
                   model=os.getenv("TRAVEL_MODEL", "") or DEFAULT_MODEL,
                   max_tool_turns=int(os.getenv("TRAVEL_MAX_TOOL_TURNS", "0")),
                   model_timeout_s=float(os.getenv("TRAVEL_MODEL_TIMEOUT_S", "120")),
                   run_timeout_s=float(os.getenv("TRAVEL_FLOWER_RUN_TIMEOUT_S", "900")),
                   reasoning_effort=os.getenv("TRAVEL_REASONING_EFFORT", "low"))

    def describe(self) -> dict:
        return {"connection": self.connection, "federation": self.federation or "account default", "model": self.model,
                "max_tool_turns": self.max_tool_turns, "reasoning_effort": self.reasoning_effort}

    # --- Flower App Bundle -------------------------------------------------------------------------------
    def _source_stamp(self) -> float:
        newest = (self.project_dir / "pyproject.toml").stat().st_mtime
        for path in (self.project_dir / "travel_agent").rglob("*"):
            if path.is_file() and "__pycache__" not in path.parts:
                newest = max(newest, path.stat().st_mtime)
        return newest

    def fab(self) -> tuple[str, bytes]:
        """Build the FAB from the project sources once; rebuild after any source change."""
        from flwr.cli.build import build_fab_from_disk
        with self._lock:
            stamp = self._source_stamp()
            if self._fab is None or self._fab[2] != stamp:
                content = build_fab_from_disk(self.project_dir)
                self._fab = (hashlib.sha256(content).hexdigest(), content, stamp)
            return self._fab[0], self._fab[1]

    # --- Control API ---------------------------------------------------------------------------------------
    def _client(self):
        from flwr.cli.flower_config import read_superlink_connection
        from flwr.cli.utils import init_http_client_from_connection
        connection = read_superlink_connection(self.connection)
        if not connection.address:
            raise FlowerError(f"The Flower connection '{self.connection}' has no address")
        return init_http_client_from_connection(connection)

    def _overrides(self, job: str, data_mode: str) -> dict:
        return {"agent.input": json.dumps({"job": job}), "agent.model": self.model,
                "agent.max-tool-turns": self.max_tool_turns, "agent.model-timeout-s": self.model_timeout_s,
                "agent.reasoning-effort": self.reasoning_effort, "travel.data-mode": data_mode}

    def run_job(self, snapshot: TripSnapshot, event: TripEvent | None, listen: Listener) -> Proposal:
        """Submit one job, relay its events, and return the validated proposal. Blocks until the run ends."""
        import click
        from flwr.cli.utils import flwr_cli_exc_handler
        from flwr.common.serde import fab_to_proto, user_config_to_proto
        from flwr.proto.control_pb2 import StartRunRequest, StopRunRequest, StreamRunEventsRequest
        from flwr.supercore.fab import Fab

        fab_hash, content = self.fab()
        listen("flower.submitting", {"fab_hash": fab_hash[:8], "model": self.model,
                                     "federation": self.federation or "account default"})
        client = self._client()
        run_id = None
        try:
            request = StartRunRequest(fab=fab_to_proto(Fab(fab_hash, content, {})),
                                      override_config=user_config_to_proto(self._overrides(encode_job(snapshot, event), snapshot.data_mode)),
                                      federation=self.federation)
            try:
                with flwr_cli_exc_handler():
                    response = client.StartRun(request)
            except click.ClickException as exc:
                raise FlowerError(f"SuperGrid rejected the run: {exc.format_message()}") from None
            if not response.HasField("run_id"):
                raise FlowerError("SuperGrid did not start a run")
            run_id = response.run_id
            listen("flower.run.started", {"run_id": str(run_id), "federation": response.federation or self.federation,
                                          "model": self.model, "note": response.note if response.HasField("note") else ""})
            outcome: dict = {}

            def consume():
                try:
                    for item in client.StreamRunEvents(StreamRunEventsRequest(run_id=run_id)):
                        kind, data = _payload(item.task_event)
                        if kind == "travel.result":
                            outcome["result"] = data
                            return
                        if kind == "travel.failed":
                            outcome["failure"] = str(data.get("error", "AgentApp failed"))[:350]
                            return
                        if kind in ("error", "response.failed"):
                            outcome["failure"] = "Model response failed on SuperGrid"
                            return
                        if kind.startswith("travel."):
                            listen(str(data.get("kind") or kind[len("travel."):]), data.get("data") if isinstance(data.get("data"), dict) else {})
                except Exception as exc:  # Surface transport failures to the waiting thread.
                    outcome["error"] = exc

            worker = threading.Thread(target=consume, name="flower-run-events", daemon=True)
            worker.start()
            worker.join(self.run_timeout_s)
            if worker.is_alive():
                try:
                    client.StopRun(StopRunRequest(run_id=run_id))
                finally:
                    client.close()
                raise FlowerError(f"SuperGrid run {run_id} exceeded {self.run_timeout_s:.0f}s and was stopped")
            if "result" in outcome:
                proposal = decode_result(outcome["result"])
                listen("flower.run.finished", {"run_id": str(run_id), "status": "completed",
                                               "metrics": outcome["result"].get("metrics", {})})
                return proposal
            if "failure" in outcome:
                raise FlowerError(f"SuperGrid run {run_id} failed: {outcome['failure']}")
            if "error" in outcome:
                raise FlowerError(f"Lost the SuperGrid event stream for run {run_id}: {type(outcome['error']).__name__}")
            raise FlowerError(f"SuperGrid run {run_id} ended without a result: {self.status(client, run_id)}")
        finally:
            client.close()

    @staticmethod
    def status(client, run_id: int) -> str:
        from flwr.common.serde import run_from_proto
        from flwr.proto.control_pb2 import ListRunsRequest
        try:
            response = client.ListRuns(ListRunsRequest(run_id=run_id))
            run = run_from_proto(next(iter(response.run_dict.values())))
            return f"{run.status.status}:{run.status.sub_status} {run.status.details}".strip()
        except Exception as exc:  # Status is informational only.
            return f"status unavailable ({type(exc).__name__})"
