#!/usr/bin/env python3
"""
============================================================
  Telegram VC Bot — Single File
  Commands:
    /join              — join VC silently
    /leave             — leave VC
    /vcinfo            — show participants + playback status
    /play <song name>  — search YouTube, download, play in VC
    /stop              — stop playback, stay in VC (silent)
    /pause             — pause current track
    /resume            — resume paused track

  Credentials hardcoded directly in CONFIGURATION section.
  YouTube download: android client, no cookies, format 18.
  Cache: cache/index.json maps video_id -> local mp3 path.
============================================================
"""

import html
import asyncio
import json
import logging
import os
import wave
import glob
import functools
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor

import yt_dlp

from pyrogram import Client
from pyrogram.raw import functions as raw_fn
from pyrogram.raw import types     as raw_ty

from pytgcalls        import PyTgCalls
from pytgcalls.types  import MediaStream, AudioQuality

from aiogram         import Bot, Dispatcher
from aiogram.filters import Command
from aiogram.types   import Message

# ══════════════════════════════════════════════════════════
#  CONFIGURATION — fill in your credentials here
# ══════════════════════════════════════════════════════════
BOT_TOKEN     = "7781750126:AAEMY67mZixlhz3W5QyQNOdSd6jPVCkbAV4"
GROUP_CHAT_ID = -1002266811493      # your group chat id (negative number)

API_ID        = 28165213            # from my.telegram.org
API_HASH      = "74983137f88bb852802637dadf3d44a3"
SESSION_STR   = "BQGtxF0ACMlQJ4Yp2QkynXYhXKVV5sTwW7m2wKdgi5jjj8YZ0q7SrcL1L7JWcM4-b54y79YA3kLKUr_XoApnnNA_9m0Kcl_Bb9jf19zxmurZlcdZbB_hIDtR7aqxDMDjJGNbhTqwCzDk0voKOiRGncIFF0R_Ch1H9YDN9ZcmagWBvCfycMP4QbJlae5lLmkGnNsk8R--zVRhhlZw0voGvlTESskkF_fbYYaN7UomTRsMPIE8IIh6Rr2FJeAR1MP9SZagejPC49ma1RB4ODfTMiYtI7S1FpvRnhbf8k4YMQjcQBWVQAi6Y0NbHt-KiAhrTcxbKpnh02acbfT76zQqCzkkmTqhaAAAAAHSbKT2AA"

SILENCE_WAV = "silence.wav"
CACHE_DIR   = "cache"
CACHE_INDEX = "cache/index.json"

