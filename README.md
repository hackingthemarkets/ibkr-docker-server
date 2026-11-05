# IB Gateway in Docker

A small starter for running **paper** IB Gateway on a Linux server, using the
community-maintained
[`gnzsnz/ib-gateway`](https://github.com/gnzsnz/ib-gateway-docker) image. This
project is not affiliated with or supported by Interactive Brokers.

The API and VNC ports are bound to `127.0.0.1`, so they can only be reached from
the server itself or through an SSH tunnel. Port 22 is the only port that should
be open to the internet.

| File | Purpose |
|---|---|
| `compose.yml` | Runs IB Gateway |
| `.env.example` | Template for the non-secret settings |
| `connect.py` | Tests the API connection |
| `quote.py` | Tests real-time market data |

## Start Gateway on the server

These steps assume Docker is installed. Keep your settings outside this
repository so that credentials never sit in a Git checkout:

```bash
mkdir -p ~/ibkr-stack/secrets
chmod 700 ~/ibkr-stack/secrets
cp compose.yml ~/ibkr-stack/
install -m 600 .env.example ~/ibkr-stack/.env
cd ~/ibkr-stack

install -m 600 /dev/null secrets/tws_password.txt
install -m 600 /dev/null secrets/vnc_password.txt
nano secrets/tws_password.txt   # your IBKR paper password only
nano secrets/vnc_password.txt   # a long, unique VNC password
nano .env                       # set TWS_USERID to your paper username

sudo docker compose up -d
sudo ss -lntp | grep -E '4002|5900'   # both should start with 127.0.0.1
```

Never commit `.env` or `secrets/`, and never open ports 4002 or 5900 to the
internet.

## View Gateway over VNC

On your own computer, open a tunnel and leave it running:

```bash
ssh -N -L 15900:127.0.0.1:5900 trader@YOUR_SERVER_IP
```

- **macOS:** press `Cmd+K` in Finder and enter `vnc://127.0.0.1:15900`.
- **Windows:** install a VNC viewer such as TigerVNC and connect to
  `127.0.0.1:15900`.

When asked for a password, use the one in `secrets/vnc_password.txt`, not your
SSH or IBKR password.

## Test the API

Use Python 3.12 or 3.13.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

On Windows, the Python path is `.venv\Scripts\python`.

**On the server**, Gateway is on port 4002:

```bash
IBKR_PORT=4002 .venv/bin/python connect.py
IBKR_PORT=4002 .venv/bin/python quote.py
```

**From your own computer**, open a second tunnel and leave it running:

```bash
ssh -N -L 14002:127.0.0.1:4002 trader@YOUR_SERVER_IP
```

Then run the tests in another terminal:

```bash
IBKR_PORT=14002 .venv/bin/python connect.py
IBKR_PORT=14002 .venv/bin/python quote.py
```

In PowerShell, set the port first:

```powershell
$env:IBKR_PORT = "14002"
.venv\Scripts\python connect.py
.venv\Scripts\python quote.py
```

Both scripts default to port 7497 for paper TWS on your own computer.

## Upgrade Gateway

The image version in `compose.yml` is pinned so that it only changes when you
choose. Don't use the download link in Gateway's upgrade popup. Instead, choose a
new version from the project's
[releases](https://github.com/gnzsnz/ib-gateway-docker/releases), change the tag
in `~/ibkr-stack/compose.yml`, and run this outside market hours:

```bash
cd ~/ibkr-stack
sudo docker compose pull
sudo docker compose up -d
```

Afterward, confirm that Read-Only API is still on, then run `connect.py` again.

## Safety

This setup is for paper trading. `READ_ONLY_API=yes` blocks orders through the
API, so leave it on until you intend to test paper orders. This project is for
education and isn't investment advice.

Licensed under the [MIT License](LICENSE).
