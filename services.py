"""AuthService, AIService (interface), LLMService — as in the class diagram."""
import io, json, os, time
from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor
from models import *

class AuthService:
    def __init__(self): self.sessions = {}
    def authenticateWithGoogle(self, credential=None, name=None, email=None, role="student"):
        cid = os.getenv("GOOGLE_CLIENT_ID")
        if cid and credential:                      # real Google sign-in
            from google.oauth2 import id_token
            from google.auth.transport import requests as r
            info = id_token.verify_oauth2_token(credential, r.Request(), cid)
            name, email = info.get("name"), info["email"]
        elif cid or not email:                      # no demo login when Google is configured
            return None
        user = (Instructor if role == "instructor" else Student)(name or email, email)
        user.loginWithGoogle(); token = new_id() + new_id()
        self.sessions[token] = user; return token, user
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

    def __init__(self):
        self.LLMService = os.getenv("LLM_MODEL", "gemini-3.8-flash")    # model ID from Google AI Studio
        self.thinking = os.getenv("LLM_THINKING", "LOW").upper()         # MINIMAL / LOW / MEDIUM / HIGH
        self.client = None
        key = (os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY") or "").strip().strip('"').strip("'")
        if key:
            from google import genai
            self.client = genai.Client(api_key=key)

    def _gen(self, system, messages, max_tokens=8192, as_json=False):
        """One Gemini call. messages = [{"role": "user"|"assistant", "content": str}].
        Returns (text, cut) where cut=True means the reply hit max_tokens and is incomplete."""
        from google.genai import types, errors
        contents = [types.Content(role="model" if m["role"] == "assistant" else "user",
                                  parts=[types.Part(text=m["content"])]) for m in messages]
        cfg = types.GenerateContentConfig(system_instruction=system, max_output_tokens=max_tokens,
                                          thinking_config=types.ThinkingConfig(thinking_level=self.thinking),
                                          response_mime_type="application/json" if as_json else None)
        for attempt in range(4):
            try:
                r = self.client.models.generate_content(model=self.LLMService, contents=contents, config=cfg)
                break
            except errors.APIError as e:            # 429 = free-tier rate limit, 5xx = Google busy -> wait and retry
                if e.code not in (429, 500, 503) or attempt == 3: raise
                time.sleep(5 * 2 ** attempt)
        cand = (r.candidates or [None])[0]
        cut = bool(cand and cand.finish_reason == types.FinishReason.MAX_TOKENS)
        return r.text or "", cut

    def _ask(self, system, prompt, **kw):
        return self._chat(system, [{"role": "user", "content": prompt}], **kw)

    def _chat(self, system, messages, max_tokens=8192):
        if not self.client: return self.DEMO
        return self._gen(system, messages, max_tokens)[0]

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

    # ---- Summary: big files are split into parts, each part is summarized in detail, then all parts are merged ----
    PART_CHARS = 60_000      # about 15-30 pages of normal text per part
    MIN_PAGE_CHARS = 30      # less text than this = page is probably a scanned image / picture only
    SUMMARY_TOKENS = 32_000  # room for a long, detailed answer (Gemini Flash allows up to 65,536)

    PART_PROMPT = ("You write detailed study notes for one part of an Arabic course file. " + RULES +
                   " Cover EVERY main idea, definition, rule, condition, list, example and evidence in this part; do not"
                   " skip sections. Write in the same language as the file. Group related ideas instead of writing one"
                   " line per page. End each point with its page like (p. 12)."
                   ' Output: {"points":["..."],"keyTerms":["..."],"tooDense":false}.'
                   " Set tooDense to true only if this part has so much information that you had to leave out"
                   " important ideas.")
    MERGE_PROMPT = ("You turn study notes (or course text) into one complete, well-organized summary for a student. " +
                    RULES + " Organize by topic, not by page. Keep every important idea, definition and key term;"
                    " merge repeated ideas; keep the page references like (p. 12). Write in the same language as the"
                    " source. Use as many sections and points as the material needs - a long file needs a long summary."
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

    def _summarize_part(self, pages):
        d = self._json(self.PART_PROMPT, f"PAGES {pages[0].pageNumber}-{pages[-1].pageNumber}:\n\n" +
                       self._pages_text(pages), max_tokens=self.SUMMARY_TOKENS)
        if not isinstance(d, dict) or not d.get("points"): return None
        return d

    def summarize(self, f, frm, to):
        pages = [p for p in f.pages if frm <= p.pageNumber <= to]
        readable = [p for p in pages if len(p.text.strip()) >= self.MIN_PAGE_CHARS]
        emptyPages = [p.pageNumber for p in pages if p not in readable]
        failedPages, dense, note = [], False, ""

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
                    results = list(ex.map(self._safe_part, parts))
                notes = []
                for part, d in zip(parts, results):
                    rng = f"{part[0].pageNumber}-{part[-1].pageNumber}"
                    if d is None: failedPages += [p.pageNumber for p in part]; continue
                    dense = dense or bool(d.get("tooDense"))
                    notes.append({"pages": rng, "points": d["points"], "keyTerms": d.get("keyTerms", [])})
                source = json.dumps(notes, ensure_ascii=False)
            d = None
            if notes is None or notes:
                try: d = self._json(self.MERGE_PROMPT, source, max_tokens=self.SUMMARY_TOKENS)
                except Exception: d = None
            if isinstance(d, dict) and d.get("sections"):
                sections = [{"title": str(s.get("title", "")), "points": [str(x) for x in s.get("points", [])]}
                            for s in d["sections"] if isinstance(s, dict)]
                terms, note = d.get("keyTerms", []), str(d.get("note") or "").strip()
            elif notes:                              # merge failed -> still show the notes of every part
                sections = [{"title": f"Pages {n['pages']}", "points": n["points"]} for n in notes]
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

    def _safe_part(self, pages):
        try: return self._summarize_part(pages)
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
        return self._chat("You are a tutor. Answer ONLY from the file below. If the question is not related to the "
                          "file, reply exactly: NOT_RELATED. The student may reply to one of your earlier answers to "
                          "ask a follow-up or to ask you to change it (simpler, shorter, more examples, another "
                          "language); do what they ask, still using only the file. " + self.RULES +
                          "\n\nFILE:\n" + self._text(f), msgs)

    def _mcqs(self, f, n, lang):
        d = self._json(f"Write {n} multiple-choice questions in {lang} from the file. " + self.RULES +
                       ' Output: [{"questionText":"","options":["A","B","C","D"],"correctAnswer":"(must equal one option)",'
                       '"explanation":"","page":0}] where page is the page number the answer comes from.',
                       self._text(f))
        return [x for x in (d or []) if isinstance(x, dict) and x.get("correctAnswer") in (x.get("options") or [])]

    def generateQuiz(self, f, n):
        qs = [MCQQuestion(x["questionText"], x["options"], x["correctAnswer"], x.get("explanation", ""))
              for x in self._mcqs(f, n, "the file's language")][:n]       # n is 5-10 (clamped in app.py)
        qz = Quiz(qs, f); f.quizzes.append(qz); return qz

    def generateExam(self, f, n, language="English"):
        language = "Arabic" if language == "Arabic" else "English"
        qs = [ExamQuestion(x["questionText"], x["options"], x["correctAnswer"], x.get("explanation", ""),
                           evidencePage=self._page(x.get("page"), f))
              for x in self._mcqs(f, n, language)][:n]
        return Exam(qs, language, f.fileName, f)

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
