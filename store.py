"""Database (Google Firebase / Cloud Firestore).

What is saved:
  files/{fileHash}                      file info, shared by everyone who uploads the same file
  files/{fileHash}/chunks/{n}           the text of the pages (read once, then reused)
  files/{fileHash}/summaries/{key}      summaries   -> the next student gets them with no AI call
  files/{fileHash}/translations/{key}   translations -> same
  users/{userKey}/files/{fileHash}      the files a user uploaded (their list)
  users/{userKey}/chats/{fileHash}      that user's AI-tutor chat for that file (private: only read for that user)

Quizzes and exam questions are NOT saved here: they are made fresh every time.
Without FIREBASE_CREDENTIALS the app keeps everything in memory (works, but resets on restart).
"""
import hashlib, json, os, threading, time

CHUNK_BYTES = 700_000          # Firestore allows 1 MB per document; page text is split into chunks below that


def file_hash(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def user_key(email: str) -> str:
    """Document id for a user (the e-mail itself is not used as the id)."""
    return hashlib.sha256(email.strip().lower().encode()).hexdigest()[:32]


class Store:
    def __init__(self):
        self.db, self.mem, self.lock, self.error = None, {}, threading.RLock(), ""
        cred = self._credentials()
        if cred:
            try:
                import firebase_admin
                from firebase_admin import credentials, firestore
                app = firebase_admin.initialize_app(credentials.Certificate(cred), name="iu-assistant")
                self.db = firestore.client(app)
            except Exception as e:                       # bad key -> keep working in memory, show it in /api/status
                self.error = f"{type(e).__name__}: {e}"[:300]
                print("Firestore not available:", self.error)

    @staticmethod
    def _credentials():
        raw = (os.getenv("FIREBASE_CREDENTIALS") or "").strip()
        if raw.startswith("{"): return json.loads(raw)
        for path in (raw, os.getenv("GOOGLE_APPLICATION_CREDENTIALS"), "firebase-key.json",
                     "/etc/secrets/firebase-key.json"):
            if path and os.path.isfile(path): return path
        return None

    @property
    def enabled(self): return self.db is not None

    # ---- tiny document API (Firestore, or a dict when there is no database) ----
    def get(self, path):
        try:
            if self.db:
                d = self.db.document(path).get()
                return d.to_dict() if d.exists else None
            with self.lock: return json.loads(json.dumps(self.mem.get(path))) if path in self.mem else None
        except Exception as e:
            print("DB read failed:", path, e); return None

    def set(self, path, data):
        try:
            if self.db: self.db.document(path).set(data)
            else:
                with self.lock: self.mem[path] = json.loads(json.dumps(data))
        except Exception as e:
            print("DB write failed:", path, e)

    def delete(self, path):
        try:
            if self.db: self.db.document(path).delete()
            else:
                with self.lock: self.mem.pop(path, None)
        except Exception as e:
            print("DB delete failed:", path, e)

    def list(self, collection):
        """[(doc_id, data)] of a collection."""
        try:
            if self.db: return [(d.id, d.to_dict()) for d in self.db.collection(collection).stream()]
            pre = collection.rstrip("/") + "/"
            with self.lock:
                return [(k[len(pre):], json.loads(json.dumps(v))) for k, v in self.mem.items()
                        if k.startswith(pre) and "/" not in k[len(pre):]]
        except Exception as e:
            print("DB list failed:", collection, e); return []

    # ---- page text, shared by file hash ----
    def load_pages(self, h):
        info = self.get(f"files/{h}")
        if not info or not info.get("chunks"): return None
        texts = []
        for i in range(info["chunks"]):
            c = self.get(f"files/{h}/chunks/{i:04d}")
            if not c: return None                     # incomplete -> read the file again
            texts += c["pages"]
        return texts if len(texts) == info.get("pages") else None

    def save_pages(self, h, name, file_type, texts):
        chunks, cur, size = [], [], 0
        for t in texts:
            b = len(t.encode("utf-8")) + 16
            if cur and size + b > CHUNK_BYTES: chunks.append(cur); cur, size = [], 0
            cur.append(t[:CHUNK_BYTES // 2]); size += b     # a single huge page is cut to fit
        if cur or not chunks: chunks.append(cur)
        for i, c in enumerate(chunks): self.set(f"files/{h}/chunks/{i:04d}", {"pages": c})
        self.set(f"files/{h}", {"name": name, "type": file_type, "pages": len(texts), "chunks": len(chunks),
                                "savedAt": time.time()})

    # ---- a user's own file list ----
    def user_files(self, email):
        return sorted(self.list(f"users/{user_key(email)}/files"), key=lambda x: x[1].get("uploadedAt", 0))

    def add_user_file(self, email, h, name, pages):
        self.set(f"users/{user_key(email)}/files/{h}", {"name": name, "pages": pages, "uploadedAt": time.time()})

    def remove_user_file(self, email, h):
        self.delete(f"users/{user_key(email)}/files/{h}")
        self.delete(f"users/{user_key(email)}/chats/{h}")

    # ---- private chat (per user, per file) ----
    MAX_CHAT = 200

    def chat(self, email, h):
        if not h: return []
        return (self.get(f"users/{user_key(email)}/chats/{h}") or {}).get("messages", [])

    def add_chat(self, email, h, *msgs):
        if not h: return
        path = f"users/{user_key(email)}/chats/{h}"
        with self.lock:                                  # one writer at a time per process
            old = (self.get(path) or {}).get("messages", [])
            old = (old + [dict(m, text=m["text"][:8000]) for m in msgs])[-self.MAX_CHAT:]
            self.set(path, {"messages": old, "updatedAt": time.time()})

    # ---- cached AI results (shared by file hash) ----
    def cached(self, h, kind, key):
        return self.get(f"files/{h}/{kind}/{key}") if h else None

    def cache(self, h, kind, key, data):
        if h: self.set(f"files/{h}/{kind}/{key}", dict(data, savedAt=time.time()))
