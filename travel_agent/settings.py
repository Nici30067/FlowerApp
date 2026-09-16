"""Model and data-provider settings shared by the local server, the SuperGrid launcher and the AgentApp.

Every knob that bounds one planning step lives here so the API server, the SuperGrid launcher and the
`/api/config` endpoint agree on one set of values. The AgentApp reads its model knobs from its run config
(`pyproject.toml`) directly and uses `ProviderSettings.from_run_config` for the data providers: inside a Flower
run there are no environment variables, so everything the live OpenStreetMap stack needs is declared under
`[tool.flwr.app.config.travel]`. This module imports only the standard library, so it is safe to import from the
AgentApp and from `travel_agent.providers.services` alike.
"""
from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import asdict, dataclass

DEFAULT_MODEL = "flower-endeavor-v1.0"
DEFAULT_WALL_TIME_S = 900
DEFAULT_MAX_OUTPUT_TOKENS = 2000

# Zero-key OpenStreetMap stack. The public FOSSGIS OSRM instance always uses "driving" as the path profile; the
# real profile (foot, bike) is in the hostname path, hence the {profile} placeholder there.
DEFAULT_OSRM_URL_TEMPLATE = "https://routing.openstreetmap.de/routed-{profile}/{service}/v1/driving"
DEFAULT_OVERPASS_URL = "https://overpass-api.de/api/interpreter"
DEFAULT_ORS_URL = "https://api.openrouteservice.org"
DEFAULT_WEATHER_URL = "https://api.open-meteo.com/v1/forecast"
DEFAULT_GEOCODER_URL = "https://geocoding-api.open-meteo.com/v1/search"
DATA_MODES = ("fixture", "live")
ROUTERS = ("osrm", "ors")
# The run-config keys of `ProviderSettings.from_run_config`, in field order, all under [tool.flwr.app.config.travel].
PROVIDER_RUN_CONFIG_KEYS = {
    "data_mode": "travel.data-mode", "contact": "travel.contact",
    "allow_public_overpass": "travel.allow-public-overpass", "router": "travel.router",
    "osrm_url_template": "travel.osrm-url-template", "overpass_url": "travel.overpass-url",
    "ors_url": "travel.ors-url", "ors_key": "travel.ors-api-key", "weather_url": "travel.weather-url",
    "geocoder_url": "travel.geocoder-url",
}
_TRUE = ("true", "yes", "on", "1")
_FALSE = ("false", "no", "off", "0")


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


# --- Data providers -----------------------------------------------------------------------------------------

def default_router(explicit: str, ors_key: str) -> str:
    """The router live mode will use: the explicit setting when given, else ors when an ORS key is set, else osrm.

    `explicit` is TRAVEL_ROUTER or travel.router; it is normalised but not validated here because `LiveProvider`
    reports an unknown router as a `ProviderError`, the same way it does today.
    """
    explicit = explicit.strip().lower()
    return explicit or ("ors" if ors_key.strip() else "osrm")


def coerce_bool(value: object, default: bool) -> bool:
    """A run-config boolean given as a bool, as an int 0/1, or as a string such as "true" or "false".

    `flwr run --run-config 'travel.allow-public-overpass="true"'` arrives as a string while the TOML default
    arrives as a bool; "" (and None) means the default. Any other text is a configuration error, not a silent no.
    """
    if value is None or isinstance(value, bool):
        return default if value is None else value
    if isinstance(value, int | float):
        if value in (0, 1):
            return bool(value)
        raise ValueError(f"Expected true or false, got {value!r}")
    text = str(value).strip().lower()
    if not text:
        return default
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    raise ValueError(f"Expected true or false, got {value!r}")


def _text(value: object, default: str, *, normalise: bool = False) -> str:
    """A run-config or environment string; "" (and None) means `default`. URLs and modes are stripped."""
    text = "" if value is None else str(value)
    if normalise:
        text = text.strip()
    return text if text else default


