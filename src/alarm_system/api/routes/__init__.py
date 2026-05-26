from alarm_system.api.routes.alerts import build_alerts_router
from alarm_system.api.routes.telegram_webhook import build_telegram_router
from alarm_system.api.routes.test_telegram import (
    build_test_telegram_router,
)

__all__ = [
    "build_alerts_router",
    "build_telegram_router",
    "build_test_telegram_router",
]