# ══════════════════════════════════════════════════════════
#  LOGGING
# ══════════════════════════════════════════════════════════
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("vc_bot")
for _noisy in ("pyrogram", "pytgcalls", "ntgcalls", "aiogram"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

_executor = ThreadPoolExecutor(max_workers=2)

# ══════════════════════════════════════════════════════════
#  SILENCE FILE  (1 hour)
# ══════════════════════════════════════════════════════════
def generate_silence(path: str, seconds: int = 3600) -> None:
    sample_rate = 48_000
    channels    = 2
    sampwidth   = 2
    n_frames    = sample_rate * seconds
    with wave.open(path, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(sampwidth)
        wf.setframerate(sample_rate)
        wf.writeframes(b"\x00" * n_frames * channels * sampwidth)
    logger.info("Generated silence.wav -> %.1f MB",
                os.path.getsize(path) / 1_048_576)

# ══════════════════════════════════════════════════════════
#  CACHE SYSTEM
#  cache/index.json  →  { "VIDEO_ID": { "file": "cache/VIDEO_ID.mp3",
#                                        "title": "...", "duration": N,
#                                        "uploader": "..." } }
# ══════════════════════════════════════════════════════════
def _load_cache() -> dict:
    if os.path.exists(CACHE_INDEX):
        try:
            with open(CACHE_INDEX, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save_cache(index: dict) -> None:
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(CACHE_INDEX, "w") as f:
        json.dump(index, f, indent=2)


def _cache_lookup(video_id: str) -> dict | None:
    """Return cached info dict if file exists on disk, else None."""
    index = _load_cache()
    entry = index.get(video_id)
    if entry and os.path.exists(entry.get("file", "")):
        return entry
    return None


def _cache_store(video_id: str, info: dict) -> None:
    """Add or update entry in cache index."""
    index = _load_cache()
    index[video_id] = {
        "file":     info["file"],
        "title":    info.get("title",    "Unknown"),
        "duration": info.get("duration", 0),
        "uploader": info.get("uploader", "Unknown"),
        "url":      info.get("url",      ""),
    }
    _save_cache(index)
    logger.info("Cached: %s -> %s", video_id, info["file"])

# ══════════════════════════════════════════════════════════
#  YOUTUBE DOWNLOAD  (android client, no cookies, format 18)
#  ── DO NOT CHANGE THIS SECTION ──
# ══════════════════════════════════════════════════════════
def _ytdlp_download_sync(query: str) -> dict:
    """
    Blocking — runs in thread executor.
    1. Extracts video info WITHOUT downloading to get video_id.
    2. Checks cache — returns cached entry immediately on hit.
    3. Downloads to cache/VIDEO_ID.mp3 on cache miss.
    """
    os.makedirs(CACHE_DIR, exist_ok=True)

    # Locate ffmpeg explicitly — yt-dlp running inside a venv may not
    # find the system ffmpeg automatically. We search common paths and
    # pass the absolute path so post-processing never silently fails.
    import shutil
    _ffmpeg_path = (
        shutil.which("ffmpeg")          # checks $PATH (works most of the time)
        or "/usr/bin/ffmpeg"            # Ubuntu/Debian default
        or "/usr/local/bin/ffmpeg"      # compiled-from-source installs
    )
    if not os.path.isfile(_ffmpeg_path or ""):
        raise FileNotFoundError(
            "ffmpeg not found. Install it: sudo apt install ffmpeg -y"
        )
    logger.info("Using ffmpeg: %s", _ffmpeg_path)

    # HTTP 403 fix: spoof the exact User-Agent YouTube's Android app sends,
    # and widen the client fallback chain so if android 403s we try
    # android_embedded then android_vr — all bypass PO Token requirements.
    base_opts = {
        "format": (
            "18"
            "/bestaudio[acodec!=none][protocol=https]"
            "/bestaudio[acodec!=none][protocol^=http]"
            "/bestaudio[acodec!=none]"
            "/best[acodec!=none]"
            "/best"
        ),
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "socket_timeout": 30,
        "retries": 5,
        "fragment_retries": 5,
        # Explicitly tell yt-dlp where ffmpeg is — fixes post-processing
        # failures inside Python venvs where PATH may not include /usr/bin
        "ffmpeg_location": _ffmpeg_path,
        "extractor_args": {
            # Try android first, fall back to android_embedded and android_vr
            # All three bypass PO Token; none accept cookies
            "youtube": {"player_client": ["android", "android_embedded", "android_vr"]}
        },
        # Spoof Android YouTube app User-Agent to avoid 403 on VPS IPs
        "http_headers": {
            "User-Agent": (
                "com.google.android.youtube/19.09.37 "
                "(Linux; U; Android 11) gzip"
            ),
        },
        # NO cookiefile — android clients reject cookies
    }

    search_target = query
    if not query.startswith("http://") and not query.startswith("https://"):
        search_target = "ytsearch1:" + query

    # Step 1: info only (no download) — get video_id
    info_opts = {**base_opts, "skip_download": True}
    with yt_dlp.YoutubeDL(info_opts) as ydl:
        data  = ydl.extract_info(search_target, download=False)
        entry = (data["entries"][0]
                 if "entries" in data and data["entries"]
                 else data)

    video_id = entry.get("id",           "")
    title    = entry.get("title",        "Unknown")
    duration = entry.get("duration",     0)
    uploader = entry.get("uploader",     "Unknown")
    url      = entry.get("webpage_url",  "")

    # Step 2: cache hit?
    if video_id:
        cached = _cache_lookup(video_id)
        if cached:
            logger.info("Cache HIT: %s (%s)", video_id, cached["file"])
            cached["from_cache"] = True
            return cached

    # Step 3: cache miss — download to cache/VIDEO_ID.mp3
    out_name = os.path.join(CACHE_DIR, video_id if video_id else "track")
    dl_opts  = {
        **base_opts,
        "outtmpl": out_name + ".%(ext)s",
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }],
    }

    with yt_dlp.YoutubeDL(dl_opts) as ydl:
        ydl.extract_info(search_target, download=True)

    # Find the output file (should be cache/VIDEO_ID.mp3)
    candidates = glob.glob(out_name + ".*")
    mp3_file   = ""
    for ext in ["mp3", "m4a", "webm", "ogg", "opus", "mp4"]:
        for c in candidates:
            if c.endswith("." + ext):
                mp3_file = c
                break
        if mp3_file:
            break
    if not mp3_file and candidates:
        mp3_file = candidates[0]

    if not mp3_file or not os.path.exists(mp3_file):
        raise FileNotFoundError(
            "yt-dlp finished but no audio file found. "
            "Is ffmpeg installed? Run: sudo apt install ffmpeg -y"
        )

    logger.info("Downloaded: %s (%.2f MB)",
                mp3_file, os.path.getsize(mp3_file) / 1_048_576)

    result = {
        "file":       mp3_file,
        "title":      title,
        "duration":   duration,
        "uploader":   uploader,
        "url":        url,
        "from_cache": False,
    }

    if video_id:
        _cache_store(video_id, result)

    return result


async def ytdlp_download(query: str) -> dict:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        _executor,
        functools.partial(_ytdlp_download_sync, query)
    )


