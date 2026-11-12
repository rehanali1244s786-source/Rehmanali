"""
main.py - Telegram Music + Video downloader (YouTube) with full group management
(v3: artist pages, song info, share cards, release alerts, referrals, premium, group branding)

Setup
    pip install -U "python-telegram-bot>=21" yt-dlp
    ffmpeg required (Linux: sudo apt install ffmpeg | Termux: pkg install ffmpeg)

Run
    cp .env.example .env   # then edit .env (BOT_TOKEN, ADMIN_IDS)
    python main.py

Admin commands
    /admin                      panel (force-join chats)
    /groups [search]            group manager (settings, stats, broadcast, cleanup...)
    /ban [global] (reply|id)    ban a user in this group (or globally)
    /unban [global] (reply|id)
    /warn (reply)               3 warnings = ban in that group
    /approve <id> /unapprove <id>   users allowed to add the bot to groups
"""

import asyncio
import difflib
import glob
import heapq
import html
import importlib.util
import itertools
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import quote

import httpx
import yt_dlp
from telegram import (
    BotCommand,
    InlineKeyboardButton as _IKB,
    InlineKeyboardMarkup as Markup,
    InlineQueryResultArticle,
    InlineQueryResultsButton,
    InputTextMessageContent,
    Update,
)
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest, Forbidden, RetryAfter, TelegramError
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    CallbackQueryHandler,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    TypeHandler,
    InlineQueryHandler,
    filters,
)

# ───────────────────────── Coloured buttons ─────────────────────────
# Telegram supports 3 button colours: primary (blue), success (green), danger (red).
_RED = re.compile(r"remove|cancel|delete|block|\bban\b|clean|clear|reset|🗑|❌|🚫|🧹", re.I)
_GREEN = re.compile(r"audio|joined|✅|approve|confirm|send|start|ڈاؤن|آڈیو|ऑडियो|unblock|verify", re.I)


def Btn(text, callback_data=None, url=None, style=None, **kw):
    """InlineKeyboardButton with an automatic colour: red = destructive, green = go/confirm, blue = everything else."""
    if style is None:
        t = text.replace("✅ Unblock", "unblock")
        style = "danger" if _RED.search(t) and "unblock" not in t.lower() else "success" if _GREEN.search(t) else "primary"
    return _IKB(text, callback_data=callback_data, url=url, api_kwargs={"style": style}, **kw)


# ───────────────────────── Config ─────────────────────────
def _load_env(path=".env"):
    """Tiny .env loader (no extra dependency). Real environment variables win."""
    p = Path(__file__).resolve().parent / path
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_env()

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
COOKIES_FILE = os.getenv("COOKIES_FILE")
# Railway/hosting: cookies can come from env var COOKIES_B64 (base64 of cookies.txt) or from /data/cookies.txt
try:
    import base64 as _b64
    _cb = os.getenv("COOKIES_B64", "").strip()
    if _cb:
        _cp = os.path.join(tempfile.gettempdir(), "yt_cookies.txt")
        with open(_cp, "wb") as _f:
            _f.write(_b64.b64decode(_cb))
        COOKIES_FILE = _cp
    elif not COOKIES_FILE and os.path.exists("/data/cookies.txt"):
        _cp = os.path.join(tempfile.gettempdir(), "yt_cookies.txt")
        shutil.copyfile("/data/cookies.txt", _cp)
        COOKIES_FILE = _cp
except Exception as _e:
    print("cookies setup failed:", _e)
ADMIN_IDS = {int(x) for x in re.findall(r"\d+", os.getenv("ADMIN_IDS", ""))}
DATA_FILE = Path(os.getenv("DATA_FILE", str(Path(__file__).resolve().parent / "musicbot_data.json")))

AUDD_TOKEN = os.getenv("AUDD_TOKEN", "")                       # https://dashboard.audd.io
RECOGNIZE_DAILY_LIMIT = int(os.getenv("RECOGNIZE_DAILY_LIMIT", "5"))  # per user per day (admins unlimited)
MAX_RECOGNIZE_FILE = 20 * 1024 * 1024                           # Telegram bot download limit

MAX_AUDIO_DURATION = 30 * 60
MAX_VIDEO_DURATION = 15 * 60
MAX_FILE_SIZE = 49 * 1024 * 1024
MAX_PARALLEL_DOWNLOADS = int(os.getenv("MAX_PARALLEL_DOWNLOADS", "6"))
MEMBER_CACHE_TTL = 300
SEARCH_CACHE_TTL = 1800
SEARCH_CACHE_MAX = 500
INACTIVE_DAYS = 30
WARN_LIMIT = 3
GROUPS_PER_PAGE = 8

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("MusicBot")

HTML = ParseMode.HTML
GROUP_TYPES = ("group", "supergroup")

# v3 settings (all optional, set in .env)
PREMIUM_RECOG_LIMIT = int(os.getenv("PREMIUM_RECOG_LIMIT", "50"))     # recognitions/day for premium users
REF_BONUS = int(os.getenv("REF_BONUS", "1"))                          # +N recognitions/day per invited friend
REF_MAX_BONUS = int(os.getenv("REF_MAX_BONUS", "10"))                 # cap on the referral bonus
FOLLOW_LIMIT_FREE = int(os.getenv("FOLLOW_LIMIT_FREE", "5"))
FOLLOW_LIMIT_PREMIUM = int(os.getenv("FOLLOW_LIMIT_PREMIUM", "25"))
RELEASE_CHECK_HOURS = float(os.getenv("RELEASE_CHECK_HOURS", "6"))    # how often followed artists are checked
BRANDING_FREE = os.getenv("BRANDING_FREE", "0") == "1"                # 1 = every group admin may set a footer
GETSONGBPM_KEY = os.getenv("GETSONGBPM_KEY", "")                      # optional BPM/key source (free key)
MB_USER_AGENT = os.getenv("MB_USER_AGENT", "MusicBot/3.0 ( https://t.me/Dockworldwide )")
HAVE_LIBROSA = importlib.util.find_spec("librosa") is not None        # optional local BPM/key analysis


class PrioSlots:
    """Download slots with a priority line: premium/admin (prio 0) go before everyone else (prio 1); FIFO inside a level."""

    def __init__(self, n: int):
        self.n, self.running, self._heap, self._seq = n, 0, [], itertools.count()

    def ticket(self, prio: int):
        return (prio, next(self._seq))

    def _clean(self):
        while self._heap and self._heap[0][1].done():
            heapq.heappop(self._heap)

    def position(self, ticket):
        waiting = sorted(t for t, f in self._heap if not f.done())
        return waiting.index(ticket) + 1 if ticket in waiting else None

    async def acquire(self, ticket):
        self._clean()
        if self.running < self.n and not self._heap:
            self.running += 1
            return
        fut = asyncio.get_running_loop().create_future()
        heapq.heappush(self._heap, (ticket, fut))
        try:
            await fut
        except asyncio.CancelledError:
            if fut.done() and not fut.cancelled():
                self.release()          # the slot was already handed to us; pass it on
            raise

    def release(self):
        self._clean()
        while self._heap:
            _, fut = heapq.heappop(self._heap)
            if not fut.done():
                fut.set_result(None)    # hand the slot straight to the next in line
                return
        self.running -= 1


SLOTS = PrioSlots(MAX_PARALLEL_DOWNLOADS)
# Separate thread pools: quick lookups (search / info) must never wait behind long downloads.
LOOKUP_POOL = ThreadPoolExecutor(max_workers=8, thread_name_prefix="lookup")
DL_POOL = ThreadPoolExecutor(max_workers=MAX_PARALLEL_DOWNLOADS, thread_name_prefix="dl")
_tls = threading.local()  # one reusable yt-dlp session per lookup thread
NUMS = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣"]
VIDEO_QUALITY = 480  # video is ALWAYS downloaded at 480p
CREDIT_USER = "@Dockworldwide"
CREDIT_HTML = f"\n\n👨‍💻 <i>Developed by</i> <b>Dock</b> — {CREDIT_USER}"
CREDIT_PLAIN = f"\n\n👨‍💻 Developed by Dock — {CREDIT_USER}"
YT_RE = re.compile(r"(?:youtu\.be/|youtube\.com/(?:watch\?.*?v=|shorts/|embed/)|music\.youtube\.com/watch\?.*?v=)([\w-]{11})")

# ───────────────────────── Storage ─────────────────────────
GDEF = {
    "title": "", "blocked": False,
    "fmt": "both",      # both | audio | video
    "qual": 720,        # max video quality
    "dur": 0,           # max minutes (0 = default)
    "cool": 0,          # seconds between requests per user
    "adm": False,       # only group admins can use the bot
    "del": 0,           # auto-delete bot messages after N seconds (0 = off)
    "txt": False,       # reply to plain text (no /song needed)
    "force": [],        # extra force-join chat ids for this group
    "added": 0, "last": 0, "members": 0,
    "brand": "", "brand_by": 0,   # custom footer (premium perk) and who set it
}

db = {
    "enabled": True, "chats": [], "users": [], "cache": {}, "groups": {},
    "approval": False, "approved": [], "banned_global": [], "banned_groups": {},
    "warns": {}, "gstats": {},
    "ulang": {}, "recog": {}, "tot": {"a": 0, "v": 0}, "daily": {},
    "premium": {}, "pending": {}, "ref": {"by": {}, "n": {}}, "artists": {}, "bpm": {},
    "sponsor": {"on": False, "text": ""},
    "pay": {"price": 0, "days": 30, "cur": "PKR", "jc": {"num": "", "name": ""}, "ep": {"num": "", "name": ""}},
}
if DATA_FILE.exists():
    try:
        db.update(json.loads(DATA_FILE.read_text(encoding="utf-8")))
    except Exception:
        log.exception("Could not read %s, starting fresh", DATA_FILE)

_now = int(time.time())
for _g in db["groups"].values():
    for _k, _v in GDEF.items():
        _g.setdefault(_k, list(_v) if isinstance(_v, list) else _v)
    _g["added"] = _g["added"] or _now
    _g["last"] = _g["last"] or _now
_users = set(db["users"])
_dirty = False


def _write_db(payload: str):
    tmp = DATA_FILE.with_suffix(".tmp")
    tmp.write_text(payload, encoding="utf-8")
    tmp.replace(DATA_FILE)


def save_db():
    db["users"] = list(_users)
    _write_db(json.dumps(db, ensure_ascii=False))


def touch():
    """Mark data as changed; the background saver writes it (keeps the bot fast)."""
    global _dirty
    _dirty = True


async def saver():
    global _dirty
    while True:
        await asyncio.sleep(15)
        if _dirty:
            _dirty = False
            try:
                db["users"] = list(_users)
                payload = json.dumps(db, ensure_ascii=False)   # snapshot on the loop (no races)
                await asyncio.to_thread(_write_db, payload)    # disk I/O off the loop
            except Exception:
                log.exception("save failed")


def is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS


META: dict = {}
SEARCH_CACHE: dict = {}
_member_cache: dict = {}
_check_alert: dict = {}
_gadm_cache: dict = {}
_cooldown: dict = {}
_bg: set = set()


def remember(meta: dict):
    if len(META) > 3000:
        META.clear()
    META[meta["id"]] = meta


# ───────────────────────── Helpers ─────────────────────────
def esc(s) -> str:
    return html.escape(str(s or ""))


