"""Жека — телеграм-бот, который ведёт себя как живой участник группового чата.

Прод-режим (Render free): Flask-webhook (как factbot). Telegram шлёт POST на
/webhook/<secret>, gunicorn держит порт. Локально для дева есть poll.py.

Экономия токенов: дешёвый гейт (обычный код) решает, отвечать ли вообще,
и только если да — зовём Claude Haiku с маленьким окном последних сообщений.
"""
import os
import re
import time
import random
import logging
from pathlib import Path
from collections import deque

import requests
from flask import Flask, request, abort
from anthropic import Anthropic

try:
    from dotenv import load_dotenv
    # сперва общий E:/project/.env (там может лежать ANTHROPIC_API_KEY),
    # потом локальный chatbot/.env — он перетирает общий.
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    load_dotenv(Path(__file__).resolve().parent / ".env", override=True)
except ImportError:
    pass

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("zheka")

BOT_TOKEN = os.environ["BOT_TOKEN"]
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")
ALLOWED_CHAT_ID = os.environ.get("ALLOWED_CHAT_ID", "").strip()

BOT_NAME = os.environ.get("BOT_NAME", "Жека")
NAME_TRIGGERS = [s.strip().lower() for s in os.environ.get("NAME_TRIGGERS", "жека,жек,жэка,zheka").split(",") if s.strip()]

CHATTINESS = float(os.environ.get("CHATTINESS", "0.20"))
QUESTION_BOOST = float(os.environ.get("QUESTION_BOOST", "0.15"))
REPLY_COOLDOWN_SEC = float(os.environ.get("REPLY_COOLDOWN_SEC", "45"))
MAX_REPLIES_PER_HOUR = int(os.environ.get("MAX_REPLIES_PER_HOUR", "30"))
MAX_REPLIES_PER_DAY = int(os.environ.get("MAX_REPLIES_PER_DAY", "250"))

MODEL = os.environ.get("MODEL", "claude-haiku-4-5")
CONTEXT_WINDOW = int(os.environ.get("CONTEXT_WINDOW", "14"))
MAX_TOKENS = int(os.environ.get("MAX_TOKENS", "120"))
TYPING_MAX_SEC = float(os.environ.get("TYPING_MAX_SEC", "3.0"))

# эмодзи, которые Telegram принимает как реакции (setMessageReaction).
# модель может выбирать только из этого набора; что вне набора — игнорим.
ALLOWED_REACTIONS = {
    "👍","👎","🔥","😁","🤔","🤯","😱","🤬","🎉","🤩","💩","🤡","🥱","😈","🙈",
    "🗿","🤓","👀","🤣","💯","⚡","🥴","😍","🤝","🫡","💅","🤪","🆒","😎","👾","😡","❤️","🙏","👌","🤨",
}

TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

app = Flask(__name__)
anthropic = Anthropic(api_key=ANTHROPIC_API_KEY) if ANTHROPIC_API_KEY else None

BOT_ID = None
BOT_USERNAME = None

log.info("startup: anthropic=%s, chat=%s, webhook_secret=%s",
         "ok" if anthropic else "MISSING",
         ALLOWED_CHAT_ID or "UNSET (will only log chat ids)",
         "set" if WEBHOOK_SECRET else "MISSING")