def _fmt_duration(seconds) -> str:
    if not seconds:
        return "unknown"
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"

# ══════════════════════════════════════════════════════════
#  MODULE-LEVEL REFERENCES (assigned inside main)
# ══════════════════════════════════════════════════════════
userbot: Client    = None  # type: ignore
calls:  PyTgCalls  = None  # type: ignore
bot:    Bot        = None  # type: ignore
dp:     Dispatcher = None  # type: ignore

_in_vc:      bool = False
_playing:    bool = False
_paused:     bool = False
_switching:  bool = False
_track_info: dict = {}

# Confirmed via check_pytgcalls.py — py-tgcalls v2.2.11
_PAUSE_METHOD:  str = "pause"
_RESUME_METHOD: str = "resume"

# ══════════════════════════════════════════════════════════
#  HELPERS
# ══════════════════════════════════════════════════════════
def _fmt_name(user) -> str:
    first = getattr(user, "first_name", "") or ""
    last  = getattr(user, "last_name",  "") or ""
    return (first + " " + last).strip() or "Unknown"


async def _get_call_and_peer(chat_id: int):
    peer = await userbot.resolve_peer(chat_id)
    try:
        if isinstance(peer, raw_ty.InputPeerChannel):
            fc = await userbot.invoke(raw_fn.channels.GetFullChannel(channel=peer))
        else:
            fc = await userbot.invoke(raw_fn.messages.GetFullChat(chat_id=peer.chat_id))
        return peer, fc.full_chat.call
    except Exception as e:
        logger.warning("Could not resolve group call: %s", e)
        return peer, None


async def _play_silence() -> None:
    global _switching
    _switching = True
    try:
        await calls.play(
            GROUP_CHAT_ID,
            MediaStream(SILENCE_WAV, audio_parameters=AudioQuality.STUDIO),
        )
        await asyncio.sleep(3)
    finally:
        _switching = False


