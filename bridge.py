#!/usr/bin/env python3
"""
Meshtastic ⇄ Matrix Bridge

Bidirectional bridge between Meshtastic mesh radios and Matrix chat rooms.
Includes a full suite of bot commands (weather, earthquakes, NWS alerts, etc.)
matching community conventions (meshing-around / 868meshbot style).

Configuration: Copy config.example.yaml to config.yaml and edit.
"""

import asyncio
import json
import os
import random
import re
import subprocess
import shutil
import sys
import time
import urllib.request
from pathlib import Path
from typing import Dict, Optional, Any

import yaml
from pubsub import pub
from meshtastic.tcp_interface import TCPInterface
from nio import AsyncClient, RoomMessageText

# ---------------------------
# Configuration
# ---------------------------

CONFIG_PATH = Path(__file__).parent / "config.yaml"

def load_config() -> dict:
    """Load config from YAML file or environment variables."""
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH) as f:
            return yaml.safe_load(f)

    # Fallback to environment variables
    channels = {}
    for key, val in os.environ.items():
        if key.startswith("BRIDGE_CHANNEL_"):
            ch = int(key.replace("BRIDGE_CHANNEL_", ""))
            channels[ch] = val

    return {
        "meshtastic": {
            "host": os.environ.get("MESHTASTIC_HOST", "127.0.0.1"),
            "port": int(os.environ.get("MESHTASTIC_PORT", "4403")),
        },
        "matrix": {
            "homeserver": os.environ.get("MATRIX_HOMESERVER", "https://matrix.example.com"),
            "bot_user_id": os.environ.get("MATRIX_BOT_USER_ID", "@meshbot:matrix.example.com"),
            "access_token": os.environ.get("MATRIX_ACCESS_TOKEN", ""),
        },
        "channels": channels,
        "bot": {
            "location": os.environ.get("BOT_LOCATION", "Your City, State"),
            "nws_zone": os.environ.get("BOT_NWS_ZONE", ""),
            "quake_bounds": os.environ.get("BOT_QUAKE_BOUNDS", ""),
            "network_name": os.environ.get("BOT_NETWORK_NAME", "MeshNet"),
            "network_url": os.environ.get("BOT_NETWORK_URL", ""),
            "timezone": os.environ.get("BOT_TIMEZONE", "UTC"),
            "units": os.environ.get("BOT_UNITS", "imperial"),
            "motd": os.environ.get("BOT_MOTD", ""),
        },
    }

cfg = load_config()

MESHTASTIC_HOST = cfg["meshtastic"]["host"]
MESHTASTIC_PORT = cfg["meshtastic"]["port"]
MATRIX_HS = cfg["matrix"]["homeserver"]
BOT_USER_ID = cfg["matrix"]["bot_user_id"]
ACCESS_TOKEN = cfg["matrix"]["access_token"]

# Channel mapping: Meshtastic channel index → Matrix room ID
ROOM_BY_CH: Dict[int, str] = {int(k): v for k, v in cfg.get("channels", {}).items()}

# Bot config
BOT_LOCATION = cfg.get("bot", {}).get("location", "Unknown")
BOT_NWS_ZONE = cfg.get("bot", {}).get("nws_zone", "")
BOT_QUAKE_BOUNDS = cfg.get("bot", {}).get("quake_bounds", "")
BOT_NETWORK_NAME = cfg.get("bot", {}).get("network_name", "MeshNet")
BOT_NETWORK_URL = cfg.get("bot", {}).get("network_url", "")
BOT_TIMEZONE = cfg.get("bot", {}).get("timezone", "UTC")
BOT_UNITS = cfg.get("bot", {}).get("units", "imperial")
BOT_MOTD = cfg.get("bot", {}).get("motd", "")

UNIT_FLAG = "&u" if BOT_UNITS == "imperial" else "&m"
IN_PREFIX = "📡 Mesh"
DEDUP_TTL_SECONDS = 10

# ---------------------------
# Globals
# ---------------------------

MAIN_LOOP: Optional[asyncio.AbstractEventLoop] = None
mesh_iface: Optional[TCPInterface] = None
MESHTASTIC_BIN = shutil.which("meshtastic") or "meshtastic"

