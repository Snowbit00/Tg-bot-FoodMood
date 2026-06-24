#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Бот-листенер: управление каналом командами в личном чате с ботом.

Команды (только для админов из TELEGRAM_ADMIN_IDS):
  /new            — создать рецепт с превью и кнопками Нравится/Не нравится/Отмена
  /post [рубрика] — сразу сгенерировать и опубликовать рецепт (как post_now.bat)
  /poll           — опубликовать опрос-вовлечение
  /pollresults    — текущие голоса опроса
  /preview        — быстрый текст-превью без публикации
  /start, /help   — список команд и ваш Telegram ID

Запуск: bot.bat (двойной клик) или `python bot_control.py`.
Должен работать постоянно, чтобы принимать команды.
"""
from __future__ import annotations

try:
    import truststore
    truststore.inject_into_ssl()
except ImportError:
    pass

import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

import generate_and_post as gp   # переиспользуем генерацию/картинку/публикацию

BASE_DIR = Path(__file__).resolve().parent
SCRIPT = BASE_DIR / "generate_and_post.py"
PREVIEW_TXT = BASE_DIR / "out" / "preview.txt"
ACTIVE_POLL = BASE_DIR / "state" / "active_poll.json"   # текущий опрос (пишет generate_and_post)
POLL_PREF = BASE_DIR / "state" / "poll_pref.json"       # голоса опроса (пишет бот)
LOG_FILE = BASE_DIR / "logs" / "bot.log"

log = logging.getLogger("bot")

HELP = (
    "🤖 Управление каналом:\n\n"
    "/new — создать рецепт с превью и кнопками (Нравится/Не нравится/Отмена)\n"
    "/post [рубрика] — сразу опубликовать рецепт\n"
    "/poll — опубликовать опрос-вовлечение\n"
    "/pollresults — текущие голоса опроса\n"
    "/preview — быстрый текст-превью без публикации\n"
    "/help — это сообщение"
)

# Кнопки под полем ввода (reply-клавиатура)
MENU_KB = json.dumps({
    "keyboard": [["🆕 Новый", "📝 Пост"], ["📊 Опрос", "👁 Превью"], ["ℹ️ Помощь"]],
    "resize_keyboard": True,
    "is_persistent": True,
}, ensure_ascii=False)

# Текст кнопки → команда
BUTTON_CMD = {"🆕 Новый": "new", "📝 Пост": "post", "📊 Опрос": "poll",
              "👁 Превью": "preview", "ℹ️ Помощь": "help"}

# Меню команд бота (кнопка «Menu» и автодополнение «/»)
BOT_COMMANDS = [
    {"command": "new", "description": "Создать рецепт с превью и одобрением"},
    {"command": "post", "description": "Сразу опубликовать рецепт"},
    {"command": "poll", "description": "Опубликовать опрос"},
    {"command": "pollresults", "description": "Текущие голоса опроса"},
    {"command": "preview", "description": "Быстрый текст-превью"},
    {"command": "help", "description": "Список команд"},
]

# Inline-кнопки одобрения черновика
APPROVE_KB = json.dumps({"inline_keyboard": [
    [{"text": "👍 Нравится", "callback_data": "like"},
     {"text": "👎 Не нравится", "callback_data": "dislike"}],
    [{"text": "❌ Отмена", "callback_data": "cancel"}],
]}, ensure_ascii=False)

# Inline-кнопки на шаге правок
FEEDBACK_KB = json.dumps({"inline_keyboard": [
    [{"text": "⏭ Пропустить", "callback_data": "skip"},
     {"text": "❌ Отмена", "callback_data": "cancel"}],
]}, ensure_ascii=False)


def setup_logging() -> None:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s  %(levelname)-7s  %(message)s")
    log.setLevel(logging.INFO)
    for h in (logging.FileHandler(LOG_FILE, encoding="utf-8"), logging.StreamHandler(sys.stdout)):
        h.setFormatter(fmt)
        log.addHandler(h)


def parse_admins() -> set[int]:
    raw = os.environ.get("TELEGRAM_ADMIN_IDS", "")
    return {int(p) for p in raw.replace(";", ",").split(",") if p.strip().isdigit()}


class Bot:
    def __init__(self, token: str, admins: set[int]):
        self.api = f"https://api.telegram.org/bot{token}"
        self.token = token
        self.admins = admins
        self.sessions: dict[int, dict] = {}   # chat_id → {recipe, rubric, avoid, awaiting_feedback}

    def send(self, chat_id, text: str, html: bool = False, reply_markup: str | None = None) -> None:
        data = {"chat_id": chat_id, "text": text[:4096], "disable_web_page_preview": "true"}
        if html:
            data["parse_mode"] = "HTML"
        if reply_markup:
            data["reply_markup"] = reply_markup
        try:
            requests.post(f"{self.api}/sendMessage", data=data, timeout=30)
        except Exception as ex:  # noqa: BLE001
            log.warning("Не удалось ответить: %s", ex)

    def answer_callback(self, cq_id: str, text: str = "") -> None:
        try:
            requests.post(f"{self.api}/answerCallbackQuery",
                          data={"callback_query_id": cq_id, "text": text}, timeout=20)
        except Exception as ex:  # noqa: BLE001
            log.warning("answerCallbackQuery: %s", ex)

    def edit_text(self, chat_id, message_id, text: str, html: bool = False) -> None:
        data = {"chat_id": chat_id, "message_id": message_id, "text": text[:4096]}
        if html:
            data["parse_mode"] = "HTML"
        try:
            requests.post(f"{self.api}/editMessageText", data=data, timeout=30)
        except Exception as ex:  # noqa: BLE001
            log.warning("editMessageText: %s", ex)

    def set_menu(self) -> None:
        """Регистрирует меню команд бота (кнопка «Menu» и автодополнение «/»)."""
        try:
            requests.post(f"{self.api}/setMyCommands", json={"commands": BOT_COMMANDS}, timeout=30)
        except Exception as ex:  # noqa: BLE001
            log.warning("Не удалось установить меню команд: %s", ex)

    def run_script(self, extra_args: list[str]) -> tuple[bool, str]:
        """Запускает generate_and_post.py как подпроцесс (тот же путь, что post_now)."""
        try:
            r = subprocess.run(
                [sys.executable, str(SCRIPT), *extra_args],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                cwd=str(BASE_DIR), timeout=300,
            )
            return r.returncode == 0, (r.stdout or "") + (r.stderr or "")
        except subprocess.TimeoutExpired:
            return False, "Превышено время выполнения (300с)."
        except Exception as ex:  # noqa: BLE001
            return False, f"Ошибка запуска: {ex}"

    # ── интерактивное создание: черновик → превью → одобрение ─────────────────
    def show_preview(self, chat_id) -> None:
        sess = self.sessions.get(chat_id)
        if not sess:
            return
        caption, _ = gp.build_post(sess["recipe"], gp.load_config())
        self.send(chat_id, "📋 <b>Превью</b> (картинка создаётся после «Нравится»):", html=True)
        self.send(chat_id, caption, html=True, reply_markup=APPROVE_KB)

    def start_new(self, chat_id, rubric_arg: str = "") -> None:
        self.send(chat_id, "⏳ Генерирую черновик…")
        try:
            cfg = gp.load_config()
            rubric = rubric_arg or gp.pick_rubric(cfg)
            posted = gp.load_posted()
            recipe = gp.generate_recipe(rubric, posted, cfg, gp.build_poll_note(cfg))
        except Exception as ex:  # noqa: BLE001
            log.exception("start_new: %s", ex)
            self.send(chat_id, f"❌ Не удалось сгенерировать: {ex}")
            return
        self.sessions[chat_id] = {"recipe": recipe, "rubric": rubric,
                                  "avoid": posted, "awaiting_feedback": False}
        self.show_preview(chat_id)

    def regenerate(self, chat_id, feedback: str = "") -> None:
        sess = self.sessions.get(chat_id)
        if not sess:
            self.send(chat_id, "Черновик не найден. Начните заново: /new")
            return
        self.send(chat_id, "⏳ Переделываю…")
        try:
            cfg = gp.load_config()
            avoid = list(sess["avoid"]) + [sess["recipe"].title]
            note = gp.build_poll_note(cfg)
            note += (f" Внеси правки по пожеланию пользователя: {feedback}." if feedback
                     else " Предыдущий вариант не подошёл — предложи заметно другой рецепт.")
            recipe = gp.generate_recipe(sess["rubric"], avoid, cfg, note)
        except Exception as ex:  # noqa: BLE001
            log.exception("regenerate: %s", ex)
            self.send(chat_id, f"❌ Не удалось переделать: {ex}")
            return
        sess["recipe"] = recipe
        sess["awaiting_feedback"] = False
        self.show_preview(chat_id)

    def publish_recipe(self, chat_id, recipe) -> None:
        cfg = gp.load_config()
        channel = os.environ.get("TELEGRAM_CHANNEL_ID")
        if not channel:
            self.send(chat_id, "❌ Не задан TELEGRAM_CHANNEL_ID в .env")
            return
        try:
            image = gp.generate_image(recipe.image_prompt, cfg)
        except Exception as ex:  # noqa: BLE001
            log.exception("generate_image: %s", ex)
            self.send(chat_id, f"❌ Картинка не создалась: {ex}\nРецепт не опубликован.")
            return
        if not image and cfg.get("require_image", True):
            self.send(chat_id, "❌ Картинка не создалась. Рецепт не опубликован.")
            return
        if image and cfg.get("watermark", True):
            url = gp.channel_url(cfg)
            if url:
                try:
                    image = gp.add_watermark(image, "@" + url.rsplit("/", 1)[-1])
                except Exception as ex:  # noqa: BLE001
                    log.warning("watermark: %s", ex)
        try:
            caption, overflow = gp.build_post(recipe, cfg)
            gp.publish(image, caption, overflow, self.token, channel, cfg)
        except Exception as ex:  # noqa: BLE001
            log.exception("publish: %s", ex)
            self.send(chat_id, f"❌ Ошибка публикации: {ex}")
            return
        posted = gp.load_posted()
        posted.append(recipe.title)
        gp.save_posted(posted)
        self.send(chat_id, "✅ Рецепт опубликован в канал с картинкой!", reply_markup=MENU_KB)

    def handle(self, msg: dict) -> None:
        if (msg.get("chat") or {}).get("type") != "private":
            return  # команды принимаем только в личке с ботом
        text = (msg.get("text") or "").strip()
        chat_id = msg["chat"]["id"]
        uid = (msg.get("from") or {}).get("id")

        # шаг правок: обычный текст после «Не нравится» → это пожелания к рецепту
        sess = self.sessions.get(chat_id)
        if (sess and sess.get("awaiting_feedback") and text
                and not text.startswith("/") and text not in BUTTON_CMD):
            if uid in self.admins:
                self.regenerate(chat_id, feedback=text)
            return

        # текст кнопки reply-клавиатуры → команда; иначе разбираем /команду
        if text in BUTTON_CMD:
            cmd, args = BUTTON_CMD[text], []
        elif text.startswith("/"):
            parts = text.split()
            cmd = parts[0].lstrip("/").split("@")[0].lower()
            args = parts[1:]
        else:
            return

        if cmd in ("start", "help"):
            access = ("✅ доступ есть" if uid in self.admins
                      else "⛔ доступа нет — впишите этот ID в TELEGRAM_ADMIN_IDS в .env и перезапустите бота")
            self.send(chat_id, f"{HELP}\n\nВаш ID: {uid}\n{access}", reply_markup=MENU_KB)
            return

        if uid not in self.admins:
            self.send(chat_id, f"⛔ Нет доступа. Ваш ID: {uid}. Добавьте его в TELEGRAM_ADMIN_IDS (.env).")
            log.info("Отказ в доступе: uid=%s cmd=/%s", uid, cmd)
            return

        log.info("Команда /%s от uid=%s args=%s", cmd, uid, args)
        if cmd == "new":
            self.start_new(chat_id, " ".join(args))
        elif cmd == "post":
            self.send(chat_id, "⏳ Генерирую и публикую рецепт…")
            extra = ["--rubric", " ".join(args)] if args else []
            ok, out = self.run_script(extra)
            self.send(chat_id, "✅ Опубликовано в канал." if ok else f"❌ Ошибка:\n{out[-1200:]}")
        elif cmd == "poll":
            self.send(chat_id, "⏳ Публикую опрос…")
            ok, out = self.run_script(["--poll"])
            self.send(chat_id, "✅ Опрос опубликован." if ok else f"❌ Ошибка:\n{out[-1200:]}")
        elif cmd == "pollresults":
            if not POLL_PREF.exists():
                self.send(chat_id, "Опрос ещё не запускался или голосов нет.")
            else:
                pref = json.loads(POLL_PREF.read_text(encoding="utf-8"))
                counts = pref.get("counts") or {}
                lines = "\n".join(f"• {k}: {v}" for k, v in sorted(counts.items(), key=lambda x: -x[1]))
                self.send(chat_id, f"📊 Голоса опроса:\n{lines or '—'}\n\nЛидер: {pref.get('winner')}"
                                   f"\nОбновлено: {pref.get('updated_at')}")
        elif cmd == "preview":
            self.send(chat_id, "⏳ Готовлю превью (без публикации)…")
            ok, out = self.run_script(["--dry-run", "--skip-image"])
            if ok and PREVIEW_TXT.exists():
                self.send(chat_id, PREVIEW_TXT.read_text(encoding="utf-8"), html=True)
            else:
                self.send(chat_id, f"❌ Не удалось:\n{out[-1200:]}")
        else:
            self.send(chat_id, f"Неизвестная команда.\n\n{HELP}")

    def handle_callback(self, cq: dict) -> None:
        self.answer_callback(cq.get("id", ""))
        uid = (cq.get("from") or {}).get("id")
        data = cq.get("data") or ""
        message = cq.get("message") or {}
        chat_id = (message.get("chat") or {}).get("id")
        msg_id = message.get("message_id")
        if uid not in self.admins:
            return
        sess = self.sessions.get(chat_id)

        if data == "cancel":
            self.sessions.pop(chat_id, None)
            if msg_id:
                self.edit_text(chat_id, msg_id, "❌ Отменено.")
            self.send(chat_id, "Готов к работе — выберите команду.", reply_markup=MENU_KB)
        elif data == "like":
            if not sess:
                self.send(chat_id, "Черновик не найден. Начните заново: /new")
                return
            if msg_id:
                self.edit_text(chat_id, msg_id, "👍 Принято! Создаю картинку и публикую…")
            recipe = sess["recipe"]
            self.sessions.pop(chat_id, None)
            self.publish_recipe(chat_id, recipe)
        elif data == "dislike":
            if not sess:
                self.send(chat_id, "Черновик не найден. Начните заново: /new")
                return
            sess["awaiting_feedback"] = True
            self.send(chat_id, "✍️ Напишите, что изменить (одним сообщением), "
                               "или нажмите «Пропустить» для другого варианта.",
                      reply_markup=FEEDBACK_KB)
        elif data == "skip":
            if not sess:
                self.send(chat_id, "Черновик не найден. Начните заново: /new")
                return
            self.regenerate(chat_id, feedback="")

    def handle_poll(self, poll: dict) -> None:
        """Голоса опроса → state/poll_pref.json (читает generate_and_post при генерации)."""
        try:
            active = json.loads(ACTIVE_POLL.read_text(encoding="utf-8"))
        except Exception:
            return
        if active.get("poll_id") != poll.get("id"):
            return  # голоса не от нашего активного опроса
        counts = {o["text"].strip().lower(): int(o.get("voter_count", 0)) for o in poll.get("options", [])}
        winner = max(counts, key=counts.get) if any(counts.values()) else None
        pref = {
            "counts": counts,
            "winner": winner,
            "total": int(poll.get("total_voter_count", 0)),
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        POLL_PREF.parent.mkdir(parents=True, exist_ok=True)
        POLL_PREF.write_text(json.dumps(pref, ensure_ascii=False, indent=2), encoding="utf-8")
        log.info("Опрос: %s (лидер: %s)", counts, winner)

    def poll_loop(self) -> None:
        self.set_menu()
        offset = None
        log.info("Бот запущен, слушаю команды. Админы: %s", self.admins or "(не заданы!)")
        while True:
            try:
                resp = requests.get(
                    f"{self.api}/getUpdates",
                    params={"offset": offset, "timeout": 30}, timeout=40,
                )
                data = resp.json()
                if not data.get("ok"):
                    # 409 Conflict = где-то уже запущен второй экземпляр бота
                    log.warning("getUpdates не ok: %s", data.get("description"))
                    time.sleep(5)
                    continue
                for upd in data.get("result", []):
                    offset = upd["update_id"] + 1
                    if "callback_query" in upd:  # нажата inline-кнопка
                        try:
                            self.handle_callback(upd["callback_query"])
                        except Exception as ex:  # noqa: BLE001
                            log.exception("Ошибка callback: %s", ex)
                        continue
                    if "poll" in upd:  # обновление голосов опроса
                        try:
                            self.handle_poll(upd["poll"])
                        except Exception as ex:  # noqa: BLE001
                            log.exception("Ошибка обработки опроса: %s", ex)
                        continue
                    msg = upd.get("message")
                    if msg:
                        try:
                            self.handle(msg)
                        except Exception as ex:  # noqa: BLE001
                            log.exception("Ошибка обработки сообщения: %s", ex)
            except requests.exceptions.RequestException as ex:
                log.warning("Сеть недоступна: %s — повтор через 5с", ex)
                time.sleep(5)
            except Exception as ex:  # noqa: BLE001
                log.exception("Ошибка цикла: %s — повтор через 5с", ex)
                time.sleep(5)


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    load_dotenv(BASE_DIR / ".env")
    setup_logging()
    gp.log.handlers = log.handlers          # логи генерации/публикации → bot.log
    gp.log.setLevel(logging.INFO)

    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        log.error("Не задан TELEGRAM_BOT_TOKEN в .env")
        return 1
    admins = parse_admins()
    if not admins:
        log.warning("TELEGRAM_ADMIN_IDS пуст — команды выполняться не будут. "
                    "Напишите боту /start, чтобы узнать свой ID, и впишите его в .env.")
    Bot(token, admins).poll_loop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
