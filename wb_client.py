"""
Клиент Wildberries: получение данных карточек товаров.

Как устроена защита WB (по результатам анализа трафика):
  * Данные карточки отдаёт внутренний эндпоинт
    /__internal/u-card/cards/v4/detail?nm=<артикул>[;<артикул>…]
  * Без прохождения JS-проверки сервер отвечает 498/403.
  * После проверки браузер получает куку `x_wbaas_token`. Токен подписан,
    привязан к IP и точному User-Agent и живёт около 36 часов.
  * Фронтенд дополнительно шлёт заголовки deviceid, x-spa-version,
    x-requested-with и client hints (sec-ch-ua*), а сервер сверяет
    их согласованность с User-Agent.

Поэтому клиент:
  1. Открывает страницу товара в установленном Google Chrome (Playwright)
     с постоянным профилем — проверка проходит как у обычного пользователя.
  2. Перехватывает настоящий запрос страницы к API и запоминает его
     заголовки и куки (ничего не прописано вручную — при обновлении
     фронтенда WB всё подхватится само).
  3. Делает дальнейшие запросы через curl_cffi с TLS-отпечатком Chrome,
     пачками по нескольку артикулов. Сессия кэшируется на диск.
  4. Если прямые запросы отклонены — обновляет сессию, а при повторной
     неудаче переходит в браузерный режим: открывает страницы товаров
     и забирает ответ, который запрашивает сам сайт.
"""

from __future__ import annotations

import json
import logging
import random
import time
from dataclasses import dataclass
from pathlib import Path

from curl_cffi import requests as cffi
from playwright.sync_api import BrowserContext, sync_playwright

log = logging.getLogger(__name__)

SITE = "https://www.wildberries.ru"
DETAIL_PATH = "/__internal/u-card/cards/v4/detail"

PROFILE_DIR = Path(".wb_profile")      # профиль браузера между запусками
SESSION_FILE = Path(".wb_session.json")  # кэш заголовков и кук

BLOCK_STATUSES = {401, 403, 498}
CHUNK_SIZE = 10
# заголовки, которые не копируем из перехваченного запроса
SKIP_HEADERS = {"cookie", "content-length", "accept-encoding", "host", "referer"}


@dataclass
class Product:
    nm: int
    name: str
    brand: str
    supplier: str
    price: float | None        # реальная цена, ₽ (None — нет в наличии)
    basic_price: float | None  # перечёркнутая цена, ₽
    quantity: int
    rating: float
    feedbacks: int

    @property
    def url(self) -> str:
        return f"{SITE}/catalog/{self.nm}/detail.aspx"

    @property
    def in_stock(self) -> bool:
        return self.price is not None and self.quantity > 0

    @property
    def discount(self) -> int | None:
        if not self.price or not self.basic_price:
            return None
        return round((1 - self.price / self.basic_price) * 100)


def parse_product(raw: dict) -> Product:
    """Сырой JSON карточки → Product.

    Цена хранится на уровне размера (sizes[].price) в копейках:
    basic — перечёркнутая, product — реальная. Берём минимальную
    среди размеров, у которых цена есть.
    """
    prices = [s["price"] for s in raw.get("sizes", []) if s.get("price")]
    best = min(prices, key=lambda p: p.get("product", 0)) if prices else None
    return Product(
        nm=raw["id"],
        name=raw.get("name", ""),
        brand=raw.get("brand", ""),
        supplier=raw.get("supplier", ""),
        price=best["product"] / 100 if best else None,
        basic_price=best["basic"] / 100 if best and best.get("basic") else None,
        quantity=raw.get("totalQuantity", 0),
        rating=raw.get("reviewRating", raw.get("rating", 0)),
        feedbacks=raw.get("feedbacks", 0),
    )


class BlockedError(Exception):
    """Сервер отклонил запрос антибот-защитой."""