client = AsyncClient(MATRIX_HS)
client.user_id = BOT_USER_ID
client.access_token = ACCESS_TOKEN

CH_BY_ROOM = {room_id: ch for ch, room_id in ROOM_BY_CH.items()}

STARTUP_MS = int(time.time() * 1000)
MATRIX_LIVE = False

# ---------------------------
# Helpers
# ---------------------------

def _now() -> float:
    return time.time()

class Deduper:
    def __init__(self, ttl: int):
        self.ttl = ttl
        self.cache: Dict[str, float] = {}

    def seen_recently(self, key: str) -> bool:
        t = self.cache.get(key)
        if t is None:
            return False
        if _now() - t > self.ttl:
            self.cache.pop(key, None)
            return False
        return True

    def mark(self, key: str) -> None:
        self.cache[key] = _now()

dedup = Deduper(DEDUP_TTL_SECONDS)

def _to_int(v: Any) -> Optional[int]:
    if v is None:
        return None
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, int):
        return v
    if isinstance(v, bytes):
        if len(v) == 1:
            return v[0]
        return int.from_bytes(v, "big", signed=False)
    if isinstance(v, str):
        s = v.strip()
        if s.isdigit():
            return int(s)
    return None

def get_channel_index_from_packet(packet: Dict[str, Any]) -> Optional[int]:
    """Extract channel index from Meshtastic packet."""
    decoded = (packet or {}).get("decoded") or {}
    candidates = [
        packet.get("channel"), packet.get("channelIndex"), packet.get("channelNum"),
        decoded.get("channel"), decoded.get("channelIndex"), decoded.get("channelNum"),
        packet.get("rxChannel"), decoded.get("rxChannel"),
    ]
    for v in candidates:
        iv = _to_int(v)
        if iv is not None:
            return iv
    return None

def matrix_sender_short(mxid: str) -> str:
    """@dakota:matrix.example.com -> dakota"""
    if not mxid:
        return "unknown"
    if mxid.startswith("@") and ":" in mxid:
        return mxid[1:].split(":", 1)[0]
    return mxid

async def matrix_send(room_id: str, body: str) -> None:
    await client.room_send(
        room_id=room_id,
        message_type="m.room.message",
        content={"msgtype": "m.text", "body": body},
    )

# ---------------------------
# Meshtastic → Matrix
# ---------------------------

def on_mesh_receive(packet: Dict[str, Any], interface) -> None:
    decoded = (packet or {}).get("decoded") or {}
    text = decoded.get("text")
    if not text:
        return

    ch = get_channel_index_from_packet(packet)
    if ch is None:
        ch = 0

    room_id = ROOM_BY_CH.get(ch)
    if not room_id:
        return

    from_id = packet.get("fromId", "unknown")
    snr = packet.get("rxSnr")
    rssi = packet.get("rxRssi")

    meta = []
    if snr is not None:
        meta.append(f"snr={snr}")
    if rssi is not None:
        meta.append(f"rssi={rssi}")
    meta_s = f" ({', '.join(meta)})" if meta else ""

    body = f"{IN_PREFIX} [ch{ch}] {from_id}{meta_s}: {text}"

    dkey = f"mesh->{room_id}:{text}"
    if dedup.seen_recently(dkey):
        return
    dedup.mark(dkey)

    if MAIN_LOOP:
        asyncio.run_coroutine_threadsafe(matrix_send(room_id, body), MAIN_LOOP)

# ---------------------------
# Bot Commands
# ---------------------------

