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

CHATTINESS = float(os.environ.get("CHATTINESS", "0.15"))
QUESTION_BOOST = float(os.environ.get("QUESTION_BOOST", "0.15"))
REPLY_COOLDOWN_SEC = float(os.environ.get("REPLY_COOLDOWN_SEC", "45"))
MAX_REPLIES_PER_HOUR = int(os.environ.get("MAX_REPLIES_PER_HOUR", "30"))
MAX_REPLIES_PER_DAY = int(os.environ.get("MAX_REPLIES_PER_DAY", "250"))
# режим активной беседы: после ответа Жека N секунд «в диалоге» и держит нить
CONVO_WINDOW_SEC = float(os.environ.get("CONVO_WINDOW_SEC", "75"))
CONVO_CHATTINESS = float(os.environ.get("CONVO_CHATTINESS", "0.3"))
CONVO_MIN_GAP = float(os.environ.get("CONVO_MIN_GAP", "25"))

MODEL = os.environ.get("MODEL", "claude-haiku-4-5")
CONTEXT_WINDOW = int(os.environ.get("CONTEXT_WINDOW", "14"))
MAX_TOKENS = int(os.environ.get("MAX_TOKENS", "120"))
# «режим мнения»: когда у Жеки прямо спрашивают, что он думает — берём больше
# контекста и даём ответить развёрнутее (анализирует обсуждение)
OPINION_CONTEXT = int(os.environ.get("OPINION_CONTEXT", "25"))
OPINION_MAX_TOKENS = int(os.environ.get("OPINION_MAX_TOKENS", "280"))
OPINION_TRIGGERS = [s.strip().lower() for s in os.environ.get(
    "OPINION_TRIGGERS",
    "что думаешь,чё думаешь,че думаешь,как думаешь,твоё мнение,твое мнение,как считаешь,"
    "что скажешь,а ты как,а ты что,а ты чё,как тебе такое,что думаете,а ты чё молчишь,"
    "а ты че молчишь,ты согласен,а по-твоему").split(",") if s.strip()]

# --- память чата (Supabase): любимые эмодзи и фразочки/мемы ---
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_SECRET_KEY = os.environ.get("SUPABASE_SECRET_KEY", "")
MEMORY_TABLE = os.environ.get("MEMORY_TABLE", "zheka_memory")
USE_SUPABASE = bool(SUPABASE_URL and SUPABASE_SECRET_KEY)
MEMORY_FLUSH_EVERY = int(os.environ.get("MEMORY_FLUSH_EVERY", "8"))
TOP_EMOJIS = int(os.environ.get("TOP_EMOJIS", "8"))
TOP_PHRASES = int(os.environ.get("TOP_PHRASES", "6"))
TYPING_MAX_SEC = float(os.environ.get("TYPING_MAX_SEC", "3.0"))

# эмодзи, которые Telegram принимает как реакции (setMessageReaction).
# модель может выбирать только из этого набора; что вне набора — игнорим.
ALLOWED_REACTIONS = {
    "👍","👎","🔥","😁","🤔","🤯","😱","🤬","🎉","🤩","💩","🤡","🥱","😈","🙈",
    "🗿","🤓","👀","🤣","💯","⚡","🥴","😍","🤝","🫡","💅","🤪","🆒","😎","👾","😡","❤️","🙏","👌","🤨",
}
# из реакций, что модель захотела поставить, реально ставим лишь эту долю (чтобы не на каждое)
REACT_CHANCE = float(os.environ.get("REACT_CHANCE", "0.4"))

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
    "ГЛАВНОЕ: реально читай беседу и отвечай на то, что написали. Если тебе задали "
    "вопрос — отвечай на него по сути (в своём стиле, можно с приколом), не уходи в "
    "свою тему и не отмахивайся односложно. Держи нить разговора: помни, о чём только "
    "что шла речь. Юмор идёт ПОВЕРХ ответа, а не вместо него.\n\n"
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
    "— или просто текст без реакции — и ТАК ЧАЩЕ ВСЕГО. Реакция — это РЕДКАЯ приправа, "
    "а не привычка: ставь её, только если сообщение реально смешное, меткое или зацепило. "
    "Большинству сообщений реакция НЕ нужна — не лепи её по привычке и не на каждое.\n\n"
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


