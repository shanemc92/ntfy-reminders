"""
Storage helpers shared by the web server (ntfy_server.py) and the cron
dispatcher (ntfy_dispatch.py). Both read, change and rewrite the same JSON
files, so a bare open(path, 'w') lets one overwrite the other's change, and a
kill part-way through leaves half a file that no longer parses.

  locked(path)           exclusive lock held across a load-modify-save
  write_json(path, data) atomic replace: readers and crashes only ever see a whole file

Both prefer to put a file beside `path`, but cope when that directory can't be
written to (a bind mount owned by another user, with a file inside we can still
change): the lock falls back to the system temp dir and the write to in-place,
which is how the files were written before these helpers existed.

Kept free of any env/config, like ntfy_schedule.py.
"""

import errno
import hashlib
import json
import os
import shutil
import tempfile
from contextlib import contextmanager

try:
    import fcntl
except ImportError:       # Windows, which is for running the tests, not the app
    fcntl = None
    import msvcrt

_NOT_ALLOWED = (errno.EACCES, errno.EPERM, errno.EROFS)


def _open_lock(path):
    """The lock file for `path`: beside it, or failing that in the temp dir.
    Every process sharing `path` makes the same choice, because it depends only
    on the directory's permissions; the fallback assumes they also share a
    temp dir (true in one container, not under systemd's PrivateTmp)."""
    beside = path + '.lock'
    elsewhere = os.path.join(tempfile.gettempdir(),
                             'ntfy-reminders-%s.lock' % hashlib.sha256(os.path.abspath(path).encode()).hexdigest()[:16])
    try:
        return open(beside, 'a+')
    except OSError as e:
        if e.errno not in _NOT_ALLOWED:
            raise
    return open(elsewhere, 'a+')


@contextmanager
def locked(path):
    """Hold an exclusive lock, shared by every process that passes the same
    `path`, until the block exits. Take it around the whole load-modify-save of
    `path` (or of files that live beside it) and keep what runs inside short.
    It is not re-entrant: taking it again in the same block waits forever.
    Plain reads don't need it, because write_json never exposes a partial file."""
    with _open_lock(path) as lock:
        if fcntl:
            fcntl.flock(lock, fcntl.LOCK_EX)
        else:
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)   # retries for ~10 s, then raises OSError
        try:
            yield
        finally:
            if not fcntl:
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            # on POSIX, closing the file below releases the flock


def write_json(path, data):
    """Write `data` to `path` as JSON, replacing the old file in one step.
    A failure part-way (an unserialisable value, a full disk) leaves the old
    file untouched and no stray temp file behind. If the directory can't be
    written to, the file is rewritten in place instead (not atomic)."""
    text = json.dumps(data, indent=2)      # before touching anything: a bad value can't truncate the file
    try:
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or '.',
                                   prefix=os.path.basename(path) + '.', suffix='.tmp')
    except OSError as e:
        if e.errno not in _NOT_ALLOWED:
            raise
        with open(path, 'w') as f:
            f.write(text)
        return
    try:
        with os.fdopen(fd, 'w') as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        if os.path.exists(path):
            shutil.copymode(path, tmp)      # mkstemp makes it 0600; keep whatever the file had
        else:
            os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
