from __future__ import annotations

from contextlib import asynccontextmanager
import logging
import os
from typing import Any

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
from alarm_system.api.routes import build_alerts_router, build_telegram_router
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


def create_app(
    *,
    store: AlertStore | None = None,
    telegram_client: TelegramApiClient | None = None,
    mute_store: MuteStore | None = None,
    attempt_store: DeliveryAttemptStore | None = None,
    session_store: SessionStore | None = None,
) -> FastAPI:
    alarm_env = _read_alarm_env()

    shared_redis_client = _build_shared_redis_client(
        alarm_env=alarm_env,
    )

    resolved_store = store or _store_from_env(
        shared_redis_client=shared_redis_client,
    )

    resolved_telegram_client = (
        telegram_client or _telegram_client_from_env()
    )

    resolved_mute_store, resolved_attempt_store = (
        _resolve_runtime_stores(
            mute_store=mute_store,
            attempt_store=attempt_store,
            redis_client=shared_redis_client,
            alarm_env=alarm_env,
        )
    )

    resolved_session_store = session_store or _resolve_session_store(
        redis_client=shared_redis_client,
        alarm_env=alarm_env,
    )

    require_internal_auth = (
        _should_require_internal_api_auth(alarm_env=alarm_env)
    )

    internal_api_key = _resolve_internal_api_key(
        require_auth=require_internal_auth,
    )

    webhook_url = _optional_env("ALARM_TELEGRAM_WEBHOOK_URL")
    webhook_secret = _optional_env("ALARM_TELEGRAM_WEBHOOK_SECRET")

    app = FastAPI(
        title="Alarm System Internal API",
        version="0.1.0",
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
                    extra={
                        "url": webhook_url,
                        "error": str(exc),
                    },
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

    app.router.lifespan_context = lifespan

    @app.get("/health", tags=["health"])
    def health() -> dict[str, str]:
        return {"status": "ok"}

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
                "has_body": (
                    request.headers.get("content-length", "0") != "0"
                ),
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