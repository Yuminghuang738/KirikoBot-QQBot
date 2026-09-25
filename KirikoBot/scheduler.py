from __future__ import annotations

import logging
import threading
import time
from typing import Any

import maintenance_service

logger = logging.getLogger(__name__)


class BotScheduler:
    """Background scheduler for daily maintenance.

    这里原本还负责提醒、早间问候、群推送订阅和箱头抓取。QQ 官方平台
    下线了主动推送（2025-04-21），这些「到点主动发一条消息」的功能
    都不可能实现，已全部删除。剩下的只有保留策略 + 数据库备份，
    它们不需要发消息，也只在每天跑一次。
    """

    CHECK_INTERVAL = 5  # seconds between checks

    def __init__(
        self, db: Any, client: Any = None, feature_gate: Any = None,
    ) -> None:
        self.db = db
        self.client = client
        self.feature_gate = feature_gate
        self._running = False
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True, name="scheduler")
        self._thread.start()
        logger.info("Scheduler started")

    def stop(self) -> None:
        self._running = False

    def _loop(self) -> None:
        while self._running:
            try:
                # Retention + DB backup, self-guarded to run once per day
                maintenance_service.run_daily(self.db, self.db.db_file)
            except Exception:
                logger.exception("Scheduler loop error")
            time.sleep(self.CHECK_INTERVAL)
