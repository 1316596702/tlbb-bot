"""Keep timestamped bot records for one hour, including traceback continuations."""
import datetime
import logging
import shutil
import tempfile
import threading
import time
from pathlib import Path


def prune_log(path, now=None):
    path = Path(path)
    if not path.exists():
        return
    now = time.time() if now is None else now
    cutoff = now - 3600
    # Untimestamped error output is kept only while its file is recent.
    keep = path.stat().st_mtime >= cutoff
    changed = False
    with path.open('rb') as source, tempfile.TemporaryFile() as recent:
        for line in source:
            if len(line) >= 21 and line.startswith(b'['):
                try:
                    timestamp = datetime.datetime.strptime(
                        line[1:20].decode('ascii'), '%Y-%m-%d %H:%M:%S').timestamp()
                except (ValueError, UnicodeDecodeError):
                    pass
                else:
                    keep = timestamp >= cutoff
            if keep:
                recent.write(line)
            else:
                changed = True
        if changed:
            recent.seek(0)
            with path.open('r+b') as output:
                shutil.copyfileobj(recent, output)
                output.truncate()


class HourWindowFileHandler(logging.FileHandler):
    def __init__(self, filename):
        prune_log(filename)
        prune_log(Path(filename).with_suffix('.error.log'))
        super().__init__(filename, encoding='utf-8')
        self.cleanup_stop = threading.Event()
        self.cleanup_thread = threading.Thread(target=self.cleanup_loop, daemon=True)
        self.cleanup_thread.start()

    def cleanup_once(self):
        self.acquire()
        try:
            self.flush()
            prune_log(self.baseFilename)
            prune_log(Path(self.baseFilename).with_suffix('.error.log'))
        finally:
            self.release()

    def cleanup_loop(self):
        while not self.cleanup_stop.wait(60):
            try:
                self.cleanup_once()
            except OSError:
                # Retry next minute if another process temporarily locks the log.
                pass

    def close(self):
        self.cleanup_stop.set()
        super().close()
