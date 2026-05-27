from __future__ import annotations

from contextlib import asynccontextmanager
import logging
import os
from typing import Any

import sentry_sdk

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.requests import Request
from fastapi.responses import JSONResponse

from alarm_system.alert_store import (
    AlertStore,
    InMemoryAlertStore,
    RedisAlertCache,
    build_cached_alert_store,
)
from alarm_system.api.migrations import (
    apply_sql_migrations,
    should_auto_apply_sql_migrations,
)
from alarm_system.api.routes import (
    build_alerts_router,
    build_telegram_router,
)
from alarm_system.api.routes.telegram_commands import TELEGRAM_BOT_COMMANDS
from alarm_system.api.telegram_client import TelegramApiClient
from alarm_system.state import (
    DeliveryAttemptStore,
    InMemoryDeliveryAttemptStore,
    InMemoryMuteStore,
    InMemorySessionStore,
    MuteStore,
    RedisDeliveryAttemptStore,
    RedisMuteStore,
    RedisSessionStore,
    SessionStore,
)

logger = logging.getLogger(__name__)
_PROD_ENVS = {"staging", "prod"}

def _init_sentry() -> None:
    sentry_dsn = os.getenv("SENTRY_DSN")

    if not sentry_dsn:
        return

    sentry_sdk.init(
        dsn=sentry_dsn,
        traces_sample_rate=1.0,
        environment=os.getenv("ALARM_ENV", "dev"),
    )

def create_app(
    *,
    store: AlertStore | None = None,
    telegram_client: TelegramApiClient | None = None,
    mute_store: MuteStore | None = None,
    attempt_store: DeliveryAttemptStore | None = None,
    session_store: SessionStore | None = None,
) -> FastAPI:
    _init_sentry()

    alarm_env = _read_alarm_env()
    shared_redis_client = _build_shared_redis_client(alarm_env=alarm_env)
    resolved_store = store or _store_from_env(
        shared_redis_client=shared_redis_client,
    )
    resolved_telegram_client = telegram_client or _telegram_client_from_env()
    resolved_mute_store, resolved_attempt_store = _resolve_runtime_stores(
        mute_store=mute_store,
        attempt_store=attempt_store,
        redis_client=shared_redis_client,
        alarm_env=alarm_env,
    )
    resolved_session_store = session_store or _resolve_session_store(
        redis_client=shared_redis_client,
        alarm_env=alarm_env,
    )
    require_internal_auth = _should_require_internal_api_auth(alarm_env=alarm_env)
    internal_api_key = _resolve_internal_api_key(
        require_auth=require_internal_auth
    )
    webhook_url = _optional_env("ALARM_TELEGRAM_WEBHOOK_URL")
    webhook_secret = _optional_env("ALARM_TELEGRAM_WEBHOOK_SECRET")

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        if (
            webhook_url is not None
            and (webhook_secret is None or not str(webhook_secret).strip())
            and alarm_env in _PROD_ENVS
        ):
            logger.warning(
                "telegram_webhook_missing_secret_token",
                extra={
                    "hint": (
                        "Set ALARM_TELEGRAM_WEBHOOK_SECRET so Telegram sends "
                        "X-Telegram-Bot-Api-Secret-Token on webhook requests."
                    ),
                },
            )

        if webhook_url is not None:
            try:
                await resolved_telegram_client.set_webhook(
                    webhook_url=webhook_url,
                    secret_token=webhook_secret,
                )
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "telegram_webhook_registration_failed",
                    extra={"url": webhook_url, "error": str(exc)},
                )
            else:
                logger.info(
                    "telegram_webhook_registered",
                    extra={"url": webhook_url},
                )

        try:
            await resolved_telegram_client.set_my_commands(
                commands=TELEGRAM_BOT_COMMANDS,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "telegram_set_my_commands_failed",
                extra={"error": str(exc)},
            )
        else:
            logger.info(
                "telegram_set_my_commands_registered",
                extra={"count": len(TELEGRAM_BOT_COMMANDS)},
            )

        yield

    app = FastAPI(
        title="Alarm System Internal API",
        description=(
            "Interactive Telegram webhook and internal CRUD API for alerts."
        ),
        version="0.1.0",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://localhost:5173",
            "https://alarm-system-frontend.vercel.app",
        ],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/health", tags=["health"])
    def health() -> dict[str, str]:
        raise RuntimeError("Sentry backend test error")

    @app.exception_handler(RequestValidationError)
    async def request_validation_error_handler(
        request: Request,
        exc: RequestValidationError,
    ) -> JSONResponse:
        logger.warning(
            "request_validation_error",
            extra={
                "path": request.url.path,
                "method": request.method,
                "query": dict(request.query_params),
                "has_body": request.headers.get("content-length", "0") != "0",
                "errors": exc.errors(),
            },
        )

        return JSONResponse(
            status_code=422,
            content={"detail": exc.errors()},
        )

    app.include_router(
        build_alerts_router(
            resolved_store,
            resolved_telegram_client,
            internal_api_key=internal_api_key,
        )
    )

    app.include_router(
        build_telegram_router(
            store=resolved_store,
            telegram_client=resolved_telegram_client,
            mute_store=resolved_mute_store,
            attempt_store=resolved_attempt_store,
            session_store=resolved_session_store,
            secret_token=webhook_secret,
        )
    )

    return app