# ---------- ПАМЯТЬ ЧАТА: эмодзи + фразочки (персист в Supabase) ----------
EMOJI_RE = re.compile(
    "[\U0001F1E6-\U0001F1FF\U0001F300-\U0001F5FF\U0001F600-\U0001F64F"
    "\U0001F680-\U0001F6FF\U0001F780-\U0001F7FF\U0001F800-\U0001F8FF"
    "\U0001F900-\U0001F9FF\U0001FA00-\U0001FAFF\U00002600-\U000026FF"
    "\U00002700-\U000027BF\U00002B00-\U00002BFF\U00002190-\U000021FF\U00002328\U00002B50\U00002728]",
    flags=re.UNICODE,
)
_EMOJI_MOD = set("️‍\U0001F3FB\U0001F3FC\U0001F3FD\U0001F3FE\U0001F3FF")

MEM = {}        # chat_id -> {"emojis": {e: n}, "phrases": {p: n}}
_MEM_LOADED = set()
_MEM_DIRTY = {}  # chat_id -> сколько инкрементов с последнего сейва


def _sb_headers():
    return {"apikey": SUPABASE_SECRET_KEY, "Authorization": "Bearer " + SUPABASE_SECRET_KEY,
            "Content-Type": "application/json"}


def _mem_load(chat_id):
    d = {"emojis": {}, "phrases": {}, "facts": []}
    if not USE_SUPABASE:
        return d
    try:
        r = requests.get(f"{SUPABASE_URL}/rest/v1/{MEMORY_TABLE}", headers=_sb_headers(),
                         params={"chat_id": f"eq.{chat_id}", "select": "data"}, timeout=10)
        if r.ok and r.json():
            got = r.json()[0].get("data") or {}
            d["emojis"] = got.get("emojis", {}) or {}
            d["phrases"] = got.get("phrases", {}) or {}
            d["facts"] = got.get("facts", []) or []
        elif not r.ok:
            log.warning("memory load %s: %s", r.status_code, r.text[:150])
    except Exception:
        log.exception("memory load failed")
    return d


def _mem_save(chat_id):
    if not USE_SUPABASE:
        return
    try:
        r = requests.post(f"{SUPABASE_URL}/rest/v1/{MEMORY_TABLE}",
                          headers={**_sb_headers(), "Prefer": "resolution=merge-duplicates,return=minimal"},
                          params={"on_conflict": "chat_id"},
                          json={"chat_id": str(chat_id), "data": MEM.get(chat_id, {})}, timeout=10)
        if not r.ok:
            log.warning("memory save %s: %s", r.status_code, r.text[:150])
    except Exception:
        log.exception("memory save failed")


def _mem(chat_id):
    if chat_id not in _MEM_LOADED:
        MEM[chat_id] = _mem_load(chat_id)
        _MEM_LOADED.add(chat_id)
        _MEM_DIRTY[chat_id] = 0
    return MEM[chat_id]


def learn(chat_id, text):
    """Запоминаем эмодзи и короткие повторяющиеся фразы из сообщения."""
    m = _mem(chat_id)
    changed = False
    for c in EMOJI_RE.findall(text):
        if c in _EMOJI_MOD:
            continue
        m["emojis"][c] = m["emojis"].get(c, 0) + 1
        changed = True
    norm = " ".join(text.strip().lower().split())
    if norm and not norm.startswith("/") and 1 <= len(norm.split()) <= 5 and 2 <= len(norm) <= 40:
        m["phrases"][norm] = m["phrases"].get(norm, 0) + 1
        changed = True
    if changed:
        _MEM_DIRTY[chat_id] = _MEM_DIRTY.get(chat_id, 0) + 1
        if _MEM_DIRTY[chat_id] >= MEMORY_FLUSH_EVERY:
            _mem_save(chat_id)
            _MEM_DIRTY[chat_id] = 0


def memory_hint(chat_id):
    """Подсказка для модели: запомненные факты + любимые эмодзи и мемы чата."""
    m = _mem(chat_id)
    te = sorted(m["emojis"].items(), key=lambda x: -x[1])[:TOP_EMOJIS]
    tp = [(p, c) for p, c in sorted(m["phrases"].items(), key=lambda x: -x[1]) if c >= 2][:TOP_PHRASES]
    facts = m.get("facts", [])
    parts = []
    if facts:
        parts.append("Что ты ПОМНИШЬ (факты/правила — учитывай и применяй):\n"
                     + "\n".join("- " + f for f in facts[-40:]))
    if te:
        parts.append("Эмодзи, которые любит этот чат: " + " ".join(e for e, _ in te))
    if tp:
        parts.append("Местные фразочки/мемы чата: " + ", ".join("«%s»" % p for p, _ in tp))
    return "\n".join(parts)