async def _play_local_file(path: str) -> None:
    global _switching
    _switching = True
    try:
        await calls.play(
            GROUP_CHAT_ID,
            MediaStream(path, audio_parameters=AudioQuality.STUDIO),
        )
        await asyncio.sleep(3)
    finally:
        _switching = False


def _wrong_chat(message: Message) -> bool:
    return message.chat.id != GROUP_CHAT_ID

# ══════════════════════════════════════════════════════════
#  /join
# ══════════════════════════════════════════════════════════
async def cmd_join(message: Message) -> None:
    global _in_vc
    if _wrong_chat(message):
        await message.reply("This bot only works in its designated group.")
        return
    if _in_vc:
        await message.reply("Already in the voice chat!")
        return
    status_msg = await message.reply("Joining voice chat...")
    try:
        await _play_silence()
        _in_vc = True
        await status_msg.edit_text(
            "Joined the voice chat!\n"
            "Secondary account is now present in the VC (silent).\n"
            "Use /play <song name> to play music or /leave to disconnect."
        )
    except Exception as exc:
        logger.error("join failed: %s", exc, exc_info=True)
        err = str(exc)
        if "GROUPCALL_FORBIDDEN" in err:
            await status_msg.edit_text(
                "No active voice chat found. Start one in the group first.")
        elif "already" in err.lower():
            _in_vc = True
            await status_msg.edit_text("Secondary account is already in the VC.")
        else:
            await status_msg.edit_text("Failed to join:\n" + html.escape(err))

# ══════════════════════════════════════════════════════════
#  /leave
# ══════════════════════════════════════════════════════════
async def cmd_leave(message: Message) -> None:
    global _in_vc, _playing, _paused, _track_info
    if _wrong_chat(message):
        await message.reply("This bot only works in its designated group.")
        return
    if not _in_vc:
        await message.reply("Not currently in any voice chat.")
        return
    status_msg = await message.reply("Leaving voice chat...")
    try:
        await calls.leave_call(GROUP_CHAT_ID)
        _in_vc = _playing = _paused = False
        _track_info = {}
        await status_msg.edit_text("Left the voice chat.")
    except Exception as exc:
        logger.error("leave failed: %s", exc, exc_info=True)
        _in_vc = _playing = _paused = False
        _track_info = {}
        err = str(exc)
        if "not" in err.lower() or "GROUPCALL_JOIN_MISSING" in err:
            await status_msg.edit_text("Was not connected to any voice chat.")
        else:
            await status_msg.edit_text("Error leaving:\n" + html.escape(err))