def _build_shared_redis_client(*, alarm_env: str) -> Any | None:
    """Construct a single Redis client reused across stores.

    In ``staging``/``prod`` Redis is mandatory for runtime state stores
    (mute/session/attempt history), so startup fails if URL is missing
    or the client cannot be built.

    Centralising construction here avoids opening two independent
    connection pools for alerts cache vs mute/attempt stores.
    """

    redis_url = os.getenv("ALARM_REDIS_URL")

    if redis_url is None or not redis_url.strip():
        if alarm_env in _PROD_ENVS:
            _raise_startup_error(
                "ALARM_REDIS_URL is required in staging/prod for runtime state stores.",
                event="api_shared_redis_missing",
                alarm_env=alarm_env,
            )
        return None

    try:
        client = _build_redis_client(redis_url.strip())
    except Exception as exc:  # noqa: BLE001
        if alarm_env in _PROD_ENVS:
            _raise_startup_error(
                "Failed to initialize Redis client required by runtime state stores.",
                event="api_shared_redis_unavailable",
                alarm_env=alarm_env,
                error=str(exc),
            )

        logger.error(
            "api_shared_redis_unavailable",
            extra={"error": str(exc)},
        )

        return None

    logger.info(
        "api_shared_redis_built",
        extra={"connectivity": "shared"},
    )

    return client


def _resolve_runtime_stores(
    *,
    mute_store: MuteStore | None,
    attempt_store: DeliveryAttemptStore | None,
    redis_client: Any | None,
    alarm_env: str,
) -> tuple[MuteStore, DeliveryAttemptStore]:
    resolved_mute = mute_store

    if resolved_mute is None:
        if redis_client is None and alarm_env in _PROD_ENVS:
            _raise_startup_error(
                "Redis-backed mute store is required in staging/prod.",
                event="api_mute_store_requires_redis",
                alarm_env=alarm_env,
            )

        resolved_mute = (
            RedisMuteStore(redis_client)
            if redis_client is not None
            else InMemoryMuteStore()
        )

    resolved_attempt = attempt_store

    if resolved_attempt is None:
        if redis_client is None and alarm_env in _PROD_ENVS:
            _raise_startup_error(
                "Redis-backed delivery-attempt store is required in staging/prod.",
                event="api_attempt_store_requires_redis",
                alarm_env=alarm_env,
            )

        resolved_attempt = (
            RedisDeliveryAttemptStore(redis_client)
            if redis_client is not None
            else InMemoryDeliveryAttemptStore()
        )

    return resolved_mute, resolved_attempt


def _resolve_session_store(
    *,
    redis_client: Any | None,
    alarm_env: str,
) -> SessionStore:
    """Pick ``RedisSessionStore`` when Redis is wired, otherwise fall back.

    Mirrors the mute / attempt resolution policy: Redis for
    staging/prod, in-memory for dev/test or a missing/failed
    ``ALARM_REDIS_URL``.
    """

    if redis_client is not None:
        return RedisSessionStore(redis_client)

    if alarm_env in _PROD_ENVS:
        _raise_startup_error(
            "Redis-backed session store is required in staging/prod.",
            event="api_session_store_requires_redis",
            alarm_env=alarm_env,
        )

    return InMemorySessionStore()


