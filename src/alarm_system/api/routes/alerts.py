from __future__ import annotations

import logging
from typing import NoReturn

from fastapi import APIRouter, Depends, Header, HTTPException, Query

from alarm_system.alert_store import (
    AlertStore,
    AlertStoreBackendError,
    AlertStoreContractError,
    AlertStoreConflictError,
)
from alarm_system.api.rule_catalog import (
    load_rule_identities_cached,
    load_rules_cached,
)
from alarm_system.api.schemas import (
    AlertCreateRequest,
    AlertListResponse,
    AlertResponse,
    AlertUpdateRequest,
    ChannelBindingListResponse,
    ChannelBindingResponse,
    ChannelBindingUpsertRequest,
    RuleCatalogResponse,
    RuleSummary,
)
from alarm_system.api.telegram_client import TelegramApiClient
from alarm_system.entities import DeliveryChannel

logger = logging.getLogger(__name__)


def _raise_backend_unavailable(exc: AlertStoreBackendError) -> NoReturn:
    logger.error("alert_store_backend_error", exc_info=exc)
    raise HTTPException(
        status_code=503,
        detail="alert store temporarily unavailable",
    ) from exc


def _validate_alert_rule_identity(
    *,
    rule_id: str,
    rule_version: int,
) -> None:
    try:
        rule_identities = load_rule_identities_cached()
    except ValueError as exc:
        raise HTTPException(
            status_code=503,
            detail=str(exc),
        ) from exc
    if rule_identities is None:
        return
    if (rule_id, rule_version) not in rule_identities:
        raise HTTPException(
            status_code=422,
            detail=(
                "unknown rule identity for alert: "
                f"{rule_id}#{rule_version}"
            ),
        )


def _list_alerts(
    store: AlertStore,
    user_id: str | None,
    include_disabled: bool,
) -> AlertListResponse:
    try:
        return AlertListResponse(
            alerts=store.list_alerts(
                user_id=user_id,
                include_disabled=include_disabled,
            )
        )
    except AlertStoreBackendError as exc:
        _raise_backend_unavailable(exc)


def _get_alert(store: AlertStore, alert_id: str) -> AlertResponse:
    try:
        alert = store.get_alert(alert_id)
    except AlertStoreBackendError as exc:
        _raise_backend_unavailable(exc)
    if alert is None:
        raise HTTPException(status_code=404, detail="alert not found")
    return AlertResponse(alert=alert)


def _format_alert_created_message(alert) -> str:
    alert_title = alert.alert_type.replace("_", " ").title()

    return (
        "🚨 Alert created\n\n"
        f"Type: {alert_title}\n"
        f"Cooldown: {alert.cooldown_seconds}s\n"
        f"Channels: {', '.join(alert.channels)}"
    )


def _format_alert_status_message(alert) -> str:
    alert_title = alert.alert_type.replace("_", " ").title()

    if alert.enabled:
        status = "resumed"
        emoji = "✅"
    else:
        status = "paused"
        emoji = "⏸️"

    return (
        f"{emoji} Alert {status}\n\n"
        f"Type: {alert_title}\n"
        f"Cooldown: {alert.cooldown_seconds}s\n"
        f"Channels: {', '.join(alert.channels)}"
    )


def _format_alert_deleted_message(alert) -> str:
    alert_title = alert.alert_type.replace("_", " ").title()

    return (
        "🗑️ Alert deleted\n\n"
        f"Type: {alert_title}\n"
        f"Cooldown: {alert.cooldown_seconds}s\n"
        f"Channels: {', '.join(alert.channels)}"
    )


async def _create_alert(
    store: AlertStore,
    payload: AlertCreateRequest,
    telegram_client: TelegramApiClient,
) -> AlertResponse:
    _validate_alert_rule_identity(
        rule_id=payload.rule_id,
        rule_version=payload.rule_version,
    )
    alert = payload.to_alert()

    try:
        existing = store.get_alert(alert.alert_id)
        if existing is not None:
            raise HTTPException(
                status_code=409,
                detail=f"alert {alert.alert_id} already exists",
            )
        saved = store.upsert_alert(alert, expected_version=0)
    except AlertStoreConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except AlertStoreContractError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except AlertStoreBackendError as exc:
        _raise_backend_unavailable(exc)

    try:
        await telegram_client.send_message(
            chat_id=saved.user_id,
            text=_format_alert_created_message(saved),
        )
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "telegram_alert_notification_failed",
            exc_info=exc,
        )

    return AlertResponse(alert=saved)


async def _update_alert(
    store: AlertStore,
    alert_id: str,
    payload: AlertUpdateRequest,
    telegram_client: TelegramApiClient,
) -> AlertResponse:
    _validate_alert_rule_identity(
        rule_id=payload.rule_id,
        rule_version=payload.rule_version,
    )

    try:
        existing = store.get_alert(alert_id)
        if existing is None:
            raise HTTPException(status_code=404, detail="alert not found")

        alert = payload.to_alert(
            alert_id=alert_id,
            created_at=existing.created_at,
        )

        saved = store.upsert_alert(
            alert,
            expected_version=payload.expected_version,
        )

    except AlertStoreConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except AlertStoreContractError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except AlertStoreBackendError as exc:
        _raise_backend_unavailable(exc)

    if existing.enabled != saved.enabled:
        try:
            await telegram_client.send_message(
                chat_id=saved.user_id,
                text=_format_alert_status_message(saved),
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "telegram_alert_status_notification_failed",
                exc_info=exc,
            )

    return AlertResponse(alert=saved)


