"""Выгрузка истории цен в Google Таблицы (через сервисный аккаунт)."""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

import gspread

from storage import HEADERS, history_rows
from wb_client import Product

log = logging.getLogger(__name__)

WORKSHEET = "История цен"


class SheetsError(Exception):
    """Ошибка настройки: неверная ссылка, нет доступа и т. п."""


def _sheet_key(value: str) -> str:
    """Принимает id таблицы или ссылку на неё."""
    value = value.strip()
    m = re.search(r"/spreadsheets/(?:u/\d+/)?d/([\w-]+)", value)
    key = m.group(1) if m else value
    if key == "e" or not re.fullmatch(r"[\w-]{20,}", key):
        raise SheetsError(
            f"GOOGLE_SHEET не похож на ссылку на таблицу: {value!r}. Вставьте "
            "ссылку из адресной строки, вида https://docs.google.com/spreadsheets/d/<id>/edit")
    return key


class GoogleSheet:
    def __init__(self, sheet: str, credentials: str = "service_account.json"):
        self.key = _sheet_key(sheet)
        self.credentials = credentials
        self._ws: gspread.Worksheet | None = None

    def _worksheet(self) -> gspread.Worksheet:
        if self._ws is None:
            try:
                gc = gspread.service_account(filename=self.credentials)
            except FileNotFoundError as ex:
                raise SheetsError(f"Не найден JSON-ключ сервисного аккаунта: "
                                  f"{self.credentials}") from ex
            try:
                sh = gc.open_by_key(self.key)
            except (gspread.SpreadsheetNotFound, PermissionError) as ex:
                email = json.loads(Path(self.credentials).read_text("utf-8")).get(
                    "client_email", "сервисного аккаунта")
                raise SheetsError(
                    "Таблица не найдена или нет доступа. Проверьте ссылку и выдайте "
                    f"права редактора для {email}") from ex
            try:
                ws = sh.worksheet(WORKSHEET)
            except gspread.WorksheetNotFound:
                ws = sh.add_worksheet(WORKSHEET, rows=1000, cols=len(HEADERS))
            if not ws.row_values(1):
                ws.update([HEADERS], "A1")
                ws.format(f"A1:{gspread.utils.rowcol_to_a1(1, len(HEADERS))}", {
                    "textFormat": {"bold": True,
                                   "foregroundColor": {"red": 1, "green": 1, "blue": 1}},
                    "backgroundColor": {"red": 0.48, "green": 0.17, "blue": 0.75},
                    "horizontalAlignment": "CENTER",
                })
                ws.freeze(rows=1)
            self._ws = ws
        return self._ws

    def append(self, products: list[Product]) -> None:
        """Дописывает строки в конец листа «История цен»."""
        self._worksheet().append_rows(history_rows(products),
                                      value_input_option="RAW",
                                      table_range="A1")