PERSONA = (
    f"Ты — {BOT_NAME}, свой в доску пацан из этого чата. Ты живой человек, "
    "НЕ бот и НЕ ассистент.\n\n"
    "Вайб:\n"
    "— дерзкий, с приколом: можешь огрызнуться, поддеть, кинуть сарказм; лёгкая "
    "агрессия в тему — норм, но по-пацански, не всерьёз;\n"
    "— мемный: мемы, цитаты, «это база», «скуф», «кринж», «жиза», «ну ты понял», "
    "«реально чел»; говоришь как в чате, а не как из методички;\n"
    "— сленга много, пишешь строчными, на пунктуацию забиваешь;\n"
    "— иногда душнила: можешь занудно поправить («ну вообще-то…», «технически…»), "
    "но коротко, без лекций;\n"
    "— ОООЧЕНЬ редко (примерно 1 ответ из 20) тебя накрывает «философом под кайфом»: "
    "вдруг выдаёшь странную псевдоглубокую мысль про жизнь/вселенную, а потом как ни "
    "в чём не бывало. Не злоупотребляй, это должно быть неожиданно.\n\n"
    "Как пишешь:\n"
    "— коротко: одна-две фразы, часто пару слов;\n"
    "— материться можно по-пацански (бля, нахрен, хуйня, заебись, охренеть) — тут все "
    "свои; НО без перехода на личности по нации, расе, религии, ориентации, внешности "
    "или болезни, без угроз и реальной жести;\n"
    "— без markdown, без списков, без простыней.\n\n"
    "Реакции на сообщения: можешь вместо текста или вместе с ним влепить эмодзи-реакцию. "
    "Чтобы поставить реакцию — добавь В НАЧАЛЕ ответа тег [react:ЭМОДЗИ]. Только из этого "
    "набора: 👍 👎 🔥 😁 🤔 🤯 😱 🤬 🎉 🤩 💩 🤡 🥱 😈 🙈 🗿 🤓 👀 🤣 💯 ⚡ 🥴 😍 🤝 🫡 💅 🤪 🆒 😎 👾 😡 ❤️ 🙏 👌 🤨\n"
    "— только реакция: ответь просто «[react:🔥]» и больше ничего;\n"
    "— реакция + коммент: «[react:😁] да ну нахер лол»;\n"
    "— или просто текст без реакции. Реакции — это приправа, не на каждое сообщение.\n\n"
    "Чего НИКОГДА не делаешь:\n"
    "— не признаёшься, что ты ИИ / бот / программа, не объясняешь, как устроен;\n"
    "— не пишешь «как ассистент», «чем могу помочь», «я не могу»;\n"
    "— не выдаёшь длинные полезные инструкции, ты не справочник;\n"
    "— не оскорбляешь по-настоящему по нации, расе, религии, ориентации, внешности, болезни.\n\n"
    "Если сказать реально нечего и реакция не просится — ответь ровно SKIP "
    "(это увидит только система, в чат не попадёт)."
)


def tg(method, **payload):
    try:
        r = requests.post(f"{TG_API}/{method}", json=payload, timeout=15)
        if not r.ok:
            log.warning("TG %s failed [%s]: %s", method, r.status_code, r.text)
        return r.json() if r.ok else None
    except requests.RequestException as e:
        log.warning("TG %s exception: %s", method, e)
        return None


def send(chat_id, text, reply_to=None):
    payload = {"chat_id": chat_id, "text": text}
    if reply_to:
        payload["reply_to_message_id"] = reply_to
    return tg("sendMessage", **payload)


def send_typing(chat_id):
    tg("sendChatAction", chat_id=chat_id, action="typing")


def set_reaction(chat_id, message_id, emoji):
    if not message_id:
        return
    tg("setMessageReaction", chat_id=chat_id, message_id=message_id,
       reaction=[{"type": "emoji", "emoji": emoji}])


def ensure_identity():
    """Один раз узнаём свой id/username (нужно для детекта обращений)."""
    global BOT_ID, BOT_USERNAME
    if BOT_ID is not None:
        return
    me = tg("getMe")
    if me and me.get("ok"):
        BOT_ID = me["result"]["id"]
        BOT_USERNAME = me["result"].get("username")
        log.info("identity: @%s id=%s", BOT_USERNAME, BOT_ID)


class ChatState:
    def __init__(self):
        self.messages = deque(maxlen=40)   # {"name", "text"}
        self.last_reply = 0.0
        self.reply_times = deque()          # таймстемпы отправленных реплик


STATES = {}
_logged_chats = set()


def is_addressed(msg, text):
    """Обращаются ли к боту: реплай на него, @username или имя в тексте."""
    rt = msg.get("reply_to_message") or {}
    if BOT_ID is not None and (rt.get("from") or {}).get("id") == BOT_ID:
        return True
    low = text.lower()
    if BOT_USERNAME and ("@" + BOT_USERNAME.lower()) in low:
        return True
    for nm in NAME_TRIGGERS:
        if re.search(r"(?<![0-9a-zA-Zа-яёА-ЯЁ])" + re.escape(nm) + r"(?![0-9a-zA-Zа-яёА-ЯЁ])", low):
            return True
    return False


