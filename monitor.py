"""
Мониторинг цен и наличия товаров на Wildberries.

Запуск:
    python monitor.py --once           # одна проверка
    python monitor.py --interval 60    # проверка каждые 60 минут
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import time
from pathlib import Path

from dotenv import load_dotenv

from notifier import TelegramNotifier, build_message
from storage import append_history, find_changes, load_state, save_state
from wb_client import WBClient

log = logging.getLogger("monitor")


def read_articles(path: Path) -> list[int]:
    """Читает артикулы: по одному на строку, можно ссылки на товар."""
    nms: list[int] = []
    for line in path.read_text("utf-8").splitlines():
        line = line.split("#")[0].strip()
        if not line:
            continue
        m = re.search(r"/catalog/(\d+)", line) or re.fullmatch(r"(\d+)", line)
        if m:
            nms.append(int(m.group(1)))
        else:
            log.warning("Не понял строку: %s", line)
    return list(dict.fromkeys(nms))  # убираем дубли, сохраняя порядок


def check(client: WBClient, notifier: TelegramNotifier, args) -> None:
    nms = read_articles(Path(args.articles))
    if not nms:
        log.error("Список артикулов пуст")
        return

    log.info("Проверяю %d товаров…", len(nms))
    products = client.fetch(nms)
    log.info("Получены данные по %d товарам", len(products))

    state_path = Path(args.state)
    changes = find_changes(products, load_state(state_path), args.threshold)
    append_history(Path(args.report), products)
    save_state(state_path, products)

    if changes:
        notifier.send(build_message(changes))
        log.info("Отправлено уведомление: %d изменений", len(changes))
    else:
        log.info("Изменений нет")


def main() -> None:
    load_dotenv()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s  %(levelname)s  %(message)s",
                        datefmt="%H:%M:%S")

    ap = argparse.ArgumentParser(description="Мониторинг цен Wildberries")
    ap.add_argument("--articles", default="articles.txt")
    ap.add_argument("--report", default="prices.xlsx")
    ap.add_argument("--state", default="state.json")
    ap.add_argument("--interval", type=int, default=60, help="минуты")
    ap.add_argument("--threshold", type=float,
                    default=float(os.getenv("PRICE_THRESHOLD", 0)),
                    help="мин. изменение цены в %% для уведомления")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--show-browser", action="store_true",
                    help="показывать окно браузера (для отладки)")
    args = ap.parse_args()

    client = WBClient(dest=os.getenv("WB_DEST", "-1257786"),
                      headless=not args.show_browser)
    notifier = TelegramNotifier(os.getenv("TG_BOT_TOKEN"), os.getenv("TG_CHAT_ID"))

    while True:
        try:
            check(client, notifier, args)
        except Exception:  # noqa: BLE001
            log.exception("Ошибка проверки")
        if args.once:
            break
        log.info("Следующая проверка через %d мин.", args.interval)
        time.sleep(args.interval * 60)


if __name__ == "__main__":
    main()