# ══════════════════════════════════════════════════════════
#  /vcinfo
# ══════════════════════════════════════════════════════════
async def cmd_vcinfo(message: Message) -> None:
    if _wrong_chat(message):
        await message.reply("This bot only works in its designated group.")
        return
    status_msg = await message.reply("Fetching voice chat info...")
    try:
        peer, call = await _get_call_and_peer(GROUP_CHAT_ID)
        if call is None:
            await status_msg.edit_text(
                "No active voice chat in this group.\n"
                "Start one and use /join to hop in!")
            return
        result = await userbot.invoke(
            raw_fn.phone.GetGroupParticipants(
                call=call, ids=[], sources=[], offset="", limit=500,
            )
        )
        participants = result.participants
        users_map    = {u.id: u for u in result.users}
        chats_map    = {c.id: c for c in result.chats}
        lines = []
        for idx, p in enumerate(participants, 1):
            muted    = "muted" if getattr(p, "muted", False) else "unmuted"
            raised   = " [hand raised]" if getattr(p, "raise_hand_rating", 0) else ""
            peer_obj = p.peer
            if isinstance(peer_obj, raw_ty.PeerUser):
                u     = users_map.get(peer_obj.user_id)
                name  = _fmt_name(u) if u else "User#" + str(peer_obj.user_id)
                uname = ("@" + u.username if u and u.username
                         else "id:" + str(peer_obj.user_id))
                lines.append(str(idx) + ". " + html.escape(name)
                             + " (" + html.escape(uname) + ") [" + muted + "]" + raised)
            elif isinstance(peer_obj, raw_ty.PeerChannel):
                c    = chats_map.get(peer_obj.channel_id)
                name = c.title if c else "Channel#" + str(peer_obj.channel_id)
                lines.append(str(idx) + ". " + html.escape(name) + " [channel]" + raised)
            elif isinstance(peer_obj, raw_ty.PeerChat):
                c    = chats_map.get(peer_obj.chat_id)
                name = c.title if c else "Chat#" + str(peer_obj.chat_id)
                lines.append(str(idx) + ". " + html.escape(name) + " [chat]" + raised)
        count             = len(participants)
        participant_block = "\n".join(lines) if lines else "  (no participants found)"
        ts                = datetime.now().strftime("%H:%M:%S")
        if _playing and not _paused and _track_info:
            pb = ("Playing: " + _track_info.get("title", "?") +
                  " [" + _fmt_duration(_track_info.get("duration", 0)) + "]")
        elif _playing and _paused and _track_info:
            pb = "Paused: " + _track_info.get("title", "?")
        else:
            pb = "Silent (no audio)"
        text = (
            "Voice Chat Info  [" + ts + "]\n"
            "--------------------\n"
            "Participants: " + str(count) + "\n"
            "Playback: " + pb + "\n\n"
            + participant_block
        )
        await status_msg.edit_text(text)
    except Exception as exc:
        logger.error("vcinfo failed: %s", exc, exc_info=True)
        await status_msg.edit_text(
            "Could not fetch VC info:\n" + html.escape(str(exc)))

# ══════════════════════════════════════════════════════════
#  /play <song name or YouTube URL>
# ══════════════════════════════════════════════════════════
async def cmd_play(message: Message) -> None:
    global _in_vc, _playing, _paused, _track_info
    if _wrong_chat(message):
        await message.reply("This bot only works in its designated group.")
        return
    text  = message.text or ""
    parts = text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await message.reply(
            "Usage: /play <song name or YouTube URL>\n"
            "Example: /play Shape of You Ed Sheeran")
        return
    query = parts[1].strip()
    if _playing and not _paused:
        await message.reply(
            "Already playing! Use /pause to pause or /stop to stop first.")
        return
    status_msg = await message.reply(
        "Searching YouTube for: " + html.escape(query) + "...")
    try:
        await status_msg.edit_text("Downloading audio...")
        info = await ytdlp_download(query)

        cached_note = " (from cache)" if info.get("from_cache") else ""
        title       = info.get("title",    "Unknown")
        duration    = info.get("duration", 0)
        uploader    = info.get("uploader", "Unknown")
        filepath    = info.get("file",     "")

        await status_msg.edit_text("Starting playback" + cached_note + "...")
        await _play_local_file(filepath)

        _in_vc      = True
        _playing    = True
        _paused     = False
        _track_info = info

        await status_msg.edit_text(
            "Now playing" + cached_note + "!\n"
            "Title: "    + html.escape(title)    + "\n"
            "Artist: "   + html.escape(uploader) + "\n"
            "Duration: " + _fmt_duration(duration) + "\n\n"
            "/pause  |  /stop  |  /leave"
        )
    except yt_dlp.utils.DownloadError as exc:
        logger.error("yt-dlp error: %s", exc)
        await status_msg.edit_text(
            "YouTube download failed:\n" + html.escape(str(exc)[:300]))
    except FileNotFoundError as exc:
        logger.error("Missing ffmpeg or output file: %s", exc)
        await status_msg.edit_text(
            "ffmpeg not installed.\nFix: sudo apt install ffmpeg -y")
    except Exception as exc:
        logger.error("play failed: %s", exc, exc_info=True)
        err = str(exc)
        if "GROUPCALL_FORBIDDEN" in err:
            await status_msg.edit_text(
                "No active voice chat found. Start one in the group first.")
        else:
            await status_msg.edit_text(
                "Failed to play:\n" + html.escape(err[:300]))

