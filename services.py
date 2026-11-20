"""AuthService, AIService (interface), LLMService — as in the class diagram."""
import io, json, os, re, threading, time
from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor
from models import *

class ProfileError(ValueError):
    """A problem in the first-login form. code -> the message the page shows (Arabic/English)."""
    def __init__(self, code, msg): super().__init__(msg); self.code = code

class AuthService:
    """Google sign-in + the first-login profile. Settings (Render -> Environment):
       GOOGLE_CLIENT_ID      turns on real Google sign-in (and turns the demo login off)
       INSTRUCTOR_EMAILS     optional comma list of Google e-mails; if set, a 6-digit staff number is only
                             accepted from these accounts (stops students from typing a fake staff number)
       ALLOWED_EMAIL_DOMAIN  optional comma list of domains for the Google account (e.g. gmail.com,iu.edu.sa)
    Role comes from the number on the profile: 9 digits = university (student) number, 6 digits = staff number."""
    STUDENT_ID_LEN, STAFF_ID_LEN = 9, 6
    STUDENT_DOMAIN, STAFF_DOMAIN = "stu.iu.edu.sa", "iu.edu.sa"
    DEFAULT_UNIVERSITY = "الجامعة الإسلامية"

    def __init__(self): self.sessions, self.users = {}, {}

    @staticmethod
    def _list(name): return [x.strip().lower() for x in os.getenv(name, "").split(",") if x.strip()]
    @property
    def clientId(self): return os.getenv("GOOGLE_CLIENT_ID", "").strip()

    @classmethod
    def roleFromId(cls, uid):
        if not uid.isdigit(): return None
        return {cls.STUDENT_ID_LEN: "student", cls.STAFF_ID_LEN: "instructor"}.get(len(uid))

    def checkProfile(self, googleEmail, j):
        """Validates the first-login form. Returns the profile to save, or raises ProfileError."""
        name = " ".join(str(j.get("name") or "").split())[:80]
        uid = "".join(str(j.get("universityId") or "").split()).translate(str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789"))
        uemail = str(j.get("universityEmail") or "").strip().lower()
        uni = " ".join(str(j.get("university") or "").split())[:100] or self.DEFAULT_UNIVERSITY
        if not name: raise ProfileError("name", "Write your name.")
        role = self.roleFromId(uid)
        if not role: raise ProfileError("uid", "The university number is 9 digits; the staff number is 6 digits.")
        if uemail:
            want = self.STUDENT_DOMAIN if role == "student" else self.STAFF_DOMAIN
            if not uemail.endswith("@" + want):
                raise ProfileError("uemail_" + role, f"A {role} university e-mail ends with @{want}.")
        staff = self._list("INSTRUCTOR_EMAILS")
        if role == "instructor" and staff and googleEmail not in staff:
            raise ProfileError("staff", "This Google account is not on the staff list. Please contact the administrator.")
        return dict(name=name, universityId=uid, role=role, universityEmail=uemail, university=uni)

    def domainOk(self, email):
        doms = self._list("ALLOWED_EMAIL_DOMAIN")
        return not doms or email.rsplit("@", 1)[-1] in [d.lstrip("@") for d in doms]

    def _session(self, name, email, role):
        u = self.users.get(email)
        if not u or u.role != role:                 # same e-mail -> same User object (keeps their files and exams)
            u = (Instructor if role == "instructor" else Student)(name or email, email); self.users[email] = u
        u.loginWithGoogle(); token = new_id() + new_id()
        self.sessions[token] = u; return token, u

    def authenticateWithGoogle(self, credential=None, name=None, email=None):
        """Checks WHO the person is (Google account). Returns (name, email); the role comes from their profile.
        Raises PermissionError with a message the page can show."""
        if self.clientId:                           # real Google sign-in only
            if not credential: raise PermissionError("Please sign in with Google.")
            from google.oauth2 import id_token
            from google.auth.transport import requests as r
            try: info = id_token.verify_oauth2_token(credential, r.Request(), self.clientId)
            except ValueError: raise PermissionError("Google sign-in could not be verified. Please try again.")
            if not info.get("email_verified"): raise PermissionError("Your Google e-mail is not verified.")
            name, email = info.get("name"), info["email"]
        elif not email: raise PermissionError("Write your e-mail.")       # demo login (no GOOGLE_CLIENT_ID)
        email = email.strip().lower()
        if not self.domainOk(email): raise PermissionError("Please sign in with your university e-mail.")
        return name, email

    def signIn(self, profile, email):
        """Starts a session for a user whose profile is complete. Returns (token, user)."""
        token, u = self._session(profile["name"], email, profile["role"])
        u.name, u.universityId, u.university = profile["name"], profile["universityId"], profile.get("university", "")
        return token, u

    def validateSession(self, token): return self.sessions.get(token)

class AIService(ABC):
    @abstractmethod
    def processFile(self, f): ...
    @abstractmethod
    def summarize(self, f, frm, to): ...
    @abstractmethod
    def translatePart(self, f, frm, to): ...
    @abstractmethod
    def translateSummary(self, s): ...
    @abstractmethod
    def answer(self, f, q, history): ...
    @abstractmethod
    def generateQuiz(self, f, n): ...
    @abstractmethod
    def generateExam(self, f, n, language): ...
    @abstractmethod
    def verifyQuestion(self, f, q): ...

class LLMService(AIService):
    DEMO = "[DEMO MODE: set GEMINI_API_KEY for real AI output]"

    # Several Gemini models, one API key. Each model has its own free-tier limit, so using several
    # gives more requests per minute, and when one model is busy (503) or at its limit (429) we move on.
    DEFAULT_MODELS = "gemini-3.5-flash-lite,gemini-3.1-flash-lite,gemini-3.8-flash"
    BUSY = (429, 500, 503, 504)

    def __init__(self):
        names = os.getenv("LLM_MODELS") or os.getenv("LLM_MODEL") or self.DEFAULT_MODELS
        self.models = [m.strip() for m in names.split(",") if m.strip()]
        self.LLMService = self.models[0]                                  # main model (shown in /api/status)
        self.thinking = os.getenv("LLM_THINKING", "MINIMAL").upper()      # MINIMAL saves the most tokens
        self.client, self._turn, self._lock = None, 0, threading.Lock()
        key = (os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY") or "").strip().strip('"').strip("'")
        if key:
            from google import genai
            self.client = genai.Client(api_key=key)

    def _order(self):
        """Each call starts with the next model in the list, so parallel calls are spread over all models."""
        with self._lock:
            i = self._turn % len(self.models); self._turn += 1
        return self.models[i:] + self.models[:i]

    def _gen(self, system, messages, max_tokens=8192, as_json=False, thinking_level=None):
        """One AI call. messages = [{"role": "user"|"assistant", "content": str}].
        Returns (text, cut) where cut=True means the reply hit max_tokens and is incomplete."""
        from google.genai import types, errors
        contents = [types.Content(role="model" if m["role"] == "assistant" else "user",
                                  parts=m["content"] if isinstance(m["content"], list)     # e.g. a PDF + a prompt
                                  else [types.Part(text=m["content"])]) for m in messages]
        def cfg(thinking):
            return types.GenerateContentConfig(
                system_instruction=system, max_output_tokens=max_tokens,
                thinking_config=types.ThinkingConfig(thinking_level=thinking_level or self.thinking) if thinking else None,
                response_mime_type="application/json" if as_json else None)
        last = None
        for rnd in range(2):                         # try every model; if all are busy, wait a little and try once more
            if rnd: time.sleep(4)
            for model in self._order():
                try:
                    try: r = self.client.models.generate_content(model=model, contents=contents, config=cfg(True))
                    except errors.ClientError as e:  # model does not accept the thinking setting -> call without it
                        if e.code != 400 or "think" not in str(e).lower(): raise
                        r = self.client.models.generate_content(model=model, contents=contents, config=cfg(False))
                except errors.APIError as e:
                    if e.code in self.BUSY or e.code == 404: last = e; continue   # busy / not available -> next model
                    raise
                cand = (r.candidates or [None])[0]
                return r.text or "", bool(cand and cand.finish_reason == types.FinishReason.MAX_TOKENS)
        raise last

    def _ask(self, system, prompt, **kw):
        return self._chat(system, [{"role": "user", "content": prompt}], **kw)

    def _chat(self, system, messages, max_tokens=8192, thinking_level=None):
        if not self.client: return self.DEMO
        return self._gen(system, messages, max_tokens, thinking_level=thinking_level)[0]

    def _json(self, system, prompt, max_tokens=8192):
        if not self.client: return None
        t, cut = self._gen(system + " Reply with JSON only, no markdown.",
                           [{"role": "user", "content": prompt}], max_tokens, as_json=True)
        if cut: return None                          # cut-off JSON is broken JSON
        try: return json.loads(t.strip().strip("`").removeprefix("json").strip())
        except Exception: return None

    def _text(self, f, a=1, b=None):
        return "\n\n".join(f"[Page {p.pageNumber}]\n{p.text}" for p in f.pages if a <= p.pageNumber <= (b or 10**9))[:400_000]

    RULES = ("Preserve the original academic and religious meaning of Arabic terms. "
             "Never issue fatwas or independent religious rulings.")

    @staticmethod
    def _lang(f, frm=1, to=None):
        """Arabic if most pages with text in the range are Arabic, else English."""
        ps = [p for p in f.pages if frm <= p.pageNumber <= (to or 10**9) and p.text.strip()]
        return "Arabic" if ps and sum(p.isRTL for p in ps) >= 0.4 * len(ps) else "English"

    @staticmethod
    def _only(lang):
        """A strict 'write only in X' line; repeated in Arabic because small models follow it better."""
        if lang == "Arabic":
            return (" IMPORTANT: write ALL output (titles, points, questions, options, explanations, key terms, notes)"
                    " in Arabic only, even if some source text is in English. اكتب كل المخرجات باللغة العربية فقط.")
        return " IMPORTANT: write ALL output in English only, even if some source text is in Arabic."

    def processFile(self, f):
        if f.fileType == "pdf":
            from pypdf import PdfReader
            texts = [p.extract_text() or "" for p in PdfReader(io.BytesIO(f.raw)).pages]
        else:
            from pptx import Presentation
            texts = ["\n".join(s.text_frame.text for s in sl.shapes if s.has_text_frame)
                     for sl in Presentation(io.BytesIO(f.raw)).slides]
        f.pages = [Page(i + 1, t) for i, t in enumerate(texts)]
        f.encrypt()

    # ---- Scanned pages (pictures of text): Gemini reads the page image and writes out its text ----
    SCAN_PROMPT = ("You are an OCR engine for Arabic and English course books. The attached PDF has {k} page(s). "
                   "Write out ALL the text on every page exactly as printed: keep Arabic diacritics (tashkeel), "
                   "Quran verses, hadith, poetry, headings, lists and footnotes; keep the reading order of the page. "
                   "Do not translate, summarize, correct or explain anything. Before each page write a line "
                   "=== PAGE n === where n is 1 to {k} in order. If a page has no text, write only its line.")
    SCAN_TOKENS = 32_000

    @staticmethod
    def pdf_subset(raw, numbers):
        """A small PDF holding only the given pages (1-based), to send to the AI."""
        from pypdf import PdfReader, PdfWriter
        r, w = PdfReader(io.BytesIO(raw)), PdfWriter()
        for n in numbers: w.add_page(r.pages[n - 1])
        out = io.BytesIO(); w.write(out); return out.getvalue()

    def readScannedPages(self, raw, numbers):
        """Read the text of scanned PDF pages. Returns {page number: text}.
        Raises if the AI is busy or the reply was cut off (the caller retries / splits the batch)."""
        from google.genai import types
        data = self.pdf_subset(raw, numbers)
        k = len(numbers)
        text, cut = self._gen("You transcribe documents.",
                              [{"role": "user", "content": [types.Part.from_bytes(data=data, mime_type="application/pdf"),
                                                            types.Part(text=self.SCAN_PROMPT.format(k=k))]}],
                              self.SCAN_TOKENS)
        if cut: raise OverflowError("reply cut off")
        out = {n: "" for n in numbers}
        for m in re.finditer(r"=+\s*PAGE\s*(\d+)\s*=+\s*\n?(.*?)(?=\n?=+\s*PAGE\s*\d+\s*=+|\Z)", text, re.S):
            i = int(m.group(1))
            if 1 <= i <= k: out[numbers[i - 1]] = m.group(2).strip()
        if k == 1 and not re.search(r"=+\s*PAGE", text): out[numbers[0]] = text.strip()
        return out

    # ---- Summary: big files are split into parts, each part is summarized in detail, then all parts are merged ----
    PART_CHARS = 60_000      # about 15-30 pages of normal text per part
    MIN_PAGE_CHARS = 30      # less text than this = page is probably a scanned image / picture only
    SUMMARY_TOKENS = 32_000  # room for a long, detailed answer (Gemini Flash allows up to 65,536)

    PART_PROMPT = ("You write detailed study notes for one part of an Arabic course file. " + RULES +
                   " Cover EVERY main idea, definition, rule, condition, list, example and evidence in this part; do not"
                   " skip sections. Group related ideas instead of writing one"
                   " line per page. End each point with its page like (p. 12)."
                   ' Output: {"points":["..."],"keyTerms":["..."],"tooDense":false}.'
                   " Set tooDense to true only if this part has so much information that you had to leave out"
                   " important ideas.")
    MERGE_PROMPT = ("You turn study notes (or course text) into one complete, well-organized summary for a student. " +
                    RULES + " Organize by topic, not by page. Keep every important idea, definition and key term;"
                    " merge repeated ideas; keep the page references like (p. 12). Use as many sections and points as the material needs - a long file needs a long summary."
                    ' Output: {"sections":[{"title":"...","points":["..."]}],"keyTerms":["..."],"note":""}.'
                    " Leave note empty unless the material was too large or too dense to cover well; then say in one"
                    " short sentence what may be missing.")

    def _parts(self, pages):
        """Split readable pages into parts of about PART_CHARS characters (a page is never split)."""
        parts, cur, size = [], [], 0
        for p in pages:
            if cur and size + len(p.text) > self.PART_CHARS:
                parts.append(cur); cur, size = [], 0
            cur.append(p); size += len(p.text)
        return parts + [cur] if cur else parts

    @staticmethod
    def _pages_text(pages):
        return "\n\n".join(f"[Page {p.pageNumber}]\n{p.text}" for p in pages)

    def _summarize_part(self, pages, lang="Arabic"):
        d = self._json(self.PART_PROMPT + self._only(lang), f"PAGES {pages[0].pageNumber}-{pages[-1].pageNumber}:\n\n" +
                       self._pages_text(pages), max_tokens=self.SUMMARY_TOKENS)
        if not isinstance(d, dict) or not d.get("points"): return None
        return d

    def summarize(self, f, frm, to):
        pages = [p for p in f.pages if frm <= p.pageNumber <= to]
        readable = [p for p in pages if len(p.text.strip()) >= self.MIN_PAGE_CHARS
                    and len(re.findall(r"\.{4,}\s*\d+", p.text)) < 3]      # skip table-of-contents pages ("title ..... 7")
        emptyPages = [p.pageNumber for p in pages if len(p.text.strip()) < self.MIN_PAGE_CHARS]
        failedPages, dense, note = [], False, ""
        lang = self._lang(f, frm, to)

        if not self.client:
            sections, terms = [{"title": "Demo", "points": [self.DEMO]}], []
        elif not readable:
            sections, terms = [], []
        else:
            parts = self._parts(readable)
            if len(parts) == 1:                      # small file: one detailed pass is enough
                notes, source = None, self._pages_text(readable)
            else:                                    # big file: detailed notes for each part (3 at a time)
                with ThreadPoolExecutor(max_workers=3) as ex:
                    results = list(ex.map(lambda ps: self._safe_part(ps, lang), parts))
                notes = []
                for part, d in zip(parts, results):
                    rng = f"{part[0].pageNumber}-{part[-1].pageNumber}"
                    if d is None: failedPages += [p.pageNumber for p in part]; continue
                    dense = dense or bool(d.get("tooDense"))
                    notes.append({"pages": rng, "points": d["points"], "keyTerms": d.get("keyTerms", [])})
                source = json.dumps(notes, ensure_ascii=False)
            d = None
            if notes is None or notes:
                try: d = self._json(self.MERGE_PROMPT + self._only(lang), source, max_tokens=self.SUMMARY_TOKENS)
                except Exception: d = None
            if isinstance(d, dict) and d.get("sections"):
                sections = [{"title": str(s.get("title", "")), "points": [str(x) for x in s.get("points", [])]}
                            for s in d["sections"] if isinstance(s, dict)]
                terms, note = d.get("keyTerms", []), str(d.get("note") or "").strip()
            elif notes:                              # merge failed -> still show the notes of every part
                sections = [{"title": ("الصفحات " if lang == "Arabic" else "Pages ") + n["pages"], "points": n["points"]}
                            for n in notes]
                terms = list(dict.fromkeys(t for n in notes for t in n["keyTerms"]))
            else:                                    # nothing worked
                sections, terms = [], []
                if len(parts) == 1: failedPages = [p.pageNumber for p in readable]

        covered = len(pages) - len(emptyPages) - len(failedPages)
        coverage = {"totalPages": len(pages), "coveredPages": covered, "emptyPages": emptyPages,
                    "failedPages": failedPages, "tooDense": dense, "note": note, "demo": not self.client,
                    "complete": bool(self.client) and covered == len(pages) and not dense and not note}
        bullets = [x for s in sections for x in ([f"**{s['title']}**"] if s["title"] else []) + s["points"]]
        sm = Summary(bullets, terms, frm, to, sections=sections, coverage=coverage)
        f.summaries.append(sm); return sm

    def _safe_part(self, pages, lang="Arabic"):
        try: return self._summarize_part(pages, lang)
        except Exception: return None

    def translatePart(self, f, frm, to):
        src = self._text(f, frm, to)
        t = Translation(src, self._ask("Translate to English. " + self.RULES, src)); f.translations.append(t); return t

    def translateSummary(self, s):
        src = "\n".join(b if b.startswith("**") else "- " + b for b in s.bulletPoints)
        return Translation(src, self._ask("Translate to English. Keep the same headings and bullet points. " + self.RULES,
                                          src, max_tokens=self.SUMMARY_TOKENS))

    def answer(self, f, q, history=()):
        """history = earlier ChatMessages this question replies to (oldest first)."""
        msgs = []
        for m in history:
            role = "assistant" if m.role == "ai" else "user"
            if msgs and msgs[-1]["role"] == role: msgs[-1]["content"] += "\n\n" + m.text
            elif msgs or role == "user": msgs.append({"role": role, "content": m.text})
        if history:
            q = "(I am replying to your last answer.) " + q
        if msgs and msgs[-1]["role"] == "user": msgs[-1]["content"] += "\n\n" + q
        else: msgs.append({"role": "user", "content": q})
        system = self.TUTOR_PROMPT + self.RULES + "\n\nFILE:\n" + self._text(f)
        text = self._chat(system, msgs)
        if self._gave_up(text):
            # Second look before saying "not in the file": the wording of the question often differs from the
            # file. Think harder this time and try the possible meanings of the question.
            again = [dict(m) for m in msgs]
            again[-1]["content"] += self.LOOK_AGAIN
            try: text = self._chat(system, again, thinking_level="MEDIUM")
            except Exception as e: print("Tutor second look failed:", e)
        t = text.strip()
        if t.upper().startswith("NOT_FOUND"):           # related to the course but not found: keep the helpful reply
            t = re.sub(r"^NOT_FOUND\s*[:：-]?\s*", "", t, flags=re.I).strip()
            return t or "NOT_RELATED"
        return text

    TUTOR_PROMPT = (
        "You are a patient tutor for a university course. Answer ONLY from the FILE below and cite pages like (p. 12). "
        "Students often ask short, vague or informal questions, write in dialect, misspell words, use other words than "
        "the file (synonyms, a general name for the topic, the English name of an Arabic term or the other way round), "
        "or ask about an idea without naming it. Before deciding, work out what they most likely mean and look for the "
        "closest content in the file: the same idea in other words, a broader or narrower topic, a definition, rule, "
        "example or list that answers it.\n"
        "- If you find related content, answer from it. If the question can mean more than one thing, answer the most "
        "likely meaning, then add one short line naming the other meaning(s) found in the file so they can ask about it.\n"
        "- If the file covers only part of it, answer that part and say briefly what the file does not cover.\n"
        "- If it is about the subject of the file but you truly cannot find it, start your reply with NOT_FOUND: and "
        "then, in the student's language, say that you could not find it in the file and suggest 2-3 topics from the "
        "file that they may mean.\n"
        "- Reply exactly NOT_RELATED only when the question has nothing to do with the file's subject at all "
        "(e.g. sports, weather, programming, a different course).\n"
        "Answer in the language of the question. The student may reply to one of your earlier answers to ask a "
        "follow-up or to ask you to change it (simpler, shorter, more examples, another language); do what they ask, "
        "still using only the file. ")
    LOOK_AGAIN = ("\n\n(Look again before saying it is not in the file: the wording may differ from the file. "
                  "First think of 2-3 clearer ways to ask this question and the words the file might use for it, in "
                  "Arabic and English, then search the whole file for each. Answer if any of them is covered.)")

    @staticmethod
    def _gave_up(text):
        t = (text or "").strip().upper()
        return t == "NOT_RELATED" or t.startswith("NOT_FOUND")

    def _mcqs(self, f, n, lang, frm=1, to=None):
        """Questions ONLY from pages frm..to (only those pages are sent, and questions citing other pages are dropped)."""
        to = min(to or len(f.pages), len(f.pages))
        d = self._json(f"Write {n} multiple-choice questions in {lang} from the course pages below (pages {frm}-{to}). "
                       f"Use ONLY these pages; never ask about anything that is not in them. " + self.RULES +
                       ' Output: [{"questionText":"","options":["A","B","C","D"],"correctAnswer":"(must equal one option)",'
                       '"explanation":"","page":0}] where page is the page number the answer comes from.' + self._only(lang),
                       self._text(f, frm, to))
        ok = []
        for x in (d or []):
            if not isinstance(x, dict) or x.get("correctAnswer") not in (x.get("options") or []): continue
            p = self._page(x.get("page"), f)
            if p and not frm <= p <= to: continue                             # cited a page outside the range
            ok.append(x)
        return ok

    @staticmethod
    def _language(language, f, frm, to):
        if language in ("Arabic", "English"): return language
        return LLMService._lang(f, frm, to)                                  # not chosen -> language of the pages

    def generateQuiz(self, f, n, language=None, frm=1, to=None):
        language = self._language(language, f, frm, to)
        qs = [MCQQuestion(x["questionText"], x["options"], x["correctAnswer"], x.get("explanation", ""))
              for x in self._mcqs(f, n, language, frm, to)][:n]               # n is 5-10 (clamped in app.py)
        qz = Quiz(qs, f); f.quizzes.append(qz); return qz

    def generateExam(self, f, n, language="English", frm=1, to=None):
        language = "Arabic" if language == "Arabic" else "English"
        qs = [ExamQuestion(x["questionText"], x["options"], x["correctAnswer"], x.get("explanation", ""),
                           evidencePage=self._page(x.get("page"), f))
              for x in self._mcqs(f, n, language, frm, to)][:n]
        if language == "English": self.addArabic(qs)
        return Exam(qs, language, f.fileName, f)

    # ---- English exams: an Arabic translation under each question, for the instructor only ----
    def addArabic(self, qs):
        """Translate English exam questions to Arabic (one AI call). Sets q.ar; leaves it None if the AI fails."""
        if not qs: return
        items = [{"question": q.questionText, "options": q.options} for q in qs]
        d = self._json("Translate these English multiple-choice exam questions into clear Modern Standard Arabic for "
                       "the course instructor, who will check that they understand each question. " + self.RULES +
                       " Translate faithfully; keep numbers, names and technical terms accurate. Keep the same number "
                       'and order of questions and options. Output: [{"question":"","options":[""]}]',
                       json.dumps(items, ensure_ascii=False))
        if not isinstance(d, list) or len(d) != len(qs): return
        for q, x in zip(qs, d):
            if isinstance(x, dict) and isinstance(x.get("options"), list) and len(x["options"]) == len(q.options):
                q.ar = {"questionText": str(x.get("question") or ""), "options": [str(o) for o in x["options"]]}

    def bilingualQuestion(self, text, options):
        """An instructor's question for an ENGLISH exam; any field may be written in Arabic.
        Returns (english text, english options, arabic translation dict or None).
        Fields already in English are kept as written; on any AI problem the input comes back unchanged."""
        d = self._json("You prepare one multiple-choice question for an exam written in English. The teacher may have "
                       "written the question and/or some options in Arabic. " + self.RULES +
                       ' Output: {"en":{"question":"","options":[""]},"ar":{"question":"","options":[""]}}.'
                       " en = the English exam version: keep every field that is already in English exactly as written;"
                       " translate fields written in Arabic into clear academic English (use the usual English or"
                       " transliterated form of Arabic and Islamic terms). ar = a faithful Modern Standard Arabic"
                       " translation of the en version, for the teacher. Keep the same number and order of options.",
                       json.dumps({"question": text, "options": options}, ensure_ascii=False))
        try:
            en, ar = d["en"], d["ar"]
            en_q, en_o = str(en["question"]).strip(), [str(o).strip() for o in en["options"]]
            ar_q, ar_o = str(ar["question"]).strip(), [str(o).strip() for o in ar["options"]]
            if not en_q or len(en_o) != len(options) or len(ar_o) != len(options) or not all(en_o): raise ValueError
        except Exception:
            return text, options, None
        return en_q, en_o, {"questionText": ar_q, "options": ar_o}

    @staticmethod
    def _page(p, f):
        try: p = int(p)
        except (TypeError, ValueError): return 0
        return p if 1 <= p <= len(f.pages) else 0

    def verifyQuestion(self, f, q):
        """Checks an instructor's question against the file. Sets isVerified / evidencePage / verifyNote."""
        d = self._json("You check an exam question against a course file. Decide: (1) is the answer to the question "
                       "found in the file? (2) is the marked correct answer right according to the file? " + self.RULES +
                       ' Output: {"inFile":true,"markedIsRight":true,"page":0,"bestOption":"(one of the options)",'
                       '"note":"one short sentence in simple English"}',
                       f"QUESTION: {q.questionText}\nOPTIONS: {json.dumps(q.options, ensure_ascii=False)}\n"
                       f"MARKED CORRECT: {q.correctAnswer}\n\nFILE:\n{self._text(f)}")
        if not isinstance(d, dict):
            q.isVerified, q.evidencePage = False, 0
            q.verifyNote = "Could not check this question right now (AI not available)."
            return False
        inFile, right = bool(d.get("inFile")), bool(d.get("markedIsRight"))
        q.isVerified, q.evidencePage = inFile and right, self._page(d.get("page"), f) if inFile else 0
        note = str(d.get("note") or "").strip()
        best = d.get("bestOption")
        if not inFile: note = note or "The answer to this question was not found in the file."
        elif not right and best in q.options and best != q.correctAnswer:
            note = (note + " " if note else "") + f'The file suggests the correct answer is: "{best}".'
        q.verifyNote = note or "The answer was found in the file."
        return q.isVerified