def add_fact(chat_id, fact):
    """Сохранить факт/правило в долгую память (с дедупом и лимитом)."""
    fact = fact.strip().strip("«»\"'").strip()
    if not fact or len(fact) > 200:
        return False
    m = _mem(chat_id)
    facts = m.setdefault("facts", [])
    if any(fact.lower() == f.lower() for f in facts):
        return False
    facts.append(fact)
    del facts[:-80]  # держим максимум 80 фактов
    _mem_save(chat_id)
    log.info("MEMO chat=%s: %s", chat_id, fact[:80])
    return True


def forget_facts(chat_id, query):
    """Удалить из памяти факты, содержащие query."""
    q = query.strip().lower()
    if not q:
        return 0
    m = _mem(chat_id)
    facts = m.setdefault("facts", [])
    before = len(facts)
    m["facts"] = [f for f in facts if q not in f.lower()]
    removed = before - len(m["facts"])
    if removed:
        _mem_save(chat_id)
        log.info("FORGET chat=%s: %d removed by %r", chat_id, removed, query[:60])
    return removed


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
        self.convo_until = 0.0              # до этого времени бот «в активной беседе»


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


_DIRECTED_WORDS = ("ты", "тебя", "тебе", "тобой", "твой", "твоя", "твоё", "твое",
                   "твои", "твоего", "твоей", "твоих", "твоим", "твоём", "твоем")


def looks_directed(text):
    """Похоже, что сообщение адресовано собеседнику (2-е лицо: «ты/тебе/твой…»)."""
    low = text.lower()
    for w in _DIRECTED_WORDS:
        if re.search(r"(?<![0-9a-zа-яё])" + w + r"(?![0-9a-zа-яё])", low):
            return True
    return False


def decide(state, addressed, text, reply_to_other=False):
    """Возвращает (отвечать, в_контексте, judge).
    judge=True → неоднозначный «ты» в беседе: пусть модель сама решит, к ней ли обращаются (иначе SKIP)."""
    now = time.time()
    while state.reply_times and now - state.reply_times[0] > 86400:
        state.reply_times.popleft()
    replies_day = len(state.reply_times)
    replies_hour = sum(1 for t in state.reply_times if now - t < 3600)

    if replies_day >= MAX_REPLIES_PER_DAY:
        return False, False, False

    # прямое обращение по имени Гены / реплай на бота — точно ему
    if addressed:
        return True, True, False

    # «ты…» без имени Гены и не реплай другому — адресат неясен → пусть решит модель (judge)
    directed = looks_directed(text) and not reply_to_other

    if now < state.convo_until:
        if directed:
            return True, False, True
        if now - state.last_reply < CONVO_MIN_GAP:
            return False, False, False
        return (random.random() < CONVO_CHATTINESS), True, False

    # фоновое встревание в чужой разговор — изредка и случайно
    if now - state.last_reply < REPLY_COOLDOWN_SEC:
        return False, False, False
    if replies_hour >= MAX_REPLIES_PER_HOUR:
        return False, False, False

    p = CHATTINESS + (QUESTION_BOOST if "?" in text else 0.0)
    if random.random() < p:
        return True, False, directed  # если на «ты» — модель проверит, к ней ли
    return False, False, False


