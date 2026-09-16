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
import time
from collections.abc import Callable, Iterable
from dataclasses import replace
from pathlib import Path

from travel_agent import jobcodec
from travel_agent.schemas import Proposal, TripEvent, TripSnapshot
from travel_agent.settings import DEFAULT_MODEL, ModelSettings, model_settings_from_env

PROJECT_DIR = Path(__file__).resolve().parents[1]
Listener = Callable[[str, dict], None]
STREAM_RETRY_DELAYS_S = (2, 4, 6)
RUN_TIMEOUT_MARGIN_S = 300  # default headroom over the AgentApp's own wall-time budget
RUN_TIMEOUT_FLOOR_S = 60  # the local stop must never fire before the AgentApp's budget can
_sleep = time.sleep  # replaced in tests


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


def run_id_from(response) -> int:
    """The run ID of a StartRun response. Without one, SuperGrid's note (for example: no credits) is surfaced."""
    if response.HasField("run_id"):
        return int(response.run_id)
    note = (response.note if response.HasField("note") else "").strip() or "no reason was given"
    raise FlowerError(f"SuperGrid did not start a run: {note}")


class FlowerBackend:
    """Runs one planning job per SuperGrid run and relays the specialist events to a listener."""

    def __init__(self, *, connection: str = "supergrid", federation: str = "", settings: ModelSettings | None = None,
                 run_timeout_s: float | None = None, project_dir: Path = PROJECT_DIR, **overrides):
        """`overrides` are ModelSettings fields (model, max_tool_turns, ...) applied on top of `settings`."""
        settings = replace(settings or ModelSettings(), **overrides)
        if not settings.model.strip():
            raise ValueError("Set TRAVEL_MODEL to the runtime model ID")
        if federation and not federation.startswith("@"):
            raise ValueError("A Flower federation ID looks like @account/federation")
        self.connection, self.federation = connection, federation
        self.settings = replace(settings, model=settings.model.strip())
        # The AgentApp enforces wall_time_s itself and reports a clean failure; the local stop is only a backstop.
        requested = float(run_timeout_s) if run_timeout_s is not None else settings.wall_time_s + RUN_TIMEOUT_MARGIN_S
        self.run_timeout_s = max(requested, float(settings.wall_time_s + RUN_TIMEOUT_FLOOR_S))
        self.project_dir = Path(project_dir)
        self._lock = threading.Lock()
        self._fab: tuple[str, bytes, float] | None = None

    @classmethod
    def from_env(cls) -> FlowerBackend:
        raw_timeout = os.getenv("TRAVEL_FLOWER_RUN_TIMEOUT_S", "").strip()
        return cls(connection=os.getenv("TRAVEL_FLOWER_CONNECTION", "supergrid"),
                   federation=os.getenv("TRAVEL_FLOWER_FEDERATION", ""),
                   settings=model_settings_from_env(DEFAULT_MODEL),
                   run_timeout_s=float(raw_timeout) if raw_timeout else None)

    @property
    def model(self) -> str:
        return self.settings.model

    @property
    def max_tool_turns(self) -> int:
        return self.settings.max_tool_turns

    @property
    def model_timeout_s(self) -> float:
        return self.settings.model_timeout_s

    @property
    def reasoning_effort(self) -> str:
        return self.settings.reasoning_effort

    def describe(self) -> dict:
        return {"connection": self.connection, "federation": self.federation or "account default", "model": self.model,
                "run_timeout_s": self.run_timeout_s, **self.settings.describe()}

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
        import click
        from flwr.cli.flower_config import read_superlink_connection
        from flwr.cli.utils import init_http_client_from_connection
        try:
            connection = read_superlink_connection(self.connection)
            if not connection.address:
                raise FlowerError(f"The Flower connection '{self.connection}' has no address")
            return init_http_client_from_connection(connection)
        except click.ClickException as exc:
            raise FlowerError(f"Flower connection '{self.connection}' is not usable: {exc.format_message()}") from None

    def _overrides(self, job: str, data_mode: str) -> dict:
        """Run-config overrides. Every key must exist under [tool.flwr.app.config] in pyproject.toml."""
        s = self.settings
        return {"agent.input": json.dumps({"job": job}), "agent.model": s.model,
                "agent.max-tool-turns": s.max_tool_turns, "agent.model-timeout-s": s.model_timeout_s,
                "agent.reasoning-effort": s.reasoning_effort, "agent.max-model-calls": s.model_call_cap,
                "agent.wall-time-s": s.wall_time_s, "agent.max-output-tokens": s.max_output_tokens,
                "travel.data-mode": data_mode}

    @staticmethod
    def _stop_run(client, run_id: int) -> str:
        """Ask SuperGrid to stop a run. Never raises: the caller is already reporting a failure."""
        import click
        from flwr.cli.utils import flwr_cli_exc_handler
        from flwr.proto.control_pb2 import StopRunRequest
        try:
            with flwr_cli_exc_handler():
                response = client.StopRun(StopRunRequest(run_id=run_id))
            return "the run was stopped" if response.success else "SuperGrid declined to stop the run"
        except click.ClickException as exc:
            return f"stopping the run failed: {exc.format_message()}"
        except Exception as exc:
            return f"stopping the run failed: {type(exc).__name__}"

    @staticmethod
    def relay_stream(open_stream: Callable[[int | None], Iterable], listen: Listener, outcome: dict,
                     deadline: float, stop: threading.Event) -> None:
        """Consume run events until the AgentApp reports a result or failure, or the stream ends.

        `open_stream(after_task_event_id)` returns an iterable of StreamRunEvents responses. A listener failure is
        counted in `outcome["listener_errors"]` and never ends the run. A transport failure reconnects after the last
        relayed task event (2s, 4s, 6s) while the deadline allows; the final exception is left in `outcome["error"]`.
        """
        outcome.update({"relayed": 0, "listener_errors": 0, "reconnects": 0})
        last_id: int | None = None

        def relay(kind: str, data: dict) -> None:
            try:
                listen(kind, data)
            except Exception:  # The store or UI listener must not take the SuperGrid run down with it.
                outcome["listener_errors"] += 1

        attempt = 0
        while not stop.is_set():
            try:
                for item in open_stream(last_id):
                    task_event = item.task_event
                    last_id = int(task_event.id)
                    outcome["relayed"] += 1
                    kind, data = _payload(task_event)
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
                        relay(str(data.get("kind") or kind[len("travel."):]),
                              data.get("data") if isinstance(data.get("data"), dict) else {})
                return  # The stream closed: the run ended without a travel.* verdict.
            except Exception as exc:
                attempt += 1
                delay = STREAM_RETRY_DELAYS_S[attempt - 1] if attempt <= len(STREAM_RETRY_DELAYS_S) else None
                if delay is None or stop.is_set() or time.monotonic() + delay >= deadline:
                    outcome["error"] = exc
                    return
                outcome["reconnects"] = attempt
                relay("flower.stream.reconnecting", {"attempt": attempt, "delay_s": delay,
                                                     "after_task_event_id": last_id, "error": type(exc).__name__})
                _sleep(delay)

    def run_job(self, snapshot: TripSnapshot, event: TripEvent | None, listen: Listener) -> Proposal:
        """Submit one job, relay its events, and return the validated proposal. Blocks until the run ends.

        After StartRun the run is billed: listener failures are counted, not raised; a lost event stream is
        resumed; and the run is stopped before a timeout or a stream loss is reported.
        """
        import click
        from flwr.cli.utils import flwr_cli_exc_handler
        from flwr.common.serde import fab_to_proto, user_config_to_proto
        from flwr.proto.control_pb2 import StartRunRequest, StreamRunEventsRequest
        from flwr.supercore.fab import Fab

        fab_hash, content = self.fab()
        # Nothing is submitted yet: a store failure here fails the job before SuperGrid bills a run.
        listen("flower.submitting", {"fab_hash": fab_hash[:8], "model": self.model,
                                     "federation": self.federation or "account default"})
        listener_errors = 0

        def relay(kind: str, data: dict) -> None:
            """Deliver one of run_job's own events (streamed events go through relay_stream)."""
            nonlocal listener_errors
            try:
                listen(kind, data)
            except Exception:  # After StartRun a listener failure (say a locked store) must not abandon the run.
                listener_errors += 1

        client = self._client()
        try:
            request = StartRunRequest(fab=fab_to_proto(Fab(fab_hash, content, {})),
                                      override_config=user_config_to_proto(self._overrides(encode_job(snapshot, event), snapshot.data_mode)),
                                      federation=self.federation)
            try:
                with flwr_cli_exc_handler():
                    response = client.StartRun(request)
            except click.ClickException as exc:
                raise FlowerError(f"SuperGrid rejected the run: {exc.format_message()}") from None
            run_id = run_id_from(response)
            started = {"run_id": str(run_id), "federation": response.federation or self.federation,
                       "model": self.model, "note": response.note if response.HasField("note") else ""}
            relay("flower.run.started", started)

            def open_stream(after_id: int | None):
                if after_id is None:
                    return client.StreamRunEvents(StreamRunEventsRequest(run_id=run_id))
                return client.StreamRunEvents(StreamRunEventsRequest(run_id=run_id, after_task_event_id=after_id))

            outcome: dict = {}
            stop = threading.Event()
            deadline = time.monotonic() + self.run_timeout_s
            worker = threading.Thread(target=self.relay_stream, args=(open_stream, listen, outcome, deadline, stop),
                                      name="flower-run-events", daemon=True)
            worker.start()
            worker.join(self.run_timeout_s)
            relayed = outcome.get("relayed", 0)
            if worker.is_alive():
                stop.set()
                stopped = self._stop_run(client, run_id)
                raise FlowerError(f"SuperGrid run {run_id} exceeded {self.run_timeout_s:.0f}s after relaying "
                                  f"{relayed} events; {stopped}")
            if "result" in outcome:
                proposal = decode_result(outcome["result"])
                finished = {"run_id": str(run_id), "status": "completed", "relayed": relayed,
                            "metrics": outcome["result"].get("metrics", {}),
                            "listener_errors": listener_errors + outcome.get("listener_errors", 0)}
                relay("flower.run.finished", finished)
                return proposal
            if "failure" in outcome:
                raise FlowerError(f"SuperGrid run {run_id} failed: {outcome['failure']}")
            if "error" in outcome:
                stopped = self._stop_run(client, run_id)
                raise FlowerError(f"Lost the SuperGrid event stream for run {run_id} after relaying {relayed} events "
                                  f"({type(outcome['error']).__name__}); {stopped}")
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
