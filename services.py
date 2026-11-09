"""AuthService, AIService (interface), LLMService — as in the class diagram."""
import io, json, os
from abc import ABC, abstractmethod
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
    def answer(self, f, q): ...
    @abstractmethod
    def generateQuiz(self, f, n): ...
    @abstractmethod
    def generateExam(self, f, n): ...

class LLMService(AIService):
    def __init__(self):
        self.LLMService = os.getenv("LLM_MODEL", "claude-haiku-5-5")   # must match the model ID shown in CometAPI
        self.client = None
        if os.getenv("COMETAPI_KEY"):
            import anthropic
            self.client = anthropic.Anthropic(api_key=os.environ["COMETAPI_KEY"],
                                              base_url="https://api.cometapi.com", max_retries=0)

    def _ask(self, system, prompt):
        if not self.client: return "[DEMO MODE: set COMETAPI_KEY for real AI output]"
        r = self.client.messages.create(model=self.LLMService, max_tokens=4000, system=system,
                                        messages=[{"role": "user", "content": prompt}])
        return "".join(b.text for b in r.content if getattr(b, "type", "") == "text")

    def _json(self, system, prompt):
        t = self._ask(system + " Reply with JSON only, no markdown.", prompt)
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

    def summarize(self, f, frm, to):
        d = self._json("You summarize Arabic course material into simple bullet points. " + self.RULES +
                       ' Output: {"bulletPoints":[...],"keyTerms":[...]}. Keep important terms.',
                       self._text(f, frm, to))
        d = d or {"bulletPoints": [self._ask("Summarize in bullet points.", self._text(f, frm, to))], "keyTerms": []}
        sm = Summary(d["bulletPoints"], d["keyTerms"], frm, to); f.summaries.append(sm); return sm

    def translatePart(self, f, frm, to):
        src = self._text(f, frm, to)
        t = Translation(src, self._ask("Translate to English. " + self.RULES, src)); f.translations.append(t); return t

    def translateSummary(self, s):
        src = "\n".join("- " + b for b in s.bulletPoints)
        return Translation(src, self._ask("Translate to English. " + self.RULES, src))

    def answer(self, f, q):
        return self._ask("You are a tutor. Answer ONLY from the file below. If the question is not related to the "
                         "file, reply exactly: NOT_RELATED. " + self.RULES + "\n\nFILE:\n" + self._text(f), q)

    def _mcqs(self, f, n, lang):
        d = self._json(f"Write {n} multiple-choice questions in {lang} from the file. " + self.RULES +
                       ' Output: [{"questionText":"","options":["A","B","C","D"],"correctAnswer":"(must equal one option)","explanation":""}]',
                       self._text(f))
        return d or []

    def generateQuiz(self, f, n):
        qs = [MCQQuestion(x["questionText"], x["options"], x["correctAnswer"], x.get("explanation", ""))
              for x in self._mcqs(f, n, "the file's language")][:n]       # n is 5-10 (clamped in app.py)
        qz = Quiz(qs, f); f.quizzes.append(qz); return qz

    def generateExam(self, f, n):
        return Exam([ExamQuestion(x["questionText"], x["options"], x["correctAnswer"], x.get("explanation", ""))
                     for x in self._mcqs(f, n, "English")][:n])