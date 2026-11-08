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
| `scripts/gateway_watchdog.py` | Checks Gateway and restarts it when it's stuck |
| `scripts/gateway_readiness.py` | Prints the Sunday readiness report |
| `deploy/gateway-watchdog/` | Installs the watchdog as a system service |

## Start Gateway on the server

These steps assume Docker is installed. Get the code:

```bash
sudo apt-get install -y git python3-venv
git clone https://github.com/hackingthemarkets/ibkr-docker-server.git
cd ibkr-docker-server
```

Keep your settings outside this repository so that credentials never sit in a
Git checkout:

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

**On the server**, the scripts connect to Gateway on port 4002 by default:

```bash
.venv/bin/python connect.py
.venv/bin/python quote.py
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

To test paper TWS on your own computer instead, set `IBKR_PORT` to `7497`.

## Keep Gateway healthy

A running container doesn't mean Gateway is usable. The watchdog connects
through the API once a minute, and it checks that it can resolve a contract and
read account data. After five consecutive failures outside scheduled
maintenance, it makes one automatic restart attempt. If Gateway still does not
recover, it stops restarting and records that an operator is required. Even
after a successful recovery, it permits no more than one automatic restart in
24 hours.

IBKR publishes a [nightly North American reset
window](https://ndcdyn.interactivebrokers.com/en/software/systemStatus.php), and
its Saturday maintenance can run later. From 11:50 PM through 2:05 AM Eastern,
the watchdog continues recording health but deliberately suppresses automatic
restarts. The window can be changed in
`/etc/default/ibkr-gateway-watchdog`.

Saturday is treated as a quiet day, and automatic recovery resumes at 8:00 AM
Eastern on Sunday. The watchdog still records probe results, but it does not
restart Gateway or repeat urgent log messages while the market is closed.

Test one probe first. It doesn't change anything:

```bash
.venv/bin/python scripts/gateway_watchdog.py --probe-only
```

Then install the service:

```bash
sudo deploy/gateway-watchdog/install.sh
sudo systemctl status ibkr-gateway-watchdog
sudo journalctl -u ibkr-gateway-watchdog -f
```

The installer automatically recognizes a repository environment named either
`.venv` or `venv`. Set `IBKR_WATCHDOG_PYTHON` only if you keep Python somewhere
else.

The installer finds the existing Gateway container and bakes its exact name into
a root-owned restart script. Its sudo rule lets your user run that fixed script
without granting general Docker access. Settings are in
`/etc/default/ibkr-gateway-watchdog`. After changing them, run:

```bash
sudo systemctl restart ibkr-gateway-watchdog
```

The current machine-readable status is stored at
`/var/lib/ibkr-gateway-watchdog/state.json`:

```bash
sudo cat /var/lib/ibkr-gateway-watchdog/state.json
```

The status moves through `HEALTHY`, `DEGRADED`, `MAINTENANCE`, `WEEKEND_QUIET`,
`RESTARTING` and `OPERATOR_REQUIRED`. A successful probe begins a new incident;
the separate 24-hour restart cap remains in effect.

This service watches Gateway only. It does not monitor a strategy process or
send strategy alerts; those checks belong in the trading application.

The watchdog can't complete a login or 2FA prompt for you. If Gateway is waiting
for one, or another login is competing for the session, open it over VNC. IBKR
may also require [manual authentication after its weekly Sunday security
reset](https://www.ibkrguides.com/traderworkstation/auto-restart-considerations.htm).
Fix the login or manually restart the fixed container; the next successful
probe clears `OPERATOR_REQUIRED`:

```bash
sudo /usr/local/sbin/restart-ibkr-gateway
```

### Optional email alerts

Email is optional. Without it, the watchdog still restarts Gateway, records its
state and writes to the system journal. When authenticated SMTP is configured,
it sends one message when Gateway requires an operator and another after
recovery. A Sunday timer also runs a readiness probe at 9:00 AM Eastern and
emails the result. The report still runs and is written to the journal when
email is disabled.

Edit the non-secret settings:

```bash
sudo nano /etc/default/ibkr-gateway-watchdog
```

For Mailgun in the US region, the relevant values look like this:

```ini
IBKR_ALERT_SMTP_HOST=smtp.mailgun.org
IBKR_ALERT_SMTP_PORT=587
IBKR_ALERT_SMTP_SECURITY=starttls
IBKR_ALERT_SMTP_USERNAME=postmaster@mg.example.com
IBKR_ALERT_FROM=IBKR Server <alerts@mg.example.com>
IBKR_ALERT_TO=you@example.com
```

Use `smtp.eu.mailgun.org` for a Mailgun domain in the EU region. Other providers
work if they offer authenticated SMTP. Put only the SMTP password in the
protected file created by the installer:

```bash
sudo nano /etc/ibkr-gateway-watchdog/smtp-password
sudo systemctl restart ibkr-gateway-watchdog
```

Test delivery by starting the readiness service immediately:

```bash
sudo systemctl start ibkr-gateway-readiness.service
sudo journalctl -u ibkr-gateway-readiness.service -n 50 --no-pager
```

The scheduled run is visible with:

```bash
systemctl list-timers ibkr-gateway-readiness.timer
```

To run the tests:

```bash
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pytest tests
```

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

Gateway's settings live in the `gateway-settings` Docker volume, so they carry
over. Afterward, run `connect.py` again.

## Safety

This setup is for paper trading, and `compose.yml` fixes `TRADING_MODE` to
`paper`. The API accepts paper orders, because `READ_ONLY_API` is set to `no` in
`.env`. Set it to `yes` and recreate the container if you want to block orders.
The API port is only reachable from the server, so anything running there can
place paper orders. This project is for education and isn't investment advice.

Licensed under the [MIT License](LICENSE).
