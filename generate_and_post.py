#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Автогенерация и публикация рецептов в Telegram-канал на Claude.

Один запуск = один пост:
  1. выбирает рубрику дня из config.yaml (рубрикатор);
  2. генерирует рецепт через Claude (structured output, Pydantic-схема);
  3. рисует фото блюда через выбранный image-провайдер;
  4. форматирует пост под лимиты Telegram и публикует через Bot API;
  5. запоминает блюдо в state/posted.json (антиповтор).

Запуск:
  python generate_and_post.py            # боевой запуск (генерация + публикация)
  python generate_and_post.py --dry-run  # превью в консоль, без публикации
  python generate_and_post.py --skip-image          # без картинки
  python generate_and_post.py --rubric "супы"       # форсировать рубрику
"""
from __future__ import annotations

try:
    # Использует системное хранилище сертификатов Windows вместо отдельного
    # списка библиотеки requests. Нужно на машинах, где антивирус/VPN
    # подменяет HTTPS-сертификаты («self-signed certificate in certificate
    # chain») — Windows и браузер этому сертификату доверяют, requests — нет.
    import truststore
    truststore.inject_into_ssl()
except ImportError:
    pass

import argparse
import base64
import datetime as dt
import html
import json
import logging
import os
import random
import re
import sys
import time
from pathlib import Path
from urllib.parse import quote

import requests
import yaml
from dotenv import load_dotenv
from pydantic import BaseModel

# ── пути ──────────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / "config.yaml"
PROMPT_FILE = BASE_DIR / "prompts" / "system_recipe.md"
STATE_FILE = BASE_DIR / "state" / "posted.json"
ACTIVE_POLL = BASE_DIR / "state" / "active_poll.json"   # текущий опрос (id + опции)
POLL_PREF = BASE_DIR / "state" / "poll_pref.json"       # голоса опроса (пишет бот)
LOG_FILE = BASE_DIR / "logs" / "run.log"
OUT_DIR = BASE_DIR / "out"

TELEGRAM_CAPTION_LIMIT = 1024
TELEGRAM_MESSAGE_LIMIT = 4096

log = logging.getLogger("recipes")


# ── схема ответа модели ───────────────────────────────────────────────────────
class Nutrition(BaseModel):
    calories: float   # ккал на 100 г
    protein: float    # белки, г на 100 г
    fat: float        # жиры, г на 100 г
    carbs: float      # углеводы, г на 100 г


class Recipe(BaseModel):
    title: str
    caption: str
    ingredients: list[str]
    steps: list[str]
    servings: int                     # число порций, на которое рассчитан рецепт
    nutrition: Nutrition              # КБЖУ на 100 г
    nutrition_per_serving: Nutrition  # КБЖУ на одну порцию
    image_prompt: str
    hashtags: list[str]


# ── вспомогательное ───────────────────────────────────────────────────────────
def setup_logging() -> None:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s  %(levelname)-7s  %(message)s")
    log.setLevel(logging.INFO)

    fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
    fh.setFormatter(fmt)
    log.addHandler(fh)

    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    log.addHandler(sh)


def load_config() -> dict:
    return yaml.safe_load(CONFIG_FILE.read_text(encoding="utf-8")) or {}


def load_posted() -> list[str]:
    if not STATE_FILE.exists():
        return []
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        log.warning("state/posted.json повреждён — начинаю с пустого списка")
        return []


def save_posted(titles: list[str]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(titles, ensure_ascii=False, indent=2), encoding="utf-8")


def pick_rubric(cfg: dict) -> str:
    """Рубрика по текущему времени суток (утро — завтраки, вечер — ужины/десерты)."""
    hour = dt.datetime.now().hour
    for slot in cfg.get("rubric_by_hour") or []:
        if int(slot["from"]) <= hour < int(slot["to"]):
            return random.choice(slot["rubrics"])
    return random.choice(cfg.get("default_rubrics") or ["домашние блюда"])


def current_meal(hour: int) -> str:
    """Приём пищи для текущего часа (соответствует слотам 09/14/19)."""
    if hour < 11:
        return "завтрак"
    if hour < 16:
        return "обед"
    return "ужин"


def build_poll_note(cfg: dict) -> str:
    """Контекст по результатам опроса: агрегат (все посты) + акцент на слоте-победителе."""
    if not POLL_PREF.exists():
        return ""
    try:
        pref = json.loads(POLL_PREF.read_text(encoding="utf-8"))
    except Exception:
        return ""
    counts = {str(k): int(v) for k, v in (pref.get("counts") or {}).items()}
    if not any(counts.values()):
        return ""
    dist = ", ".join(f"{k} — {v}" for k, v in sorted(counts.items(), key=lambda x: -x[1]))
    note = f"Учитывай опрос аудитории (голоса по приёмам пищи): {dist}."
    winner = (pref.get("winner") or "").lower()
    if winner and winner == current_meal(dt.datetime.now().hour):
        note += (f" Сейчас слот «{winner}» — самый востребованный по опросу: "
                 "сделай рецепт особенно аппетитным и «к столу».")
    return note


def require_env(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        raise RuntimeError(f"Не задана переменная окружения {name} (см. .env)")
    return val


# ── генерация рецепта (Yandex AI Studio / DeepSeek) ───────────────────────────
def _extract_json(text: str) -> dict:
    """Достать JSON-объект из ответа модели (убирает ```-ограждения и текст вокруг)."""
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\s*", "", t)
        t = re.sub(r"\s*```$", "", t).strip()
    start, end = t.find("{"), t.rfind("}")
    if start != -1 and end != -1:
        t = t[start:end + 1]
    return json.loads(t)


def generate_recipe(rubric: str, avoid: list[str], cfg: dict, poll_note: str = "") -> Recipe:
    api = cfg.get("text_api") or {}
    base_url = api.get("base_url", "https://api.deepseek.com/chat/completions")
    auth = (api.get("auth") or "bearer").lower()
    key = require_env(api.get("api_key_env", "DEEPSEEK_API_KEY"))
    folder = os.environ.get("YANDEX_FOLDER_ID", "")
    # {folder_id} нужен только для Yandex-варианта (gpt://{folder_id}/…)
    model = str(api.get("model", "deepseek-chat")).format(folder_id=folder)

    headers = {"Content-Type": "application/json"}
    if auth == "api-key":  # Yandex AI Studio
        headers["Authorization"] = f"Api-Key {key}"
        if folder:
            headers["x-folder-id"] = folder
    else:                  # DeepSeek и прочие OpenAI-совместимые
        headers["Authorization"] = f"Bearer {key}"

    system = PROMPT_FILE.read_text(encoding="utf-8")
    avoid_str = ", ".join(avoid[-150:]) if avoid else "—"
    user = (
        f"Рубрика на сегодня: {rubric}.\n"
        f"НЕ повторяй эти блюда: {avoid_str}.\n"
        + (f"{poll_note}\n" if poll_note else "")
        + "Сгенерируй один оригинальный, реалистичный рецепт по рубрике.\n"
        "Верни ТОЛЬКО JSON-объект, без markdown и текста вокруг."
    )
    body = {
        "model": model,
        "temperature": float(cfg.get("temperature", 0.7)),
        "max_tokens": int(cfg.get("max_tokens", 2000)),
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }

    last_err: Exception | None = None
    for attempt in range(4):
        try:
            r = requests.post(base_url, headers=headers, json=body, timeout=120)
            r.raise_for_status()
            content = (r.json()["choices"][0]["message"].get("content") or "").strip()
            if not content:
                raise RuntimeError("пустой ответ модели (модель не успела ответить — мало max_tokens?)")
            return Recipe(**_extract_json(content))
        except Exception as ex:  # noqa: BLE001 — повторяем сетевые/парс/валидационные ошибки
            last_err = ex
            wait = 2 ** attempt
            log.warning("Ошибка генерации текста (попытка %d/4): %s — повтор через %ss",
                        attempt + 1, ex, wait)
            time.sleep(wait)
    raise RuntimeError(f"Не удалось сгенерировать рецепт: {last_err}")


# ── генерация изображения ─────────────────────────────────────────────────────
def _img_kandinsky(prompt: str, cfg: dict) -> bytes:
    key = require_env("FUSIONBRAIN_API_KEY")
    secret = require_env("FUSIONBRAIN_SECRET_KEY")
    base = "https://api-key.fusionbrain.ai"
    headers = {"X-Key": f"Key {key}", "X-Secret": f"Secret {secret}"}
    size = int(cfg.get("image_size", 1024))

    r = requests.get(f"{base}/key/api/v1/pipelines", headers=headers, timeout=30)
    r.raise_for_status()
    pipeline_id = r.json()[0]["id"]

    params = {
        "type": "GENERATE",
        "numImages": 1,
        "width": size,
        "height": size,
        "generateParams": {"query": prompt},
    }
    files = {
        "pipeline_id": (None, pipeline_id),
        "params": (None, json.dumps(params), "application/json"),
    }
    r = requests.post(f"{base}/key/api/v1/pipeline/run", headers=headers, files=files, timeout=60)
    r.raise_for_status()
    uuid = r.json()["uuid"]

    for _ in range(60):
        time.sleep(5)
        s = requests.get(f"{base}/key/api/v1/pipeline/status/{uuid}", headers=headers, timeout=30).json()
        status = s.get("status")
        if status == "DONE":
            if s.get("result", {}).get("censored"):
                raise RuntimeError("Kandinsky: изображение зацензурено")
            return base64.b64decode(s["result"]["files"][0])
        if status == "FAIL":
            raise RuntimeError(f"Kandinsky FAIL: {s.get('errorDescription')}")
    raise TimeoutError("Kandinsky: превышено время ожидания")


def _img_yandexart(prompt: str, cfg: dict) -> bytes:
    key = require_env("YANDEX_API_KEY")
    folder = require_env("YANDEX_FOLDER_ID")
    headers = {"Authorization": f"Api-Key {key}", "x-folder-id": folder}
    body = {
        "modelUri": f"art://{folder}/yandex-art/latest",
        "generationOptions": {"aspectRatio": {"widthRatio": "1", "heightRatio": "1"}},
        "messages": [{"weight": "1", "text": prompt}],
    }
    r = requests.post(
        "https://llm.api.cloud.yandex.net/foundationModels/v1/imageGenerationAsync",
        headers=headers, json=body, timeout=60,
    )
    r.raise_for_status()
    op_id = r.json()["id"]

    for _ in range(60):
        time.sleep(5)
        s = requests.get(
            f"https://llm.api.cloud.yandex.net/operations/{op_id}", headers=headers, timeout=30
        ).json()
        if s.get("done"):
            if "response" in s:
                return base64.b64decode(s["response"]["image"])
            raise RuntimeError(f"YandexART ошибка: {s.get('error')}")
    raise TimeoutError("YandexART: превышено время ожидания")


def _img_openai(prompt: str, cfg: dict) -> bytes:
    require_env("OPENAI_API_KEY")
    from openai import OpenAI  # отдельная зависимость: pip install openai

    client = OpenAI()
    size = int(cfg.get("image_size", 1024))
    res = client.images.generate(model="gpt-image-1", prompt=prompt, size=f"{size}x{size}", n=1)
    return base64.b64decode(res.data[0].b64_json)


IMAGE_PROVIDERS = {
    "kandinsky": _img_kandinsky,
    "yandexart": _img_yandexart,
    "openai": _img_openai,
}


def generate_image(prompt: str, cfg: dict) -> bytes | None:
    provider = (cfg.get("image_provider") or "none").lower()
    if provider == "none":
        return None
    fn = IMAGE_PROVIDERS.get(provider)
    if fn is None:
        raise ValueError(f"Неизвестный image_provider: {provider}")
    return fn(prompt, cfg)


# ── рост: URL канала, вотермарка ──────────────────────────────────────────────
def channel_url(cfg: dict) -> str | None:
    """URL канала для кнопок/вотермарки: из config или из @username в TELEGRAM_CHANNEL_ID."""
    uname = (cfg.get("channel_username") or "").lstrip("@").strip()
    if not uname:
        cid = os.environ.get("TELEGRAM_CHANNEL_ID", "")
        if cid.startswith("@"):
            uname = cid[1:]
    return f"https://t.me/{uname}" if uname else None


def add_watermark(image_bytes: bytes, text: str) -> bytes:
    """Наносит @ник канала в правый нижний угол фото (Pillow), с тенью для читаемости."""
    from io import BytesIO
    from PIL import Image, ImageDraw, ImageFont

    img = Image.open(BytesIO(image_bytes)).convert("RGB")
    draw = ImageDraw.Draw(img)
    size = max(18, img.width // 28)
    font = None
    for path in ("C:/Windows/Fonts/arialbd.ttf", "C:/Windows/Fonts/arial.ttf"):
        try:
            font = ImageFont.truetype(path, size)
            break
        except OSError:
            continue
    if font is None:
        font = ImageFont.load_default()

    margin = max(10, img.width // 60)
    bbox = draw.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    x, y = img.width - tw - margin, img.height - th - margin
    draw.text((x + 2, y + 2), text, font=font, fill=(0, 0, 0))      # тень
    draw.text((x, y), text, font=font, fill=(255, 255, 255))        # текст

    out = BytesIO()
    img.save(out, format="JPEG", quality=90)
    return out.getvalue()


# ── форматирование поста под лимиты Telegram ──────────────────────────────────
def _clean_tag(tag: str) -> str:
    t = tag.strip().lstrip("#")
    t = t.replace(" ", "").replace("-", "")
    return f"#{html.escape(t)}" if t else ""


def build_post(r: Recipe, cfg: dict) -> tuple[str, str | None]:
    """Один пост = одно сообщение: фото + весь рецепт (ингредиенты и шаги) в подписи.

    Telegram ограничивает подпись к фото 1024 символами, поэтому модель в
    prompts/system_recipe.md просят уложить весь пост в этот лимит. Здесь — только
    подстраховка: если вдруг длиннее, аккуратно обрезаем по границе строки, чтобы
    остаться ОДНИМ сообщением (а не разбивать на два).
    """
    e = html.escape
    title = f"<b>{e(r.title)}</b>"
    lead = e(r.caption)
    ingredients = "🛒 <b>Ингредиенты:</b>\n" + "\n".join(f"• {e(i)}" for i in r.ingredients)
    steps = "👩‍🍳 <b>Приготовление:</b>\n" + "\n".join(
        f"{n}. {e(s)}" for n, s in enumerate(r.steps, 1)
    )
    n, ps = r.nutrition, r.nutrition_per_serving
    nutrition = (
        f"📊 <b>КБЖУ на 100 г:</b> {round(n.calories)} ккал · "
        f"Б {round(n.protein)} · Ж {round(n.fat)} · У {round(n.carbs)}\n"
        f"🍽 <b>На порцию</b> (всего {r.servings}): {round(ps.calories)} ккал · "
        f"Б {round(ps.protein)} · Ж {round(ps.fat)} · У {round(ps.carbs)}"
    )
    cta = html.escape((cfg.get("cta_text") or "").strip())
    tags = " ".join(t for t in (_clean_tag(x) for x in r.hashtags) if t)

    blocks = [title, lead, ingredients, steps, nutrition, cta, tags]
    full = "\n\n".join(b for b in blocks if b).strip()
    if len(full) > TELEGRAM_CAPTION_LIMIT:
        log.warning(
            "Подпись %d симв. > %d — обрезаю по границе строки (упростите рецепт в промпте).",
            len(full), TELEGRAM_CAPTION_LIMIT,
        )
        # rsplit по \n сохраняет целые строки → не рвём HTML-теги (они закрыты внутри строки)
        full = full[:TELEGRAM_CAPTION_LIMIT - 1].rsplit("\n", 1)[0].rstrip() + "…"
    return full, None


# ── публикация в Telegram ─────────────────────────────────────────────────────
def _check_tg(resp: requests.Response) -> None:
    if not resp.ok or not resp.json().get("ok", False):
        raise RuntimeError(f"Telegram API ошибка {resp.status_code}: {resp.text}")


def build_keyboard(cfg: dict, message_id: int | None = None) -> str | None:
    """JSON inline-клавиатуры «Подписаться»/«Поделиться».

    Если передан message_id — «Поделиться» ведёт на КОНКРЕТНЫЙ пост
    (t.me/<канал>/<id>); иначе — на канал (временно, до получения id поста).
    """
    if not cfg.get("buttons", True):
        return None
    url = channel_url(cfg)
    if not url:
        return None
    share_text = quote((cfg.get("share_text") or "Вкусные рецепты каждый день").strip())
    post_url = f"{url}/{message_id}" if message_id else url
    keyboard = {"inline_keyboard": [[
        {"text": "📤 Поделиться",
         "url": f"https://t.me/share/url?url={quote(post_url, safe='')}&text={share_text}"},
    ]]}
    return json.dumps(keyboard, ensure_ascii=False)


def publish(image_bytes: bytes | None, caption: str, overflow: str | None,
            token: str, chat_id: str, cfg: dict) -> None:
    api = f"https://api.telegram.org/bot{token}"
    initial_kb = build_keyboard(cfg)        # пока «Поделиться» ведёт на канал
    target_msg_id: int | None = None

    if image_bytes:
        data = {"chat_id": chat_id, "caption": caption, "parse_mode": "HTML"}
        if initial_kb:
            data["reply_markup"] = initial_kb
        resp = requests.post(
            f"{api}/sendPhoto", data=data,
            files={"photo": ("dish.jpg", image_bytes, "image/jpeg")}, timeout=60,
        )
        _check_tg(resp)
        target_msg_id = resp.json()["result"]["message_id"]
        if overflow:
            resp2 = requests.post(
                f"{api}/sendMessage",
                data={"chat_id": chat_id, "text": overflow, "parse_mode": "HTML"},
                timeout=60,
            )
            _check_tg(resp2)
    else:
        text = caption if not overflow else f"{caption}\n\n{overflow}"
        chunks = [text[i:i + TELEGRAM_MESSAGE_LIMIT] for i in range(0, len(text), TELEGRAM_MESSAGE_LIMIT)]
        for idx, chunk in enumerate(chunks):
            last = idx == len(chunks) - 1
            data = {"chat_id": chat_id, "text": chunk, "parse_mode": "HTML"}
            if initial_kb and last:
                data["reply_markup"] = initial_kb
            resp = requests.post(f"{api}/sendMessage", data=data, timeout=60)
            _check_tg(resp)
            if last:
                target_msg_id = resp.json()["result"]["message_id"]

    # перевести кнопку «Поделиться» на КОНКРЕТНЫЙ пост (а не на канал)
    if target_msg_id and cfg.get("buttons", True):
        post_kb = build_keyboard(cfg, message_id=target_msg_id)
        if post_kb:
            try:
                r = requests.post(
                    f"{api}/editMessageReplyMarkup",
                    data={"chat_id": chat_id, "message_id": target_msg_id, "reply_markup": post_kb},
                    timeout=60,
                )
                _check_tg(r)
            except Exception as ex:  # noqa: BLE001 — пост уже опубликован, кнопка не критична
                log.warning("Не удалось привязать «Поделиться» к посту: %s", ex)


def send_poll(poll: dict, token: str, chat_id: str) -> None:
    """Публикует опрос (вовлечение) в канал и запоминает его для учёта голосов."""
    api = f"https://api.telegram.org/bot{token}"
    resp = requests.post(
        f"{api}/sendPoll",
        data={
            "chat_id": chat_id,
            "question": poll["question"],
            "options": json.dumps(poll["options"], ensure_ascii=False),
            "is_anonymous": "true",
        },
        timeout=60,
    )
    _check_tg(resp)
    # запоминаем опрос — бот по нему будет считать голоса в poll_pref.json
    result = resp.json().get("result", {})
    active = {
        "poll_id": (result.get("poll") or {}).get("id"),
        "message_id": result.get("message_id"),
        "options": poll["options"],
    }
    ACTIVE_POLL.parent.mkdir(parents=True, exist_ok=True)
    ACTIVE_POLL.write_text(json.dumps(active, ensure_ascii=False, indent=2), encoding="utf-8")


# ── точка входа ───────────────────────────────────────────────────────────────
def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Генерация и публикация рецепта в Telegram.")
    p.add_argument("--dry-run", action="store_true", help="превью в консоль, без публикации")
    p.add_argument("--skip-image", action="store_true", help="не генерировать картинку")
    p.add_argument("--rubric", help="форсировать рубрику вместо рубрикатора")
    p.add_argument("--poll", action="store_true", help="опубликовать опрос-вовлечение вместо рецепта")
    return p.parse_args()


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # корректный вывод кириллицы в консоль Windows
    except Exception:
        pass

    args = parse_args()
    load_dotenv(BASE_DIR / ".env")
    setup_logging()

    cfg = load_config()

    # Режим опроса — публикует опрос-вовлечение и выходит.
    if args.poll:
        polls = cfg.get("polls") or [{
            "question": "Что приготовить на этой неделе?",
            "options": ["Супы", "Выпечка", "Мясное", "Десерты", "Салаты"],
        }]
        poll = random.choice(polls)
        if args.dry_run:
            log.info("DRY-RUN опрос: «%s» → %s", poll["question"], poll["options"])
            return 0
        token = require_env("TELEGRAM_BOT_TOKEN")
        chat_id = require_env("TELEGRAM_CHANNEL_ID")
        send_poll(poll, token, chat_id)
        log.info("Опрос опубликован: «%s»", poll["question"])
        return 0

    rubric = args.rubric or pick_rubric(cfg)
    posted = load_posted()
    poll_note = build_poll_note(cfg)
    log.info("── Запуск. Рубрика: «%s». В истории: %d блюд.%s",
             rubric, len(posted), " Учитываю опрос." if poll_note else "")

    # 1. рецепт
    recipe = generate_recipe(rubric, posted, cfg, poll_note)
    log.info("Рецепт сгенерирован: «%s»", recipe.title)

    # 2. картинка
    image_bytes: bytes | None = None
    if not args.skip_image:
        try:
            image_bytes = generate_image(recipe.image_prompt, cfg)
            if image_bytes:
                log.info("Картинка получена (%d КБ)", len(image_bytes) // 1024)
        except Exception as ex:
            log.error("Не удалось сгенерировать картинку: %s", ex)
            if not args.dry_run and cfg.get("require_image", True):
                log.error("require_image=true → пост отменён.")
                return 1

    # 2.1 вотермарка @канала на фото
    if image_bytes and cfg.get("watermark", True):
        url = channel_url(cfg)
        if url:
            try:
                image_bytes = add_watermark(image_bytes, "@" + url.rsplit("/", 1)[-1])
                log.info("Вотермарка нанесена.")
            except Exception as ex:
                log.warning("Вотермарка не нанесена: %s", ex)

    # 3. формат поста
    caption, overflow = build_post(recipe, cfg)

    # 4. dry-run: показать и выйти
    if args.dry_run:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        # текст превью в файл — его читает бот для /preview
        (OUT_DIR / "preview.txt").write_text(
            caption + (f"\n\n{overflow}" if overflow else ""), encoding="utf-8")
        if image_bytes:
            preview = OUT_DIR / "preview.jpg"
            preview.write_bytes(image_bytes)
            log.info("Картинка сохранена в %s", preview)
        print("\n================= ПРЕВЬЮ ПОСТА (caption) =================\n")
        print(caption)
        if overflow:
            print("\n----------------- второе сообщение -----------------\n")
            print(overflow)
        print("\n--------------- image_prompt (для картинки) ---------------\n")
        print(recipe.image_prompt)
        print("\n=========================================================\n")
        log.info("DRY-RUN: публикация пропущена, история не изменена.")
        return 0

    # 5. публикация
    token = require_env("TELEGRAM_BOT_TOKEN")
    chat_id = require_env("TELEGRAM_CHANNEL_ID")
    publish(image_bytes, caption, overflow, token, chat_id, cfg)
    log.info("Опубликовано в %s.", chat_id)

    # 6. антиповтор
    posted.append(recipe.title)
    save_posted(posted)
    log.info("История обновлена (%d блюд).", len(posted))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001 — верхнеуровневый лог любой ошибки
        log.exception("Критическая ошибка: %s", exc)
        raise SystemExit(1)
