"""Scanned PDF pages (pictures of text, no real text inside) are read by the AI in the background.

- Upload finishes at once; reading runs in batches of a few pages, several batches at the same time.
- After every batch the text is saved in the database, so nothing is read twice: if the server
  restarts, uploading the file again carries on from where it stopped, and anyone who uploads the
  same file later gets the text straight away.
- When the user asks for pages that are not read yet (summary, translation, tutor, quiz, exam),
  those pages jump to the front of the queue and the request waits for them.
- A batch that fails (AI busy) is tried again later; a reply that was cut off is split into smaller batches.
"""
import threading, time
from models import _fernet

MIN_CHARS = 30      # a PDF page with less text than this is treated as a scanned image
BATCH = 5           # pages per AI request
WORKERS = 3         # AI requests at the same time
TRIES = 3           # attempts per page before it is marked as failed
RETRY_WAIT = 15     # seconds to wait after the AI was busy


class ScanJob:
    def __init__(self, h, name, ftype, texts, scan, done, failed=()):
        self.h, self.name, self.ftype, self.texts = h, name, ftype, texts
        self.scan = sorted(scan)                        # every page that needs the AI
        self.done = set(done)                           # pages read (text saved)
        self.failed = set(failed)                       # pages the AI could not read after TRIES attempts
        self.pending = [n for n in self.scan if n not in self.done and n not in self.failed]
        self.inflight, self.tries = set(), {}
        self.blob, self.workers = None, 0               # blob: the encrypted file while it is being read
        self.size = BATCH                               # pages per request (smaller after a cut-off reply)
        self.solo = set()                               # pages that failed in a batch: next try on their own
        self.cond, self.save_lock = threading.Condition(), threading.Lock()

    @property
    def running(self): return self.workers > 0

    def status(self):
        left = len(self.scan) - len(self.done) - len(self.failed)
        return dict(total=len(self.scan), done=len(self.done), failed=len(self.failed),
                    running=self.running, paused=left > 0 and not self.running)

    def ready(self, frm, to):
        """True when no page in the range is still waiting to be read."""
        return not any(frm <= n <= to for n in self.pending + list(self.inflight))


class Scanner:
    def __init__(self, ai, db, on_pages):
        self.ai, self.db, self.on_pages = ai, db, on_pages      # on_pages(h, {page: text}) updates open files
        self.jobs, self.lock = {}, threading.Lock()

    @staticmethod
    def needs(texts):
        return [i + 1 for i, t in enumerate(texts) if len(t.strip()) < MIN_CHARS]

    def job(self, h): return self.jobs.get(h)

    def status(self, h):
        j = self.jobs.get(h)
        return j.status() if j and j.scan else None

    def register(self, h, name, ftype, texts, info):
        """Know about a file loaded from the database (no file bytes: reading stays paused until re-upload)."""
        with self.lock:
            if h in self.jobs or not info.get("scanPages"): return self.jobs.get(h)
            self.jobs[h] = ScanJob(h, name, ftype, texts, info["scanPages"], info.get("scanDone", []),
                                   info.get("scanFailed", []))
            return self.jobs[h]

    def start(self, h, name, ftype, raw, texts, info):
        """Start (or carry on) reading the scanned pages of an uploaded PDF. Failed pages are tried again."""
        if ftype != "pdf" or not self.ai.client: return None
        scan = info.get("scanPages")
        if scan is None: scan = self.needs(texts)               # new file, or saved before this feature existed
        if not scan: return None
        with self.lock:
            j = self.jobs.get(h)
            if j and j.running: return j
            j = ScanJob(h, name, ftype, texts, scan, info.get("scanDone", []))   # failed pages get a new chance
            self.jobs[h] = j
            if not j.pending: return j
            j.blob = _fernet.encrypt(raw)
            j.workers = min(WORKERS, (len(j.pending) + BATCH - 1) // BATCH)
        self._save(j)
        for _ in range(j.workers): threading.Thread(target=self._work, args=(j,), daemon=True).start()
        return j

    def prioritize(self, h, frm, to):
        j = self.jobs.get(h)
        if not j: return
        with j.cond:
            first = [n for n in j.pending if frm <= n <= to]
            j.pending = first + [n for n in j.pending if not frm <= n <= to]

    def wait(self, h, frm, to, timeout=120):
        """Wait until the pages in the range are read (or failed). Returns True if none is still waiting."""
        j = self.jobs.get(h)
        if not j: return True
        self.prioritize(h, frm, to)
        end = time.time() + timeout
        with j.cond:
            while not j.ready(frm, to) and j.running and time.time() < end:
                j.cond.wait(min(2, max(0.1, end - time.time())))
            return j.ready(frm, to)

    def ready(self, h, frm, to):
        j = self.jobs.get(h)
        return not j or (j.ready(frm, to) and not any(frm <= n <= to for n in j.failed))

    # ---- background work ----
    def _take(self, j):
        with j.cond:
            if not j.pending: return None
            first = j.pending[0]; batch = [first]
            for n in j.pending[1:]:                     # neighbouring pages go together
                if first in j.solo or n in j.solo or len(batch) >= j.size or n != batch[-1] + 1: break
                batch.append(n)
            j.pending = [n for n in j.pending if n not in batch]
            j.inflight.update(batch)
            return batch

    def _work(self, j):
        try:
            raw = _fernet.decrypt(j.blob)
            while True:
                batch = self._take(j)
                if batch is None:
                    with j.cond:
                        if not j.inflight: break        # all done
                        j.cond.wait(1); continue       # others may still split a batch back into the queue
                try:
                    got = self.ai.readScannedPages(raw, batch)
                except OverflowError:                   # too much text for one reply -> smaller batches
                    with j.cond:
                        j.inflight.difference_update(batch)
                        if len(batch) > 1:
                            j.size = min(j.size, max(1, len(batch) // 2))
                            j.pending = batch + j.pending
                        else: j.failed.add(batch[0])
                        j.cond.notify_all()
                    if len(batch) == 1: self._save(j)
                    continue
                except Exception as e:                  # AI busy / limit reached / bad page -> try again later
                    print("Scan batch failed:", j.h[:8], batch, e)
                    with j.cond:
                        j.inflight.difference_update(batch)
                        again = []
                        for n in batch:
                            j.tries[n] = j.tries.get(n, 0) + 1
                            if len(batch) > 1 and j.tries[n] >= TRIES - 1: j.solo.add(n)   # one bad page must not sink the others
                            (again.append(n) if j.tries[n] < TRIES else j.failed.add(n))
                        j.pending += again
                        j.cond.notify_all()
                    self._save(j)
                    if again: time.sleep(RETRY_WAIT)
                    continue
                with j.cond:
                    for n, t in got.items(): j.texts[n - 1] = t
                    j.done.update(batch); j.inflight.difference_update(batch)
                    j.cond.notify_all()
                self.on_pages(j.h, got)
                self._save(j)
        finally:
            with j.cond:
                j.workers -= 1
                if j.workers <= 0: j.blob = None        # finished: the file bytes are not kept
                j.cond.notify_all()

    def _save(self, j):
        with j.save_lock:                               # one save at a time, always the newest state
            with j.cond:
                texts = list(j.texts)
                extra = dict(scanPages=j.scan, scanDone=sorted(j.done), scanFailed=sorted(j.failed))
            self.db.save_pages(j.h, j.name, j.ftype, texts, extra)
