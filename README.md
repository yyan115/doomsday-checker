# Doomsday Checker

Doomsday Checker monitors Singapore cinema listings for new showtimes and adjacent seat
pairs. It sends Telegram alerts with links to the cinema's booking page.

Every newly listed showtime gets an alert with its local start time, venue, format,
availability, and booking link. Alerts include a seat map when one is available.
Private alerts can highlight a suitable pair. Channel alerts report availability
for all seats.

## Docker deployment

Use a Linux server with Docker Engine and the Docker Compose plugin. Copy the
current project files to the server, including the Docker files in the working
directory. Transfer `config.local.toml` and `.env` privately. For a new
installation, copy `config.example.toml` to `config.local.toml` and create `.env`
using the Telegram setup below.

```sh
chmod 600 .env
mkdir -p -m 700 data
docker compose build
docker compose run --rm -T watcher --once --no-notify
```

The watcher stores its SQLite database at `data/state.db` on the server. If the
owner of `.env` and `data` has a UID or GID
other than `1000`, set `CHECKER_UID` and `CHECKER_GID` in `.env` to that owner's
numeric IDs. Use `id -u` and `id -g` to find the IDs for your account.

For a migration, stop the old watcher before copying its database to
`data/state.db` on the server. This preserves alert history and pending alerts.
Pending alerts go to the destination configured on the server.

After setting the Telegram destination, run the test and start the watcher.

```sh
docker compose run --rm -T watcher --test-alert
docker compose up -d
docker compose logs -f watcher
```

Docker runs the watcher and its Chromium service with `restart: unless-stopped`.
They resume after a reboot if Docker starts at boot and the services were
running before the reboot. The watcher reaches Chromium over the Compose
network. Only the watcher mounts configuration, credentials, and SQLite state.
Both services publish zero host ports. Use `docker compose stop` to stop the project.

## Local installation

Requires Python 3.12 or later and Chromium.

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --require-hashes --only-binary=:all: -r requirements.lock
python -m pip install --no-build-isolation --no-deps -e .
python -m playwright install chromium
cp config.example.toml config.local.toml
doomsday-checker --once --no-notify
```

Set the movie title, schedule and seat preferences in `config.local.toml`. Use
`--config PATH` to select a different file. Shaw uses a headless Chromium context.
Golden Village showtimes come from NowShowing. The watcher adds Popcorn availability
labels when the cinema, date and time match. These feeds provide showtimes and
availability labels. Golden Village seat pairs remain unavailable in listings mode.

For another movie, update `gv_movie_slugs` and `gv_popcorn_path` to match its public
listing pages.

## Telegram setup

Create a bot with `@BotFather` and save its token in `.env` beside the local
configuration file. Keep the file private. A private chat or group can receive
all alerts. You can also configure a separate broadcast channel for showtimes.

```dotenv
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
```

If the private chat already works, keep its existing `TELEGRAM_CHAT_ID`. For a
new private chat, press Start in Telegram and run the lookup.

```sh
docker compose run --rm -T watcher --telegram-chat private
```

For a group, add the bot as a member and allow it to send messages and photos.
Send `/start@your_bot_username` in the group using your bot's username, then run
the lookup.

```sh
docker compose run --rm -T watcher --telegram-chat group
```

Copy the printed `TELEGRAM_CHAT_ID` line into `.env`, including the minus sign
for a group. Run the lookup soon after sending the command. Telegram retains
pending updates for up to 24 hours. Group setup works with Telegram's default
bot privacy mode. If the watcher is already running, apply the destination with
`docker compose up -d --force-recreate watcher`.

For a local Python installation, use `doomsday-checker --telegram-chat private`
or `doomsday-checker --telegram-chat group` for setup. Run
`doomsday-checker --test-alert` to test delivery and `doomsday-checker` to start
polling.

### Broadcast channel

Add the bot as a channel administrator with **Post Messages** permission. Set
`TELEGRAM_CHANNEL_ID` in `.env` to the channel's negative numeric chat ID. Keep
`TELEGRAM_CHAT_ID` set to your private chat for heartbeats and source errors.
The channel receives new showtimes and confirmed reopenings with seat maps where
available.

To preview and queue the current showtimes for a new channel, stop the existing
watcher before queuing. The queue command records the initial batch once per
channel. The watcher sends queued posts when it starts.

```sh
docker compose run --rm -T watcher --queue-current-showtimes --no-notify
docker compose stop watcher
docker compose run --rm -T watcher --queue-current-showtimes
docker compose up -d --force-recreate watcher
```

## Systemd user service

Copy `deploy/doomsday-checker.service` to `~/.config/systemd/user/`. Replace both
`/path/to/doomsday-checker` entries with the absolute checkout path.

```sh
systemctl --user daemon-reload
systemctl --user enable --now doomsday-checker.service
journalctl --user -u doomsday-checker.service
```

The computer must stay on and awake for polling to continue.

## Development

```sh
python -m pip install --require-hashes --only-binary=:all: -r requirements-dev.lock
ruff check
pytest
python -m pip_audit --strict --disable-pip --require-hashes -r requirements.lock
```

The Docker build pins the Python base image digest and uses a dated Debian
snapshot. Playwright downloads the Chromium revision associated with its locked
version. Regenerate the Python locks with `uv` when updating dependencies.

```sh
uv pip compile pyproject.toml --group build --python-version 3.12 --universal --generate-hashes --only-binary=:all: -o requirements.lock
uv pip compile pyproject.toml --group build --extra dev --constraint requirements.lock --python-version 3.12 --universal --generate-hashes --only-binary=:all: -o requirements-dev.lock
```

## License

Licensed under the [Apache License, Version 2.0](LICENSE).
