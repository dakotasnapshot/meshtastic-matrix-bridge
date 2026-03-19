#!/usr/bin/env python3
"""
Meshtastic ⇄ Matrix Bridge

Bidirectional relay between Meshtastic mesh radios and Matrix chat rooms.
Messages flow both ways with dedup/loop prevention.

For bot commands (weather, earthquakes, etc.), see:
https://github.com/dakotasnapshot/meshbot

Configuration: Copy config.example.yaml to config.yaml and edit.
"""

import asyncio
import os
import sys
import time
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
    }


cfg = load_config()

MESHTASTIC_HOST = cfg["meshtastic"]["host"]
MESHTASTIC_PORT = cfg["meshtastic"]["port"]
MATRIX_HS = cfg["matrix"]["homeserver"]
BOT_USER_ID = cfg["matrix"]["bot_user_id"]
ACCESS_TOKEN = cfg["matrix"]["access_token"]

ROOM_BY_CH: Dict[int, str] = {int(k): v for k, v in cfg.get("channels", {}).items()}

IN_PREFIX = "📡 Mesh"
DEDUP_TTL_SECONDS = 10

# ---------------------------
# Globals
# ---------------------------

MAIN_LOOP: Optional[asyncio.AbstractEventLoop] = None
mesh_iface: Optional[TCPInterface] = None

client = AsyncClient(MATRIX_HS)
client.user_id = BOT_USER_ID
client.access_token = ACCESS_TOKEN

CH_BY_ROOM = {room_id: ch for ch, room_id in ROOM_BY_CH.items()}

STARTUP_MS = int(time.time() * 1000)
MATRIX_LIVE = False

# ---------------------------
# Helpers
# ---------------------------

class Deduper:
    def __init__(self, ttl: int):
        self.ttl = ttl
        self.cache: Dict[str, float] = {}

    def seen_recently(self, key: str) -> bool:
        t = self.cache.get(key)
        if t is None:
            return False
        if time.time() - t > self.ttl:
            self.cache.pop(key, None)
            return False
        return True

    def mark(self, key: str) -> None:
        self.cache[key] = time.time()


dedup = Deduper(DEDUP_TTL_SECONDS)


def _to_int(v: Any) -> Optional[int]:
    if v is None:
        return None
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, int):
        return v
    if isinstance(v, bytes):
        return v[0] if len(v) == 1 else int.from_bytes(v, "big", signed=False)
    if isinstance(v, str) and v.strip().isdigit():
        return int(v.strip())
    return None


def get_channel_index_from_packet(packet: Dict[str, Any]) -> Optional[int]:
    """Extract channel index from Meshtastic packet."""
    decoded = (packet or {}).get("decoded") or {}
    for v in [
        packet.get("channel"), packet.get("channelIndex"), packet.get("channelNum"),
        decoded.get("channel"), decoded.get("channelIndex"), decoded.get("channelNum"),
        packet.get("rxChannel"), decoded.get("rxChannel"),
    ]:
        iv = _to_int(v)
        if iv is not None:
            return iv
    return None


def matrix_sender_short(mxid: str) -> str:
    """@user:server.com -> user"""
    if mxid and mxid.startswith("@") and ":" in mxid:
        return mxid[1:].split(":", 1)[0]
    return mxid or "unknown"


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

    ch = get_channel_index_from_packet(packet) or 0
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
    if isinstance(ts, int) and ts < (STARTUP_MS - 2000):
        return

    text_raw = (event.body or "").strip()
    if not text_raw:
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
        await matrix_send(room_id, f"⚠️ Bridge error on ch{ch}: {e}")

# ---------------------------
# Main
# ---------------------------

async def main():
    global mesh_iface, MAIN_LOOP, MATRIX_LIVE
    MAIN_LOOP = asyncio.get_running_loop()

    mesh_iface = TCPInterface(hostname=MESHTASTIC_HOST, portNumber=MESHTASTIC_PORT)
    pub.subscribe(on_mesh_receive, "meshtastic.receive")

    client.add_event_callback(on_matrix_message, RoomMessageText)

    print("✅ Meshtastic ⇄ Matrix bridge starting")
    print(f"   Meshtastic: {MESHTASTIC_HOST}:{MESHTASTIC_PORT}")
    print(f"   Matrix: {MATRIX_HS} as {BOT_USER_ID}")
    print("   Channel mapping:")
    for ch, room_id in ROOM_BY_CH.items():
        print(f"     ch{ch} → {room_id}")

    await client.sync(timeout=30000, full_state=True)
    MATRIX_LIVE = True
    print("✅ Matrix sync complete — bridge is LIVE")

    await client.sync_forever(timeout=30000, full_state=False)


if __name__ == "__main__":
    asyncio.run(main())
