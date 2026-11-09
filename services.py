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
    def answer(self, f, q, history): ...
    @abstractmethod
    def generateQuiz(self, f, n): ...
    @abstractmethod
    def generateExam(self, f, n, language): ...
    @abstractmethod
    def verifyQuestion(self, f, q): ...

class LLMService(AIService):
    def __init__(self):
        self.LLMService = os.getenv("LLM_MODEL", "claude-haiku-5-5")   # must match the model ID shown in CometAPI
        self.client = None
        if os.getenv("COMETAPI_KEY"):
            import anthropic
            self.client = anthropic.Anthropic(api_key=os.environ["COMETAPI_KEY"],
                                              base_url="https://api.cometapi.com", max_retries=0)

    def _ask(self, system, prompt):
        return self._chat(system, [{"role": "user", "content": prompt}])

    def _chat(self, system, messages):
        if not self.client: return "[DEMO MODE: set COMETAPI_KEY for real AI output]"
        r = self.client.messages.create(model=self.LLMService, max_tokens=4000, system=system, messages=messages)
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