async def _delete_alert(
    store: AlertStore,
    alert_id: str,
    telegram_client: TelegramApiClient,
) -> dict[str, bool]:
    try:
        existing = store.get_alert(alert_id)

        if existing is None:
            raise HTTPException(status_code=404, detail="alert not found")

        deleted = store.delete_alert(alert_id)

    except AlertStoreBackendError as exc:
        _raise_backend_unavailable(exc)

    if deleted:
        try:
            await telegram_client.send_message(
                chat_id=existing.user_id,
                text=_format_alert_deleted_message(existing),
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "telegram_alert_delete_notification_failed",
                exc_info=exc,
            )

    return {"deleted": deleted}


def _list_bindings(
    store: AlertStore,
    user_id: str | None,
    channel: DeliveryChannel | None,
) -> ChannelBindingListResponse:
    try:
        return ChannelBindingListResponse(
            bindings=store.list_bindings(
                user_id=user_id,
                channel=channel,
            )
        )
    except AlertStoreBackendError as exc:
        _raise_backend_unavailable(exc)


def _get_binding(store: AlertStore, binding_id: str) -> ChannelBindingResponse:
    try:
        binding = store.get_binding(binding_id)
    except AlertStoreBackendError as exc:
        _raise_backend_unavailable(exc)

    if binding is None:
        raise HTTPException(status_code=404, detail="binding not found")

    return ChannelBindingResponse(binding=binding)


def _upsert_binding(
    store: AlertStore,
    payload: ChannelBindingUpsertRequest,
) -> ChannelBindingResponse:
    try:
        saved = store.upsert_binding(payload.to_binding())
    except AlertStoreBackendError as exc:
        _raise_backend_unavailable(exc)

    return ChannelBindingResponse(binding=saved)


def _delete_binding(store: AlertStore, binding_id: str) -> dict[str, bool]:
    try:
        return {"deleted": store.delete_binding(binding_id)}
    except AlertStoreBackendError as exc:
        _raise_backend_unavailable(exc)


def build_alerts_router(  # noqa: C901
    store: AlertStore,
    telegram_client: TelegramApiClient,
    *,
    internal_api_key: str | None = None,
) -> APIRouter:
    def _require_internal_api_auth(
        x_alarm_internal_api_key: str | None = Header(
            default=None,
            alias="X-Alarm-Internal-Api-Key",
        ),
    ) -> None:
        if internal_api_key is None:
            return

        if x_alarm_internal_api_key == internal_api_key:
            return

        raise HTTPException(
            status_code=401,
            detail="invalid internal api key",
        )

    router = APIRouter(
        prefix="/internal",
        tags=["internal-alerts"],
        dependencies=[Depends(_require_internal_api_auth)],
    )

    @router.get("/rules", response_model=RuleCatalogResponse)
    def list_rules() -> RuleCatalogResponse:
        """Server rule catalog (same identities as alert whitelist)."""

        try:
            rules = load_rules_cached()
        except ValueError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

        return RuleCatalogResponse(
            rules=[
                RuleSummary(
                    rule_id=r.rule_id,
                    rule_version=r.version,
                    name=r.name,
                    rule_type=r.rule_type,
                )
                for r in rules
            ]
        )

    @router.get("/alerts", response_model=AlertListResponse)
    def list_alerts(
        user_id: str | None = Query(default=None),
        include_disabled: bool = Query(default=False),
    ) -> AlertListResponse:
        return _list_alerts(store, user_id, include_disabled)

    @router.get("/alerts/{alert_id}", response_model=AlertResponse)
    def get_alert(alert_id: str) -> AlertResponse:
        return _get_alert(store, alert_id)

    @router.post("/alerts", response_model=AlertResponse)
    async def create_alert(payload: AlertCreateRequest) -> AlertResponse:
        return await _create_alert(
            store,
            payload,
            telegram_client,
        )

    @router.put("/alerts/{alert_id}", response_model=AlertResponse)
    async def update_alert(
        alert_id: str,
        payload: AlertUpdateRequest,
    ) -> AlertResponse:
        return await _update_alert(
            store,
            alert_id,
            payload,
            telegram_client,
        )

    @router.delete("/alerts/{alert_id}")
    async def delete_alert(alert_id: str) -> dict[str, bool]:
        return await _delete_alert(
            store,
            alert_id,
            telegram_client,
        )

    @router.get("/channel-bindings", response_model=ChannelBindingListResponse)
    def list_channel_bindings(
        user_id: str | None = Query(default=None),
        channel: DeliveryChannel | None = Query(default=None),
    ) -> ChannelBindingListResponse:
        return _list_bindings(store, user_id, channel)

    @router.get(
        "/channel-bindings/{binding_id}",
        response_model=ChannelBindingResponse,
    )
    def get_channel_binding(binding_id: str) -> ChannelBindingResponse:
        return _get_binding(store, binding_id)

    @router.post("/channel-bindings", response_model=ChannelBindingResponse)
    def upsert_channel_binding(
        payload: ChannelBindingUpsertRequest,
    ) -> ChannelBindingResponse:
        return _upsert_binding(store, payload)

    @router.delete("/channel-bindings/{binding_id}")
    def delete_channel_binding(binding_id: str) -> dict[str, bool]:
        return _delete_binding(store, binding_id)

    return router