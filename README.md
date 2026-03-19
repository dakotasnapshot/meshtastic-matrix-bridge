# Meshtastic ⇄ Matrix Bridge

A bidirectional bridge between [Meshtastic](https://meshtastic.org/) mesh radios and [Matrix](https://matrix.org/) chat rooms.

Built for [OKMesh](https://okmesh.org) — Oklahoma's Meshtastic community.

## Features

- Messages from Meshtastic mesh → Matrix rooms (with SNR/RSSI metadata)
- Messages from Matrix → Meshtastic mesh (with sender name prefix)
- Multi-channel support (map different Meshtastic channels to different Matrix rooms)
- Dedup / loop prevention
- Automatic restart on crash (systemd)
- Watchdog timer detects connection failures and restarts
- Initial sync skips old messages (no replay on restart)

### Optional: Bot Commands

Want auto-responder commands (weather, earthquakes, NWS alerts, jokes, etc.)? See our companion project **[MeshBot](https://github.com/dakotasnapshot/meshbot)** — a standalone Meshtastic bot that works with or without this bridge.

## Quick Start

### Prerequisites

- Python 3.10+
- A Meshtastic radio accessible via TCP (WiFi-enabled devices like Heltec V3, T-Beam, etc.)
- A Matrix account for the bot (create a dedicated user)
- A Matrix room (invite the bot user)

### Installation

```bash
git clone https://github.com/dakotasnapshot/meshtastic-matrix-bridge.git
cd meshtastic-matrix-bridge

python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

cp config.example.yaml config.yaml
# Edit config.yaml with your settings
```

### Configuration

Edit `config.yaml`:

```yaml
meshtastic:
  host: "192.168.1.100"    # Your radio's IP
  port: 4403

matrix:
  homeserver: "https://matrix.example.com"
  bot_user_id: "@meshbot:matrix.example.com"
  access_token: "your_token_here"

channels:
  0: "!room_id:matrix.example.com"    # Primary / LongFast
  # 1: "!room_id:matrix.example.com"  # Secondary channel
```

#### Getting a Matrix Access Token

```bash
curl -s -X POST "https://matrix.example.com/_matrix/client/r0/login" \
  -H "Content-Type: application/json" \
  -d '{"type":"m.login.password","user":"meshbot","password":"your_password"}' \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])"
```

### Run

```bash
# Direct
python bridge.py

# Or with systemd (recommended)
sudo cp systemd/*.service systemd/*.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now meshtastic-matrix-bridge
sudo systemctl enable --now meshtastic-bridge-watchdog.timer
```

### Environment Variables (Alternative to config.yaml)

| Variable | Description |
|----------|-------------|
| `MESHTASTIC_HOST` | Radio IP address |
| `MESHTASTIC_PORT` | Radio TCP port |
| `MATRIX_HOMESERVER` | Matrix server URL |
| `MATRIX_BOT_USER_ID` | Bot's Matrix user ID |
| `MATRIX_ACCESS_TOKEN` | Bot's access token |
| `BRIDGE_CHANNEL_0` | Matrix room for channel 0 |
| `BRIDGE_CHANNEL_1` | Matrix room for channel 1 |

## Architecture

```
┌──────────────┐         ┌────────────┐         ┌──────────────┐
│  Meshtastic  │◄───────►│ bridge.py  │◄───────►│    Matrix    │
│  Radio (TCP) │  mesh   │            │  nio    │  Homeserver  │
└──────────────┘         └────────────┘         └──────────────┘
```

## Also See

- **[MeshBot](https://github.com/dakotasnapshot/meshbot)** — Standalone Meshtastic auto-responder bot (weather, earthquakes, NWS alerts, jokes, and more)
- **[meshing-around](https://github.com/SpudGunMan/meshing-around)** — Feature-rich Meshtastic BBS bot
- **[868meshbot](https://github.com/868meshbot/meshbot)** — EU-focused Meshtastic bot

## Contributing

Pull requests welcome! Some ideas:
- Support for Meshtastic serial/USB connections
- Message history / store-and-forward
- Telemetry forwarding to Matrix
- Media/image relay

## License

MIT — see [LICENSE](LICENSE).

## Credits

- Built with [meshtastic-python](https://github.com/meshtastic/python) and [matrix-nio](https://github.com/poljar/matrix-nio)
- Made for the [OKMesh](https://okmesh.org) community 📡