def get_weather() -> str:
    """Fetch current weather from wttr.in."""
    try:
        loc = BOT_LOCATION.replace(" ", "+")
        url = f"https://wttr.in/{loc}?format=%c+%t+%h+%w+%P{UNIT_FLAG}"
        req = urllib.request.Request(url, headers={"User-Agent": "meshtastic-matrix-bridge"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            line = resp.read().decode().strip()
        return f"WX {BOT_LOCATION}: {line}"
    except Exception as e:
        return f"WX unavailable: {e}"

def get_weather_forecast() -> str:
    """Fetch 3-day forecast from wttr.in."""
    try:
        loc = BOT_LOCATION.replace(" ", "+")
        url = f"https://wttr.in/{loc}?format=%c+%t(%f)+%w+%p{UNIT_FLAG}&T"
        req = urllib.request.Request(url, headers={"User-Agent": "meshtastic-matrix-bridge"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.read().decode().strip()[:200]
    except Exception as e:
        return f"Forecast unavailable: {e}"

def get_dad_joke() -> str:
    """Fetch a short dad joke from icanhazdadjoke.com."""
    try:
        req = urllib.request.Request(
            "https://icanhazdadjoke.com/",
            headers={"Accept": "text/plain", "User-Agent": "meshtastic-matrix-bridge"}
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            joke = resp.read().decode().strip()
        return joke[:200] if len(joke) > 200 else joke
    except Exception:
        return "Couldn't fetch a joke right now."

def get_fortune() -> str:
    """Get a random fortune/quote."""
    try:
        result = subprocess.run(["fortune", "-s"], capture_output=True, text=True, timeout=5)
        if result.returncode == 0 and result.stdout.strip():
            txt = result.stdout.strip()
            return txt[:200] if len(txt) > 200 else txt
    except Exception:
        pass
    quotes = [
        "The best time to plant a tree was 20 years ago. The second best time is now.",
        "Not all who wander are lost. — Tolkien",
        "In the middle of difficulty lies opportunity. — Einstein",
        "Stay hungry, stay foolish. — Steve Jobs",
        "It always seems impossible until it's done. — Mandela",
    ]
    return random.choice(quotes)

def get_mesh_nodes() -> str:
    """Get active Meshtastic node count and list."""
    try:
        result = subprocess.run(
            [MESHTASTIC_BIN, "--host", MESHTASTIC_HOST, "--nodes"],
            capture_output=True, text=True, timeout=15
        )
        lines = result.stdout.strip().split("\n")
        node_lines = [l for l in lines if "│" in l and "User" not in l and "───" not in l]
        count = len(node_lines)
        names = []
        for l in node_lines:
            parts = [p.strip() for p in l.split("│") if p.strip()]
            if len(parts) >= 2:
                names.append(parts[1][:20])
        if names:
            msg = f"📡 {count} nodes online: {', '.join(names[:10])}"
            if count > 10:
                msg += f" (+{count-10} more)"
            return msg
        return f"📡 {count} nodes seen on mesh"
    except Exception as e:
        return f"📡 Node check failed: {e}"

def get_sun() -> str:
    """Get sunrise/sunset times."""
    try:
        loc = BOT_LOCATION.replace(" ", "+")
        url = f"https://wttr.in/{loc}?format=%S+%s{UNIT_FLAG}"
        req = urllib.request.Request(url, headers={"User-Agent": "meshtastic-matrix-bridge"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            line = resp.read().decode().strip()
        parts = line.split()
        if len(parts) >= 2:
            return f"☀️ {BOT_LOCATION} — Sunrise: {parts[0]} / Sunset: {parts[1]}"
        return f"☀️ {BOT_LOCATION} — {line}"
    except Exception as e:
        return f"Sun data unavailable: {e}"

def get_earthquake() -> str:
    """Get recent earthquakes from USGS."""
    bounds = BOT_QUAKE_BOUNDS
    if not bounds:
        return "🌍 Earthquake monitoring not configured (set quake_bounds in config)"
    try:
        parts = bounds.split(",")
        minlat, maxlat, minlon, maxlon = [p.strip() for p in parts]
        url = (f"https://earthquake.usgs.gov/fdsnws/event/1/query?"
               f"format=geojson&minlatitude={minlat}&maxlatitude={maxlat}"
               f"&minlongitude={minlon}&maxlongitude={maxlon}"
               f"&minmagnitude=2.5&orderby=time&limit=3")
        req = urllib.request.Request(url, headers={"User-Agent": "meshtastic-matrix-bridge"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
        features = data.get("features", [])
        if not features:
            return "🌍 No M2.5+ earthquakes in area (last 7 days)"
        lines = ["🌍 Recent quakes:"]
        for f in features[:3]:
            p = f["properties"]
            mag = p.get("mag", "?")
            place = p.get("place", "Unknown")
            t = p.get("time", 0)
            from datetime import datetime
            dt = datetime.fromtimestamp(t / 1000)
            lines.append(f"M{mag} {place} ({dt.strftime('%b %d %I:%M%p')})")
        return "\n".join(lines)
    except Exception as e:
        return f"Earthquake data unavailable: {e}"

def get_alerts() -> str:
    """Get active NWS weather alerts."""
    if not BOT_NWS_ZONE:
        return "⚠️ NWS alerts not configured (set nws_zone in config)"
    try:
        url = f"https://api.weather.gov/alerts/active?zone={BOT_NWS_ZONE}"
        req = urllib.request.Request(url, headers={
            "User-Agent": "meshtastic-matrix-bridge",
            "Accept": "application/geo+json"
        })
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
        features = data.get("features", [])
        if not features:
            return "⚠️ No active weather alerts"
        lines = ["⚠️ Active alerts:"]
        for f in features[:3]:
            p = f["properties"]
            event = p.get("event", "Unknown")
            severity = p.get("severity", "")
            headline = p.get("headline", "")
            short = headline if len(headline) < 120 else f"{event} ({severity})"
            lines.append(short)
        return "\n".join(lines)
    except Exception as e:
        return f"Alert check failed: {e}"

def get_motd() -> str:
    """Return network MOTD / info."""
    if BOT_MOTD:
        return BOT_MOTD
    lines = [f"📡 {BOT_NETWORK_NAME}"]
    if BOT_NETWORK_URL:
        lines.append(f"Map: {BOT_NETWORK_URL}")
    lines.append(f"Matrix bridge active | {BOT_LOCATION}")
    lines.append("Type 'cmd' for available commands")
    return "\n".join(lines)

def get_whoami(sender: str) -> str:
    """Return info about the message sender."""
    return f"👤 You are: {sender} (via Matrix→Mesh bridge)"


async def handle_keyword(room_id: str, body: str, sender: str = "") -> bool:
    """Check for keyword commands. Returns True if handled."""
    body_lower = body.lower().strip()

    cmd_list = (
        f"🤖 {BOT_NETWORK_NAME} Bot Commands:\n"
        "wx — weather | forecast — 3-day\n"
        "alerts — NWS alerts | sun — rise/set\n"
        "quake — recent earthquakes\n"
        "nodes — mesh nodes | ping — pong\n"
        "time — current time | whoami — your info\n"
        "info — network info | joke — dad joke\n"
        "fortune — random quote | test — radio check"
    )

    if re.fullmatch(r"(cmd|commands)[.!?\s]*", body_lower):
        await matrix_send(room_id, cmd_list)
        return True
    if re.fullmatch(r"(wx|weather)[.!?\s]*", body_lower):
        await matrix_send(room_id, get_weather())
        return True
    if re.fullmatch(r"forecast[.!?\s]*", body_lower):
        await matrix_send(room_id, get_weather_forecast())
        return True
    if re.fullmatch(r"(alerts?|nws)[.!?\s]*", body_lower):
        await matrix_send(room_id, get_alerts())
        return True
    if re.fullmatch(r"sun(rise|set)?[.!?\s]*", body_lower):
        await matrix_send(room_id, get_sun())
        return True
    if re.fullmatch(r"(quake|eq|earthquake)[.!?\s]*", body_lower):
        await matrix_send(room_id, get_earthquake())
        return True
    if re.fullmatch(r"joke[.!?\s]*", body_lower):
        await matrix_send(room_id, f"😄 {get_dad_joke()}")
        return True
    if re.fullmatch(r"fortune[.!?\s]*", body_lower):
        await matrix_send(room_id, f"🔮 {get_fortune()}")
        return True
    if re.fullmatch(r"ping[.!?\s]*", body_lower):
        await matrix_send(room_id, "pong 🏓")
        return True
    if re.fullmatch(r"test[.!?\s]*", body_lower):
        await matrix_send(room_id, "roger that, reading you loud and clear ✅")
        return True
    if re.fullmatch(r"time[.!?\s]*", body_lower):
        from datetime import datetime
        try:
            from zoneinfo import ZoneInfo
            now = datetime.now(ZoneInfo(BOT_TIMEZONE))
            await matrix_send(room_id, f"🕐 {now.strftime('%I:%M %p %Z — %A, %B %d')}")
        except Exception:
            now = datetime.now()
            await matrix_send(room_id, f"🕐 {now.strftime('%I:%M %p — %A, %B %d')}")
        return True
    if re.fullmatch(r"nodes[.!?\s]*", body_lower):
        await matrix_send(room_id, get_mesh_nodes())
        return True
    if re.fullmatch(r"whoami[.!?\s]*", body_lower):
        await matrix_send(room_id, get_whoami(sender))
        return True
    if re.fullmatch(r"(info|motd)[.!?\s]*", body_lower):
        await matrix_send(room_id, get_motd())
        return True
    return False

# ---------------------------
# Matrix → Meshtastic
# ---------------------------

async def on_matrix_message(room, event: RoomMessageText) -> None:
    global MATRIX_LIVE
    if not MATRIX_LIVE:
        return

    room_id = room.room_id
    if room_id not in CH_BY_ROOM:
        return

    if event.sender == BOT_USER_ID:
        return

    ts = getattr(event, "server_timestamp", None)
    if isinstance(ts, int):
        if ts < (STARTUP_MS - 2000):
            return

    text_raw = (event.body or "").strip()
    if not text_raw:
        return

    # Check for keyword commands first — respond in Matrix only, don't forward to mesh
    if await handle_keyword(room_id, text_raw, sender=event.sender or ""):
        return

    ch = CH_BY_ROOM[room_id]

    sender_short = matrix_sender_short(event.sender or "")
    text = f"({sender_short}) {text_raw}"

    dkey = f"matrix->{room_id}:{text}"
    if dedup.seen_recently(dkey):
        return
    dedup.mark(dkey)

    global mesh_iface
    try:
        if mesh_iface is None:
            print(f"[WARN] mesh_iface is None, cannot send to ch{ch}")
            return
        mesh_iface.sendText(text, channelIndex=ch, wantAck=False)
        print(f"[TX] → mesh ch{ch}: {text}")
    except (BrokenPipeError, ConnectionError, OSError) as e:
        print(f"[FATAL] Mesh connection lost on ch{ch}: {e} — exiting for systemd restart")
        sys.exit(1)
    except Exception as e:
        print(f"[ERROR] Error sending to mesh ch{ch}: {e}")
        await matrix_send(room_id, f"⚠️ Bridge error sending to mesh on ch{ch}: {e}")

# ---------------------------
# Main
# ---------------------------

async def main():
    global mesh_iface, MAIN_LOOP, MATRIX_LIVE
    MAIN_LOOP = asyncio.get_running_loop()

    mesh_iface = TCPInterface(hostname=MESHTASTIC_HOST, portNumber=MESHTASTIC_PORT)
    pub.subscribe(on_mesh_receive, "meshtastic.receive")

    client.add_event_callback(on_matrix_message, RoomMessageText)

    print(f"✅ Meshtastic ⇄ Matrix bridge starting ({BOT_NETWORK_NAME})")
    print(f"   Meshtastic: {MESHTASTIC_HOST}:{MESHTASTIC_PORT}")
    print(f"   Matrix: {MATRIX_HS} as {BOT_USER_ID}")
    print(f"   Location: {BOT_LOCATION} | Units: {BOT_UNITS}")
    print("   Channel mapping:")
    for ch, room_id in ROOM_BY_CH.items():
        print(f"     ch{ch} → {room_id}")

    # Initial sync — prevents replaying old messages on restart
    await client.sync(timeout=30000, full_state=True)
    MATRIX_LIVE = True
    print("✅ Matrix sync complete — bridge is LIVE")

    await client.sync_forever(timeout=30000, full_state=False)

if __name__ == "__main__":
    asyncio.run(main())