# ══════════════════════════════════════════════════════════
#  /stop
# ══════════════════════════════════════════════════════════
async def cmd_stop(message: Message) -> None:
    global _playing, _paused, _track_info
    if _wrong_chat(message):
        await message.reply("This bot only works in its designated group.")
        return
    if not _playing:
        await message.reply("Nothing is playing right now.")
        return
    status_msg = await message.reply("Stopping playback...")
    try:
        await _play_silence()
        _playing    = False
        _paused     = False
        _track_info = {}
        await status_msg.edit_text(
            "Playback stopped.\n"
            "Secondary account is still in VC (silent).\n"
            "Use /play <song name> to play again or /leave to exit."
        )
    except Exception as exc:
        logger.error("stop failed: %s", exc, exc_info=True)
        await status_msg.edit_text("Failed to stop:\n" + html.escape(str(exc)))

# ══════════════════════════════════════════════════════════
#  /pause
# ══════════════════════════════════════════════════════════
async def cmd_pause(message: Message) -> None:
    global _paused
    if _wrong_chat(message):
        await message.reply("This bot only works in its designated group.")
        return
    if not _playing:
        await message.reply("Nothing is playing right now.")
        return
    if _paused:
        await message.reply("Already paused. Use /resume to continue.")
        return
    status_msg = await message.reply("Pausing...")
    try:
        await getattr(calls, _PAUSE_METHOD)(GROUP_CHAT_ID)
        _paused = True
        title   = _track_info.get("title", "track")
        await status_msg.edit_text(
            "Paused: " + html.escape(title) + "\n"
            "Use /resume to continue.")
    except Exception as exc:
        logger.error("pause failed: %s", exc, exc_info=True)
        await status_msg.edit_text("Failed to pause:\n" + html.escape(str(exc)))

# ══════════════════════════════════════════════════════════
#  /resume
# ══════════════════════════════════════════════════════════
async def cmd_resume(message: Message) -> None:
    global _paused
    if _wrong_chat(message):
        await message.reply("This bot only works in its designated group.")
        return
    if not _playing:
        await message.reply("Nothing is playing. Use /play <song name> to start.")
        return
    if not _paused:
        await message.reply("Already playing! Use /pause to pause.")
        return
    status_msg = await message.reply("Resuming...")
    try:
        await getattr(calls, _RESUME_METHOD)(GROUP_CHAT_ID)
        _paused  = False
        title    = _track_info.get("title",    "track")
        uploader = _track_info.get("uploader", "")
        duration = _track_info.get("duration", 0)
        await status_msg.edit_text(
            "Resumed!\n"
            "Title: "    + html.escape(title)    + "\n"
            "Artist: "   + html.escape(uploader) + "\n"
            "Duration: " + _fmt_duration(duration) + "\n\n"
            "/pause  |  /stop  |  /leave"
        )
    except Exception as exc:
        logger.error("resume failed: %s", exc, exc_info=True)
        await status_msg.edit_text("Failed to resume:\n" + html.escape(str(exc)))

