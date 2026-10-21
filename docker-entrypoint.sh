#!/usr/bin/env sh
set -eu

# ntfy_dispatch.py reads NTFY_TOPIC with no fallback. Stop here with a readable
# message rather than letting the loop below print a KeyError every minute.
: "${NTFY_TOPIC:?NTFY_TOPIC is not set - copy .env.example to .env and fill it in}"

# Say up front if the data volume is not writable by this user (a bind mount
# owned by someone else is the usual cause), rather than leaving it to a
# traceback later.
if [ -e /app/data/reminders.json ] && [ ! -w /app/data/reminders.json ]; then
    echo "[ERROR] /app/data/reminders.json is not writable by uid $(id -u): no reminders will be sent or saved until it is." >&2
    echo "        Fix: chown -R $(id -u):$(id -g) the host data directory, or set 'user:' to the owner's uid:gid." >&2
elif [ ! -w /app/data ]; then
    echo "[WARNING] /app/data is not writable by uid $(id -u). Saving still works through the existing files, but not atomically." >&2
    echo "          Fix: chown -R $(id -u):$(id -g) the host data directory, or set 'user:' to the owner's uid:gid." >&2
fi

# The cron line from the README, as a loop: run the dispatcher, then sleep to
# the top of the next minute. Runs never overlap, and the loop ends with the
# container.
(
    while true; do
        python ntfy_dispatch.py || echo "[ERROR] dispatcher exited with status $?"
        sleep "$((60 - $(date +%-S)))"
    done
) &

exec "$@"