class WBClient:
    def __init__(self, dest: str = "-1257786", headless: bool = True):
        self.dest = dest
        self.headless = headless
        self.headers: dict[str, str] = {}
        self.cookies: dict[str, str] = {}
        self._load_session()

    # ---------- браузер ----------

    def _open_context(self, p) -> BrowserContext:
        """Постоянный профиль в установленном Chrome (или в Chromium)."""
        opts = dict(
            user_data_dir=str(PROFILE_DIR),
            headless=self.headless,
            locale="ru-RU",
            args=["--disable-blink-features=AutomationControlled"],
        )
        try:
            return p.chromium.launch_persistent_context(channel="chrome", **opts)
        except Exception:  # noqa: BLE001 — Chrome не установлен
            log.warning("Google Chrome не найден, использую встроенный Chromium")
            return p.chromium.launch_persistent_context(**opts)

    # ---------- сессия ----------

    def _load_session(self) -> None:
        if SESSION_FILE.exists():
            data = json.loads(SESSION_FILE.read_text("utf-8"))
            self.headers, self.cookies = data["headers"], data["cookies"]

    def _save_session(self) -> None:
        SESSION_FILE.write_text(
            json.dumps({"headers": self.headers, "cookies": self.cookies},
                       ensure_ascii=False, indent=2), "utf-8")

    def refresh_session(self, nm_hint: int) -> None:
        """Открывает товар в браузере и перехватывает настоящий запрос к API."""
        log.info("Обновляю сессию через браузер…")
        with sync_playwright() as p:
            ctx = self._open_context(p)
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            try:
                with page.expect_request(lambda r: DETAIL_PATH in r.url,
                                         timeout=60_000) as req:
                    page.goto(f"{SITE}/catalog/{nm_hint}/detail.aspx",
                              wait_until="domcontentloaded")
                headers = req.value.all_headers()
            except Exception as e:  # noqa: BLE001
                ctx.close()
                raise BlockedError(f"страница не запросила данные: {e}") from e

            self.headers = {k: v for k, v in headers.items()
                            if not k.startswith(":") and k not in SKIP_HEADERS}
            self.cookies = {c["name"]: c["value"] for c in ctx.cookies()
                            if "wildberries" in c["domain"]}
            ctx.close()

        if "x_wbaas_token" not in self.cookies:
            raise BlockedError("браузер не получил x_wbaas_token")
        self._save_session()
        log.info("Сессия обновлена")

    # ---------- прямые запросы ----------

    def _fetch_chunk(self, nms: list[int]) -> list[dict]:
        params = {
            "appType": 1, "curr": "rub", "dest": self.dest, "spp": 30,
            "hide_vflags": 4294967296, "mtype": 257, "lang": "ru",
            "ab_testing": "false", "nm": ";".join(map(str, nms)),
        }
        r = cffi.get(
            SITE + DETAIL_PATH, params=params, impersonate="chrome",
            headers={**self.headers,
                     "referer": f"{SITE}/catalog/{nms[0]}/detail.aspx"},
            cookies=self.cookies, timeout=20,
        )
        if r.status_code in BLOCK_STATUSES:
            raise BlockedError(f"HTTP {r.status_code}")
        r.raise_for_status()
        return r.json().get("products", [])

    def fetch_direct(self, nms: list[int]) -> list[Product]:
        if not self.cookies:
            self.refresh_session(nms[0])

        result: list[Product] = []
        for i in range(0, len(nms), CHUNK_SIZE):
            chunk = nms[i:i + CHUNK_SIZE]
            try:
                raw = self._fetch_chunk(chunk)
            except BlockedError as e:
                log.info("Запрос отклонён (%s), обновляю сессию…", e)
                self.refresh_session(chunk[0])
                raw = self._fetch_chunk(chunk)  # вторая неудача → наружу
            result += [parse_product(p) for p in raw]
            time.sleep(random.uniform(1, 2))  # не нагружаем сервер
        return result

    # ---------- браузерный режим (запасной) ----------

    def fetch_via_browser(self, nms: list[int]) -> list[Product]:
        log.info("Перехожу в браузерный режим")
        result: list[Product] = []
        with sync_playwright() as p:
            ctx = self._open_context(p)
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            for nm in nms:
                try:
                    with page.expect_response(
                        lambda r, nm=nm: DETAIL_PATH in r.url
                        and f"nm={nm}" in r.url and r.status == 200,
                        timeout=60_000,
                    ) as resp:
                        page.goto(f"{SITE}/catalog/{nm}/detail.aspx",
                                  wait_until="domcontentloaded")
                    raw = resp.value.json().get("products", [])
                    result += [parse_product(x) for x in raw if x["id"] == nm]
                except Exception as e:  # noqa: BLE001
                    log.warning("Артикул %s: не удалось получить данные (%s)", nm, e)
                time.sleep(random.uniform(2, 4))
            ctx.close()
        return result

    # ---------- общий вход ----------

    def fetch(self, nms: list[int]) -> list[Product]:
        try:
            return self.fetch_direct(nms)
        except Exception as e:  # noqa: BLE001
            log.warning("Прямые запросы не прошли (%s)", e)
            return self.fetch_via_browser(nms)
