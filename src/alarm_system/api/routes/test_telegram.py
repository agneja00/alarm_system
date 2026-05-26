from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from alarm_system.api.telegram_client import TelegramApiClient


class TestTelegramRequest(BaseModel):
    chat_id: str


def build_test_telegram_router(
    telegram_client: TelegramApiClient,
) -> APIRouter:
    router = APIRouter(tags=["telegram"])

    @router.post("/internal/test-telegram")
    async def send_test_telegram(
        payload: TestTelegramRequest,
    ) -> dict[str, str]:
        await telegram_client.send_message(
            chat_id=payload.chat_id,
            text="🚨 Test notification from Alarm System",
        )

        return {"status": "sent"}

    return router