def _store_from_env(*, shared_redis_client: Any | None = None) -> AlertStore:
    alarm_env = _read_alarm_env()
    postgres_dsn = os.getenv("ALARM_POSTGRES_DSN")

    if not postgres_dsn or not postgres_dsn.strip():
        if alarm_env in {"dev", "test"}:
            return InMemoryAlertStore()

        raise RuntimeError(
            "ALARM_POSTGRES_DSN is required when ALARM_ENV is staging/prod."
        )

    postgres_dsn_stripped = postgres_dsn.strip()

    cache_ttl_seconds = _parse_int_env(
        "ALARM_CONFIG_CACHE_TTL_SECONDS",
        default=30,
    )

    if should_auto_apply_sql_migrations():
        apply_sql_migrations(postgres_dsn=postgres_dsn_stripped)

        if shared_redis_client is not None:
            RedisAlertCache(
                redis_client=shared_redis_client,
            ).invalidate_runtime_snapshot()

    if shared_redis_client is None:
        return build_cached_alert_store(
            postgres_dsn=postgres_dsn_stripped,
            redis_client=_build_noop_redis(),
            cache_ttl_seconds=cache_ttl_seconds,
        )

    return build_cached_alert_store(
        postgres_dsn=postgres_dsn_stripped,
        redis_client=shared_redis_client,
        cache_ttl_seconds=cache_ttl_seconds,
    )


def _telegram_client_from_env() -> TelegramApiClient:
    bot_token = os.getenv("ALARM_TELEGRAM_BOT_TOKEN")

    if bot_token is None or not bot_token.strip():
        raise RuntimeError(
            "ALARM_TELEGRAM_BOT_TOKEN is required for Telegram webhook API."
        )

    return TelegramApiClient(bot_token=bot_token.strip())


def _build_redis_client(redis_url: str) -> Any:
    try:
        import redis
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "The 'redis' package is required for API cache integration."
        ) from exc

    return redis.Redis.from_url(redis_url, decode_responses=True)


def _build_noop_redis() -> Any:
    class _NoopRedis:
        def get(self, key: str) -> None:
            return None

        def set(
            self,
            key: str,
            value: str,
            ex: int | None = None,
            nx: bool = False,
        ) -> bool:
            return True

        def delete(self, key: str) -> int:
            return 0

    return _NoopRedis()


def _parse_int_env(name: str, default: int) -> int:
    value = os.getenv(name)

    if value is None:
        return default

    return int(value.strip())


def _optional_env(name: str) -> str | None:
    value = os.getenv(name)

    if value is None:
        return None

    normalized = value.strip()

    if not normalized:
        return None

    return normalized


def _read_alarm_env() -> str:
    value = os.getenv("ALARM_ENV", "dev").strip().lower()

    if value in {"dev", "test", "staging", "prod"}:
        return value

    raise ValueError(
        "Invalid ALARM_ENV value. Use one of dev/test/staging/prod."
    )


def _should_require_internal_api_auth(*, alarm_env: str) -> bool:
    raw = os.getenv("ALARM_INTERNAL_API_AUTH_REQUIRED")

    if raw is None or not raw.strip():
        return alarm_env in _PROD_ENVS

    return _parse_bool(raw)


def _resolve_internal_api_key(*, require_auth: bool) -> str | None:
    raw = os.getenv("ALARM_INTERNAL_API_KEY")
    normalized = raw.strip() if raw is not None else ""

    if not require_auth:
        return None

    if not normalized:
        _raise_startup_error(
            "ALARM_INTERNAL_API_KEY is required when internal API auth is enabled.",
            event="api_internal_auth_key_missing",
        )

    return normalized


def _parse_bool(value: str) -> bool:
    normalized = value.strip().lower()

    if normalized in {"1", "true", "yes", "on"}:
        return True

    if normalized in {"0", "false", "no", "off"}:
        return False

    raise ValueError(f"Invalid boolean value: {value}")


def _raise_startup_error(message: str, *, event: str, **extra: Any) -> None:
    logger.error(event, extra=extra or None)
    raise RuntimeError(message)