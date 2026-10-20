"""Логи с вырезанием секретов: токены, сессия и initData никогда не попадают в вывод."""
from __future__ import annotations

import logging
import re
import sys

# initData Telegram (query_id=...&user=...&hash=...) и заголовок "tma ..."
_INIT_DATA_RE = re.compile(r"(tma\s+)?(query_id|user|auth_date)=[^\s'\"]+")


class RedactFilter(logging.Filter):
    def __init__(self, secrets: list[str]):
        super().__init__()
        self.secrets = [s for s in secrets if len(s) >= 6]

    def redact(self, text: str) -> str:
        for s in self.secrets:
            text = text.replace(s, "***")
        return _INIT_DATA_RE.sub("***initData***", text)

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = self.redact(record.getMessage())
        record.args = None
        if record.exc_text:
            record.exc_text = self.redact(record.exc_text)
        return True


def setup_logging(secrets: list[str], level: int = logging.INFO) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    handler.addFilter(RedactFilter(secrets))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    for noisy in ("pyrogram", "aiogram.event", "httpx"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