@dataclass(frozen=True)
class ProviderSettings:
    """Everything the fixture or live OpenStreetMap stack needs, from the environment or from a Flower run config.

    The zero-key defaults are Open-Meteo geocoding, the public Overpass instance (which must be allowed explicitly),
    the public FOSSGIS OSRM instance and the Open-Meteo forecast. `contact` goes into the User-Agent of every
    live request and is required in live mode. Validation of the combination (contact present, public Overpass
    allowed, ors with a key, HTTPS endpoints) happens in `LiveProvider`, so a misconfiguration fails the same way
    on every path.
    """
    data_mode: str = "fixture"
    contact: str = ""
    allow_public_overpass: bool = False
    router: str = "osrm"
    osrm_url_template: str = DEFAULT_OSRM_URL_TEMPLATE
    overpass_url: str = DEFAULT_OVERPASS_URL
    ors_url: str = DEFAULT_ORS_URL
    ors_key: str = ""
    weather_url: str = DEFAULT_WEATHER_URL
    geocoder_url: str = DEFAULT_GEOCODER_URL

    @classmethod
    def from_env(cls) -> ProviderSettings:
        """Today's environment names. Unset and empty variables fall back to the defaults above.

        TRAVEL_ROUTER resolves like `resolve_router`: explicit, else ors when ORS_API_KEY is set, else osrm.
        TRAVEL_ALLOW_PUBLIC_OVERPASS is true only when it reads exactly "true" (case-insensitively), as before.
        """
        env = os.getenv
        ors_key = env("ORS_API_KEY", "")
        return cls(data_mode=_text(env("TRAVEL_DATA_MODE"), cls.data_mode, normalise=True).lower(),
                   contact=env("TRAVEL_CONTACT", ""),
                   allow_public_overpass=env("TRAVEL_ALLOW_PUBLIC_OVERPASS", "false").lower() == "true",
                   router=default_router(env("TRAVEL_ROUTER", ""), ors_key),
                   osrm_url_template=_text(env("OSRM_URL_TEMPLATE"), DEFAULT_OSRM_URL_TEMPLATE, normalise=True),
                   overpass_url=_text(env("TRAVEL_OVERPASS_URL"), DEFAULT_OVERPASS_URL, normalise=True),
                   ors_url=_text(env("ORS_BASE_URL"), DEFAULT_ORS_URL, normalise=True),
                   ors_key=ors_key,
                   weather_url=_text(env("OPEN_METEO_URL"), DEFAULT_WEATHER_URL, normalise=True),
                   geocoder_url=_text(env("GEOCODER_URL"), DEFAULT_GEOCODER_URL, normalise=True))

    @classmethod
    def from_run_config(cls, config: Mapping[str, object]) -> ProviderSettings:
        """The `travel.*` keys of a Flower run config (`context.run_config`), see PROVIDER_RUN_CONFIG_KEYS.

        A missing key or an empty string means the default. Booleans may be TOML booleans or the strings "true"
        and "false" (a `--run-config` override is always a string). An empty `travel.router` resolves like the
        environment: ors when `travel.ors-api-key` is set, otherwise osrm.
        """
        def get(field: str) -> object:
            return config.get(PROVIDER_RUN_CONFIG_KEYS[field])

        ors_key = _text(get("ors_key"), "")
        return cls(data_mode=_text(get("data_mode"), cls.data_mode, normalise=True).lower(),
                   contact=_text(get("contact"), ""),
                   allow_public_overpass=coerce_bool(get("allow_public_overpass"), cls.allow_public_overpass),
                   router=default_router(_text(get("router"), ""), ors_key),
                   osrm_url_template=_text(get("osrm_url_template"), DEFAULT_OSRM_URL_TEMPLATE, normalise=True),
                   overpass_url=_text(get("overpass_url"), DEFAULT_OVERPASS_URL, normalise=True),
                   ors_url=_text(get("ors_url"), DEFAULT_ORS_URL, normalise=True),
                   ors_key=ors_key,
                   weather_url=_text(get("weather_url"), DEFAULT_WEATHER_URL, normalise=True),
                   geocoder_url=_text(get("geocoder_url"), DEFAULT_GEOCODER_URL, normalise=True))

    @property
    def live(self) -> bool:
        return self.data_mode == "live"

    def describe(self) -> dict:
        """The operator-facing view (status output, `/api/config`): every field except the ORS key."""
        view = asdict(self)
        view["ors_key_set"] = bool(view.pop("ors_key").strip())
        return view
