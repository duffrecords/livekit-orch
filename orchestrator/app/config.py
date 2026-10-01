"""Settings, read from the environment."""

import os
from dataclasses import dataclass, field


def _csv(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in value.split(",") if item.strip())


@dataclass(frozen=True)
class Settings:
    # LiveKit server API as seen from this pod, and the credentials for it
    livekit_url: str = "http://livekit.livekit.svc.cluster.local:7880"
    livekit_api_key: str = ""
    livekit_api_secret: str = ""
    # signaling URL handed to clients along with their token
    livekit_public_url: str = "ws://localhost:7880"

    # agent names that may be dispatched; the first is the default
    agent_names: tuple[str, ...] = ("echo",)

    # room closes if nobody joins within this many seconds
    empty_timeout_seconds: int = 120
    # room closes this long after the last participant leaves
    departure_timeout_seconds: int = 10
    # the reaper deletes sessions older than this, occupied or not
    session_max_seconds: int = 1800
    reaper_interval_seconds: float = 30.0
    token_ttl_seconds: int = 3600
    # ended sessions remembered in memory
    ended_history_size: int = 200

    cors_origins: tuple[str, ...] = field(
        default=("http://localhost:8000", "http://127.0.0.1:8000")
    )
    log_level: str = "INFO"

    @classmethod
    def from_env(cls) -> "Settings":
        env = os.environ
        d = cls()
        return cls(
            livekit_url=env.get("LIVEKIT_URL", d.livekit_url),
            livekit_api_key=env.get("LIVEKIT_API_KEY", ""),
            livekit_api_secret=env.get("LIVEKIT_API_SECRET", ""),
            livekit_public_url=env.get("LIVEKIT_PUBLIC_URL", d.livekit_public_url),
            agent_names=_csv(env.get("AGENT_NAMES", "")) or d.agent_names,
            empty_timeout_seconds=int(env.get("EMPTY_TIMEOUT_SECONDS", d.empty_timeout_seconds)),
            departure_timeout_seconds=int(
                env.get("DEPARTURE_TIMEOUT_SECONDS", d.departure_timeout_seconds)
            ),
            session_max_seconds=int(env.get("SESSION_MAX_SECONDS", d.session_max_seconds)),
            reaper_interval_seconds=float(
                env.get("REAPER_INTERVAL_SECONDS", d.reaper_interval_seconds)
            ),
            token_ttl_seconds=int(env.get("TOKEN_TTL_SECONDS", d.token_ttl_seconds)),
            ended_history_size=int(env.get("ENDED_HISTORY_SIZE", d.ended_history_size)),
            cors_origins=_csv(env.get("CORS_ORIGINS", "")) or d.cors_origins,
            log_level=env.get("LOG_LEVEL", d.log_level).upper(),
        )
