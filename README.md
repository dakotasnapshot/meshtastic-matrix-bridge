# Meshtastic ⇄ Matrix Bridge

A bidirectional bridge between [Meshtastic](https://meshtastic.org/) mesh radios and [Matrix](https://matrix.org/) chat rooms, with a full suite of community-standard bot commands.

Built for [OKMesh](https://okmesh.org) — Oklahoma's Meshtastic community.

## Features

### Bidirectional Relay
- Messages from Meshtastic mesh → Matrix rooms (with SNR/RSSI metadata)
- Messages from Matrix → Meshtastic mesh (with sender name prefix)
- Multi-channel support (map different Meshtastic channels to different Matrix rooms)
- Dedup / loop prevention

### Bot Commands
Type `cmd` to discover all commands — follows the community convention used by [meshing-around](https://github.com/SpudGunMan/meshing-around) and [868meshbot](https://github.com/868meshbot/meshbot).

| Command | Description |
|---------|-------------|
| `cmd` / `commands` | List all available commands |
| `wx` / `weather` | Current weather (temperature, humidity, wind, pressure) |
| `forecast` | 3-day weather forecast |
| `alerts` / `nws` | Active NWS weather alerts for your area |
| `sun` / `sunrise` / `sunset` | Sunrise and sunset times |
| `quake` / `eq` | Recent earthquakes from USGS |
| `nodes` | Active Meshtastic node count and names |
| `ping` | Connectivity test (responds "pong 🏓") |
| `test` | Radio check confirmation |
| `time` | Current local time |
| `whoami` | Show sender info |
| `info` / `motd` | Network info and map URL |
| `joke` | Random dad joke |
| `fortune` | Random quote |

Bot commands respond in Matrix only — they are **not** forwarded to the mesh network.

### Reliability
- Automatic restart on crash (systemd)
- Watchdog timer detects BrokenPipe errors and restarts
- Initial sync skips old messages (no replay on restart)
- Startup gating prevents processing stale events

## Quick Start

### Prerequisites
- Python 3.10+
- A Meshtastic radio accessible via TCP (WiFi-enabled devices like Heltec V3, T-Beam, etc.)
- A Matrix account for the bot (create a dedicated user)
- A Matrix room (invite the bot user)

### Installation

```bash
git clone https://github.com/dakcole/meshtastic-matrix-bridge.git
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
  0: "!room_id:matrix.example.com"

bot:
  location: "Your City, State"
  nws_zone: "OKC081"              # Find yours at https://alerts.weather.gov/
  quake_bounds: "33.5, 37.5, -103, -94.5"  # minlat, maxlat, minlon, maxlon
  network_name: "MyMesh"
  network_url: "https://mymesh.org"
  timezone: "America/Chicago"
  units: "imperial"                # or "metric"
```

#### Getting a Matrix Access Token

```bash
curl -s -X POST "https://matrix.example.com/_matrix/client/r0/login" \
  -H "Content-Type: application/json" \
  -d '{"type":"m.login.password","user":"meshbot","password":"your_password"}' \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])"
```

#### Finding Your NWS Zone

Visit [https://alerts.weather.gov/](https://alerts.weather.gov/) and look up your county's zone code (e.g., `OKC081` for Payne County, Oklahoma).

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

If you prefer environment variables over a config file:

| Variable | Description |
|----------|-------------|
| `MESHTASTIC_HOST` | Radio IP address |
| `MESHTASTIC_PORT` | Radio TCP port |
| `MATRIX_HOMESERVER` | Matrix server URL |
| `MATRIX_BOT_USER_ID` | Bot's Matrix user ID |
| `MATRIX_ACCESS_TOKEN` | Bot's access token |
| `BRIDGE_CHANNEL_0` | Matrix room for channel 0 |
| `BRIDGE_CHANNEL_1` | Matrix room for channel 1 |
| `BOT_LOCATION` | Location for weather |
| `BOT_NWS_ZONE` | NWS zone code |
| `BOT_QUAKE_BOUNDS` | USGS bounding box |
| `BOT_NETWORK_NAME` | Your mesh network name |
| `BOT_NETWORK_URL` | Your mesh map URL |
| `BOT_TIMEZONE` | IANA timezone |
| `BOT_UNITS` | `imperial` or `metric` |

## Data Sources

All APIs are free and require no keys:

| Data | API |
|------|-----|
| Weather & Forecast | [wttr.in](https://wttr.in) |
| Sunrise/Sunset | [wttr.in](https://wttr.in) |
| Earthquakes | [USGS FDSNWS](https://earthquake.usgs.gov/fdsnws/event/1/) |
| Weather Alerts | [NWS API](https://api.weather.gov/) |
| Dad Jokes | [icanhazdadjoke.com](https://icanhazdadjoke.com/) |

## Architecture

```
┌──────────────┐         ┌─────────────┐         ┌──────────────┐
│  Meshtastic  │◄───────►│  bridge.py  │◄───────►│    Matrix    │
│  Radio (TCP) │  mesh   │  + bot cmds │  nio    │  Homeserver  │
└──────────────┘         └─────────────┘         └──────────────┘
                               │
                    ┌──────────┼──────────┐
                    │          │          │
                 wttr.in    USGS     NWS API
                (weather)  (quakes)  (alerts)
```

## Contributing

Pull requests welcome! Some ideas:
- Additional bot commands
- Support for Meshtastic serial/USB connections
- Message history / BBS functionality
- Telemetry forwarding to Matrix

## License

MIT — see [LICENSE](LICENSE).

## Credits

- Built with [meshtastic-python](https://github.com/meshtastic/python) and [matrix-nio](https://github.com/poljar/matrix-nio)
- Inspired by [meshing-around](https://github.com/SpudGunMan/meshing-around) and [868meshbot](https://github.com/868meshbot/meshbot)
- Made for the [OKMesh](https://okmesh.org) community 📡