def decide(state, addressed, text):
    """Решаем, отвечать ли. Возвращает (отвечать, позвали_лично)."""
    now = time.time()
    while state.reply_times and now - state.reply_times[0] > 86400:
        state.reply_times.popleft()
    replies_day = len(state.reply_times)
    replies_hour = sum(1 for t in state.reply_times if now - t < 3600)

    if replies_day >= MAX_REPLIES_PER_DAY:
        return False, False

    if addressed:
        return True, True  # прямое обращение — игнорим кулдаун и часовой лимит

    if now - state.last_reply < REPLY_COOLDOWN_SEC:
        return False, False
    if replies_hour >= MAX_REPLIES_PER_HOUR:
        return False, False

    p = CHATTINESS + (QUESTION_BOOST if "?" in text else 0.0)
    return (random.random() < p), False


def build_reply(state, forced):
    """Зовём Haiku с маленьким окном контекста. None — если сказать нечего."""
    if anthropic is None:
        log.warning("no ANTHROPIC_API_KEY — cannot generate reply")
        return None

    recent = list(state.messages)[-CONTEXT_WINDOW:]
    transcript = "\n".join(f"{m['name']}: {m['text']}" for m in recent)

    instr = ("Сейчас твоя очередь. Ответь как " + BOT_NAME + " — коротко, по-человечески, в тему. "
             "Можно: только текст, или только реакция-эмодзи в начале как [react:🔥], или и то и другое.")
    if forced:
        instr += " Тебя позвали лично — обязательно среагируй (текстом или хотя бы реакцией), не пиши SKIP."
    else:
        instr += " Если совсем нечего сказать и реакция не просится — ответь ровно SKIP."

    user = f"Последние сообщения в чате:\n\n{transcript}\n\n{instr}"
    try:
        resp = anthropic.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=PERSONA,
            messages=[{"role": "user", "content": user}],
        )
        return resp.content[0].text.strip()
    except Exception:
        log.exception("anthropic call failed")
        return None


def handle_update(update):
    msg = update.get("message")
    if not msg:
        return
    chat = msg.get("chat", {})
    chat_id = chat.get("id")

    if ALLOWED_CHAT_ID:
        if str(chat_id) != ALLOWED_CHAT_ID:
            return
    else:
        if chat_id not in _logged_chats:
            _logged_chats.add(chat_id)
            log.info("UNCONFIGURED chat detected: ALLOWED_CHAT_ID=%s  (type=%s)", chat_id, chat.get("type"))
        return

    user = msg.get("from") or {}
    if user.get("is_bot"):
        return
    text = (msg.get("text") or "").strip()
    if not text:
        return
    name = user.get("first_name") or user.get("username") or "Кто-то"

    ensure_identity()

    state = STATES.setdefault(chat_id, ChatState())
    state.messages.append({"name": name, "text": text[:300]})

    addressed = is_addressed(msg, text)
    ok, forced = decide(state, addressed, text)
    if not ok:
        return

    raw = build_reply(state, forced)
    if not raw:
        return

    # необязательная реакция: тег [react:ЭМОДЗИ] где-то в ответе
    react = None
    m = re.search(r"\[react:\s*([^\]\s]+)\s*\]", raw)
    text = raw
    if m:
        react = m.group(1)
        text = (raw[:m.start()] + raw[m.end():]).strip()
    if text.upper().startswith("SKIP"):
        text = ""
    if react not in ALLOWED_REACTIONS:
        react = None
    if not react and not text:
        return  # сказать нечего и реакция не подошла

    msg_id = msg.get("message_id")
    if react:
        set_reaction(chat_id, msg_id, react)
    if text:
        send_typing(chat_id)
        time.sleep(min(len(text) / 18.0 + 0.5, TYPING_MAX_SEC))
        send(chat_id, text, reply_to=msg_id if forced else None)

    now = time.time()
    state.messages.append({"name": BOT_NAME, "text": text or f"(реакция {react})"})
    state.last_reply = now
    state.reply_times.append(now)
    log.info("engaged (forced=%s) chat=%s react=%s text=%r", forced, chat_id, react, text[:60])


@app.post("/webhook/<secret>")
def webhook(secret):
    if not WEBHOOK_SECRET or secret != WEBHOOK_SECRET:
        abort(403)
    update = request.get_json(force=True, silent=True) or {}
    try:
        handle_update(update)
    except Exception:
        log.exception("handle_update failed")
    return "ok"


@app.get("/health")
def health():
    return "ok"


@app.get("/")
def index():
    return "zheka is alive"


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
