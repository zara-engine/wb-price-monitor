"""Уведомления в Telegram."""

from __future__ import annotations

import html
import logging

import httpx

from storage import Change

log = logging.getLogger(__name__)


def _fmt_price(v: float | None) -> str:
    return f"{v:,.0f} ₽".replace(",", " ") if v is not None else "—"


def format_change(ch: Change) -> str:
    p = ch.product
    title = f'<a href="{p.url}">{html.escape(p.name)}</a> ({html.escape(p.brand)})'
    if ch.kind == "out":
        return f"⚪ {title}\nЗакончился в наличии"
    if ch.kind == "back":
        return f"🟢 {title}\nСнова в наличии: {_fmt_price(p.price)}"
    arrow = "🔻" if (ch.percent or 0) < 0 else "🔺"
    return (f"{arrow} {title}\n"
            f"{_fmt_price(ch.old_price)} → <b>{_fmt_price(p.price)}</b> "
            f"({ch.percent:+.1f}%)")


def build_message(changes: list[Change]) -> str:
    return "📊 <b>Изменения на Wildberries</b>\n\n" + "\n\n".join(
        format_change(c) for c in changes)


class TelegramNotifier:
    def __init__(self, token: str | None, chat_id: str | None):
        self.token, self.chat_id = token, chat_id

    def send(self, text: str) -> None:
        if not (self.token and self.chat_id):
            log.info("Telegram не настроен, вывожу в консоль:\n%s", text)
            return
        r = httpx.post(
            f"https://api.telegram.org/bot{self.token}/sendMessage",
            json={"chat_id": self.chat_id, "text": text, "parse_mode": "HTML",
                  "disable_web_page_preview": True},
            timeout=15,
        )
        if r.status_code != 200:
            log.error("Ошибка Telegram: %s %s", r.status_code, r.text)