def fmt_dur(sec) -> str:
    sec = int(sec or 0)
    h, r = divmod(sec, 3600)
    m, s = divmod(r, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def ago(ts) -> str:
    d = int(time.time()) - int(ts or 0)
    if d < 60:
        return "now"
    if d < 3600:
        return f"{d // 60}m"
    if d < 86400:
        return f"{d // 3600}h"
    return f"{d // 86400}d"


def thumb_url(vid: str) -> str:
    return f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"


async def safe_edit(msg, text: str, markup=None):
    try:
        if msg.photo:
            await msg.edit_caption(caption=text, parse_mode=HTML, reply_markup=markup)
        else:
            await msg.edit_text(text, parse_mode=HTML, reply_markup=markup, disable_web_page_preview=True)
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            log.warning("edit failed: %s", e)
    except TelegramError as e:
        log.warning("edit failed: %s", e)


async def keep_action(chat, action):
    try:
        while True:
            await chat.send_action(action)
            await asyncio.sleep(4)
    except (asyncio.CancelledError, TelegramError):
        pass


async def _del_later(msg, secs: int):
    try:
        await asyncio.sleep(secs)
        await msg.delete()
    except (asyncio.CancelledError, TelegramError):
        pass


def autodel(msg, g: dict, secs: int = 0):
    secs = secs or g.get("del", 0)
    if msg and secs:
        t = asyncio.create_task(_del_later(msg, secs))
        _bg.add(t)
        t.add_done_callback(_bg.discard)


async def notify_admins(bot, text: str, markup=None):
    for a in ADMIN_IDS:
        try:
            await bot.send_message(a, text, parse_mode=HTML, reply_markup=markup)
        except TelegramError:
            pass


# ───────────────────────── Group registry / policy ─────────────────────────
def ensure_group(chat) -> dict:
    gid = str(chat.id)
    g = db["groups"].get(gid)
    now = int(time.time())
    if g is None:
        g = {"title": chat.title or gid, "added": now, "last": now}
        db["groups"][gid] = g
        touch()
    for k, v in GDEF.items():
        if k not in g:
            g[k] = list(v) if isinstance(v, list) else v
    if chat.title and g["title"] != chat.title:
        g["title"] = chat.title
        touch()
    return g


def gsettings(chat) -> dict:
    return ensure_group(chat) if chat.type in GROUP_TYPES else GDEF


async def is_group_admin(bot, cid: int, uid: int) -> bool:
    hit = _gadm_cache.get((cid, uid))
    if hit and time.time() - hit[0] < 60:
        return hit[1]
    try:
        m = await bot.get_chat_member(cid, uid)
        ok = m.status in ("administrator", "creator")
    except TelegramError:
        ok = False
    _gadm_cache[(cid, uid)] = (time.time(), ok)
    return ok


async def deny(text: str, msg=None, cq=None):
    if cq:
        await cq.answer(text, show_alert=True)
    elif msg:
        try:
            m = await msg.reply_text(text)
            autodel(m, GDEF, 8)
        except TelegramError:
            pass


async def group_ok(context, chat, user, *, cooldown=False, msg=None, cq=None) -> bool:
    """Per-group rules: admins-only mode and cooldown. Bot admins bypass everything."""
    if chat.type not in GROUP_TYPES or is_admin(user.id):
        return True
    g = ensure_group(chat)
    if g["adm"] and not await is_group_admin(context.bot, chat.id, user.id):
        await deny("🔒 Only group admins can use the bot here.", msg, cq)
        return False
    if cooldown and g["cool"]:
        k = (chat.id, user.id)
        wait = g["cool"] - (time.time() - _cooldown.get(k, 0))
        if wait > 0:
            await deny(f"⏳ Please wait {int(wait) + 1}s before the next request.", msg, cq)
            return False
        _cooldown[k] = time.time()
    return True


def record(chat, user, meta: dict, kind: str = "a"):
    gid = str(chat.id) if chat.type in GROUP_TYPES else "pm"
    s = db["gstats"].setdefault(gid, {"n": 0, "users": {}, "songs": {}})
    s["n"] += 1
    u = s["users"].setdefault(str(user.id), {"n": 0, "name": user.full_name[:30]})
    u["n"] += 1
    u["name"] = user.full_name[:30]
    song = s["songs"].setdefault(meta["id"], {"t": meta["title"][:60], "n": 0})
    song["n"] += 1
    if len(s["songs"]) > 300:
        for k, _ in sorted(s["songs"].items(), key=lambda kv: kv[1]["n"])[:50]:
            s["songs"].pop(k, None)
    if gid in db["groups"]:
        db["groups"][gid]["last"] = int(time.time())
    db["tot"][kind] = db["tot"].get(kind, 0) + 1
    day = time.strftime("%Y-%m-%d")
    db["daily"][day] = db["daily"].get(day, 0) + 1
    if len(db["daily"]) > 40:
        for k in sorted(db["daily"])[:-35]:
            db["daily"].pop(k, None)
    touch()


# ───────────────────────── yt-dlp (blocking, run in threads) ─────────────────────────
def _base_opts() -> dict:
    o = {
        "quiet": True, "no_warnings": True, "noplaylist": True,
        "socket_timeout": 15, "retries": 3, "fragment_retries": 5,
        "concurrent_fragment_downloads": 8,
        "http_chunk_size": 10 * 1024 * 1024,
        "noprogress": True, "no_color": True,
        "check_formats": False,            # skip extra HEAD requests per format
        "throttledratelimit": 100 * 1024,  # re-extract if YouTube throttles below ~100 KB/s
    }
    if COOKIES_FILE and os.path.exists(COOKIES_FILE):
        o["cookiefile"] = COOKIES_FILE
    if shutil.which("node"):
        o["js_runtimes"] = {"node": {}}    # JS runtime needed by recent yt-dlp for YouTube
    return o


def _session(name: str, extra: dict):
    """Reuse one yt-dlp instance per thread: no re-init, no cookie re-parse, keep-alive connections."""
    y = getattr(_tls, name, None)
    if y is None:
        y = yt_dlp.YoutubeDL(_base_opts() | extra)
        setattr(_tls, name, y)
    return y


def yt_search(query: str) -> list:
    y = _session("search", {"extract_flat": "in_playlist", "skip_download": True})
    info = y.extract_info(f"ytsearch5:{query}", download=False)
    out = []
    for e in info.get("entries") or []:
        if e and e.get("id") and len(e["id"]) == 11:
            out.append({
                "id": e["id"], "title": e.get("title") or "Unknown",
                "channel": e.get("channel") or e.get("uploader") or "Unknown",
                "duration": e.get("duration") or 0,
                "views": e.get("view_count") or 0,
            })
    return out


def yt_meta(vid: str) -> dict:
    y = _session("meta", {"skip_download": True})
    e = y.extract_info(f"https://www.youtube.com/watch?v={vid}", download=False)
    return {
        "id": vid, "title": e.get("title") or "Unknown",
        "channel": e.get("channel") or e.get("uploader") or "Unknown",
        "duration": e.get("duration") or 0,
        "views": e.get("view_count") or 0,
    }


def yt_download(vid: str, fmt: str, outdir: str) -> dict:
    opts = _base_opts() | {"outtmpl": os.path.join(outdir, "%(id)s.%(ext)s")}
    if fmt == "a":
        opts["format"] = "bestaudio[ext=m4a]/bestaudio/best"
    else:
        h = VIDEO_QUALITY
        opts["format"] = (
            f"bv*[height<={h}][ext=mp4]+ba[ext=m4a]/b[height<={h}][ext=mp4]/"
            f"bv*[height<={h}]+ba/b[height<={h}]/b"
        )
        opts["merge_output_format"] = "mp4"
    with yt_dlp.YoutubeDL(opts) as y:
        info = y.extract_info(f"https://www.youtube.com/watch?v={vid}", download=True)

    path = ((info.get("requested_downloads") or [{}])[0]).get("filepath")
    if not path or not os.path.exists(path):
        files = glob.glob(os.path.join(outdir, f"{vid}.*"))
        if not files:
            raise FileNotFoundError("Download produced no file")
        path = files[0]

    if fmt == "a" and not path.lower().endswith((".m4a", ".mp3")):
        mp3 = os.path.join(outdir, f"{vid}.mp3")
        subprocess.run(
            ["ffmpeg", "-y", "-i", path, "-vn", "-b:a", "192k", mp3],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        path = mp3
    return {"path": path, "width": info.get("width"), "height": info.get("height")}


_http = None


def http_client() -> httpx.AsyncClient:
    global _http
    if _http is None:
        _http = httpx.AsyncClient(timeout=8, limits=httpx.Limits(max_keepalive_connections=20))
    return _http


async def fetch_thumb(vid: str):
    try:
        r = await http_client().get(f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg")
        return r.content if r.status_code == 200 else None
    except Exception:
        return None


async def run_lookup(fn, *args):
    """Run a quick blocking yt-dlp lookup in its own pool (never blocked by downloads)."""
    return await asyncio.get_running_loop().run_in_executor(LOOKUP_POOL, fn, *args)


# ───────────────────────── Gate: bans, blocked groups, force-join ─────────────────────────
def required_chats(g) -> list:
    req = list(db["chats"]) if db["enabled"] else []
    if g:
        ids = {c["id"] for c in req}
        req += [c for c in db["chats"] if c["id"] in g["force"] and c["id"] not in ids]
    return req


async def missing_chats(bot, user_id: int, required: list) -> list:
    async def one(c):
        key = (user_id, c["id"])
        if time.time() - _member_cache.get(key, 0) < MEMBER_CACHE_TTL:
            return None
        try:
            m = await bot.get_chat_member(c["id"], user_id)
        except TelegramError as e:
            if time.time() - _check_alert.get(c["id"], 0) > 3600:
                _check_alert[c["id"]] = time.time()
                await notify_admins(
                    bot,
                    f"⚠️ <b>Force-join problem</b>\n\nI can't check members of <b>{esc(c['title'])}</b> "
                    f"(<code>{c['id']}</code>), so users are being let through.\n"
                    f"👉 Make me an <b>admin</b> in that channel/group.\n\n<code>{esc(str(e)[:150])}</code>",
                )
            return None
        ok = m.status in ("member", "administrator", "creator") or (
            m.status == "restricted" and getattr(m, "is_member", False)
        )
        if ok:
            _member_cache[key] = time.time()
            return None
        return c

    res = await asyncio.gather(*(one(c) for c in required))
    return [c for c in res if c]


def join_markup(missing: list, lang: str = "en") -> Markup:
    rows = []
    for c in missing:
        if c.get("link"):
            icon = "📢" if c.get("type") == "channel" else "👥"
            rows.append([Btn(f"{icon} " + tr(lang, "join_btn", title=c["title"][:30]), url=c["link"], style="primary")])
    rows.append([Btn(tr(lang, "join_done"), callback_data="chk", style="success")])
    return Markup(rows)


async def gate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg, cq = update.message, update.callback_query
    user = update.effective_user
    if not user or user.is_bot or not (msg or cq):
        return
    if user.id not in _users:
        _users.add(user.id)
        touch()
        m_ref = re.match(r"/start(?:@\w+)?\s+ref_(\d+)", msg.text or "") if msg else None
        if m_ref:
            await register_referral(context.bot, user, int(m_ref.group(1)))

    chat = update.effective_chat
    admin = is_admin(user.id)
    g = None
    if chat.type in GROUP_TYPES:
        g = ensure_group(chat)
        now = int(time.time())
        if now - g["last"] > 60:
            g["last"] = now
            touch()
        if g["blocked"] and not admin:
            raise ApplicationHandlerStop

    # bans
    if not admin:
        gid = str(chat.id)
        if user.id in db["banned_global"] or user.id in db["banned_groups"].get(gid, []):
            if cq:
                await cq.answer(tr(L(user.id), "banned"), show_alert=True)
            elif chat.type == "private":
                await msg.reply_text(tr(L(user.id), "banned"))
            raise ApplicationHandlerStop

    required = required_chats(g)
    lang = L(user.id)

    if cq and cq.data == "chk":
        for k in [k for k in _member_cache if k[0] == user.id]:
            _member_cache.pop(k, None)
        missing = await missing_chats(context.bot, user.id, required)
        if missing:
            await cq.answer(tr(lang, "join_not_yet"), show_alert=True)
            await safe_edit(cq.message, tr(lang, "join_text"), join_markup(missing, lang))
        else:
            await cq.answer(tr(lang, "join_verified"))
            await safe_edit(cq.message, tr(lang, "join_ok"))
        raise ApplicationHandlerStop

    if admin or not required:
        return
    is_cmd = bool(msg and msg.text and msg.text.startswith("/"))
    is_text_query = bool(msg and msg.text and (chat.type == "private" or (g and g["txt"])))
    if msg and chat.type == "private" and (msg.voice or msg.audio or msg.video or msg.video_note):
        is_text_query = True
    if not (cq or is_cmd or is_text_query):
        return
    missing = await missing_chats(context.bot, user.id, required)
    if not missing:
        return
    if cq:
        await cq.answer(tr(lang, "join_first"))
        await cq.message.chat.send_message(tr(lang, "join_text"), parse_mode=HTML, reply_markup=join_markup(missing, lang))
    else:
        await msg.reply_text(tr(lang, "join_text"), parse_mode=HTML, reply_markup=join_markup(missing, lang))
    raise ApplicationHandlerStop


# ───────────────────────── Languages (en / ur / hi) ─────────────────────────
LANG_NAMES = {"en": "English 🇬🇧", "ur": "اردو 🇵🇰", "hi": "हिन्दी 🇮🇳"}

STR = {
    "en": {
        "rec_usage": "🎧 <b>Song recognition</b>\n\nSend me a <b>voice note</b>, audio, or video clip — or reply to one with /find — and I'll tell you the song.",
        "rec_listening": "🎧 <b>Listening…</b>\nIdentifying the song, please wait.",
        "rec_found": "✅ <b>Song found!</b>\n\n🎶 <b>{title}</b>\n👤 {artist}{album}\n\n👇 Download it below:",
        "rec_none": "😕 <b>I couldn't recognize this song.</b>\nTry a clearer clip (10–20 seconds with the music loud).",
        "rec_fail": "❌ Recognition is not working right now. Please try again later.",
        "rec_limit": "⏳ Daily limit reached (<b>{n}</b> recognitions per day). Try again tomorrow.",
        "rec_big": "❌ File is too big. Send a clip under 20 MB.",
        "rec_help": "🎧 /find — reply to a voice/audio/video to identify the song (or just send it in private chat)",
        "join_text": "🔒 <b>Join Required</b>\n\nTo use this bot, please join the channel(s)/group(s) below,\nthen tap <b>✅ I've Joined</b>.",
        "join_btn": "Join {title}",
        "join_done": "✅ I've Joined",
        "join_not_yet": "❌ You haven't joined everything yet!",
        "join_first": "🔒 Join required first",
        "join_ok": "✅ <b>Verified!</b>\n\nSend me a song name or use /song to start 🎧",
        "join_verified": "✅ Verified!",
        "banned": "🚫 You are banned from using this bot.",
        "start": (
            "🎧 <b>Music &amp; Video Downloader</b>\n\nHey <b>{name}</b>! 👋\n"
            "Send me a <b>song name</b> or a <b>YouTube link</b> and I'll get it as:\n\n"
            "🎵 <b>Audio</b> — high quality\n🎬 <b>Video</b> — 480p\n\n"
            "⚡ <b>Quick commands</b>\n<code>/song Tum Hi Ho a</code> → 🎵 audio\n"
            "<code>/song Tum Hi Ho v</code> → 🎬 video (480p)\n\n"
            "✨ <i>Or just type a name to pick from a list.</i>"
        ),
        "help": (
            "📖 <b>How to use</b>\n\n"
            "🎵 <code>/song Tum Hi Ho a</code> — get the <b>audio</b>\n"
            "🎬 <code>/song Tum Hi Ho v</code> — get the <b>video</b> (always 480p)\n"
            "<b>A</b> = Audio · <b>V</b> = Video\n\n"
            "🔎 <code>/search Tum Hi Ho</code> — pick from a results list\n"
            "🎤 <code>/lyrics Tum Hi Ho</code> — song lyrics\n"
            "📊 /stats — bot statistics\n🌐 /lang — change language\n"
            "🔍 Inline: type <code>@{bot} song name</code> in any chat\n\n"
            "💡 Without <b>a</b>/<b>v</b> I show a list to pick from.\n"
            "🔗 You can also paste a YouTube link.\n📦 Max upload size: 50 MB."
        ),
        "search_usage": "🔎 Send the song name too.\n<code>/search On Top Karan Aujla</code>",
        "song_usage": "🎵 Send the song name too.\n<b>Audio:</b> <code>/song DXB Karan Aujla a</code>\n<b>Video:</b> <code>/song DXB Karan Aujla v</code>",
        "fetching": "⏳ <b>Fetching info…</b>",
        "bad_link": "❌ Couldn't read that link. Try again later.",
        "load_failed": "❌ Couldn't load that video. Search again.",
        "searching": "🔎 <b>Searching…</b>\n<i>{q}</i>",
        "search_failed": "❌ <b>Search failed.</b>\n{hint}",
        "no_results": "😕 <b>No results found.</b>\nTry adding the artist name.",
        "results_title": "🔎 <b>Results for</b> <i>{q}</i>\n",
        "tap": "\n👇 <i>Tap a number to choose</i>",
        "choose": "👇 <b>Choose a format</b>",
        "views": "views",
        "btn_audio": "🎵 Audio",
        "btn_video": "🎬 Video 480p",
        "btn_lyrics": "🎤 Lyrics",
        "label_audio": "🎵 Audio",
        "label_video": "🎬 Video 480p",
        "downloading": "⏬ <b>Downloading…</b>\n{label}\n<i>{title}</i>",
        "uploading": "⬆️ <b>Uploading…</b>\n{label}",
        "queue": "⏳ <b>You're #{n} in line</b>\n<i>{title}</i>\nThe server is busy — I'll start as soon as it's your turn.",
        "too_long": "❌ Too long (max {n} min here).",
        "too_big": "❌ File is {mb} MB — over Telegram's 50 MB limit.\nTry the audio version instead 👇",
        "failed": "❌ <b>Failed.</b>\n{hint}",
        "disabled": "🚫 This format is disabled in this group.",
        "hint_cookies": "YouTube blocked the server. The owner has been notified.",
        "hint_ffmpeg": "ffmpeg problem on the server. The owner has been notified.",
        "hint_unavail": "This video is unavailable.",
        "hint_retry": "Please try again in a moment.",
        "lang_pick": "🌐 <b>Choose your language</b>",
        "lang_set": "✅ Language set to <b>{name}</b>",
        "lyr_usage": "🎤 Send the song name.\n<b>Example:</b> <code>/lyrics Tum Hi Ho</code>",
        "lyr_search": "🎤 <b>Looking for lyrics…</b>\n<i>{q}</i>",
        "lyr_none": "😕 <b>Lyrics not found.</b>\nTry <code>/lyrics artist song name</code>.",
        "lyr_fail": "❌ Lyrics service is not responding. Try again later.",
        "st_title": "📊 <b>Bot statistics</b>\n",
        "st_users": "Users", "st_total": "Total downloads", "st_today": "Today",
        "st_audio": "Audio", "st_video": "Video", "st_you": "Your downloads",
        "st_top": "🏆 <b>Top songs</b>", "st_none": "No data yet",
        "inl_open": "🎧 Open bot",
        "unk_suggest": "❌ <code>/{cmd}</code> is not a command.\n✅ Did you mean <b>/{sug}</b>?\n\n<b>Example:</b> <code>{ex}</code>",
        "unk_list": "❌ <code>/{cmd}</code> is not a command.\n\n📋 <b>Available commands</b>\n{lst}",
    },
    "ur": {
        "rec_usage": "🎧 <b>گانا پہچانیں</b>\n\nمجھے <b>وائس نوٹ</b>، آڈیو یا ویڈیو کلپ بھیجیں — یا اس پر /find سے جواب دیں — میں گانا بتا دوں گا۔",
        "rec_listening": "🎧 <b>سن رہا ہوں…</b>\nگانا پہچانا جا رہا ہے، انتظار کریں۔",
        "rec_found": "✅ <b>گانا مل گیا!</b>\n\n🎶 <b>{title}</b>\n👤 {artist}{album}\n\n👇 نیچے سے ڈاؤنلوڈ کریں:",
        "rec_none": "😕 <b>میں یہ گانا پہچان نہیں سکا۔</b>\nزیادہ صاف کلپ (10–20 سیکنڈ، آواز بلند) بھیجیں۔",
        "rec_fail": "❌ پہچان کی سروس ابھی کام نہیں کر رہی۔ بعد میں کوشش کریں۔",
        "rec_limit": "⏳ روزانہ کی حد پوری ہو گئی (دن میں <b>{n}</b> بار)۔ کل دوبارہ کوشش کریں۔",
        "rec_big": "❌ فائل بہت بڑی ہے۔ 20 MB سے چھوٹا کلپ بھیجیں۔",
        "rec_help": "🎧 /find — وائس/آڈیو/ویڈیو پر جواب دے کر گانا پہچانیں (یا پرائیویٹ چیٹ میں بھیج دیں)",
        "join_text": "🔒 <b>جوائن کرنا ضروری ہے</b>\n\nبوٹ استعمال کرنے کے لیے نیچے دیے گئے چینل/گروپ جوائن کریں،\nپھر <b>✅ میں نے جوائن کر لیا</b> دبائیں۔",
        "join_btn": "جوائن کریں: {title}",
        "join_done": "✅ میں نے جوائن کر لیا",
        "join_not_yet": "❌ آپ نے ابھی سب جوائن نہیں کیے!",
        "join_first": "🔒 پہلے جوائن کریں",
        "join_ok": "✅ <b>تصدیق ہو گئی!</b>\n\nگانے کا نام بھیجیں یا /song استعمال کریں 🎧",
        "join_verified": "✅ تصدیق ہو گئی!",
        "banned": "🚫 آپ کو اس بوٹ کے استعمال سے بلاک کر دیا گیا ہے۔",
        "start": (
            "🎧 <b>میوزک اور ویڈیو ڈاؤنلوڈر</b>\n\nہیلو <b>{name}</b>! 👋\n"
            "مجھے <b>گانے کا نام</b> یا <b>یوٹیوب لنک</b> بھیجیں، میں آپ کو یہ دوں گا:\n\n"
            "🎵 <b>آڈیو</b> — اعلیٰ کوالٹی\n🎬 <b>ویڈیو</b> — 480p\n\n"
            "⚡ <b>فوری کمانڈز</b>\n<code>/song Tum Hi Ho a</code> → 🎵 آڈیو\n"
            "<code>/song Tum Hi Ho v</code> → 🎬 ویڈیو (480p)\n\n"
            "✨ <i>یا صرف نام لکھیں اور فہرست میں سے چنیں۔</i>"
        ),
        "help": (
            "📖 <b>استعمال کا طریقہ</b>\n\n"
            "🎵 <code>/song Tum Hi Ho a</code> — <b>آڈیو</b> حاصل کریں\n"
            "🎬 <code>/song Tum Hi Ho v</code> — <b>ویڈیو</b> حاصل کریں (ہمیشہ 480p)\n"
            "<b>A</b> = آڈیو · <b>V</b> = ویڈیو\n\n"
            "🔎 <code>/search Tum Hi Ho</code> — فہرست سے چنیں\n"
            "🎤 <code>/lyrics Tum Hi Ho</code> — گانے کے بول\n"
            "📊 /stats — بوٹ کے اعداد و شمار\n🌐 /lang — زبان تبدیل کریں\n"
            "🔍 ان لائن: کسی بھی چیٹ میں <code>@{bot} گانے کا نام</code> لکھیں\n\n"
            "💡 <b>a</b>/<b>v</b> کے بغیر میں فہرست دکھاؤں گا۔\n"
            "🔗 آپ یوٹیوب لنک بھی بھیج سکتے ہیں۔\n📦 زیادہ سے زیادہ سائز: 50 MB۔"
        ),
        "search_usage": "🔎 گانے کا نام بھی لکھیں۔\n<code>/search On Top Karan Aujla</code>",
        "song_usage": "🎵 گانے کا نام بھی لکھیں۔\n<b>آڈیو:</b> <code>/song DXB Karan Aujla a</code>\n<b>ویڈیو:</b> <code>/song DXB Karan Aujla v</code>",
        "fetching": "⏳ <b>معلومات حاصل کی جا رہی ہیں…</b>",
        "bad_link": "❌ یہ لنک پڑھا نہیں جا سکا۔ بعد میں کوشش کریں۔",
        "load_failed": "❌ یہ ویڈیو لوڈ نہیں ہو سکی۔ دوبارہ تلاش کریں۔",
        "searching": "🔎 <b>تلاش جاری ہے…</b>\n<i>{q}</i>",
        "search_failed": "❌ <b>تلاش ناکام ہو گئی۔</b>\n{hint}",
        "no_results": "😕 <b>کوئی نتیجہ نہیں ملا۔</b>\nفنکار کا نام بھی لکھ کر دیکھیں۔",
        "results_title": "🔎 <b>نتائج:</b> <i>{q}</i>\n",
        "tap": "\n👇 <i>چننے کے لیے نمبر دبائیں</i>",
        "choose": "👇 <b>فارمیٹ منتخب کریں</b>",
        "views": "ویوز",
        "btn_audio": "🎵 آڈیو",
        "btn_video": "🎬 ویڈیو 480p",
        "btn_lyrics": "🎤 بول",
        "label_audio": "🎵 آڈیو",
        "label_video": "🎬 ویڈیو 480p",
        "downloading": "⏬ <b>ڈاؤنلوڈ ہو رہا ہے…</b>\n{label}\n<i>{title}</i>",
        "uploading": "⬆️ <b>اپلوڈ ہو رہا ہے…</b>\n{label}",
        "queue": "⏳ <b>آپ قطار میں #{n} پر ہیں</b>\n<i>{title}</i>\nسرور مصروف ہے — باری آتے ہی شروع کر دوں گا۔",
        "too_long": "❌ بہت لمبا ہے (یہاں زیادہ سے زیادہ {n} منٹ)۔",
        "too_big": "❌ فائل {mb} MB ہے — ٹیلیگرام کی 50 MB حد سے زیادہ۔\nآڈیو آزمائیں 👇",
        "failed": "❌ <b>ناکام۔</b>\n{hint}",
        "disabled": "🚫 اس گروپ میں یہ فارمیٹ بند ہے۔",
        "hint_cookies": "یوٹیوب نے سرور کو بلاک کر دیا۔ مالک کو اطلاع دے دی گئی ہے۔",
        "hint_ffmpeg": "سرور پر ffmpeg کا مسئلہ ہے۔ مالک کو اطلاع دے دی گئی ہے۔",
        "hint_unavail": "یہ ویڈیو دستیاب نہیں ہے۔",
        "hint_retry": "براہِ کرم تھوڑی دیر بعد دوبارہ کوشش کریں۔",
        "lang_pick": "🌐 <b>اپنی زبان منتخب کریں</b>",
        "lang_set": "✅ زبان <b>{name}</b> سیٹ ہو گئی",
        "lyr_usage": "🎤 گانے کا نام لکھیں۔\n<b>مثال:</b> <code>/lyrics Tum Hi Ho</code>",
        "lyr_search": "🎤 <b>بول تلاش کیے جا رہے ہیں…</b>\n<i>{q}</i>",
        "lyr_none": "😕 <b>بول نہیں ملے۔</b>\n<code>/lyrics فنکار گانے کا نام</code> آزمائیں۔",
        "lyr_fail": "❌ بول والی سروس جواب نہیں دے رہی۔ بعد میں کوشش کریں۔",
        "st_title": "📊 <b>بوٹ کے اعداد و شمار</b>\n",
        "st_users": "صارفین", "st_total": "کل ڈاؤنلوڈ", "st_today": "آج",
        "st_audio": "آڈیو", "st_video": "ویڈیو", "st_you": "آپ کے ڈاؤنلوڈ",
        "st_top": "🏆 <b>مقبول گانے</b>", "st_none": "ابھی کوئی ڈیٹا نہیں",
        "inl_open": "🎧 بوٹ کھولیں",
        "unk_suggest": "❌ <code>/{cmd}</code> کوئی کمانڈ نہیں ہے۔\n✅ کیا آپ کا مطلب <b>/{sug}</b> تھا؟\n\n<b>مثال:</b> <code>{ex}</code>",
        "unk_list": "❌ <code>/{cmd}</code> کوئی کمانڈ نہیں ہے۔\n\n📋 <b>دستیاب کمانڈز</b>\n{lst}",
    },
    "hi": {
        "rec_usage": "🎧 <b>गाना पहचानें</b>\n\nमुझे <b>वॉइस नोट</b>, ऑडियो या वीडियो क्लिप भेजें — या उस पर /find से रिप्लाई करें — मैं गाना बता दूँगा।",
        "rec_listening": "🎧 <b>सुन रहा हूँ…</b>\nगाना पहचाना जा रहा है, कृपया रुकें।",
        "rec_found": "✅ <b>गाना मिल गया!</b>\n\n🎶 <b>{title}</b>\n👤 {artist}{album}\n\n👇 नीचे से डाउनलोड करें:",
        "rec_none": "😕 <b>मैं यह गाना पहचान नहीं सका।</b>\nज़्यादा साफ़ क्लिप (10–20 सेकंड, तेज़ आवाज़) भेजें।",
        "rec_fail": "❌ पहचान सेवा अभी काम नहीं कर रही। बाद में कोशिश करें।",
        "rec_limit": "⏳ रोज़ की सीमा पूरी हो गई (दिन में <b>{n}</b> बार)। कल फिर कोशिश करें।",
        "rec_big": "❌ फ़ाइल बहुत बड़ी है। 20 MB से छोटी क्लिप भेजें।",
        "rec_help": "🎧 /find — वॉइस/ऑडियो/वीडियो पर रिप्लाई करके गाना पहचानें (या प्राइवेट चैट में भेजें)",
        "join_text": "🔒 <b>जॉइन करना ज़रूरी है</b>\n\nबॉट इस्तेमाल करने के लिए नीचे दिए चैनल/ग्रुप जॉइन करें,\nफिर <b>✅ मैंने जॉइन कर लिया</b> दबाएँ।",
        "join_btn": "जॉइन करें: {title}",
        "join_done": "✅ मैंने जॉइन कर लिया",
        "join_not_yet": "❌ आपने अभी सब जॉइन नहीं किए!",
        "join_first": "🔒 पहले जॉइन करें",
        "join_ok": "✅ <b>पुष्टि हो गई!</b>\n\nगाने का नाम भेजें या /song इस्तेमाल करें 🎧",
        "join_verified": "✅ पुष्टि हो गई!",
        "banned": "🚫 आपको इस बॉट के इस्तेमाल से बैन कर दिया गया है।",
        "start": (
            "🎧 <b>म्यूज़िक और वीडियो डाउनलोडर</b>\n\nनमस्ते <b>{name}</b>! 👋\n"
            "मुझे <b>गाने का नाम</b> या <b>YouTube लिंक</b> भेजें, मैं आपको दूँगा:\n\n"
            "🎵 <b>ऑडियो</b> — हाई क्वालिटी\n🎬 <b>वीडियो</b> — 480p\n\n"
            "⚡ <b>क्विक कमांड</b>\n<code>/song Tum Hi Ho a</code> → 🎵 ऑडियो\n"
            "<code>/song Tum Hi Ho v</code> → 🎬 वीडियो (480p)\n\n"
            "✨ <i>या बस नाम लिखें और लिस्ट से चुनें।</i>"
        ),
        "help": (
            "📖 <b>कैसे इस्तेमाल करें</b>\n\n"
            "🎵 <code>/song Tum Hi Ho a</code> — <b>ऑडियो</b> पाएँ\n"
            "🎬 <code>/song Tum Hi Ho v</code> — <b>वीडियो</b> पाएँ (हमेशा 480p)\n"
            "<b>A</b> = ऑडियो · <b>V</b> = वीडियो\n\n"
            "🔎 <code>/search Tum Hi Ho</code> — सूची से चुनें\n"
            "🎤 <code>/lyrics Tum Hi Ho</code> — गाने के बोल\n"
            "📊 /stats — बॉट के आँकड़े\n🌐 /lang — भाषा बदलें\n"
            "🔍 इनलाइन: किसी भी चैट में <code>@{bot} गाने का नाम</code> लिखें\n\n"
            "💡 <b>a</b>/<b>v</b> के बिना मैं लिस्ट दिखाऊँगा।\n"
            "🔗 आप YouTube लिंक भी भेज सकते हैं।\n📦 अधिकतम साइज़: 50 MB।"
        ),
        "search_usage": "🔎 गाने का नाम भी लिखें।\n<code>/search On Top Karan Aujla</code>",
        "song_usage": "🎵 गाने का नाम भी लिखें।\n<b>ऑडियो:</b> <code>/song DXB Karan Aujla a</code>\n<b>वीडियो:</b> <code>/song DXB Karan Aujla v</code>",
        "fetching": "⏳ <b>जानकारी ली जा रही है…</b>",
        "bad_link": "❌ यह लिंक पढ़ा नहीं जा सका। बाद में कोशिश करें।",
        "load_failed": "❌ यह वीडियो लोड नहीं हो सका। फिर से खोजें।",
        "searching": "🔎 <b>खोज जारी है…</b>\n<i>{q}</i>",
        "search_failed": "❌ <b>खोज विफल रही।</b>\n{hint}",
        "no_results": "😕 <b>कोई नतीजा नहीं मिला।</b>\nकलाकार का नाम भी जोड़कर देखें।",
        "results_title": "🔎 <b>नतीजे:</b> <i>{q}</i>\n",
        "tap": "\n👇 <i>चुनने के लिए नंबर दबाएँ</i>",
        "choose": "👇 <b>फ़ॉर्मैट चुनें</b>",
        "views": "व्यूज़",
        "btn_audio": "🎵 ऑडियो",
        "btn_video": "🎬 वीडियो 480p",
        "btn_lyrics": "🎤 बोल",
        "label_audio": "🎵 ऑडियो",
        "label_video": "🎬 वीडियो 480p",
        "downloading": "⏬ <b>डाउनलोड हो रहा है…</b>\n{label}\n<i>{title}</i>",
        "uploading": "⬆️ <b>अपलोड हो रहा है…</b>\n{label}",
        "queue": "⏳ <b>आप लाइन में #{n} पर हैं</b>\n<i>{title}</i>\nसर्वर व्यस्त है — आपकी बारी आते ही शुरू कर दूँगा।",
        "too_long": "❌ बहुत लंबा है (यहाँ अधिकतम {n} मिनट)।",
        "too_big": "❌ फ़ाइल {mb} MB है — टेलीग्राम की 50 MB सीमा से ज़्यादा।\nऑडियो आज़माएँ 👇",
        "failed": "❌ <b>विफल।</b>\n{hint}",
        "disabled": "🚫 इस ग्रुप में यह फ़ॉर्मैट बंद है।",
        "hint_cookies": "YouTube ने सर्वर को ब्लॉक कर दिया। मालिक को सूचित कर दिया गया है।",
        "hint_ffmpeg": "सर्वर पर ffmpeg की समस्या है। मालिक को सूचित कर दिया गया है।",
        "hint_unavail": "यह वीडियो उपलब्ध नहीं है।",
        "hint_retry": "कृपया थोड़ी देर बाद फिर कोशिश करें।",
        "lang_pick": "🌐 <b>अपनी भाषा चुनें</b>",
        "lang_set": "✅ भाषा <b>{name}</b> सेट हो गई",
        "lyr_usage": "🎤 गाने का नाम लिखें।\n<b>उदाहरण:</b> <code>/lyrics Tum Hi Ho</code>",
        "lyr_search": "🎤 <b>बोल खोजे जा रहे हैं…</b>\n<i>{q}</i>",
        "lyr_none": "😕 <b>बोल नहीं मिले।</b>\n<code>/lyrics कलाकार गाने का नाम</code> आज़माएँ।",
        "lyr_fail": "❌ बोल वाली सेवा जवाब नहीं दे रही। बाद में कोशिश करें।",
        "st_title": "📊 <b>बॉट के आँकड़े</b>\n",
        "st_users": "यूज़र", "st_total": "कुल डाउनलोड", "st_today": "आज",
        "st_audio": "ऑडियो", "st_video": "वीडियो", "st_you": "आपके डाउनलोड",
        "st_top": "🏆 <b>टॉप गाने</b>", "st_none": "अभी कोई डेटा नहीं",
        "inl_open": "🎧 बॉट खोलें",
        "unk_suggest": "❌ <code>/{cmd}</code> कोई कमांड नहीं है।\n✅ क्या आपका मतलब <b>/{sug}</b> था?\n\n<b>उदाहरण:</b> <code>{ex}</code>",
        "unk_list": "❌ <code>/{cmd}</code> कोई कमांड नहीं है।\n\n📋 <b>उपलब्ध कमांड</b>\n{lst}",
    },
}


def L(uid: int) -> str:
    return db["ulang"].get(str(uid), "en")


def tr(lang: str, key: str, **kw) -> str:
    s = STR.get(lang, {}).get(key) or STR["en"][key]
    return s.format(**kw) if kw else s


# ───────────────────────── User commands ─────────────────────────
def start_markup(uid: int) -> Markup:
    rows = [
        [Btn("📖 How to use", callback_data="help"), Btn("⭐ Premium", callback_data="pay:home", style="primary")],
        [Btn("🎁 Invite & Earn", callback_data="inv", style="success")],
    ]
    if is_admin(uid):
        rows.append([Btn("🛠 Admin Panel", callback_data="adm:home"), Btn("👥 Groups", callback_data="grp:list:0")])
    return Markup(rows)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user, msg = update.effective_user, update.message
    lang = L(user.id)

    # deep link from inline mode: /start a_<id> or v_<id>
    if context.args and re.fullmatch(r"[av]_[\w-]{11}", context.args[0]):
        if not await group_ok(context, msg.chat, user, cooldown=True, msg=msg):
            return
        g = gsettings(msg.chat)
        status = await msg.reply_text(tr(lang, "fetching"), parse_mode=HTML)
        await direct_download(context, msg, status, context.args[0][2:], context.args[0][0], g)
        return

    if context.args and re.fullmatch(r"song_[\w-]{11}", context.args[0]):      # share-card deep link
        if not await group_ok(context, msg.chat, user, cooldown=True, msg=msg):
            return
        try:
            meta = await _meta_for(context.args[0][5:])
        except Exception:
            await msg.reply_text(tr(lang, "bad_link"))
            return
        await send_card(msg.chat, meta, gsettings(msg.chat), lang)
        return
    if context.args and context.args[0] == "premium":
        text, kb = premium_view(user.id)
        await msg.reply_text(text, parse_mode=HTML, reply_markup=kb)
        return

    await msg.reply_text(
        tr(lang, "start", name=esc(user.first_name)) + SPON,
        parse_mode=HTML, reply_markup=start_markup(user.id),
    )


def help_text(lang: str, bot) -> str:
    return tr(lang, "help", bot=esc(bot or "bot")) + "\n" + tr(lang, "rec_help") + "\n" + NEW_HELP


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        help_text(L(update.effective_user.id), context.bot.username), parse_mode=HTML
    )


async def cb_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await q.message.chat.send_message(
        help_text(L(q.from_user.id), context.bot.username), parse_mode=HTML
    )


async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"👤 Your ID: <code>{update.effective_user.id}</code>\n💬 Chat ID: <code>{update.effective_chat.id}</code>",
        parse_mode=HTML,
    )


async def cmd_song(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(tr(L(update.effective_user.id), "song_usage"), parse_mode=HTML)
        return
    await handle_query(update.message, " ".join(context.args).strip(), context)


async def cmd_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/search <name> -> numbered list of results (title, channel, duration, views) to tap."""
    if not context.args:
        await update.message.reply_text(tr(L(update.effective_user.id), "search_usage"), parse_mode=HTML)
        return
    await handle_query(update.message, " ".join(context.args).strip(), context, list_only=True)


# ───────────────────────── Language ─────────────────────────
async def cmd_lang(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lang = L(update.effective_user.id)
    rows = [[Btn(("✅ " if code == lang else "") + name, callback_data=f"lang:{code}")] for code, name in LANG_NAMES.items()]
    await update.message.reply_text(tr(lang, "lang_pick"), parse_mode=HTML, reply_markup=Markup(rows))


async def cb_lang(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    code = q.data.split(":", 1)[1]
    if code not in LANG_NAMES:
        await q.answer()
        return
    db["ulang"][str(q.from_user.id)] = code
    touch()
    await q.answer("✅")
    await safe_edit(q.message, tr(code, "lang_set", name=LANG_NAMES[code]))


# ───────────────────────── Error handling + admin alerts ─────────────────────────
def error_kind(e: Exception) -> str:
    m = str(e).lower()
    if "sign in" in m or "bot" in m or "cookies" in m or "login" in m:
        return "cookies"
    if "ffmpeg" in m or "ffprobe" in m:
        return "ffmpeg"
    if "private" in m or "unavailable" in m or "removed" in m or "not available" in m:
        return "video"
    return "other"


def error_hint(e: Exception, lang: str = "en") -> str:
    return tr(lang, {"cookies": "hint_cookies", "ffmpeg": "hint_ffmpeg", "video": "hint_unavail"}.get(error_kind(e), "hint_retry"))


ERR = {"streak": 0, "alerted": False, "last": {}}
ALERT_COOLDOWN = 30 * 60
ALERT_STREAK = 3
ADMIN_FIX = {
    "cookies": "YouTube is blocking the server. Your <b>cookies.txt</b> has probably expired — export fresh cookies from a logged-in browser, replace the file, then restart the bot.",
    "ffmpeg": "<b>ffmpeg</b> is missing or broken on the server. Install it (e.g. <code>apt install ffmpeg</code>) and restart.",
    "other": "Several downloads in a row failed. Check the server's internet, disk space, and try <code>pip install -U yt-dlp</code>.",
}


async def report_error(bot, e: Exception, where: str, vid: str = "", user=None):
    """Alert bot admins when downloads break for server-side reasons (not for one bad video)."""
    kind = error_kind(e)
    if kind == "video":
        return
    ERR["streak"] += 1
    if kind == "other" and ERR["streak"] < ALERT_STREAK:
        return
    now = time.time()
    if now - ERR["last"].get(kind, 0) < ALERT_COOLDOWN:
        return
    ERR["last"][kind] = now
    ERR["alerted"] = True
    who = f"{esc(user.full_name)} (<code>{user.id}</code>)" if user else "—"
    await notify_admins(
        bot,
        f"🚨 <b>Bot alert — downloads failing</b>\n\n"
        f"📍 Where: <code>{esc(where)}</code>\n"
        f"🎬 Video: <code>{esc(vid) or '—'}</code>\n"
        f"👤 User: {who}\n"
        f"🔁 Failures in a row: <b>{ERR['streak']}</b>\n\n"
        f"🧾 <code>{esc(str(e)[:350])}</code>\n\n"
        f"🛠 {ADMIN_FIX.get(kind, ADMIN_FIX['other'])}",
    )


async def report_ok(bot):
    ERR["streak"] = 0
    if ERR["alerted"]:
        ERR["alerted"] = False
        ERR["last"].clear()
        await notify_admins(bot, "✅ <b>Bot recovered</b> — downloads are working again.")


# ───────────────────────── Cards + search ─────────────────────────
def fmt_views(n) -> str:
    n = int(n or 0)
    if n >= 1_000_000_000:
        return f"{n / 1_000_000_000:.1f}B".replace(".0B", "B")
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M".replace(".0M", "M")
    if n >= 1_000:
        return f"{n / 1_000:.1f}K".replace(".0K", "K")
    return str(n)


def meta_line(m: dict) -> str:
    bits = [f"👤 {esc(m['channel'][:30])}", f"⏱ {fmt_dur(m['duration'])}"]
    if m.get("views"):
        bits.append(f"👁 {fmt_views(m['views'])}")
    return " · ".join(bits)


def card_text(meta: dict, lang: str = "en") -> str:
    lines = [f"🎶 <b>{esc(meta['title'][:90])}</b>", "",
             f"🟦 👤 {esc(meta['channel'][:40])}", f"🟩 ⏱ {fmt_dur(meta['duration'])}"]
    if meta.get("views"):
        lines.append(f"🟨 👁 {fmt_views(meta['views'])} {tr(lang, 'views')}")
    return "\n".join(lines) + f"\n\n{tr(lang, 'choose')}"


def card_markup(vid: str, g: dict = GDEF, lang: str = "en") -> Markup:
    rows = []
    if g["fmt"] in ("both", "audio"):
        rows.append([Btn(tr(lang, "btn_audio"), callback_data=f"d:a:{vid}")])
    if g["fmt"] in ("both", "video"):
        rows.append([Btn(tr(lang, "btn_video"), callback_data=f"d:{VIDEO_QUALITY}:{vid}")])
    rows.append([Btn(tr(lang, "btn_lyrics"), callback_data=f"ly:{vid}"),
                 Btn("ℹ️ Info", callback_data=f"in:{vid}", style="primary")])
    rows.append([Btn("🔗 Share", callback_data=f"sh:{vid}", style="success")])
    return Markup(rows)


async def send_card(chat, meta: dict, g: dict, lang: str = "en"):
    try:
        m = await chat.send_photo(
            photo=thumb_url(meta["id"]), caption=card_text(meta, lang), parse_mode=HTML,
            reply_markup=card_markup(meta["id"], g, lang),
        )
    except TelegramError:
        m = await chat.send_message(card_text(meta, lang), parse_mode=HTML, reply_markup=card_markup(meta["id"], g, lang))
    autodel(m, g)


def split_mode(text: str):
    """'Tum Hi Ho a' -> ('Tum Hi Ho', 'a'); 'x v' -> ('x', 'v'); otherwise (text, None)."""
    parts = text.rsplit(None, 1)
    if len(parts) == 2 and parts[1].lower() in ("a", "v"):
        return parts[0].strip(), parts[1].lower()
    return text, None


def fmt_allowed(g: dict, is_audio: bool) -> bool:
    return g["fmt"] in ("both", "audio" if is_audio else "video")


_INFLIGHT: dict = {}


def search_key(text: str) -> str:
    return " ".join(text.lower().split())


def cached_results(text: str):
    """Instant (no network) lookup; None when not cached or expired."""
    hit = SEARCH_CACHE.get(search_key(text))
    if hit and time.time() - hit[0] < SEARCH_CACHE_TTL:
        return hit[1]
    return None


async def cached_search(text: str) -> list:
    key = search_key(text)
    hit = cached_results(text)
    if hit is not None:
        return hit
    task = _INFLIGHT.get(key)          # identical searches running right now share one request
    if task is None:
        task = asyncio.ensure_future(run_lookup(yt_search, text))
        _INFLIGHT[key] = task
        task.add_done_callback(lambda _t, k=key: _INFLIGHT.pop(k, None))
    results = await asyncio.shield(task)
    if results:
        if len(SEARCH_CACHE) >= SEARCH_CACHE_MAX:
            for k, _ in sorted(SEARCH_CACHE.items(), key=lambda kv: kv[1][0])[: SEARCH_CACHE_MAX // 5]:
                SEARCH_CACHE.pop(k, None)
        SEARCH_CACHE[key] = (time.time(), results)
    return results


def results_body(results: list, text: str, lang: str) -> tuple:
    lines = [tr(lang, "results_title", q=esc(text[:60]))]
    row = []
    for i, r in enumerate(results):
        remember(r)
        lines.append(f"{NUMS[i]} <b>{esc(r['title'][:55])}</b>\n      {meta_line(r)}")
        row.append(Btn(NUMS[i], callback_data=f"p:{r['id']}"))
    lines.append(tr(lang, "tap") + SPON)
    return "\n".join(lines), Markup([row])


async def handle_query(msg, text: str, context, list_only: bool = False):
    """list_only=True (/search): always show the numbered results list, never auto-download."""
    user = msg.from_user
    if not await group_ok(context, msg.chat, user, cooldown=True, msg=msg):
        return
    g = gsettings(msg.chat)
    lang = L(user.id)
    mode = None
    if not list_only:
        text, mode = split_mode(text)

    m = YT_RE.search(text)
    if m:
        vid = m.group(1)
        meta = META.get(vid)
        status = None
        if meta is None:
            status = await msg.reply_text(tr(lang, "fetching"), parse_mode=HTML)
            try:
                meta = await run_lookup(yt_meta, vid)
                remember(meta)
            except Exception as e:
                log.exception("meta failed for %s", vid)
                await report_error(context.bot, e, "link info", vid, user)
                await safe_edit(status, tr(lang, "bad_link"))
                return
        if mode:
            if status is None:
                status = await msg.reply_text(tr(lang, "fetching"), parse_mode=HTML)
            await direct_download(context, msg, status, vid, mode, g)
            return
        await send_card(msg.chat, meta, g, lang)
        if status:
            try:
                await status.delete()
            except TelegramError:
                pass
        return

    # Fast path: cached search -> answer with ONE message, no "Searching…" step at all.
    results = cached_results(text)
    status = None
    if results is None:
        # Start the search first, then send the status message while it runs (saves a round trip).
        search_task = asyncio.ensure_future(cached_search(text))
        try:
            status = await msg.reply_text(tr(lang, "searching", q=esc(text[:80])), parse_mode=HTML)
        except TelegramError:
            status = None
        try:
            results = await search_task
        except Exception as e:
            log.exception("search failed for %r", text)
            await report_error(context.bot, e, "search", "", user)
            if status:
                await safe_edit(status, tr(lang, "search_failed", hint=error_hint(e, lang)))
            else:
                await msg.reply_text(tr(lang, "search_failed", hint=error_hint(e, lang)), parse_mode=HTML)
            return

    if not results:
        if status:
            await safe_edit(status, tr(lang, "no_results"))
        else:
            await msg.reply_text(tr(lang, "no_results"), parse_mode=HTML)
        return

    if mode:
        remember(results[0])
        if status is None:
            status = await msg.reply_text(tr(lang, "fetching"), parse_mode=HTML)
        await direct_download(context, msg, status, results[0]["id"], mode, g)
        return

    body, kb = results_body(results, text, lang)
    if status:
        await safe_edit(status, body, kb)   # one edit, no extra photo upload / delete
        autodel(status, g)
    else:
        sent = await msg.reply_text(body, parse_mode=HTML, reply_markup=kb, disable_web_page_preview=True)
        autodel(sent, g)


async def cb_pick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    chat = q.message.chat
    if not await group_ok(context, chat, q.from_user, cq=q):
        return
    await q.answer()
    lang = L(q.from_user.id)
    vid = q.data.split(":", 1)[1]
    try:
        meta = META.get(vid) or await run_lookup(yt_meta, vid)
        remember(meta)
    except Exception as e:
        await report_error(context.bot, e, "pick info", vid, q.from_user)
        await safe_edit(q.message, tr(lang, "load_failed"))
        return
    await asyncio.gather(send_card(chat, meta, gsettings(chat), lang), q.message.delete(), return_exceptions=True)


# ───────────────────────── Lyrics ─────────────────────────
LYRICS_API = "https://lrclib.net/api/search"
_JUNK = re.compile(
    r"\(.*?\)|\[.*?\]|official\s*(music\s*)?(video|audio|lyric\s*video)?|lyrics?|lyrical|full\s*(video|song)|hd|4k|\|.*$",
    re.I,
)


def clean_title(t: str) -> str:
    return re.sub(r"\s{2,}", " ", _JUNK.sub(" ", t)).strip(" -–|") or t


async def fetch_lyrics(query: str):
    async with httpx.AsyncClient(timeout=15, headers={"User-Agent": "MusicBot (Telegram)"}) as c:
        r = await c.get(LYRICS_API, params={"q": query})
        r.raise_for_status()
        for item in r.json():
            if item.get("plainLyrics"):
                return item
    return None


async def send_lyrics(target, user, query: str, context):
    """target: a message to reply to."""
    lang = L(user.id)
    status = await target.reply_text(tr(lang, "lyr_search", q=esc(query[:80])), parse_mode=HTML)
    try:
        item = await fetch_lyrics(query)
    except Exception:
        log.exception("lyrics failed")
        await safe_edit(status, tr(lang, "lyr_fail"))
        return
    if not item:
        await safe_edit(status, tr(lang, "lyr_none"))
        return
    head = f"🎤 <b>{esc(item.get('trackName', query))}</b> — {esc(item.get('artistName', ''))}\n\n"
    text = item["plainLyrics"].strip()
    chunks = [text[i:i + 3600] for i in range(0, len(text), 3600)][:3]
    await safe_edit(status, head + esc(chunks[0]))
    for c in chunks[1:]:
        await target.reply_text(esc(c), parse_mode=HTML)


async def cmd_lyrics(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg, user = update.message, update.effective_user
    if not context.args:
        await msg.reply_text(tr(L(user.id), "lyr_usage"), parse_mode=HTML)
        return
    if not await group_ok(context, msg.chat, user, cooldown=True, msg=msg):
        return
    await send_lyrics(msg, user, " ".join(context.args).strip(), context)


async def cb_lyrics(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not await group_ok(context, q.message.chat, q.from_user, cq=q):
        return
    await q.answer()
    vid = q.data.split(":", 1)[1]
    meta = META.get(vid)
    if not meta:
        try:
            meta = await run_lookup(yt_meta, vid)
            remember(meta)
        except Exception:
            await q.message.chat.send_message(tr(L(q.from_user.id), "load_failed"))
            return
    query = clean_title(meta["title"])
    await send_lyrics(q.message, q.from_user, query, context)


# ───────────────────────── Stats ─────────────────────────
def user_total(uid: int) -> int:
    return sum(s["users"].get(str(uid), {}).get("n", 0) for s in db["gstats"].values())


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    lang = L(user.id)
    total = sum(s["n"] for s in db["gstats"].values())
    tot = db["tot"]
    today = db["daily"].get(time.strftime("%Y-%m-%d"), 0)
    songs: dict = {}
    for s in db["gstats"].values():
        for vid, x in s["songs"].items():
            e = songs.setdefault(vid, {"t": x["t"], "n": 0})
            e["n"] += x["n"]
    top = sorted(songs.values(), key=lambda x: -x["n"])[:5]

    lines = [
        tr(lang, "st_title"),
        f"👥 {tr(lang, 'st_users')}: <b>{len(_users)}</b>",
        f"⬇️ {tr(lang, 'st_total')}: <b>{total}</b>",
        f"📅 {tr(lang, 'st_today')}: <b>{today}</b>",
        f"🎵 {tr(lang, 'st_audio')}: <b>{tot.get('a', 0)}</b>  ·  🎬 {tr(lang, 'st_video')}: <b>{tot.get('v', 0)}</b>",
        "",
        f"👤 {tr(lang, 'st_you')}: <b>{user_total(user.id)}</b>",
        f"⭐ Premium: <b>{prem_label(user.id)}</b>",
        "",
        tr(lang, "st_top"),
    ]
    lines += [f"{i + 1}. {esc(x['t'])} — {x['n']}" for i, x in enumerate(top)] or [f"<i>{tr(lang, 'st_none')}</i>"]
    if is_admin(user.id):
        week = sum(v for k, v in db["daily"].items() if k >= time.strftime("%Y-%m-%d", time.localtime(time.time() - 6 * 86400)))
        lines += [
            "",
            "🛠 <b>Admin</b>",
            f"💬 Groups: <b>{len(db['groups'])}</b>",
            f"📆 Last 7 days: <b>{week}</b>",
            f"🚫 Banned (global): <b>{len(db['banned_global'])}</b>",
            f"🚦 Failure streak: <b>{ERR['streak']}</b>",
        ]
    await update.message.reply_text("\n".join(lines), parse_mode=HTML)


# ───────────────────────── Inline mode ─────────────────────────
async def on_inline(update: Update, context: ContextTypes.DEFAULT_TYPE):
    iq = update.inline_query
    user = iq.from_user
    bot_name = context.bot.username or ""
    lang = L(user.id)
    button = InlineQueryResultsButton(text=tr(lang, "inl_open"), start_parameter="start")
    q = iq.query.strip()
    if (user.id in db["banned_global"] and not is_admin(user.id)) or len(q) < 3:
        await iq.answer([], cache_time=5, button=button)
        return
    if not is_admin(user.id) and await missing_chats(context.bot, user.id, required_chats(None)):
        await iq.answer([], cache_time=5, button=InlineQueryResultsButton(text=tr(lang, "join_first"), start_parameter="start"))
        return
    try:
        results = await cached_search(q)
    except Exception as e:
        log.exception("inline search failed")
        await report_error(context.bot, e, "inline search", "", user)
        await iq.answer([], cache_time=5, button=button)
        return
    out = []
    for r in results:
        remember(r)
        kb = Markup([[
            Btn(tr(lang, "btn_audio"), url=f"https://t.me/{bot_name}?start=a_{r['id']}"),
            Btn(tr(lang, "btn_video"), url=f"https://t.me/{bot_name}?start=v_{r['id']}"),
        ]])
        views = f" · 👁 {fmt_views(r['views'])}" if r.get("views") else ""
        out.append(InlineQueryResultArticle(
            id=r["id"],
            title=r["title"][:80],
            description=f"{r['channel'][:30]} · {fmt_dur(r['duration'])}{views}",
            thumbnail_url=thumb_url(r["id"]),
            input_message_content=InputTextMessageContent(
                f"🎶 <b>{esc(r['title'][:90])}</b>\n{meta_line(r)}{CREDIT_HTML}", parse_mode=HTML,
            ),
            reply_markup=kb,
        ))
    await iq.answer(out, cache_time=300, button=button)


# ───────────────────────── Song recognition (AudD) ─────────────────────────
def pick_media(m):
    if not m:
        return None
    media = m.voice or m.audio or m.video or m.video_note
    if not media and m.document and (m.document.mime_type or "").startswith(("audio/", "video/")):
        media = m.document
    return media


def make_clip(src: str, dst: str):
    subprocess.run(
        ["ffmpeg", "-y", "-i", src, "-t", "20", "-vn", "-ac", "1", "-ar", "16000", "-b:a", "64k", dst],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=90,
    )


async def audd_identify(path: str) -> dict:
    async with httpx.AsyncClient(timeout=45) as c:
        with open(path, "rb") as f:
            r = await c.post(
                "https://api.audd.io/", data={"api_token": AUDD_TOKEN},
                files={"file": ("clip.mp3", f, "audio/mpeg")},
            )
    r.raise_for_status()
    return r.json()


def recog_used(uid: int) -> int:
    e = db["recog"].get(str(uid))
    return e["n"] if e and e["d"] == time.strftime("%Y-%m-%d") else 0


def recog_count(uid: int):
    day = time.strftime("%Y-%m-%d")
    e = db["recog"].get(str(uid))
    if not e or e["d"] != day:
        e = db["recog"][str(uid)] = {"d": day, "n": 0}
    e["n"] += 1
    touch()


async def recognize_media(msg, source, user, context):
    """msg: the message to answer; source: the message holding the audio/video."""
    lang = L(user.id)
    media = pick_media(source)
    if not media:
        await msg.reply_text(tr(lang, "rec_usage"), parse_mode=HTML)
        return
    if not await group_ok(context, msg.chat, user, cooldown=True, msg=msg):
        return
    if not is_admin(user.id) and recog_used(user.id) >= recog_limit(user.id):
        await msg.reply_text(tr(lang, "rec_limit", n=recog_limit(user.id)) + "\n\n🎁 /invite  ·  ⭐ /premium", parse_mode=HTML)
        return
    if (media.file_size or 0) > MAX_RECOGNIZE_FILE:
        await msg.reply_text(tr(lang, "rec_big"))
        return

    g = gsettings(msg.chat)
    status = await msg.reply_text(tr(lang, "rec_listening"), parse_mode=HTML)
    outdir = tempfile.mkdtemp(prefix="musicbot_rec_")
    try:
        f = await context.bot.get_file(media.file_id)
        src, clip = os.path.join(outdir, "in"), os.path.join(outdir, "clip.mp3")
        await f.download_to_drive(src)
        await asyncio.to_thread(make_clip, src, clip)
        data = await audd_identify(clip)
        if data.get("status") != "success":
            raise RuntimeError(f"AudD: {data.get('error')}")
    except Exception as e:
        log.exception("recognition failed")
        if time.time() - ERR["last"].get("recog", 0) > ALERT_COOLDOWN:
            ERR["last"]["recog"] = time.time()
            await notify_admins(
                context.bot,
                "🚨 <b>Song recognition is failing</b>\n\n"
                f"<code>{esc(str(e)[:300])}</code>\n\n"
                "🛠 Check <b>AUDD_TOKEN</b> in .env (invalid or out of free requests), and that ffmpeg is installed.",
            )
        await safe_edit(status, tr(lang, "rec_fail"))
        return
    finally:
        shutil.rmtree(outdir, ignore_errors=True)

    recog_count(user.id)
    res = data.get("result")
    if not res or not res.get("title"):
        await safe_edit(status, tr(lang, "rec_none"))
        return

    album = f"\n💿 {esc(res['album'])}" if res.get("album") else ""
    await safe_edit(status, tr(lang, "rec_found", title=esc(res["title"][:90]), artist=esc(res.get("artist", "")[:60]), album=album))
    try:
        results = await cached_search(f"{res.get('artist', '')} {res['title']}".strip())
    except Exception:
        results = []
    if results:
        remember(results[0])
        await send_card(msg.chat, results[0], g, lang)


async def cmd_find(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    await recognize_media(msg, msg.reply_to_message, update.effective_user, context)


# ───────────────────────── Unknown commands -> suggest the right one ─────────────────────────
USER_CMDS = ["start", "help", "id", "song", "search", "find", "lyrics", "artist", "follow", "following", "invite",
             "premium", "brand", "lang", "stats", "ban", "unban", "warn"]
ADMIN_CMDS = ["admin", "groups", "approve", "unapprove", "givepremium", "removepremium"]
CMD_EXAMPLE = {
    "start": "/start", "help": "/help", "id": "/id", "lang": "/lang", "stats": "/stats",
    "song": "/song Tum Hi Ho a", "search": "/search Tum Hi Ho", "find": "/find (reply to audio)", "lyrics": "/lyrics Tum Hi Ho",
    "ban": "/ban (reply)", "unban": "/unban (reply)", "warn": "/warn (reply)",
    "admin": "/admin", "groups": "/groups", "approve": "/approve 12345", "unapprove": "/unapprove 12345",
}
CMD_EXAMPLE.update({
    "artist": "/artist Karan Aujla", "follow": "/follow Karan Aujla", "following": "/following", "invite": "/invite",
    "premium": "/premium", "brand": "/brand My group footer", "givepremium": "/givepremium 12345 30",
    "removepremium": "/removepremium 12345",
})


async def unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg, user = update.message, update.effective_user
    if not msg or not msg.text or not user:
        return
    cmd, _, target = msg.text.split()[0][1:].partition("@")
    if target and target.lower() != (context.bot.username or "").lower():
        return  # addressed to another bot
    cmd = cmd.lower()
    pool = USER_CMDS + (ADMIN_CMDS if is_admin(user.id) else [])
    private = msg.chat.type == "private"
    lang = L(user.id)
    if cmd in pool:
        return
    match = difflib.get_close_matches(cmd, pool, n=1, cutoff=0.6 if private else 0.75)
    if match:
        sug = match[0]
        await msg.reply_text(
            tr(lang, "unk_suggest", cmd=esc(cmd[:30]), sug=sug, ex=esc(CMD_EXAMPLE[sug])), parse_mode=HTML
        )
    elif private:
        lst = "\n".join(f"• <code>{esc(CMD_EXAMPLE[c])}</code>" for c in USER_CMDS if c not in ("ban", "unban", "warn"))
        await msg.reply_text(tr(lang, "unk_list", cmd=esc(cmd[:30]), lst=lst), parse_mode=HTML)


# ───────────────────────── Download ─────────────────────────
async def queued_download(card, lang: str, title: str, running_text: str, fn, *args, prio: int = 1):
    """Run a blocking download under the concurrency limit. prio 0 (premium/admin) is served before prio 1."""
    ticket = SLOTS.ticket(prio)
    shown = {"pos": None}

    async def watch():
        while True:
            pos = SLOTS.position(ticket)
            if pos is not None and pos != shown["pos"]:
                shown["pos"] = pos
                await safe_edit(card, tr(lang, "queue", n=pos, title=esc(title[:60])))
            await asyncio.sleep(1.5)

    watcher = asyncio.create_task(watch())
    try:
        await SLOTS.acquire(ticket)
    except BaseException:
        watcher.cancel()
        raise
    try:
        watcher.cancel()
        try:
            await watcher
        except asyncio.CancelledError:
            pass
        if shown["pos"] is not None:
            await safe_edit(card, running_text)
        return await asyncio.get_running_loop().run_in_executor(DL_POOL, fn, *args)
    finally:
        SLOTS.release()


async def direct_download(context, msg, status, vid: str, mode: str, g: dict):
    """/song <name|link> a|v  -> skip the picker and download straight away."""
    is_audio = mode == "a"
    if not fmt_allowed(g, is_audio):
        await safe_edit(status, tr(L(msg.from_user.id), "disabled"))
        return
    await run_download(context, status.chat, msg.from_user, status, vid, is_audio, g)


async def cb_download(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    card, user = q.message, q.from_user
    chat = card.chat
    if not await group_ok(context, chat, user, cq=q):
        return
    g = gsettings(chat)
    _, fmt, vid = q.data.split(":")
    is_audio = fmt == "a"   # any video button (even an old 360/720 card) is served at 480p

    if not fmt_allowed(g, is_audio):
        await q.answer(tr(L(user.id), "disabled"), show_alert=True)
        return
    await q.answer("⏳")
    await run_download(context, chat, user, card, vid, is_audio, g)


async def run_download(context, chat, user, card, vid: str, is_audio: bool, g: dict):
    fmt = "a" if is_audio else str(VIDEO_QUALITY)
    lang = L(user.id)

    meta = META.get(vid)
    if not meta:
        try:
            meta = await run_lookup(yt_meta, vid)
            remember(meta)
        except Exception as e:
            await report_error(context.bot, e, "download info", vid, user)
            await safe_edit(card, tr(lang, "load_failed"))
            return

    label = tr(lang, "label_audio" if is_audio else "label_video")
    me = context.bot.username or "bot"
    caption = f"🎶 <b>{esc(meta['title'][:150])}</b>\n{label}\n\n🤖 @{esc(me)}"
    key = f"{vid}:{fmt}"
    kind = "a" if is_audio else "v"

    async def send_file(media, thumb=None, extra=None):
        extra = extra or {}
        if is_audio:
            return await chat.send_audio(
                audio=media, title=meta["title"][:64], performer=meta["channel"][:64],
                duration=int(meta["duration"]), caption=caption, parse_mode=HTML,
                thumbnail=thumb, read_timeout=180, write_timeout=300,
            )
        return await chat.send_video(
            video=media, duration=int(meta["duration"]), caption=caption, parse_mode=HTML,
            supports_streaming=True, thumbnail=thumb, read_timeout=180, write_timeout=300, **extra,
        )

    cached = db["cache"].get(key)
    if cached:
        try:
            sent = await send_file(cached)
            autodel(sent, g)
            record(chat, user, meta, kind)
            await card.delete()
            return
        except TelegramError:
            db["cache"].pop(key, None)

    default_limit = MAX_AUDIO_DURATION if is_audio else MAX_VIDEO_DURATION
    limit = min(default_limit, g["dur"] * 60) if g["dur"] else default_limit
    if meta["duration"] and meta["duration"] > limit:
        await safe_edit(card, tr(lang, "too_long", n=limit // 60), card_markup(vid, g, lang))
        return

    running = tr(lang, "downloading", label=label, title=esc(meta["title"][:60]))
    await safe_edit(card, running)
    action = ChatAction.UPLOAD_VOICE if is_audio else ChatAction.UPLOAD_VIDEO
    keeper = asyncio.create_task(keep_action(chat, action))
    outdir = tempfile.mkdtemp(prefix="musicbot_")
    try:
        thumb_task = asyncio.create_task(fetch_thumb(vid))
        data = await queued_download(card, lang, meta["title"], running, yt_download, vid, fmt, outdir,
                                     prio=0 if is_premium(user.id) else 1)
        size = os.path.getsize(data["path"])
        if size > MAX_FILE_SIZE:
            await safe_edit(card, tr(lang, "too_big", mb=size // (1024 * 1024)), card_markup(vid, g, lang))
            return
        await safe_edit(card, tr(lang, "uploading", label=label))
        thumb = await thumb_task
        extra = {}
        if not is_audio and data.get("width") and data.get("height"):
            extra = {"width": data["width"], "height": data["height"]}
        with open(data["path"], "rb") as f:
            sent = await send_file(f, thumb, extra)
        media = sent.audio if is_audio else sent.video
        if media:
            db["cache"][key] = media.file_id
            touch()
        autodel(sent, g)
        record(chat, user, meta, kind)
        await report_ok(context.bot)
        await card.delete()
    except Exception as e:
        log.exception("download failed %s", key)
        await report_error(context.bot, e, f"download {key}", vid, user)
        await safe_edit(card, tr(lang, "failed", hint=error_hint(e, lang)), card_markup(vid, g, lang))
    finally:
        keeper.cancel()
        shutil.rmtree(outdir, ignore_errors=True)


# ───────────────────────── Admin panel (force-join chats) ─────────────────────────
def admin_view():
    on = db["enabled"]
    lines = [
        "🛠 <b>Admin Panel</b>\n",
        f"🔐 Force-join: <b>{'ON ✅' if on else 'OFF ❌'}</b>",
        f"👥 Users: <b>{len(_users)}</b>   💾 Cached files: <b>{len(db['cache'])}</b>\n",
        f"📋 <b>Required chats ({len(db['chats'])}):</b>",
    ]
    if db["chats"]:
        for i, c in enumerate(db["chats"], 1):
            icon = "📢" if c.get("type") == "channel" else "👥"
            lines.append(f"{i}. {icon} {esc(c['title'])}")
    else:
        lines.append("<i>None added yet.</i>")
    kb = [
        [Btn("➕ Add channel / group", callback_data="adm:add")],
        [Btn(f"👥 Manage Groups ({len(db['groups'])})", callback_data="grp:list:0")],
        [Btn("⭐ Premium & Sponsor", callback_data="adm:prem", style="success")],
        [Btn(f"🔐 Force-join: {'ON ✅' if on else 'OFF ❌'}  (tap to toggle)", callback_data="adm:tog")],
    ]
    for c in db["chats"]:
        kb.append([Btn(f"🗑 Remove · {c['title'][:28]}", callback_data=f"adm:rm:{c['id']}")])
    kb.append([Btn("🔄 Refresh", callback_data="adm:home")])
    return "\n".join(lines), Markup(kb)


def clear_flags(ud):
    for k in ("await_chat", "await_gsearch", "await_bc", "await_set"):
        ud.pop(k, None)


async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Admins only.")
        return
    if update.effective_chat.type != "private":
        await update.message.reply_text("🛠 Please use /admin in private chat with me.")
        return
    clear_flags(context.user_data)
    text, kb = admin_view()
    await update.message.reply_text(text, parse_mode=HTML, reply_markup=kb)


async def cb_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not is_admin(q.from_user.id):
        await q.answer("⛔ Admins only", show_alert=True)
        return
    await q.answer()
    parts = q.data.split(":")
    action = parts[1]
    clear_flags(context.user_data)

    if action in ("prem", "set", "sp", "pl"):
        await cb_admin_prem(update, context, parts)
        return

    if action == "add":
        context.user_data["await_chat"] = True
        await safe_edit(
            q.message,
            "➕ <b>Add channel / group</b>\n\n"
            "Send me one of these:\n"
            "• <code>@username</code> of the channel/group\n"
            "• The chat ID (e.g. <code>-1001234567890</code>)\n"
            "• Or <b>forward</b> any message from the channel\n\n"
            "⚠️ Make sure I'm added there as <b>admin</b> first.",
            Markup([[Btn("❌ Cancel", callback_data="adm:home")]]),
        )
        return
    if action == "tog":
        db["enabled"] = not db["enabled"]
        save_db()
    elif action == "rm":
        cid = int(parts[2])
        db["chats"] = [c for c in db["chats"] if c["id"] != cid]
        for g in db["groups"].values():
            if cid in g["force"]:
                g["force"].remove(cid)
        save_db()
    text, kb = admin_view()
    await safe_edit(q.message, text, kb)


def parse_ref(text: str):
    t = text.strip()
    if re.fullmatch(r"-?\d+", t):
        return int(t)
    if "+" in t or "joinchat" in t:
        return None
    m = re.search(r"(?:t\.me/|@)([A-Za-z][A-Za-z0-9_]{3,})", t) or re.fullmatch(r"([A-Za-z][A-Za-z0-9_]{3,})", t)
    return f"@{m.group(1)}" if m else None


async def admin_add_flow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg, bot = update.message, context.bot
    fo = getattr(msg, "forward_origin", None)
    ref = fo.chat.id if fo is not None and getattr(fo, "chat", None) else (parse_ref(msg.text) if msg.text else None)
    if ref is None:
        await msg.reply_text("❌ Invalid. Send @username, chat ID, or forward a message from the channel.")
        return
    try:
        chat = await bot.get_chat(ref)
    except TelegramError:
        await msg.reply_text("❌ I can't find that chat. Add me as admin there, then try again.")
        return
    try:
        me = await bot.get_chat_member(chat.id, bot.id)
    except TelegramError:
        me = None
    if not me or me.status not in ("administrator", "creator"):
        await msg.reply_text("❌ I'm not an admin there. Make me admin, then send it again.")
        return

    link = f"https://t.me/{chat.username}" if chat.username else getattr(chat, "invite_link", None)
    if not link:
        try:
            link = await bot.export_chat_invite_link(chat.id)
        except TelegramError:
            link = None

    entry = {
        "id": chat.id, "title": chat.title or str(chat.id), "link": link,
        "type": "channel" if chat.type == "channel" else "group",
    }
    db["chats"] = [c for c in db["chats"] if c["id"] != chat.id] + [entry]
    save_db()
    context.user_data.pop("await_chat", None)

    warn = "" if link else "\n⚠️ No invite link available — give me the 'Invite users' permission."
    text, kb = admin_view()
    await msg.reply_text(f"✅ <b>Added:</b> {esc(entry['title'])}{warn}\n\n{text}", parse_mode=HTML, reply_markup=kb)


# ───────────────────────── Group manager ─────────────────────────
SORTS = ["name", "activity", "downloads", "members"]
SORT_LABEL = {"name": "Name", "activity": "Activity", "downloads": "Downloads", "members": "Members"}
FMT_LABEL = {"both": "Audio + Video", "audio": "Audio only", "video": "Video only"}
CYCLES = {
    "fmt": ["both", "audio", "video"],
    "qual": [720, 480, 360],
    "dur": [0, 5, 10, 15],
    "cool": [0, 10, 30, 60],
    "del": [0, 30, 120, 600],
}
TOGGLES = {"adm", "txt"}
MEDALS = ["🥇", "🥈", "🥉"]


def gdl(gid: str) -> int:
    return db["gstats"].get(gid, {}).get("n", 0)


def filtered_groups(ud) -> list:
    items = list(db["groups"].items())
    qs = ud.get("gq")
    if qs:
        items = [kv for kv in items if qs.lower() in kv[1]["title"].lower()]
    sort = ud.get("gsort", "name")
    if sort == "name":
        items.sort(key=lambda kv: kv[1]["title"].lower())
    elif sort == "activity":
        items.sort(key=lambda kv: -kv[1]["last"])
    elif sort == "downloads":
        items.sort(key=lambda kv: -gdl(kv[0]))
    else:
        items.sort(key=lambda kv: -kv[1]["members"])
    return items


def groups_list_view(page: int, ud):
    items = filtered_groups(ud)
    total = len(items)
    pages = max(1, (total + GROUPS_PER_PAGE - 1) // GROUPS_PER_PAGE)
    page = min(max(page, 0), pages - 1)
    sort = ud.get("gsort", "name")
    blocked = sum(1 for _, g in items if g["blocked"])
    text = (
        "👥 <b>Group Manager</b>\n\n"
        f"Groups: <b>{len(db['groups'])}</b>   🚫 Blocked: <b>{blocked}</b>   "
        f"🛡 Approval: <b>{'ON' if db['approval'] else 'OFF'}</b>\n"
        f"↕️ Sort: <b>{SORT_LABEL[sort]}</b>   📄 Page {page + 1}/{pages}\n"
    )
    if ud.get("gq"):
        text += f"🔍 Search: <b>{esc(ud['gq'])}</b> ({total} found)\n"
    text += (
        "\n👇 <i>Tap a group to manage it</i>" if total else "\n<i>No groups to show.</i>"
    )
    text += "\n\n<i>Commands: /ban /unban /warn (reply) · /approve /unapprove &lt;id&gt;</i>"

    kb = []
    for gid, g in items[page * GROUPS_PER_PAGE:(page + 1) * GROUPS_PER_PAGE]:
        extra = ""
        if sort == "downloads":
            extra = f" · {gdl(gid)}⬇"
        elif sort == "members":
            extra = f" · {g['members']}👤"
        elif sort == "activity":
            extra = f" · {ago(g['last'])}"
        kb.append([Btn(f"{'🚫' if g['blocked'] else '🟢'} {g['title'][:28]}{extra}", callback_data=f"grp:v:{gid}")])
    nav = []
    if page > 0:
        nav.append(Btn("⬅️ Prev", callback_data=f"grp:list:{page - 1}"))
    if page < pages - 1:
        nav.append(Btn("Next ➡️", callback_data=f"grp:list:{page + 1}"))
    if nav:
        kb.append(nav)
    kb.append([
        Btn(f"↕️ {SORT_LABEL[sort]}", callback_data="grp:sort"),
        Btn("🔍 Search", callback_data="grp:sch"),
    ] + ([Btn("❌ Clear", callback_data="grp:clr")] if ud.get("gq") else []))
    kb.append([Btn("🏆 Top groups", callback_data="grp:top"), Btn("📣 Broadcast", callback_data="grp:bcast")])
    kb.append([
        Btn("🧹 Clean inactive", callback_data="grp:inact"),
        Btn(f"🛡 Approval: {'ON' if db['approval'] else 'OFF'}", callback_data="grp:apt"),
    ])
    kb.append([Btn("🔄 Refresh", callback_data=f"grp:list:{page}"), Btn("🛠 Admin", callback_data="adm:home")])
    return text, Markup(kb)


async def detail_view(bot, gid: str):
    g = db["groups"].get(gid)
    if not g:
        return None
    try:
        g["members"] = await bot.get_chat_member_count(int(gid))
        touch()
    except TelegramError:
        pass
    banned = len(db["banned_groups"].get(gid, []))
    text = (
        f"👥 <b>{esc(g['title'])}</b>\n\n"
        f"🆔 <code>{gid}</code>\n"
        f"👤 Members: <b>{g['members'] or '?'}</b>\n"
        f"📶 Status: <b>{'🚫 Blocked' if g['blocked'] else '🟢 Active'}</b>\n"
        f"⬇️ Downloads: <b>{gdl(gid)}</b>\n"
        f"🕒 Last active: <b>{ago(g['last'])} ago</b>\n"
        f"⛔ Banned users: <b>{banned}</b>"
    )
    kb = Markup([
        [Btn("✅ Unblock" if g["blocked"] else "🚫 Block", callback_data=f"grp:blk:{gid}"),
         Btn("⚙️ Settings", callback_data=f"grp:set:{gid}")],
        [Btn("📊 Stats", callback_data=f"grp:st:{gid}"), Btn("🚪 Leave", callback_data=f"grp:leave:{gid}")],
        [Btn("⬅️ Back", callback_data="grp:list:0")],
    ])
    return text, kb


def settings_view(gid: str):
    g = db["groups"][gid]
    on = lambda v: "ON ✅" if v else "OFF"
    text = f"⚙️ <b>Settings — {esc(g['title'])}</b>\n\n<i>Tap an option to change it.</i>"
    kb = Markup([
        [Btn(f"🎚 Formats: {FMT_LABEL[g['fmt']]}", callback_data=f"grp:t:{gid}:fmt")],
        [Btn(f"📺 Video quality: {VIDEO_QUALITY}p (fixed)", callback_data=f"grp:t:{gid}:fmt")],
        [Btn(f"⏱ Max length: {str(g['dur']) + ' min' if g['dur'] else 'default'}", callback_data=f"grp:t:{gid}:dur")],
        [Btn(f"🧊 Cooldown: {str(g['cool']) + 's' if g['cool'] else 'off'}", callback_data=f"grp:t:{gid}:cool")],
        [Btn(f"👮 Admins-only: {on(g['adm'])}", callback_data=f"grp:t:{gid}:adm")],
        [Btn(f"🧹 Auto-delete: {str(g['del']) + 's' if g['del'] else 'off'}", callback_data=f"grp:t:{gid}:del")],
        [Btn(f"💬 Text search: {on(g['txt'])}", callback_data=f"grp:t:{gid}:txt")],
        [Btn(f"🔗 Force-join here ({len(g['force'])})", callback_data=f"grp:fj:{gid}")],
        [Btn("⬅️ Back", callback_data=f"grp:v:{gid}")],
    ])
    return text, kb


def force_view(gid: str):
    g = db["groups"][gid]
    if not db["chats"]:
        text = "🔗 <b>Force-join here</b>\n\n<i>No channels/groups added yet. Add them in /admin first.</i>"
        kb = [[Btn("⬅️ Back", callback_data=f"grp:set:{gid}")]]
        return text, Markup(kb)
    text = (
        f"🔗 <b>Force-join in {esc(g['title'])}</b>\n\n"
        "Selected chats are required <b>in this group</b>, even if global force-join is off."
    )
    kb = [[Btn(f"{'☑️' if c['id'] in g['force'] else '⬜'} {c['title'][:30]}",
               callback_data=f"grp:fjt:{gid}:{c['id']}")] for c in db["chats"]]
    kb.append([Btn("⬅️ Back", callback_data=f"grp:set:{gid}")])
    return text, Markup(kb)


def stats_view(gid: str):
    g = db["groups"][gid]
    s = db["gstats"].get(gid, {"n": 0, "users": {}, "songs": {}})
    tu = sorted(s["users"].values(), key=lambda u: -u["n"])[:3]
    ts = sorted(s["songs"].values(), key=lambda x: -x["n"])[:3]
    lines = [f"📊 <b>Stats — {esc(g['title'])}</b>\n", f"⬇️ Total downloads: <b>{s['n']}</b>\n", "👤 <b>Top users</b>"]
    lines += [f"{MEDALS[i]} {esc(u['name'])} — {u['n']}" for i, u in enumerate(tu)] or ["<i>No data yet</i>"]
    lines.append("\n🎵 <b>Top songs</b>")
    lines += [f"{MEDALS[i]} {esc(x['t'])} — {x['n']}" for i, x in enumerate(ts)] or ["<i>No data yet</i>"]
    return "\n".join(lines), Markup([[Btn("⬅️ Back", callback_data=f"grp:v:{gid}")]])


def top_view():
    rows = sorted(((gid, g) for gid, g in db["groups"].items()), key=lambda kv: -gdl(kv[0]))[:10]
    total = sum(s["n"] for s in db["gstats"].values())
    lines = ["🏆 <b>Top groups</b>\n", f"⬇️ Total downloads (all chats): <b>{total}</b>\n"]
    for i, (gid, g) in enumerate(rows):
        lines.append(f"{MEDALS[i] if i < 3 else str(i + 1) + '.'} {esc(g['title'][:34])} — {gdl(gid)}")
    if not rows:
        lines.append("<i>No groups yet.</i>")
    songs = {}
    for s in db["gstats"].values():
        for vid, x in s["songs"].items():
            e = songs.setdefault(vid, {"t": x["t"], "n": 0})
            e["n"] += x["n"]
    lines.append("\n🎵 <b>Most requested songs</b>")
    top = sorted(songs.values(), key=lambda x: -x["n"])[:5]
    lines += [f"{i + 1}. {esc(x['t'])} — {x['n']}" for i, x in enumerate(top)] or ["<i>No data yet</i>"]
    return "\n".join(lines), Markup([[Btn("⬅️ Back", callback_data="grp:list:0")]])


def bc_confirm_view(ud):
    n = len([1 for g in db["groups"].values() if not g["blocked"]])
    sel = len(ud["bc"]["sel"])
    text = "📣 <b>Broadcast ready</b>\n\nYour message will be copied exactly as you sent it.\nChoose who gets it 👇"
    kb = Markup([
        [Btn(f"📤 Send to ALL groups ({n})", callback_data="grp:bsa")],
        [Btn(f"☑️ Select groups ({sel} selected)", callback_data="grp:bp:0")],
        [Btn("❌ Cancel", callback_data="grp:list:0")],
    ])
    return text, kb


def bc_select_view(ud, page: int):
    items = sorted(db["groups"].items(), key=lambda kv: kv[1]["title"].lower())
    pages = max(1, (len(items) + GROUPS_PER_PAGE - 1) // GROUPS_PER_PAGE)
    page = min(max(page, 0), pages - 1)
    sel = set(ud["bc"]["sel"])
    kb = []
    for gid, g in items[page * GROUPS_PER_PAGE:(page + 1) * GROUPS_PER_PAGE]:
        kb.append([Btn(f"{'☑️' if gid in sel else '⬜'} {g['title'][:32]}", callback_data=f"grp:bt:{gid}:{page}")])
    nav = []
    if page > 0:
        nav.append(Btn("⬅️", callback_data=f"grp:bp:{page - 1}"))
    if page < pages - 1:
        nav.append(Btn("➡️", callback_data=f"grp:bp:{page + 1}"))
    if nav:
        kb.append(nav)
    kb.append([Btn(f"📤 Send to selected ({len(sel)})", callback_data="grp:bss")])
    kb.append([Btn("⬅️ Back", callback_data="grp:bv")])
    return f"☑️ <b>Select groups</b>\nPage {page + 1}/{pages}", Markup(kb)


async def run_broadcast(bot, ids, bc) -> tuple:
    ok = fail = 0
    for gid in ids:
        for attempt in (1, 2):
            try:
                await bot.copy_message(int(gid), bc["cid"], bc["mid"])
                ok += 1
            except RetryAfter as e:
                await asyncio.sleep(e.retry_after + 1)
                if attempt == 1:
                    continue
                fail += 1
            except Forbidden:
                fail += 1
                db["groups"].pop(gid, None)
            except TelegramError:
                fail += 1
            break
        await asyncio.sleep(0.05)
    save_db()
    return ok, fail


async def cmd_groups(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Admins only.")
        return
    if update.effective_chat.type != "private":
        await update.message.reply_text("👥 Please use /groups in private chat with me.")
        return
    clear_flags(context.user_data)
    if context.args:
        context.user_data["gq"] = " ".join(context.args)
    text, kb = groups_list_view(0, context.user_data)
    await update.message.reply_text(text, parse_mode=HTML, reply_markup=kb)


async def cb_groups(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not is_admin(q.from_user.id):
        await q.answer("⛔ Admins only", show_alert=True)
        return
    ud, bot, msg = context.user_data, context.bot, q.message
    parts = q.data.split(":")
    act = parts[1]
    if act not in ("sch", "bcast"):
        ud.pop("await_gsearch", None)
        ud.pop("await_bc", None)

    async def show(view):
        await safe_edit(msg, *view)

    async def to_list():
        await show(groups_list_view(0, ud))

    # ── list-level actions
    if act == "list":
        await q.answer()
        await show(groups_list_view(int(parts[2]), ud))
    elif act == "sort":
        await q.answer()
        ud["gsort"] = SORTS[(SORTS.index(ud.get("gsort", "name")) + 1) % len(SORTS)]
        await to_list()
    elif act == "sch":
        await q.answer()
        ud["await_gsearch"] = True
        await show(("🔍 <b>Search groups</b>\n\nSend part of the group name.",
                    Markup([[Btn("❌ Cancel", callback_data="grp:list:0")]])))
    elif act == "clr":
        await q.answer()
        ud.pop("gq", None)
        await to_list()
    elif act == "top":
        await q.answer()
        await show(top_view())
    elif act == "apt":
        db["approval"] = not db["approval"]
        save_db()
        await q.answer("🛡 Approval ON" if db["approval"] else "🛡 Approval OFF")
        await to_list()
    elif act == "ap":
        uid = int(parts[2])
        if uid not in db["approved"]:
            db["approved"].append(uid)
            save_db()
        await q.answer("✅ User approved")
        await safe_edit(msg, f"{msg.text_html}\n\n✅ <b>User approved.</b>", None)

    # ── inactive cleanup
    elif act == "inact":
        await q.answer()
        cutoff = time.time() - INACTIVE_DAYS * 86400
        n = sum(1 for g in db["groups"].values() if g["last"] < cutoff)
        kb = [[Btn(f"🧹 Leave {n} group(s)", callback_data="grp:inacty")]] if n else []
        kb.append([Btn("⬅️ Back", callback_data="grp:list:0")])
        await show((f"🧹 <b>Clean inactive groups</b>\n\nGroups with no activity for <b>{INACTIVE_DAYS} days</b>: <b>{n}</b>",
                    Markup(kb)))
    elif act == "inacty":
        cutoff = time.time() - INACTIVE_DAYS * 86400
        ids = [gid for gid, g in db["groups"].items() if g["last"] < cutoff]
        await q.answer("🧹 Cleaning…")
        for gid in ids:
            try:
                await bot.leave_chat(int(gid))
            except TelegramError:
                pass
            db["groups"].pop(gid, None)
            await asyncio.sleep(0.05)
        save_db()
        await show((f"✅ Left <b>{len(ids)}</b> inactive group(s).",
                    Markup([[Btn("⬅️ Back", callback_data="grp:list:0")]])))

    # ── broadcast
    elif act == "bcast":
        await q.answer()
        ud["await_bc"] = True
        ud.pop("bc", None)
        await show(("📣 <b>Broadcast</b>\n\nSend the message you want to broadcast (text, photo, video…).",
                    Markup([[Btn("❌ Cancel", callback_data="grp:list:0")]])))
    elif act in ("bp", "bt", "bv", "bsa", "bss"):
        if "bc" not in ud:
            await q.answer("Start a broadcast first", show_alert=True)
            return
        if act == "bt":
            sel = ud["bc"]["sel"]
            gid = parts[2]
            sel.remove(gid) if gid in sel else sel.append(gid)
            await q.answer()
            await show(bc_select_view(ud, int(parts[3])))
        elif act == "bp":
            await q.answer()
            await show(bc_select_view(ud, int(parts[2])))
        elif act == "bv":
            await q.answer()
            await show(bc_confirm_view(ud))
        else:
            ids = ([gid for gid, g in db["groups"].items() if not g["blocked"]] if act == "bsa"
                   else [gid for gid in ud["bc"]["sel"] if gid in db["groups"]])
            if not ids:
                await q.answer("No groups selected", show_alert=True)
                return
            await q.answer()
            await show((f"📤 <b>Sending to {len(ids)} group(s)…</b>", None))
            ok, fail = await run_broadcast(bot, ids, ud.pop("bc"))
            await show((f"✅ <b>Broadcast done</b>\n\nSent: <b>{ok}</b>\nFailed: <b>{fail}</b>",
                        Markup([[Btn("⬅️ Back", callback_data="grp:list:0")]])))

    # ── per-group actions
    elif act in ("v", "blk", "leave", "set", "t", "fj", "fjt", "st"):
        gid = parts[2]
        g = db["groups"].get(gid)
        if not g:
            await q.answer("Group no longer exists", show_alert=True)
            await to_list()
            return
        if act == "v":
            await q.answer()
            await show(await detail_view(bot, gid))
        elif act == "blk":
            g["blocked"] = not g["blocked"]
            save_db()
            await q.answer("🚫 Blocked" if g["blocked"] else "✅ Unblocked")
            await show(await detail_view(bot, gid))
        elif act == "leave":
            db["groups"].pop(gid, None)
            save_db()
            try:
                await bot.leave_chat(int(gid))
                await q.answer("🚪 Left the group", show_alert=True)
            except TelegramError:
                await q.answer("⚠️ Couldn't leave (already removed?)", show_alert=True)
            await to_list()
        elif act == "set":
            await q.answer()
            await show(settings_view(gid))
        elif act == "t":
            key = parts[3]
            if key in CYCLES:
                vals = CYCLES[key]
                g[key] = vals[(vals.index(g[key]) + 1) % len(vals)] if g[key] in vals else vals[0]
            elif key in TOGGLES:
                g[key] = not g[key]
            save_db()
            await q.answer("✅ Saved")
            await show(settings_view(gid))
        elif act == "fj":
            await q.answer()
            await show(force_view(gid))
        elif act == "fjt":
            cid = int(parts[3])
            g["force"].remove(cid) if cid in g["force"] else g["force"].append(cid)
            save_db()
            await q.answer("✅ Saved")
            await show(force_view(gid))
        elif act == "st":
            await q.answer()
            await show(stats_view(gid))
    else:
        await q.answer()


# ───────────────────────── Bot added / removed + approval ─────────────────────────
async def on_my_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ev = update.my_chat_member
    chat, by = ev.chat, ev.from_user
    if chat.type not in GROUP_TYPES:
        return
    new, old = ev.new_chat_member.status, ev.old_chat_member.status
    who = f"<a href=\"tg://user?id={by.id}\">{esc(by.full_name)}</a> (<code>{by.id}</code>)"

    if new in ("left", "kicked"):
        g = db["groups"].pop(str(chat.id), None)
        if g:
            save_db()
            await notify_admins(context.bot, f"➖ <b>Removed from group</b>\n{esc(g['title'])}\nBy: {who}")
        return

    if old in ("left", "kicked"):  # freshly added
        allowed = (not db["approval"]) or is_admin(by.id) or by.id in db["approved"]
        if not allowed:
            try:
                await context.bot.send_message(chat.id, "🚫 I can only be added by approved users. Leaving…")
                await context.bot.leave_chat(chat.id)
            except TelegramError:
                pass
            await notify_admins(
                context.bot,
                f"🛡 <b>Blocked an add attempt</b>\n{esc(chat.title)} (<code>{chat.id}</code>)\nBy: {who}",
                Markup([[Btn("✅ Approve this user", callback_data=f"grp:ap:{by.id}")]]),
            )
            return
        ensure_group(chat)
        save_db()
        await notify_admins(context.bot, f"➕ <b>Added to group</b>\n{esc(chat.title)} (<code>{chat.id}</code>)\nBy: {who}")
    else:
        ensure_group(chat)


# ───────────────────────── Moderation commands ─────────────────────────
def target_from(msg, args):
    r = msg.reply_to_message
    if r and r.from_user and not r.from_user.is_bot:
        return r.from_user.id, r.from_user.full_name
    for a in args:
        if a.lstrip("-").isdigit():
            return int(a), a
    return None, None


async def can_moderate(context, chat, user) -> bool:
    return is_admin(user.id) or (chat.type in GROUP_TYPES and await is_group_admin(context.bot, chat.id, user.id))


async def cmd_ban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg, user, chat = update.message, update.effective_user, update.effective_chat
    tid, name = target_from(msg, context.args)
    if tid is None:
        await msg.reply_text("Reply to a user (or pass their ID): /ban [global]")
        return
    glob = chat.type == "private" or any(a.lower() == "global" for a in context.args)
    if glob and not is_admin(user.id):
        await msg.reply_text("⛔ Only bot admins can ban globally.")
        return
    if not glob and not await can_moderate(context, chat, user):
        await msg.reply_text("⛔ Only group admins can do that.")
        return
    if is_admin(tid):
        await msg.reply_text("❌ I can't ban a bot admin.")
        return
    lst = db["banned_global"] if glob else db["banned_groups"].setdefault(str(chat.id), [])
    if tid not in lst:
        lst.append(tid)
    save_db()
    await msg.reply_text(f"🚫 <b>{esc(name)}</b> banned {'globally' if glob else 'in this group'}.", parse_mode=HTML)


async def cmd_unban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg, user, chat = update.message, update.effective_user, update.effective_chat
    tid, name = target_from(msg, context.args)
    if tid is None:
        await msg.reply_text("Reply to a user (or pass their ID): /unban [global]")
        return
    glob = chat.type == "private" or any(a.lower() == "global" for a in context.args)
    if glob and not is_admin(user.id):
        await msg.reply_text("⛔ Only bot admins can unban globally.")
        return
    if not glob and not await can_moderate(context, chat, user):
        await msg.reply_text("⛔ Only group admins can do that.")
        return
    lst = db["banned_global"] if glob else db["banned_groups"].setdefault(str(chat.id), [])
    if tid in lst:
        lst.remove(tid)
    db["warns"].get(str(chat.id), {}).pop(str(tid), None)
    save_db()
    await msg.reply_text(f"✅ <b>{esc(name)}</b> unbanned.", parse_mode=HTML)


async def cmd_warn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg, user, chat = update.message, update.effective_user, update.effective_chat
    if chat.type not in GROUP_TYPES:
        await msg.reply_text("⚠️ Use /warn in a group, replying to the user.")
        return
    if not await can_moderate(context, chat, user):
        await msg.reply_text("⛔ Only group admins can do that.")
        return
    tid, name = target_from(msg, [])
    if tid is None:
        await msg.reply_text("Reply to the user's message with /warn")
        return
    if is_admin(tid) or await is_group_admin(context.bot, chat.id, tid):
        await msg.reply_text("❌ I can't warn an admin.")
        return
    gid = str(chat.id)
    w = db["warns"].setdefault(gid, {})
    n = w.get(str(tid), 0) + 1
    if n >= WARN_LIMIT:
        w.pop(str(tid), None)
        lst = db["banned_groups"].setdefault(gid, [])
        if tid not in lst:
            lst.append(tid)
        save_db()
        await msg.reply_text(f"🚫 <b>{esc(name)}</b> reached {WARN_LIMIT} warnings and is banned here.", parse_mode=HTML)
    else:
        w[str(tid)] = n
        save_db()
        await msg.reply_text(f"⚠️ <b>{esc(name)}</b> warned ({n}/{WARN_LIMIT}).", parse_mode=HTML)


async def cmd_approve(update: Update, context: ContextTypes.DEFAULT_TYPE, add: bool = True):
    msg = update.message
    if not is_admin(update.effective_user.id):
        await msg.reply_text("⛔ Admins only.")
        return
    tid, _ = target_from(msg, context.args)
    if tid is None:
        await msg.reply_text(f"Usage: /{'approve' if add else 'unapprove'} <user_id>")
        return
    if add and tid not in db["approved"]:
        db["approved"].append(tid)
    if not add and tid in db["approved"]:
        db["approved"].remove(tid)
    save_db()
    await msg.reply_text(f"{'✅ Approved' if add else '🗑 Removed'}: <code>{tid}</code>", parse_mode=HTML)


async def cmd_unapprove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await cmd_approve(update, context, add=False)


# ───────────────────────── Message router ─────────────────────────
async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg:
        return
    user, chat, ud = update.effective_user, msg.chat, context.user_data

    if is_admin(user.id) and chat.type == "private":
        if ud.get("await_chat"):
            await admin_add_flow(update, context)
            return
        if ud.get("await_set"):
            await admin_set_flow(update, context)
            return
        if ud.get("await_gsearch") and msg.text:
            ud.pop("await_gsearch")
            ud["gq"] = msg.text.strip()
            text, kb = groups_list_view(0, ud)
            await msg.reply_text(text, parse_mode=HTML, reply_markup=kb)
            return
        if ud.get("await_bc"):
            ud.pop("await_bc")
            ud["bc"] = {"cid": chat.id, "mid": msg.message_id, "sel": []}
            text, kb = bc_confirm_view(ud)
            await msg.reply_text(text, parse_mode=HTML, reply_markup=kb)
            return

    if chat.type == "private" and ud.get("await_proof"):
        await handle_proof(update, context)
        return

    if chat.type == "private" and pick_media(msg):
        await recognize_media(msg, msg, user, context)
        return

    if not msg.text:
        return
    text = msg.text.strip()
    if chat.type == "private":
        await handle_query(msg, text, context)
    elif chat.type in GROUP_TYPES and ensure_group(chat)["txt"] and len(text) >= 3:
        await handle_query(msg, text, context)


# ═════════════════════════ v3: colours · info · share · follow · referral · premium ═════════════════════════
RAINBOW = "🟥🟧🟨🟩🟦🟪"
SQ = ["🟦", "🟩", "🟨", "🟧", "🟪", "🟥"]
FOOT = "\u2063"   # invisible marker: "this text already has a footer"
SPON = "\u2060"   # invisible marker: "a sponsor line may be added here"
NOTES = ["C", "C♯", "D", "D♯", "E", "F", "F♯", "G", "G♯", "A", "A♯", "B"]
METHODS = {"jc": ("JazzCash", "🔴"), "ep": ("Easypaisa", "🟢")}

NEW_HELP = (
    "\n✨ <b>More</b>\n"
    "🎤 <code>/artist Karan Aujla</code> — artist page, albums &amp; top songs\n"
    "ℹ️ <b>Info</b> button on every song — album, year, genre, BPM, key\n"
    "🔗 <b>Share</b> button — a card with a link that opens the song\n"
    "🔔 <code>/follow artist</code> · /following — get notified about new releases\n"
    "🎁 /invite — invite friends, earn more daily recognitions\n"
    "⭐ /premium — priority downloads &amp; more\n"
    "🏷 <code>/brand text</code> — custom footer for your group (premium)"
)


def fancy(icon: str, title: str, rows=(), note: str = "", tail: str = "") -> str:
    """Colourful card: rainbow bars, coloured squares, quote block. Values in rows must already be HTML-escaped."""
    out = [RAINBOW, f"{icon} <b>{esc(title)}</b>", RAINBOW, ""]
    for i, (label, value) in enumerate(rows):
        out.append(f"{SQ[i % 6]} <b>{label}</b> · {value}")
    if note:
        out += ["", f"<blockquote>{note}</blockquote>"]
    if tail:
        out += ["", tail]
    return "\n".join(out)


async def tell(bot, uid, text: str, markup=None) -> bool:
    try:
        await bot.send_message(uid, text, parse_mode=HTML, reply_markup=markup, disable_web_page_preview=True)
        return True
    except TelegramError:
        return False


# ───────────────────────── Premium helpers ─────────────────────────
def prem_exp(uid) -> int:
    return int(db["premium"].get(str(uid), 0))


def is_premium(uid) -> bool:
    return is_admin(uid) or prem_exp(uid) > time.time()


def days_left(uid) -> int:
    return max(0, (prem_exp(uid) - int(time.time()) + 86399) // 86400)


def fmt_date(ts) -> str:
    return time.strftime("%d %b %Y", time.localtime(int(ts)))


def grant_premium(uid, days) -> int:
    now = int(time.time())
    db["premium"][str(uid)] = max(now, prem_exp(uid)) + int(days) * 86400
    touch()
    return db["premium"][str(uid)]


def ref_count(uid) -> int:
    return db["ref"]["n"].get(str(uid), 0)


def recog_limit(uid) -> int:
    """Daily recognitions: premium gets a high limit; everyone else = base + referral bonus (capped)."""
    if is_premium(uid):
        return PREMIUM_RECOG_LIMIT
    return RECOGNIZE_DAILY_LIMIT + min(ref_count(uid) * REF_BONUS, REF_MAX_BONUS)


def prem_label(uid) -> str:
    if is_admin(uid):
        return "Admin"
    return f"until {fmt_date(prem_exp(uid))}" if prem_exp(uid) > time.time() else "no"


def premium_perks() -> str:
    return (
        "⚡ <b>Priority downloads</b> — skip the queue\n"
        f"🎧 <b>{PREMIUM_RECOG_LIMIT}</b> song recognitions per day\n"
        "🚫 <b>No sponsor</b> messages\n"
        "🏷 <b>Custom footer</b> for your groups (/brand)\n"
        f"🔔 Follow up to <b>{FOLLOW_LIMIT_PREMIUM}</b> artists"
    )


# ───────────────────────── Referrals ─────────────────────────
async def register_referral(bot, user, ref_uid: int):
    r = db["ref"]
    if ref_uid == user.id or ref_uid not in _users or str(user.id) in r["by"]:
        return
    r["by"][str(user.id)] = ref_uid
    r["n"][str(ref_uid)] = r["n"].get(str(ref_uid), 0) + 1
    touch()
    await tell(bot, ref_uid, fancy(
        "🎉", "A friend joined!",
        [("Friend", esc(user.full_name[:30])), ("Invited so far", f"<b>{ref_count(ref_uid)}</b>"),
         ("Your daily recognitions", f"<b>{recog_limit(ref_uid)}</b>")],
        note="Thanks for sharing! Keep inviting with /invite to unlock more.",
    ))


def invite_view(user, bot_name: str):
    link = f"https://t.me/{bot_name}?start=ref_{user.id}"
    n = ref_count(user.id)
    bonus = min(n * REF_BONUS, REF_MAX_BONUS)
    limit = "Unlimited" if is_admin(user.id) else str(recog_limit(user.id))
    text = fancy(
        "🎁", "Invite & Earn",
        [("Friends invited", f"<b>{n}</b>"),
         ("Bonus", f"+{bonus} / {REF_MAX_BONUS} per day"),
         ("Your daily recognitions", f"<b>{limit}</b>")],
        note=f"Every friend who joins with your link gives you <b>+{REF_BONUS}</b> song recognition(s) per day.",
        tail=f"🔗 <b>Your link</b>\n<code>{link}</code>",
    )
    share = f"https://t.me/share/url?url={quote(link)}&text={quote('🎧 Download any song or video — free!')}"
    kb = Markup([
        [Btn("📤 Share my link", url=share, style="success")],
        [Btn("⭐ Go Premium", callback_data="pay:home", style="primary")],
    ])
    return text, kb


async def cmd_invite(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text, kb = invite_view(update.effective_user, context.bot.username or "bot")
    await update.message.reply_text(text, parse_mode=HTML, reply_markup=kb, disable_web_page_preview=True)


async def cb_invite(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    text, kb = invite_view(q.from_user, context.bot.username or "bot")
    await q.message.chat.send_message(text, parse_mode=HTML, reply_markup=kb, disable_web_page_preview=True)


# ───────────────────────── MusicBrainz (+ optional GetSongBPM) ─────────────────────────
MB_BASE = "https://musicbrainz.org/ws/2"
_mb = {"lock": None, "last": 0.0, "client": None}
_MBC: dict = {}
MBC_TTL = 24 * 3600


def mb_client() -> httpx.AsyncClient:
    if _mb["client"] is None:
        _mb["client"] = httpx.AsyncClient(
            timeout=12, headers={"User-Agent": MB_USER_AGENT, "Accept": "application/json"},
        )
    return _mb["client"]


async def mb_get(path: str, **params) -> dict:
    """MusicBrainz allows ~1 request/second: every call waits its turn."""
    params["fmt"] = "json"
    if _mb["lock"] is None:
        _mb["lock"] = asyncio.Lock()
    for attempt in (1, 2):
        async with _mb["lock"]:
            wait = 1.1 - (time.time() - _mb["last"])
            if wait > 0:
                await asyncio.sleep(wait)
            _mb["last"] = time.time()
        r = await mb_client().get(f"{MB_BASE}/{path}", params=params)
        if r.status_code in (429, 503) and attempt == 1:
            await asyncio.sleep(2.5)
            continue
        r.raise_for_status()
        return r.json()
    return {}


def _mbc_get(key, ttl=MBC_TTL):
    hit = _MBC.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    return None


def _mbc_put(key, val):
    if len(_MBC) > 800:
        for k, _ in sorted(_MBC.items(), key=lambda kv: kv[1][0])[:200]:
            _MBC.pop(k, None)
    _MBC[key] = (time.time(), val)
    return val


def lq(s: str) -> str:
    return re.sub(r'["\\]', " ", s or "").strip()


def _sim(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, (a or "").lower(), (b or "").lower()).ratio()


def parse_artist(a: dict) -> dict:
    ls = a.get("life-span") or {}
    tags = sorted(a.get("genres") or a.get("tags") or [], key=lambda t: -t.get("count", 0))
    return {
        "id": a.get("id", ""), "name": a.get("name", ""), "dis": a.get("disambiguation", ""),
        "type": a.get("type", ""),
        "country": (a.get("area") or {}).get("name") or a.get("country", "") or "",
        "begin": ls.get("begin") or "",
        "genres": [t["name"].title() for t in tags[:4] if t.get("name")],
    }


async def mb_artist_search(name: str):
    key = ("as", name.lower())
    hit = _mbc_get(key)
    if hit is not None:
        return hit or None
    d = await mb_get("artist", query=f'artist:"{lq(name)}"', limit=3)
    best = None
    for a in d.get("artists", []):
        if int(a.get("score", 0)) >= 70 and _sim(a.get("name", ""), name) >= 0.55:
            best = a
            break
    return _mbc_put(key, parse_artist(best) if best else {}) or None


async def mb_artist_by_id(mbid: str):
    key = ("ai", mbid)
    hit = _mbc_get(key)
    if hit is not None:
        return hit or None
    d = await mb_get(f"artist/{mbid}", inc="genres+tags")
    return _mbc_put(key, parse_artist(d) if d.get("id") else {}) or None


async def mb_release_groups(mbid: str, max_pages: int = 3, fresh: bool = False) -> list:
    key = ("rg", mbid)
    if not fresh:
        hit = _mbc_get(key, 6 * 3600)
        if hit is not None:
            return hit
    out, off = [], 0
    while True:
        d = await mb_get("release-group", artist=mbid, limit=100, offset=off, type="album|single|ep")
        for r in d.get("release-groups", []):
            sec = r.get("secondary-types") or []
            if any(s in ("Compilation", "DJ-mix", "Remix", "Live", "Demo") for s in sec):
                continue
            out.append({
                "id": r.get("id", ""), "title": r.get("title", ""),
                "type": r.get("primary-type") or "Release", "date": r.get("first-release-date") or "",
            })
        off += 100
        if off >= d.get("release-group-count", 0) or off >= 100 * max_pages:
            break
    return _mbc_put(key, out)


def parse_recording(r: dict) -> dict:
    ac = r.get("artist-credit") or []
    artist = "".join((c.get("name", "") + c.get("joinphrase", "")) for c in ac).strip()
    artist_id = ((ac[0].get("artist") or {}).get("id")) if ac else None
    rels = r.get("releases") or []

    def is_album(x):
        rg = x.get("release-group") or {}
        return rg.get("primary-type") == "Album" and not rg.get("secondary-types")

    pick = (
        next((x for x in rels if is_album(x) and x.get("status") == "Official"), None)
        or next((x for x in rels if is_album(x)), None)
        or (rels[0] if rels else None)
    )
    date = r.get("first-release-date") or (pick or {}).get("date") or ""
    tags = sorted(r.get("tags") or [], key=lambda t: -t.get("count", 0))
    return {
        "mb_title": r.get("title", ""), "mb_artist": artist, "artist_id": artist_id,
        "album": (pick or {}).get("title", ""),
        "album_type": ((pick or {}).get("release-group") or {}).get("primary-type", ""),
        "year": date[:4], "date": date,
        "genres": [t["name"].title() for t in tags[:3] if t.get("name")],
    }


async def mb_recording(title: str, artist: str):
    key = ("rec", f"{artist}|{title}".lower())
    hit = _mbc_get(key)
    if hit is not None:
        return hit or None
    q = f'recording:"{lq(title)}"' + (f' AND artist:"{lq(artist)}"' if artist else "")
    d = await mb_get("recording", query=q, limit=5)
    best = None
    for r in d.get("recordings", []):
        if int(r.get("score", 0)) >= 70 and _sim(r.get("title", ""), title) >= 0.6:
            best = r
            break
    if not best:
        return _mbc_put(key, {}) or None
    info = parse_recording(best)
    if not info["genres"] and info.get("artist_id"):
        try:
            a = await mb_artist_by_id(info["artist_id"])
            info["genres"] = (a or {}).get("genres", [])[:3]
        except Exception:
            pass
    return _mbc_put(key, info)


async def getsongbpm(title: str, artist: str):
    """Optional: GetSongBPM (free API key). MusicBrainz itself has no BPM/key data."""
    if not GETSONGBPM_KEY:
        return None
    c = http_client()
    r = await c.get("https://api.getsong.co/search/", params={
        "api_key": GETSONGBPM_KEY, "type": "both", "lookup": f"song:{title} artist:{artist}",
    })
    items = r.json().get("search")
    if not isinstance(items, list) or not items:
        return None
    it = items[0]
    if not it.get("tempo") and it.get("id"):
        r2 = await c.get("https://api.getsong.co/song/", params={"api_key": GETSONGBPM_KEY, "id": it["id"]})
        it = r2.json().get("song") or it
    tempo, key = it.get("tempo"), it.get("key_of")
    if not tempo and not key:
        return None
    return {"bpm": str(tempo or ""), "key": str(key or ""), "bpm_src": "GetSongBPM"}


async def deezer_bpm(title: str, artist: str):
    """Free, keyless BPM lookup via Deezer (no key needed). Deezer has no key/scale data,
    and some tracks have BPM 0 — those are skipped."""
    if not title:
        return None
    c = http_client()
    q = f'artist:"{artist}" track:"{title}"' if artist else f'track:"{title}"'
    r = await c.get("https://api.deezer.com/search", params={"q": q, "limit": 5})
    items = (r.json() or {}).get("data") or []
    if not items and artist:
        r = await c.get("https://api.deezer.com/search", params={"q": f"{artist} {title}", "limit": 5})
        items = (r.json() or {}).get("data") or []
    if not items:
        return None
    a = (artist or "").lower()
    pick = next((i for i in items if a and (a in (i.get("artist") or {}).get("name", "").lower()
                                            or (i.get("artist") or {}).get("name", "").lower() in a)), items[0])
    r2 = await c.get(f"https://api.deezer.com/track/{pick['id']}")
    bpm = (r2.json() or {}).get("bpm")
    try:
        bpm = int(round(float(bpm)))
    except (TypeError, ValueError):
        return None
    if bpm <= 0:
        return None
    return {"bpm": str(bpm), "key": "", "bpm_src": "Deezer"}


def analyze_audio(path: str) -> dict:
    """Local BPM + key estimate (needs `pip install librosa`)."""
    import numpy as np
    import librosa

    wav = path + ".wav"
    for ss in ("20", "0"):
        subprocess.run(
            ["ffmpeg", "-y", "-ss", ss, "-t", "60", "-i", path, "-ac", "1", "-ar", "22050", wav],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        if os.path.exists(wav) and os.path.getsize(wav) > 100_000:
            break
    y, sr = librosa.load(wav, sr=22050, mono=True)
    tempo, _ = librosa.beat.beat_track(y=y, sr=sr)
    bpm = int(round(float(np.atleast_1d(tempo)[0])))
    chroma = librosa.feature.chroma_cqt(y=y, sr=sr).mean(axis=1)
    major = np.array([6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88])
    minor = np.array([6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17])
    best, best_key = -2.0, ""
    for i in range(12):
        for prof, mode in ((major, "major"), (minor, "minor")):
            c = float(np.corrcoef(chroma, np.roll(prof, i))[0, 1])
            if c > best:
                best, best_key = c, f"{NOTES[i]} {mode}"
    return {"bpm": str(bpm), "key": best_key, "bpm_src": "audio analysis"}


# ───────────────────────── Song info card ─────────────────────────
def guess_artist_title(meta: dict):
    t = clean_title(meta["title"])
    ch = re.sub(r"\s*-\s*Topic$|VEVO$|\s*Official$", "", meta.get("channel", ""), flags=re.I).strip()
    if " - " in t:
        a, s = t.split(" - ", 1)
        return a.strip(), s.strip() or t
    return ch, t


async def song_info(meta: dict) -> dict:
    artist, title = guess_artist_title(meta)
    info = {"artist": artist, "title": title}
    try:
        rec = await mb_recording(title, artist)
    except Exception as e:
        log.warning("musicbrainz failed: %s", e)
        rec = None
    if rec:
        info.update(rec)
    try:
        sb = await getsongbpm(title, info.get("mb_artist") or artist)
    except Exception as e:
        log.warning("getsongbpm failed: %s", e)
        sb = None
    if sb:
        info.update(sb)
    if not info.get("bpm"):
        try:
            dz = await deezer_bpm(title, info.get("mb_artist") or artist)
        except Exception as e:
            log.warning("deezer bpm failed: %s", e)
            dz = None
        if dz:
            info.update(dz)
    cached = db["bpm"].get(meta["id"])
    if cached and not info.get("bpm"):
        info.update(cached)
    return info


def info_view(meta: dict, info: dict):
    dash = "<i>—</i>"
    if info.get("album"):
        album = esc(info["album"]) + (f" <i>({esc(info['album_type'])})</i>" if info.get("album_type") else "")
    else:
        album = dash
    rows = [
        ("Title", f"<b>{esc((info.get('mb_title') or info['title'])[:70])}</b>"),
        ("Artist", esc((info.get("mb_artist") or info["artist"])[:50]) or dash),
        ("Album", album),
        ("Release year", esc(info["year"]) if info.get("year") else dash),
        ("Genre", esc(", ".join(info["genres"])) if info.get("genres") else dash),
        ("BPM", f"<b>{esc(info['bpm'])}</b>" if info.get("bpm") else dash),
        ("Key", f"<b>{esc(info['key'])}</b>" if info.get("key") else dash),
        ("Duration", fmt_dur(meta["duration"])),
    ]
    src = ["📚 MusicBrainz"] if info.get("mb_title") else ["⚠️ Not found on MusicBrainz"]
    if info.get("bpm_src"):
        src.append(f"🎚 BPM/key: {info['bpm_src']}")
    elif not info.get("bpm"):
        src.append("🎚 BPM/key: not available for this song")
    text = fancy("ℹ️", "Song Info", rows, note="\n".join(src))
    vid = meta["id"]
    kb = []
    if HAVE_LIBROSA and not info.get("bpm"):
        kb.append([Btn("🎚 Analyze BPM & key", callback_data=f"bk:{vid}", style="success")])
    if info.get("artist_id"):
        kb.append([Btn("🎤 Artist page", callback_data=f"ar:{info['artist_id']}", style="primary")])
    kb.append([Btn("🔗 Share", callback_data=f"sh:{vid}", style="success"), Btn("🎤 Lyrics", callback_data=f"ly:{vid}")])
    return text, Markup(kb)


async def _meta_for(vid: str):
    meta = META.get(vid)
    if not meta:
        meta = await run_lookup(yt_meta, vid)
        remember(meta)
    return meta


async def cb_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    chat = q.message.chat
    if not await group_ok(context, chat, q.from_user, cq=q):
        return
    await q.answer()
    vid = q.data.split(":", 1)[1]
    try:
        meta = await _meta_for(vid)
    except Exception:
        await chat.send_message(tr(L(q.from_user.id), "load_failed"))
        return
    status = await chat.send_message(
        fancy("ℹ️", "Song Info", note="🔎 Looking up album, year, genre, BPM and key…"), parse_mode=HTML,
    )
    info = await song_info(meta)
    text, kb = info_view(meta, info)
    await safe_edit(status, text, kb)
    autodel(status, gsettings(chat))


async def cb_bpm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    chat, user = q.message.chat, q.from_user
    if not await group_ok(context, chat, user, cq=q):
        return
    if not HAVE_LIBROSA:
        await q.answer("Audio analysis is not installed on this server.", show_alert=True)
        return
    await q.answer("🎚")
    lang = L(user.id)
    vid = q.data.split(":", 1)[1]
    try:
        meta = await _meta_for(vid)
    except Exception:
        await chat.send_message(tr(lang, "load_failed"))
        return

    def result_text(res):
        return fancy("🎚", "Analysis result", [
            ("Song", esc(meta["title"][:60])), ("BPM", f"<b>{esc(res['bpm'])}</b>"), ("Key", f"<b>{esc(res['key'])}</b>"),
        ], note="Estimated from the audio — can be off by a half/double tempo or the relative key.")

    if vid in db["bpm"]:
        await chat.send_message(result_text(db["bpm"][vid]), parse_mode=HTML)
        return
    if meta["duration"] and meta["duration"] > MAX_VIDEO_DURATION:
        await chat.send_message(fancy("🎚", "Too long", note=f"Analysis works for songs up to {MAX_VIDEO_DURATION // 60} minutes."), parse_mode=HTML)
        return
    running = fancy("🎚", "Analyzing…", note="Detecting tempo and key — about 20–40 seconds.")
    status = await chat.send_message(running, parse_mode=HTML)
    outdir = tempfile.mkdtemp(prefix="musicbot_bpm_")
    try:
        data = await queued_download(status, lang, meta["title"], running, yt_download, vid, "a", outdir,
                                     prio=0 if is_premium(user.id) else 1)
        res = await asyncio.to_thread(analyze_audio, data["path"])
        if len(db["bpm"]) > 2000:
            db["bpm"].pop(next(iter(db["bpm"])), None)
        db["bpm"][vid] = res
        touch()
        await safe_edit(status, result_text(res))
    except Exception as e:
        log.exception("bpm analysis failed")
        await report_error(context.bot, e, f"bpm {vid}", vid, user)
        await safe_edit(status, fancy("❌", "Analysis failed", note=esc(error_hint(e, lang))))
    finally:
        shutil.rmtree(outdir, ignore_errors=True)


# ───────────────────────── Share card ─────────────────────────
async def cb_share(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    chat = q.message.chat
    if not await group_ok(context, chat, q.from_user, cq=q):
        return
    await q.answer()
    vid = q.data.split(":", 1)[1]
    try:
        meta = await _meta_for(vid)
    except Exception:
        await chat.send_message(tr(L(q.from_user.id), "load_failed"))
        return
    link = f"https://t.me/{context.bot.username}?start=song_{vid}"
    share = f"https://t.me/share/url?url={quote(link)}&text={quote('🎧 ' + meta['title'][:80])}"
    text = (
        f"🎁 <b>{esc(meta['title'][:90])}</b>\n\n"
        f"🟦 👤 {esc(meta['channel'][:40])}\n🟩 ⏱ {fmt_dur(meta['duration'])}\n\n"
        "<blockquote>Tap <b>Open in bot</b> to listen &amp; download this song instantly.</blockquote>\n"
        f"🔗 <code>{link}</code>"
    )
    kb = Markup([[Btn("🎧 Open in bot", url=link, style="success"), Btn("📤 Share", url=share, style="primary")]])
    try:
        m = await chat.send_photo(photo=thumb_url(vid), caption=text, parse_mode=HTML, reply_markup=kb)
    except TelegramError:
        m = await chat.send_message(text, parse_mode=HTML, reply_markup=kb, disable_web_page_preview=True)
    autodel(m, gsettings(chat))


# ───────────────────────── /artist ─────────────────────────
async def show_artist(status, user, name: str = None, mbid: str = None):
    lang = L(user.id)
    art, rgs = None, []
    try:
        art = await (mb_artist_by_id(mbid) if mbid else mb_artist_search(name))
        if art:
            rgs = await mb_release_groups(art["id"])
    except Exception as e:
        log.warning("artist lookup failed: %s", e)
    qname = art["name"] if art else (name or "")
    try:
        songs = await cached_search(f"{qname} songs")
    except Exception:
        songs = []
    if not art and not songs:
        await safe_edit(status, tr(lang, "no_results"))
        return

    rows = []
    if art:
        rows.append(("Artist", f"<b>{esc(art['name'])}</b>" + (f" <i>({esc(art['dis'])})</i>" if art["dis"] else "")))
        if art["type"]:
            rows.append(("Type", esc(art["type"])))
        if art["country"]:
            rows.append(("Country", esc(art["country"])))
        if art["begin"]:
            rows.append(("Active since", esc(art["begin"][:4])))
        if art["genres"]:
            rows.append(("Genres", esc(", ".join(art["genres"]))))
    else:
        rows.append(("Artist", f"<b>{esc(qname)}</b>"))

    note = ""
    latest = sorted((r for r in rgs if r["date"]), key=lambda r: r["date"], reverse=True)[:5]
    if latest:
        note = "💿 <b>Latest releases</b>\n" + "\n".join(
            f"• {esc(r['date'][:4])} · {esc(r['title'][:40])} <i>({esc(r['type'])})</i>" for r in latest
        )
    elif not art:
        note = "Artist details were not found on MusicBrainz — showing songs only."

    lines = [fancy("🎤", "Artist", rows, note=note)]
    kb = []
    if songs:
        lines.append("\n🔥 <b>Top songs</b>")
        nums = []
        for i, r in enumerate(songs[:5]):
            remember(r)
            lines.append(f"{NUMS[i]} <b>{esc(r['title'][:55])}</b>\n      {meta_line(r)}")
            nums.append(Btn(NUMS[i], callback_data=f"p:{r['id']}"))
        lines.append("\n👇 <i>Tap a number to choose</i>")
        kb.append(nums)
    if art:
        kb.append([Btn("🔔 Follow — new release alerts", callback_data=f"fl:{art['id']}", style="success")])
    await safe_edit(status, "\n".join(lines) + SPON, Markup(kb) if kb else None)


async def cmd_artist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg, user = update.message, update.effective_user
    if not context.args:
        await msg.reply_text(fancy("🎤", "Artist page", note="Send an artist name.\n<b>Example:</b> <code>/artist Karan Aujla</code>"), parse_mode=HTML)
        return
    if not await group_ok(context, msg.chat, user, cooldown=True, msg=msg):
        return
    name = " ".join(context.args).strip()[:60]
    status = await msg.reply_text(fancy("🎤", "Finding artist…", note=esc(name)), parse_mode=HTML)
    await show_artist(status, user, name=name)
    autodel(status, gsettings(msg.chat))


async def cb_artist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    chat = q.message.chat
    if not await group_ok(context, chat, q.from_user, cq=q):
        return
    await q.answer()
    mbid = q.data.split(":", 1)[1]
    status = await chat.send_message(fancy("🎤", "Finding artist…"), parse_mode=HTML)
    await show_artist(status, q.from_user, mbid=mbid)
    autodel(status, gsettings(chat))


# ───────────────────────── Follow artists (new-release reminders) ─────────────────────────
def user_follows(uid: int) -> list:
    return [m for m, a in db["artists"].items() if uid in a["subs"]]


def follow_limit(uid: int) -> int:
    return FOLLOW_LIMIT_PREMIUM if is_premium(uid) else FOLLOW_LIMIT_FREE


async def do_follow(uid: int, mbid: str, name: str) -> str:
    mine = user_follows(uid)
    if mbid in mine:
        return "dup"
    if len(mine) >= follow_limit(uid):
        return "limit"
    a = db["artists"].get(mbid)
    if a is None:
        rgs = await mb_release_groups(mbid, fresh=True)   # only releases AFTER now will notify
        a = db["artists"][mbid] = {"n": name, "seen": [r["id"] for r in rgs], "subs": [], "chk": int(time.time())}
    a["subs"].append(uid)
    touch()
    return "ok"


def follow_result_text(res: str, name: str, uid: int) -> str:
    if res == "ok":
        return fancy("🔔", "Following!", [("Artist", f"<b>{esc(name)}</b>")],
                     note=f"I'll message you when new music is released.\nYou follow {len(user_follows(uid))}/{follow_limit(uid)} artists.")
    if res == "dup":
        return fancy("🔔", "Already following", [("Artist", f"<b>{esc(name)}</b>")], note="See your list with /following")
    return fancy("🔔", "Follow limit reached", [("Limit", f"<b>{follow_limit(uid)}</b> artists")],
                 note="Unfollow someone with /following, or go ⭐ /premium for more.")


async def cmd_follow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg, user = update.message, update.effective_user
    if not context.args:
        await msg.reply_text(fancy("🔔", "Follow an artist", note="Get a message when they release something new.\n<b>Example:</b> <code>/follow Karan Aujla</code>"), parse_mode=HTML)
        return
    if not await group_ok(context, msg.chat, user, cooldown=True, msg=msg):
        return
    name = " ".join(context.args).strip()[:60]
    status = await msg.reply_text(fancy("🔔", "Finding artist…", note=esc(name)), parse_mode=HTML)
    try:
        art = await mb_artist_search(name)
        if not art:
            await safe_edit(status, fancy("😕", "Artist not found", note="Check the spelling and try again."))
            return
        res = await do_follow(user.id, art["id"], art["name"])
    except Exception as e:
        log.warning("follow failed: %s", e)
        await safe_edit(status, fancy("❌", "Couldn't follow", note="The music database is busy. Try again in a moment."))
        return
    await safe_edit(status, follow_result_text(res, art["name"], user.id))


async def cb_follow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    user = q.from_user
    mbid = q.data.split(":", 1)[1]
    try:
        art = await mb_artist_by_id(mbid)
        name = art["name"] if art else "Artist"
        res = await do_follow(user.id, mbid, name)
    except Exception as e:
        log.warning("follow failed: %s", e)
        await q.answer("Couldn't follow right now. Try again.", show_alert=True)
        return
    await q.answer({"ok": "🔔 Following!", "dup": "Already following", "limit": "Follow limit reached"}[res], show_alert=res == "limit")
    if res != "limit":
        await q.message.chat.send_message(follow_result_text(res, name, user.id), parse_mode=HTML)


def following_view(uid: int):
    mine = user_follows(uid)
    if not mine:
        return fancy("🔔", "Following", note="You don't follow anyone yet.\nUse <code>/follow artist name</code>."), None
    rows = [(f"{i + 1}", esc(db["artists"][m]["n"][:40])) for i, m in enumerate(mine)]
    text = fancy("🔔", "Following", rows, note=f"{len(mine)}/{follow_limit(uid)} artists · alerts every ~{int(RELEASE_CHECK_HOURS)}h")
    kb = [[Btn(f"🗑 Unfollow · {db['artists'][m]['n'][:26]}", callback_data=f"uf:{m}", style="danger")] for m in mine]
    return text, Markup(kb)


async def cmd_following(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text, kb = following_view(update.effective_user.id)
    await update.message.reply_text(text, parse_mode=HTML, reply_markup=kb)


async def cb_unfollow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid, mbid = q.from_user.id, q.data.split(":", 1)[1]
    a = db["artists"].get(mbid)
    if a and uid in a["subs"]:
        a["subs"].remove(uid)
        if not a["subs"]:
            db["artists"].pop(mbid, None)
        touch()
    await q.answer("🔕 Unfollowed")
    text, kb = following_view(uid)
    await safe_edit(q.message, text, kb)


async def release_watcher(bot):
    """Background loop: look for new releases of followed artists and message their followers."""
    await asyncio.sleep(90)
    while True:
        try:
            for mbid, a in list(db["artists"].items()):
                if not a["subs"]:
                    db["artists"].pop(mbid, None)
                    continue
                try:
                    rgs = await mb_release_groups(mbid, fresh=True)
                except Exception as e:
                    log.warning("release check failed for %s: %s", a["n"], e)
                    continue
                seen = set(a["seen"])
                new = [r for r in rgs if r["id"] not in seen]
                if not new:
                    continue
                a["seen"] = list(seen | {r["id"] for r in new})
                a["chk"] = int(time.time())
                touch()
                cutoff = time.strftime("%Y-%m-%d", time.localtime(time.time() - 120 * 86400))
                fresh = [r for r in new if r["date"] and r["date"] >= cutoff[:len(r["date"])]]
                for r in sorted(fresh, key=lambda r: r["date"], reverse=True)[:3]:
                    text = fancy("🎉", "New release!", [
                        ("Artist", f"<b>{esc(a['n'])}</b>"),
                        ("Title", f"<b>{esc(r['title'][:60])}</b>"),
                        ("Type", esc(r["type"])), ("Date", esc(r["date"])),
                    ], note="Tap below to find it and download.")
                    kb = Markup([
                        [Btn("🔎 Find & download", switch_inline_query_current_chat=f"{a['n']} {r['title']}"[:60], style="success")],
                        [Btn("🔕 Unfollow", callback_data=f"uf:{mbid}", style="danger")],
                    ])
                    for uid in list(a["subs"]):
                        try:
                            await bot.send_message(uid, text, parse_mode=HTML, reply_markup=kb)
                        except Forbidden:
                            a["subs"].remove(uid)
                            touch()
                        except TelegramError:
                            pass
                        await asyncio.sleep(0.05)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("release watcher error")
        await asyncio.sleep(max(1800, RELEASE_CHECK_HOURS * 3600))


# ───────────────────────── Per-group branding (/brand) ─────────────────────────
def brand_active(g: dict) -> bool:
    return bool(g.get("brand")) and (BRANDING_FREE or is_premium(g.get("brand_by", 0)))


async def cmd_brand(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg, user, chat = update.message, update.effective_user, update.effective_chat
    if chat.type not in GROUP_TYPES:
        await msg.reply_text(fancy("🏷", "Group footer", note="Use <code>/brand your text</code> <b>inside a group</b> to replace the bot's footer there."), parse_mode=HTML)
        return
    if not (is_admin(user.id) or await is_group_admin(context.bot, chat.id, user.id)):
        await deny("🔒 Only group admins can change the footer.", msg)
        return
    g = ensure_group(chat)
    text = " ".join(context.args).replace("\n", " ").strip()
    if not text:
        cur = esc(g["brand"]) if brand_active(g) else "<i>default (Developed by Dock)</i>"
        await msg.reply_text(fancy("🏷", "Group footer", [("Current", cur)],
                                   note="<code>/brand Your text here</code> — set footer (max 80 chars)\n<code>/brand off</code> — back to default\n⭐ Premium perk."), parse_mode=HTML)
        return
    if text.lower() in ("off", "reset", "default", "remove"):
        g["brand"], g["brand_by"] = "", 0
        touch()
        await msg.reply_text(fancy("🏷", "Footer reset", note="Back to the default footer."), parse_mode=HTML)
        return
    if not (BRANDING_FREE or is_premium(user.id)):
        await msg.reply_text(fancy("⭐", "Premium perk", note="Custom group footers are for premium members.\nSee /premium"), parse_mode=HTML)
        return
    g["brand"], g["brand_by"] = text[:80], user.id
    touch()
    await msg.reply_text(fancy("🏷", "Footer saved", [("Footer", f"✨ <i>{esc(g['brand'])}</i>")],
                               note="It shows on this group's bot messages while your premium is active."), parse_mode=HTML)


# ───────────────────────── Premium: JazzCash / Easypaisa (manual) ─────────────────────────
def premium_view(uid: int, private: bool = True):
    p = db["pay"]
    if is_admin(uid):
        status = "👑 Admin — everything unlocked"
    elif prem_exp(uid) > time.time():
        status = f"✅ Active until <b>{fmt_date(prem_exp(uid))}</b> ({days_left(uid)} days left)"
    else:
        status = "Free plan"
    price = f"<b>{esc(p['cur'])} {p['price']:,}</b> / {p['days']} days" if p["price"] else "<i>not set yet</i>"
    text = fancy("⭐", "Premium", [("Status", status), ("Price", price)], note=premium_perks())
    kb = []
    if p["price"] and any(p[m]["num"] for m in METHODS):
        text += "\n\n👇 <b>Choose a payment method</b>"
        if p["jc"]["num"]:
            kb.append([Btn("🔴 Pay with JazzCash", callback_data="pay:m:jc", style="danger")])
        if p["ep"]["num"]:
            kb.append([Btn("🟢 Pay with Easypaisa", callback_data="pay:m:ep", style="success")])
    else:
        text += "\n\n<i>Premium sales are not open yet. Please contact the admin.</i>"
    return text, Markup(kb) if kb else None


async def cmd_premium(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg, user = update.message, update.effective_user
    text, kb = premium_view(user.id)
    if msg.chat.type != "private":
        kb = Markup([[Btn("⭐ Open in private chat", url=f"https://t.me/{context.bot.username}?start=premium", style="success")]])
    await msg.reply_text(text, parse_mode=HTML, reply_markup=kb)


def method_view(uid: int, m: str):
    p = db["pay"]
    name, dot = METHODS[m]
    acc = p[m]
    text = fancy(dot, f"Pay with {name}", [
        ("Amount", f"<b>{esc(p['cur'])} {p['price']:,}</b>"),
        ("Number", f"<code>{esc(acc['num'])}</code>"),
        ("Account name", esc(acc["name"]) or "<i>—</i>"),
        ("Your ID", f"<code>{uid}</code>"),
    ], note=(f"1️⃣ Open <b>{name}</b> → <b>Send Money</b>\n2️⃣ Send the exact amount to the number above\n"
             "3️⃣ Tap <b>✅ I've Paid</b> and send the <b>Transaction ID</b> or a <b>screenshot</b>"))
    kb = Markup([[Btn("✅ I've Paid", callback_data=f"pay:paid:{m}", style="success")],
                 [Btn("⬅️ Back", callback_data="pay:home", style="primary")]])
    return text, kb


async def cb_pay(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    user, ud, p = q.from_user, context.user_data, db["pay"]
    parts = q.data.split(":")
    act = parts[1]

    if act in ("ok", "no"):   # admin decisions on a payment proof
        if not is_admin(user.id):
            await q.answer("⛔ Admins only", show_alert=True)
            return
        uid = int(parts[2])
        pend = db["pending"].pop(str(uid), None)
        if pend is None:
            await q.answer("Already handled", show_alert=True)
            return
        touch()
        await q.answer()
        if act == "ok":
            exp = grant_premium(uid, p["days"])
            await tell(context.bot, uid, fancy("🎉", "Premium activated!", [
                ("Valid until", f"<b>{fmt_date(exp)}</b>"), ("Days", f"<b>{p['days']}</b>")],
                note=premium_perks()))
            await safe_edit(q.message, f"✅ <b>Approved</b> — <code>{uid}</code> · premium until <b>{fmt_date(exp)}</b>\n👮 by {esc(user.full_name)}")
        else:
            await tell(context.bot, uid, fancy("❌", "Payment not verified",
                       note="We couldn't verify your payment. Check the details and try again from /premium, or contact the admin."))
            await safe_edit(q.message, f"❌ <b>Rejected</b> — <code>{uid}</code>\n👮 by {esc(user.full_name)}")
        return

    if q.message.chat.type != "private":
        await q.answer("Please open me in a private chat for payments.", show_alert=True)
        return
    await q.answer()
    if act == "home":
        ud.pop("await_proof", None)
        text, kb = premium_view(user.id)
        await safe_edit(q.message, text, kb)
    elif act == "m" and parts[2] in METHODS and p[parts[2]]["num"]:
        ud.pop("await_proof", None)
        text, kb = method_view(user.id, parts[2])
        await safe_edit(q.message, text, kb)
    elif act == "paid" and parts[2] in METHODS:
        ud["await_proof"] = parts[2]
        await safe_edit(q.message, fancy("📨", "Send payment proof",
                        note="Now send the <b>Transaction ID</b> as text, or a <b>screenshot</b> of the payment."),
                        Markup([[Btn("❌ Cancel", callback_data="pay:home")]]))


async def handle_proof(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg, user, ud = update.message, update.effective_user, context.user_data
    m = ud.pop("await_proof")
    p = db["pay"]
    if not (msg.text or msg.photo or msg.document):
        ud["await_proof"] = m
        await msg.reply_text("❌ Please send the Transaction ID as text or a screenshot.")
        return
    db["pending"][str(user.id)] = {"m": m, "ts": int(time.time()), "name": user.full_name[:40]}
    touch()
    name, dot = METHODS[m]
    kb = Markup([[Btn(f"✅ Approve +{p['days']}d", callback_data=f"pay:ok:{user.id}", style="success"),
                  Btn("❌ Reject", callback_data=f"pay:no:{user.id}", style="danger")]])
    card = fancy("💳", "Payment proof", [
        ("User", f"<a href=\"tg://user?id={user.id}\">{esc(user.full_name[:30])}</a>"),
        ("ID", f"<code>{user.id}</code>"), ("Method", f"{dot} {name}"),
        ("Amount", f"<b>{esc(p['cur'])} {p['price']:,}</b>")], note="Check your wallet app, then approve or reject.")
    for a in ADMIN_IDS:
        try:
            await msg.copy(a)
        except TelegramError:
            pass
        await tell(context.bot, a, card, kb)
    await msg.reply_text(fancy("✅", "Proof received", note="An admin will verify your payment shortly. You'll get a message here."), parse_mode=HTML)


# ───────────────────────── Premium: admin panel ─────────────────────────
SET_PROMPT = {
    "price": "💰 Send the new <b>price</b> (number only).\nExample: <code>300</code>",
    "days": "⏳ Send the <b>premium duration in days</b>.\nExample: <code>30</code>",
    "jc": "🔴 Send the <b>JazzCash number and account name</b>.\nExample: <code>03001234567 Ali Khan</code>\nSend <code>-</code> to remove.",
    "ep": "🟢 Send the <b>Easypaisa number and account name</b>.\nExample: <code>03001234567 Ali Khan</code>\nSend <code>-</code> to remove.",
    "sponsor": "📣 Send the <b>sponsor line</b> (max 200 characters).\nIt appears under bot messages for non-premium users.",
    "give": "➕ Send the <b>user chat ID</b> and optionally the days.\nExample: <code>123456789 30</code>\n(No days = default duration.)",
    "rmprem": "➖ Send the <b>user chat ID</b> to remove premium.",
}


def prem_admin_view():
    p, sp = db["pay"], db["sponsor"]
    now = time.time()
    active = sum(1 for v in db["premium"].values() if v > now)

    def acc(m):
        a = p[m]
        return f"<code>{esc(a['num'])}</code> {esc(a['name'])}" if a["num"] else "<i>not set</i>"

    rows = [
        ("Price", f"<b>{esc(p['cur'])} {p['price']:,}</b> / {p['days']} days" if p["price"] else "<i>not set</i>"),
        ("JazzCash", acc("jc")),
        ("Easypaisa", acc("ep")),
        ("Premium users", f"<b>{active}</b>"),
        ("Pending proofs", f"<b>{len(db['pending'])}</b>"),
        ("Sponsor", (("ON ✅ " if sp["on"] else "OFF ❌ ") + esc(sp["text"][:60])) if sp["text"] else "<i>none</i>"),
    ]
    text = fancy("⭐", "Premium & Sponsor", rows, note="Grant premium by user chat ID with ➕ below, or <code>/givepremium ID [days]</code>.")
    kb = Markup([
        [Btn("💰 Set price", callback_data="adm:set:price"), Btn("⏳ Set days", callback_data="adm:set:days")],
        [Btn("🔴 JazzCash", callback_data="adm:set:jc", style="danger"), Btn("🟢 Easypaisa", callback_data="adm:set:ep", style="success")],
        [Btn("➕ Give premium", callback_data="adm:set:give", style="success"), Btn("➖ Remove premium", callback_data="adm:set:rmprem")],
        [Btn("📋 Premium users", callback_data="adm:pl", style="primary")],
        [Btn("📣 Set sponsor line", callback_data="adm:set:sponsor"), Btn(f"📣 {'Turn OFF' if sp['on'] else 'Turn ON'}", callback_data="adm:sp:tog")],
        [Btn("🗑 Clear sponsor", callback_data="adm:sp:clr")],
        [Btn("⬅️ Back", callback_data="adm:home", style="primary")],
    ])
    return text, kb


async def cb_admin_prem(update: Update, context: ContextTypes.DEFAULT_TYPE, parts: list):
    q, ud = update.callback_query, context.user_data
    action = parts[1]
    if action == "set" and parts[2] in SET_PROMPT:
        ud["await_set"] = parts[2]
        await safe_edit(q.message, SET_PROMPT[parts[2]], Markup([[Btn("❌ Cancel", callback_data="adm:prem")]]))
        return
    if action == "sp":
        if parts[2] == "tog":
            db["sponsor"]["on"] = not db["sponsor"]["on"]
        elif parts[2] == "clr":
            db["sponsor"] = {"on": False, "text": ""}
        touch()
    if action == "pl":
        now = time.time()
        items = sorted(((int(v), k) for k, v in db["premium"].items() if v > now), reverse=True)[:30]
        lines = [f"<code>{k}</code> — {(v - int(now)) // 86400}d left" for v, k in items] or ["<i>No premium users yet.</i>"]
        await safe_edit(q.message, fancy("📋", "Premium users", note="\n".join(lines)),
                        Markup([[Btn("⬅️ Back", callback_data="adm:prem", style="primary")]]))
        return
    text, kb = prem_admin_view()
    await safe_edit(q.message, text, kb)


async def admin_set_flow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg, ud, p = update.message, context.user_data, db["pay"]
    field = ud.pop("await_set", None)
    text = (msg.text or "").strip()
    if not text:
        ud["await_set"] = field
        await msg.reply_text("❌ Please send text.")
        return
    note = ""
    try:
        if field == "price":
            p["price"] = max(0, int(text.replace(",", "")))
        elif field == "days":
            p["days"] = max(1, int(text))
        elif field in ("jc", "ep"):
            if text == "-":
                p[field] = {"num": "", "name": ""}
            else:
                num, _, nm = text.partition(" ")
                p[field] = {"num": num.strip()[:20], "name": nm.strip()[:40]}
        elif field == "sponsor":
            db["sponsor"] = {"on": True, "text": text[:200]}
        elif field == "give":
            bits = text.split()
            uid, days = int(bits[0]), (int(bits[1]) if len(bits) > 1 else p["days"])
            exp = grant_premium(uid, days if days > 0 else p["days"])
            sent = await tell(context.bot, uid, fancy("🎉", "Premium activated!", [("Valid until", f"<b>{fmt_date(exp)}</b>")], note=premium_perks()))
            note = f"✅ Premium given to <code>{uid}</code> until <b>{fmt_date(exp)}</b>" + ("" if sent else "\n⚠️ Couldn't message the user (they must /start the bot).") + "\n\n"
        elif field == "rmprem":
            uid = int(text.split()[0])
            db["premium"].pop(str(uid), None)
            note = f"✅ Premium removed from <code>{uid}</code>\n\n"
        else:
            return
    except (ValueError, IndexError):
        ud["await_set"] = field
        await msg.reply_text("❌ Invalid value. Try again.")
        return
    touch()
    view, kb = prem_admin_view()
    await msg.reply_text(note + "✅ Saved\n\n" + view if not note else note + view, parse_mode=HTML, reply_markup=kb)


async def cmd_givepremium(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg, user = update.message, update.effective_user
    if not is_admin(user.id):
        await msg.reply_text("⛔ Admins only.")
        return
    try:
        uid = int(context.args[0])
        days = int(context.args[1]) if len(context.args) > 1 else db["pay"]["days"]
    except (ValueError, IndexError):
        await msg.reply_text("Usage: /givepremium <chat_id> [days]")
        return
    exp = grant_premium(uid, days if days > 0 else db["pay"]["days"])
    sent = await tell(context.bot, uid, fancy("🎉", "Premium activated!", [("Valid until", f"<b>{fmt_date(exp)}</b>")], note=premium_perks()))
    await msg.reply_text(f"✅ Premium given to <code>{uid}</code> until <b>{fmt_date(exp)}</b>" + ("" if sent else "\n⚠️ Couldn't message the user (they must /start the bot)."), parse_mode=HTML)


async def cmd_removepremium(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not is_admin(update.effective_user.id):
        await msg.reply_text("⛔ Admins only.")
        return
    try:
        uid = int(context.args[0])
    except (ValueError, IndexError):
        await msg.reply_text("Usage: /removepremium <chat_id>")
        return
    db["premium"].pop(str(uid), None)
    touch()
    await msg.reply_text(f"✅ Premium removed from <code>{uid}</code>", parse_mode=HTML)


# ───────────────────────── Footer / sponsor on every message ─────────────────────────
def _footer(chat_id, html_mode: bool, want_sponsor: bool) -> str:
    g = db["groups"].get(str(chat_id)) if chat_id is not None else None
    if g and brand_active(g):                       # per-group branding replaces the credit (and the sponsor)
        return f"\n\n{FOOT}✨ <i>{esc(g['brand'])}</i>" if html_mode else f"\n\n{FOOT}✨ {g['brand']}"
    out = ""
    sp = db["sponsor"]
    private_premium = isinstance(chat_id, int) and chat_id > 0 and is_premium(chat_id)
    if want_sponsor and sp.get("on") and sp.get("text") and not private_premium:
        out += f"\n\n{FOOT}📣 <i>{esc(sp['text'])}</i>" if html_mode else f"\n\n{FOOT}📣 {sp['text']}"
    credit = CREDIT_HTML if html_mode else CREDIT_PLAIN
    return out + (credit if out else credit.replace("\n\n", f"\n\n{FOOT}", 1))


def _with_credit(kw: dict, field: str, chat_id=None, sponsor_ok: bool = False):
    txt = kw.get(field)
    if not isinstance(txt, str):
        return
    had = SPON in txt
    txt = txt.replace(SPON, "")
    if FOOT in txt or CREDIT_USER in txt:
        kw[field] = txt
        return
    html_mode = str(kw.get("parse_mode") or "").upper() == "HTML"
    kw[field] = txt + _footer(chat_id, html_mode, sponsor_ok or had)


def install_credit():
    """Footer on every text/caption: 'Developed by Dock' (or the group's own footer), plus the optional sponsor line."""
    from telegram.ext import ExtBot

    def wrap(name, field, pos, sponsor_ok=False):
        orig = getattr(ExtBot, name)
        sends = name.startswith("send_")

        async def inner(self, *args, **kw):
            cid = kw.get("chat_id", args[0] if (sends and args) else None)
            if field in kw:
                _with_credit(kw, field, cid, sponsor_ok)
            elif pos is not None and len(args) > pos:
                tmp = {field: args[pos], "parse_mode": kw.get("parse_mode")}
                _with_credit(tmp, field, cid, sponsor_ok)
                args = args[:pos] + (tmp[field],) + args[pos + 1:]
            return await orig(self, *args, **kw)

        inner.__name__ = name
        setattr(ExtBot, name, inner)

    wrap("send_message", "text", 1)
    wrap("edit_message_text", "text", 0)
    wrap("edit_message_caption", "caption", None)
    for n in ("send_photo", "send_video", "send_audio"):
        wrap(n, "caption", None, sponsor_ok=True)
    wrap("send_document", "caption", None)


# ───────────────────────── Main ─────────────────────────
async def post_init(app: Application):
    app.bot_data["saver"] = asyncio.create_task(saver())
    app.bot_data["watch"] = asyncio.create_task(release_watcher(app.bot))
    await app.bot.set_my_commands([
        BotCommand("start", "Start the bot"),
        BotCommand("song", "/song name a (audio) or v (video 480p)"),
        BotCommand("search", "Search a song: /search song name"),
        BotCommand("help", "How to use"),
        BotCommand("find", "Identify a song (reply to audio/voice)"),
        BotCommand("lyrics", "Song lyrics: /lyrics song name"),
        BotCommand("lang", "Change language (EN / اردو / हिन्दी)"),
        BotCommand("artist", "Artist page: /artist name"),
        BotCommand("follow", "Get alerts for new releases: /follow artist"),
        BotCommand("following", "Artists you follow"),
        BotCommand("invite", "Invite friends, earn more recognitions"),
        BotCommand("premium", "Premium: priority downloads & more"),
        BotCommand("brand", "Group footer (premium): /brand text"),
        BotCommand("stats", "Bot statistics"),
        BotCommand("id", "Show your Telegram ID"),
    ])


async def post_shutdown(app: Application):
    save_db()


def main():
    if not BOT_TOKEN:
        raise SystemExit("Set BOT_TOKEN environment variable first.")
    if not ADMIN_IDS:
        log.warning("ADMIN_IDS is empty — admin features unavailable. Send /id to the bot to get your ID.")
    if not shutil.which("ffmpeg"):
        log.warning("ffmpeg not found! Video merging / audio conversion may fail.")

    install_credit()
    app = (
        Application.builder().token(BOT_TOKEN)
        .concurrent_updates(True).connection_pool_size(32)
        .post_init(post_init).post_shutdown(post_shutdown)
        .build()
    )

    app.add_handler(TypeHandler(Update, gate), group=-1)

    for name, fn in [
        ("start", cmd_start), ("help", cmd_help), ("id", cmd_id), ("song", cmd_song), ("search", cmd_search),
        ("admin", cmd_admin), ("groups", cmd_groups), ("ban", cmd_ban), ("unban", cmd_unban),
        ("warn", cmd_warn), ("approve", cmd_approve), ("unapprove", cmd_unapprove),
        ("lyrics", cmd_lyrics), ("find", cmd_find), ("lang", cmd_lang), ("stats", cmd_stats),
        ("artist", cmd_artist), ("follow", cmd_follow), ("following", cmd_following), ("invite", cmd_invite),
        ("premium", cmd_premium), ("brand", cmd_brand), ("givepremium", cmd_givepremium),
        ("removepremium", cmd_removepremium),
    ]:
        app.add_handler(CommandHandler(name, fn))

    app.add_handler(CallbackQueryHandler(cb_help, pattern=r"^help$"))
    app.add_handler(CallbackQueryHandler(cb_invite, pattern=r"^inv$"))
    app.add_handler(CallbackQueryHandler(cb_info, pattern=r"^in:"))
    app.add_handler(CallbackQueryHandler(cb_bpm, pattern=r"^bk:"))
    app.add_handler(CallbackQueryHandler(cb_share, pattern=r"^sh:"))
    app.add_handler(CallbackQueryHandler(cb_artist, pattern=r"^ar:"))
    app.add_handler(CallbackQueryHandler(cb_follow, pattern=r"^fl:"))
    app.add_handler(CallbackQueryHandler(cb_unfollow, pattern=r"^uf:"))
    app.add_handler(CallbackQueryHandler(cb_pay, pattern=r"^pay:"))
    app.add_handler(CallbackQueryHandler(cb_lang, pattern=r"^lang:"))
    app.add_handler(CallbackQueryHandler(cb_lyrics, pattern=r"^ly:"))
    app.add_handler(CallbackQueryHandler(cb_pick, pattern=r"^p:"))
    app.add_handler(CallbackQueryHandler(cb_download, pattern=r"^d:"))
    app.add_handler(CallbackQueryHandler(cb_admin, pattern=r"^adm:"))
    app.add_handler(CallbackQueryHandler(cb_groups, pattern=r"^grp:"))
    app.add_handler(ChatMemberHandler(on_my_member, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_handler(InlineQueryHandler(on_inline))
    app.add_handler(MessageHandler(filters.COMMAND, unknown_command))  # after all CommandHandlers
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, on_message))

    log.info("Bot started...")
    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
