#!/usr/bin/env sh
set -eu

# ntfy_dispatch.py reads NTFY_TOPIC with no fallback. Stop here with a readable
# message rather than letting the loop below print a KeyError every minute.
: "${NTFY_TOPIC:?NTFY_TOPIC is not set - copy .env.example to .env and fill it in}"

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