# ══════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════
async def main() -> None:
    global userbot, calls, bot, dp
    global _in_vc, _playing, _paused, _switching, _track_info

    # 1. Silence file
    if os.path.exists(SILENCE_WAV):
        if os.path.getsize(SILENCE_WAV) / 1_048_576 < 100:
            logger.info("Replacing short silence.wav with 1-hour version...")
            os.remove(SILENCE_WAV)
    if not os.path.exists(SILENCE_WAV):
        logger.info("Generating silence.wav (1 hour)...")
        generate_silence(SILENCE_WAV, seconds=3600)

    # 2. Cache directory
    os.makedirs(CACHE_DIR, exist_ok=True)
    cached_count = len(_load_cache())
    logger.info("Cache: %d track(s) in %s", cached_count, CACHE_DIR)

    # 3. Create all clients inside this coroutine (same event loop)
    userbot = Client(
        name           = "vc_userbot_session",
        api_id         = API_ID,
        api_hash       = API_HASH,
        session_string = SESSION_STR,
        no_updates     = False,
    )
    calls = PyTgCalls(userbot)
    bot   = Bot(token=BOT_TOKEN)
    dp    = Dispatcher()

    # 4. Register handlers
    dp.message.register(cmd_join,   Command("join"))
    dp.message.register(cmd_leave,  Command("leave"))
    dp.message.register(cmd_vcinfo, Command("vcinfo"))
    dp.message.register(cmd_play,   Command("play"))
    dp.message.register(cmd_stop,   Command("stop"))
    dp.message.register(cmd_pause,  Command("pause"))
    dp.message.register(cmd_resume, Command("resume"))

    # 5. PyTgCalls update handler
    @calls.on_update()
    async def on_update(client: PyTgCalls, update) -> None:
        global _in_vc, _playing, _paused, _switching, _track_info
        name = type(update).__name__.lower()
        logger.info("PyTgCalls update: %s (switching=%s, playing=%s)",
                    type(update).__name__, _switching, _playing)

        if any(k in name for k in ("kicked", "left", "closed")):
            _in_vc = _playing = _paused = _switching = False
            _track_info = {}
            logger.info("VC truly disconnected: %s", type(update).__name__)
            try:
                await bot.send_message(GROUP_CHAT_ID,
                    "Secondary account has left the voice chat.")
            except Exception:
                pass

        elif "ended" in name or "broken" in name:
            if _switching:
                logger.info("StreamEnded suppressed (intentional switch).")
                return
            logger.info("Stream ended naturally.")
            if _playing:
                title       = _track_info.get("title", "track")
                _playing    = False
                _paused     = False
                _track_info = {}
                try:
                    await _play_silence()
                    await bot.send_message(
                        GROUP_CHAT_ID,
                        "Playback finished: " + title + "\n"
                        "Use /play <song name> to play again."
                    )
                except Exception as e:
                    logger.warning("Failed to switch to silence: %s", e)
            else:
                logger.info("Silence ended, restarting...")
                try:
                    await _play_silence()
                except Exception as e:
                    logger.warning("Failed to restart silence: %s", e)

    # 6. Start userbot
    logger.info("Starting userbot (secondary account)...")
    await userbot.start()
    me = await userbot.get_me()
    logger.info("Userbot signed in as: %s (id=%s)", me.first_name, me.id)

    # 7. Start PyTgCalls
    logger.info("Starting PyTgCalls...")
    await calls.start()
    logger.info("PyTgCalls ready.")

    # 8. Start polling
    await bot.delete_webhook(drop_pending_updates=True)
    logger.info(
        "\n"
        "==========================================\n"
        "   VC Bot is ONLINE and READY!\n"
        "   /join              -> join VC (silent)\n"
        "   /leave             -> leave VC\n"
        "   /vcinfo            -> participants + status\n"
        "   /play <song name>  -> search YT + play\n"
        "   /stop              -> stop, stay in VC\n"
        "   /pause             -> pause\n"
        "   /resume            -> resume\n"
        "   Cache dir          : %s (%d tracks)\n"
        "==========================================",
        CACHE_DIR, cached_count
    )

    try:
        await dp.start_polling(bot, polling_timeout=30)
    finally:
        logger.info("Shutting down...")
        _executor.shutdown(wait=False)
        if _in_vc:
            try:
                await calls.leave_call(GROUP_CHAT_ID)
            except Exception:
                pass
        try:
            await bot.session.close()
        except Exception:
            pass
        try:
            await userbot.stop()
        except Exception:
            pass
        logger.info("Goodbye.")


if __name__ == "__main__":
    asyncio.run(main())