def build_reply(state, forced, opinion=False, hint="", judge=False, reply_ctx="", remember=False, forget=False):
    """Зовём Haiku. opinion — анализ; hint — стиль/память; judge — к ней ли; reply_ctx — reply-таргет; remember/forget — записать/стереть факт через MEMO/FORGET."""
    if anthropic is None:
        log.warning("no ANTHROPIC_API_KEY — cannot generate reply")
        return None

    window = OPINION_CONTEXT if opinion else CONTEXT_WINDOW
    recent = list(state.messages)[-window:]
    history = recent[:-1]
    last = recent[-1] if recent else {"name": "", "text": ""}
    transcript = "\n".join(f"{m['name']}: {m['text']}" for m in history) or "(пока пусто)"

    if opinion:
        instr = ("Тебя спросили мнение / просят влиться в обсуждение. Прочитай переписку выше "
                 "и дай свой РЕАЛЬНЫЙ, осмысленный взгляд по сути — со своим отношением, в своём "
                 "стиле (дерзко, с приколом — ок, но по делу). Можно развернуться на 2-4 фразы, "
                 "без воды и без списков. Опирайся на то, что реально обсуждали.")
        maxtok = OPINION_MAX_TOKENS
    else:
        instr = ("Ответь как " + BOT_NAME + " именно на ПОСЛЕДНЕЕ сообщение, с учётом контекста выше. "
                 "Если там вопрос — ответь на него по сути (в своём стиле, можно с приколом), "
                 "не уходи в свою тему и не отмахивайся. Коротко, обычно просто текст. "
                 "Реакцию [react:эмодзи] в начале добавляй РЕДКО — только если сообщение прям в тему/смешное, не на каждое.")
        maxtok = MAX_TOKENS
    if forced:
        instr += " Тебя позвали лично — обязательно ответь по делу, не пиши SKIP."
    else:
        instr += " Если по сути нечего сказать и реакция не просится — ответь ровно SKIP."
    if judge:
        seen, others = set(), []
        for mm in reversed(list(state.messages)):
            nm = mm.get("name", "")
            if nm and nm != BOT_NAME and nm.lower() not in seen:
                seen.add(nm.lower())
                others.append(nm)
            if len(others) >= 8:
                break
        roster = ", ".join(others) if others else "(других не видно)"
        instr += (f" В чате есть участники: {roster}; ты — {BOT_NAME}. СНАЧАЛА пойми, К КОМУ обращено "
                  "ПОСЛЕДНЕЕ сообщение: если по имени/нику/контексту оно адресовано другому участнику "
                  "(например начинается с «Серёг», «Паш», «Миш», «Алин» или там прямо назван другой) — "
                  f"это НЕ тебе, ответь ровно SKIP и больше ничего. Отвечай, только если обращаются к тебе "
                  f"({BOT_NAME}) или вопрос явно общий и к тебе тоже.")
    instr += (" Если всплыл важный устойчивый факт/правило о ком-то или о тебе, который стоит "
              "запомнить НАДОЛГО — добавь В САМОМ КОНЦЕ ответа отдельной строкой "
              "`MEMO: <короткий факт одним предложением>` (только если правда важно; обычно не нужно).")
    if remember:
        instr += (" Тебя ПРЯМО просят запомнить — обязательно добавь строку "
                  "`MEMO: <что именно запомнить, кратко и ясно>` и подтверди в своём стиле.")
    if forget:
        instr += " Тебя просят забыть — добавь строку `FORGET: <ключевые слова, что забыть>`."

    last_line = f"{last['name']}: {last['text']}"
    if reply_ctx:
        last_line += f"\n(↳ это ОТВЕТ (reply) на сообщение — {reply_ctx})"
    user = (f"История чата (контекст):\n{transcript}\n\n"
            f"ПОСЛЕДНЕЕ сообщение, на него и отвечай:\n{last_line}\n\n{instr}")
    if hint:
        user += ("\n\nПодсказка по стилю ЭТОГО чата (вплетай естественно, когда в тему, "
                 "не насильно):\n" + hint)
    try:
        resp = anthropic.messages.create(
            model=MODEL,
            max_tokens=maxtok,
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
    learn(chat_id, text)  # копим эмодзи/фразочки даже когда молчим

    addressed = is_addressed(msg, text)
    rt = msg.get("reply_to_message") or {}
    reply_to_other = bool(rt) and (rt.get("from") or {}).get("id") != BOT_ID
    reply_ctx = ""
    if rt:
        rf = rt.get("from") or {}
        rname = BOT_NAME if rf.get("id") == BOT_ID else (rf.get("first_name") or rf.get("username") or "кто-то")
        rtext = (rt.get("text") or rt.get("caption") or "").strip()
        if rtext:
            reply_ctx = f"{rname}: {rtext[:200]}"
    ok, forced, judge = decide(state, addressed, text, reply_to_other)
    if not ok:
        return

    low_text = text.lower()
    opinion = any(t in low_text for t in OPINION_TRIGGERS)
    remember = any(w in low_text for w in ("запомни", "запиши", "имей в виду", "имей ввиду", "на будущее"))
    forget = any(w in low_text for w in ("забудь", "удали из памяти"))
    raw = build_reply(state, forced, opinion, memory_hint(chat_id), judge, reply_ctx, remember, forget)
    if not raw:
        return

    # долгая память: вынимаем MEMO/FORGET, сохраняем, убираем эти строки из текста
    for fact in re.findall(r'(?im)^\s*MEMO:\s*(.+?)\s*$', raw):
        add_fact(chat_id, fact)
    for q in re.findall(r'(?im)^\s*FORGET:\s*(.+?)\s*$', raw):
        forget_facts(chat_id, q)
    raw = re.sub(r'(?im)^\s*(?:MEMO|FORGET):.*$', '', raw).strip()
    if not raw and (remember or forget):
        raw = "ок, запомнил" if remember else "ок, забыл"
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
    if react and random.random() >= REACT_CHANCE:
        react = None  # модель захотела реакцию, но ставим не на каждую — иногда пропускаем
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
    state.convo_until = now + CONVO_WINDOW_SEC  # открыли/продлили активную беседу
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
