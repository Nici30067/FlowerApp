"""Model execution settings shared by the local model path and the SuperGrid control backend.

Every knob that bounds one planning step lives here so the API server, the SuperGrid launcher and the
`/api/config` endpoint agree on one set of values. The AgentApp itself reads the same knobs from its run
config (`pyproject.toml`) and must not import this module.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

DEFAULT_MODEL = "flower-endeavor-v1.0"
DEFAULT_WALL_TIME_S = 900
DEFAULT_MAX_OUTPUT_TOKENS = 2000


def effective_model_calls(max_model_calls: int, max_tool_turns: int) -> int:
    """The model-call cap of one planning step. 0 means auto: five calls per specialist slot.

    Each specialist makes at most `max_tool_turns` tool calls, one final call and one repair call, and the
    coordinator runs up to five specialist slots (discovery may run twice), hence 5 * (turns + 2).
    """
    return int(max_model_calls) if int(max_model_calls) > 0 else 5 * (int(max_tool_turns) + 2)


@dataclass(frozen=True)
class ModelSettings:
    model: str = DEFAULT_MODEL
    api: str = "responses"
    max_tool_turns: int = 0
    reasoning_effort: str = "low"
    model_timeout_s: float = 120.0
    wall_time_s: int = DEFAULT_WALL_TIME_S
    max_model_calls: int = 0
    max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS

    def __post_init__(self):
        if self.wall_time_s < 1 or self.model_timeout_s <= 0:
            raise ValueError("Wall time and model timeout must be positive")
        if self.max_model_calls < 0 or self.max_output_tokens < 1:
            raise ValueError("Model-call cap must be 0 (auto) or positive and max output tokens at least 1")

    @property
    def model_call_cap(self) -> int:
        """`max_model_calls` with the auto rule applied."""
        return effective_model_calls(self.max_model_calls, self.max_tool_turns)

    def describe(self) -> dict:
        """The operator-facing view served by `/api/config`."""
        return {"max_tool_turns": self.max_tool_turns, "reasoning_effort": self.reasoning_effort,
                "max_model_calls": self.model_call_cap, "max_output_tokens": self.max_output_tokens,
                "wall_time_s": self.wall_time_s}


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    return int(raw) if raw else default


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    return float(raw) if raw else default


def model_settings_from_env(default_model: str = DEFAULT_MODEL) -> ModelSettings:
    """Read the TRAVEL_* model knobs. Unset and empty numeric variables fall back to the documented defaults.

    TRAVEL_REASONING_EFFORT may legitimately be empty (chat endpoints omit the field), so only that string is
    taken verbatim.
    """
    return ModelSettings(model=os.getenv("TRAVEL_MODEL", "").strip() or default_model,
                         api=os.getenv("TRAVEL_MODEL_API", "").strip() or "responses",
                         max_tool_turns=_env_int("TRAVEL_MAX_TOOL_TURNS", 0),
                         reasoning_effort=os.getenv("TRAVEL_REASONING_EFFORT", "low"),
                         model_timeout_s=_env_float("TRAVEL_MODEL_TIMEOUT_S", 120.0),
                         wall_time_s=_env_int("TRAVEL_WALL_TIME_S", DEFAULT_WALL_TIME_S),
                         max_model_calls=_env_int("TRAVEL_MAX_MODEL_CALLS", 0),
                         max_output_tokens=_env_int("TRAVEL_MAX_OUTPUT_TOKENS", DEFAULT_MAX_OUTPUT_TOKENS))
