FROM python:3.14-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Every dependency is pure Python (flask, requests, python-dotenv, gunicorn), so
# the image needs no apt-get and no compiler. The base image already ships the
# OS timezone database, which is what makes the TZ variable work.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN chmod +x docker-entrypoint.sh

# Runs as a non-root user. data/ holds reminders.json and titles.json - it is
# the one path worth mounting a volume on.
RUN useradd -m -u 1000 reminders && mkdir -p data && chown -R reminders:reminders /app
USER reminders

VOLUME /app/data
EXPOSE 5000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5000/', timeout=4)"]

# docker-entrypoint.sh starts the dispatcher loop (the cron job from the README)
# in the background, then execs the command below.
#
# -w 1 on purpose: reminders live in a JSON file that is read, modified and
# rewritten with no locking, and one sync worker handles one request at a time.
# More workers (or threads) would let two saves overwrite each other.
# --no-control-socket: gunicorn >= 25.1 otherwise writes a control socket under
# $HOME, which nothing here uses and which can fail on constrained storage.
ENTRYPOINT ["./docker-entrypoint.sh"]
CMD ["gunicorn", "-w", "1", "-b", "0.0.0.0:5000", "--no-control-socket", "--access-logfile", "-", "ntfy_server:app"]
