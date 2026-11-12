"""Domain classes — class diagram (see MAPPING.md)."""
import re, uuid, secrets
from datetime import datetime, timedelta
from cryptography.fernet import Fernet

_KEY = Fernet.generate_key()          # prototype: key lives in memory
_fernet = Fernet(_KEY)
def new_id(): return uuid.uuid4().hex[:8]
def new_code(): return "".join(secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(6))   # exam join code

class User:
    def __init__(self, name, email):
        self.name, self.email, self.logged_in = name, email, False
        self.files = []                  # Student/Instructor --Uploads--> CourseFile (0..*)
    def loginWithGoogle(self): self.logged_in = True; return True
    def logOut(self): self.logged_in = False

class Student(User):
    role = "student"
    def __init__(self, name, email):
        super().__init__(name, email); self.messages = []      # Student 1 --sends--> 0..* ChatMessage
    def uploadFile(self, f):
        ok = f.validate()
        if ok: self.files.append(f)
        return ok
    def requestSummary(self, ai, f, a=1, b=None): return ai.summarize(f, a, b or len(f.pages))
    def requestTranslation(self, ai, f, a=1, b=None): return ai.translatePart(f, a, b or len(f.pages))
    def askTutor(self, ai, f, q, replyTo=None):
        """Ask the AI tutor. replyTo = an earlier AI ChatMessage the student is replying to."""
        mine = ChatMessage("student", q, replyTo); self.messages.append(mine)
        text = ai.answer(f, q, replyTo.thread() if replyTo else [])
        if text.strip() == "NOT_RELATED":
            text = "This question is not related to your uploaded file, so I can't answer it."
        reply = ChatMessage("ai", text, mine); self.messages.append(reply)
        return reply
    def askByVoice(self, ai, f, audio, replyTo=None):
        # 'audio' arrives already turned into text by SpeechService.speechToText (runs in the browser)
        return self.askTutor(ai, f, audio, replyTo)
    def listenTo(self, msg):
        return msg.text                  # handed to SpeechService.textToSpeech (runs in the browser)
    def requestQuiz(self, ai, f, n=5, language=None, frm=1, to=None): return ai.generateQuiz(f, n, language, frm, to)
    def takeExam(self, exam, answers): return exam.submitAttempt(self, answers)   # Student --makes--> ExamAttempt

class Instructor(User):
    role = "instructor"
    def __init__(self, name, email):
        super().__init__(name, email); self.exams = []
    def uploadFile(self, f):
        ok = f.validate()
        if ok: self.files.append(f)
        return ok
    def generateExam(self, ai, f, n=5, language="English", frm=1, to=None):
        e = ai.generateExam(f, n, language, frm, to); self.exams.append(e); return e   # Instructor 1 --prepares--> 0..* Exam
    def reviewQuestion(self, q): return q
    def editQuestion(self, ai, exam, q, text, options, correctAnswer):
        q.edit(text, options, correctAnswer)
        if exam.sourceFile: ai.verifyQuestion(exam.sourceFile, q)    # re-check after every edit
        return q
    def deleteQuestion(self, exam, q): exam.removeQuestion(q)
    def addOwnQuestion(self, ai, exam, q):
        q.source = "Instructor"
        if exam.sourceFile: ai.verifyQuestion(exam.sourceFile, q)    # warning only, never blocks
        exam.addQuestion(q); return q
    def publishExam(self, exam): return exam.publish()                          # returns the join code
    def printExam(self, exam, withAnswers=False): return exam.exportText(withAnswers)   # printed in the browser
    def copyExam(self, exam, withAnswers=False): return exam.exportText(withAnswers)
    def viewResults(self, exam): return [a for a in exam.attempts if a.submittedAt]

class ChatMessage:
    def __init__(self, role, text, replyTo=None):
        self.id, self.role, self.text = new_id(), role, text           # role: "student" | "ai"
        self.createdAt, self.replyTo = datetime.now(), replyTo         # ChatMessage 0..* --replies to--> 0..1 ChatMessage
    def quotedText(self, n=160):
        t = re.sub(r"\s+", " ", self.text.replace("**", "")).strip()
        return t if len(t) <= n else t[:n].rstrip() + "…"
    def thread(self, limit=6):
        """This message and the messages it replies to, oldest first (context for the AI)."""
        out, m = [], self
        while m and len(out) < limit: out.append(m); m = m.replyTo
        return out[::-1]
    def to_dict(self):
        return dict(id=self.id, role=self.role, text=self.text,
                    replyTo=self.replyTo.id if self.replyTo else None,
                    quoted=self.replyTo.quotedText() if self.replyTo else None)

class Page:
    def __init__(self, number, text):
        self.pageNumber, self.text = number, text
        self.isRTL = bool(re.search(r"[؀-ۿ]", text))

class CourseFile:
    ALLOWED = {"pdf", "ppt", "pptx"}
    MAX = 300 * 1024 * 1024
    def __init__(self, fileName, size, owner):
        self.id, self.fileName, self.size, self.owner = new_id(), fileName, size, owner
        self.fileType = fileName.rsplit(".", 1)[-1].lower()
        self.uploadDate = datetime.now()
        self.retentionUntil = self.uploadDate + timedelta(days=180)  # one semester
        self.pages, self.raw, self._blob = [], b"", b""          # pages: CourseFile *-- Page (1..*)
        self.hash = None                                         # SHA-256 of the file (database key, set on upload)
        self.summaries, self.translations, self.quizzes = [], [], []   # CourseFile -> Summary / Translation / Quiz (0..*)
    def validate(self): return self.fileType in self.ALLOWED and 0 < self.size <= self.MAX
    def encrypt(self): self._blob = _fernet.encrypt(self.raw); self.raw = b""
    def getPages(self): return self.pages

class Summary:
    def __init__(self, bulletPoints, keyTerms, pageFrom, pageTo, sections=None, coverage=None):
        self.id, self.bulletPoints, self.keyTerms = new_id(), bulletPoints, keyTerms
        self.pageFrom, self.pageTo, self.createdAt = pageFrom, pageTo, datetime.now()
        self.sections = sections or []      # [{"title": str, "points": [str]}] grouped by topic
        self.coverage = coverage or {}      # how much of the file the summary really covers (for the warning)

class Translation:
    def __init__(self, sourceText, translatedText, targetLanguage="English"):
        self.sourceText, self.translatedText, self.targetLanguage = sourceText, translatedText, targetLanguage

class MCQQuestion:
    def __init__(self, questionText, options, correctAnswer, explanation=""):
        self.questionText, self.options = questionText, options
        self.correctAnswer, self.explanation = correctAnswer, explanation
    def checkAnswer(self, choice): return choice == self.correctAnswer
    def to_dict(self): return vars(self)

class ExamQuestion(MCQQuestion):
    def __init__(self, *a, source="AI", evidencePage=0):
        super().__init__(*a)
        self.source = source                     # "AI" | "Instructor"
        self.isVerified = source == "AI"         # AI questions come from the file; instructor ones get checked
        self.evidencePage = evidencePage         # page where the answer was found (0 = unknown)
        self.verifyNote = ""                     # short result of the check, shown to the instructor
    @staticmethod
    def clean(text, options, correctAnswer):
        """Validate an edited/new question. Returns (text, options, correct) or raises ValueError."""
        text = (text or "").strip()
        opts = [o.strip() for o in (options or []) if isinstance(o, str) and o.strip()]
        if not text: raise ValueError("The question text is empty.")
        if not 2 <= len(opts) <= 6: raise ValueError("A question needs 2 to 6 options.")
        if len(set(opts)) != len(opts): raise ValueError("Two options are the same.")
        correctAnswer = (correctAnswer or "").strip()
        if correctAnswer not in opts: raise ValueError("Choose the correct answer from the options.")
        return text, opts, correctAnswer
    def edit(self, text, options, correctAnswer):
        self.questionText, self.options, self.correctAnswer = ExamQuestion.clean(text, options, correctAnswer)
        return self

class Quiz:
    def __init__(self, questions, sourceFile=None):
        self.id, self.questions, self.score, self.createdAt = new_id(), questions, 0, datetime.now()
        self.sourceFile = sourceFile                         # Quiz --basedOn--> CourseFile
    def submitAnswers(self, answers):
        self.score = sum(q.checkAnswer(a) for q, a in zip(self.questions, answers)); return self.score

class Exam:
    LETTERS = {"English": "ABCDEF", "Arabic": "أبجدهو"}
    def __init__(self, questions, language="English", title="", sourceFile=None):
        self.id, self.questions, self.language, self.createdAt = new_id(), questions, language, datetime.now()
        self.title, self.sourceFile = title, sourceFile                 # Exam --basedOn--> CourseFile
        self.isPublished, self.code, self.attempts = False, None, []   # Exam 1 *-- 0..* ExamAttempt
    def addQuestion(self, q): self.questions.append(q)
    def removeQuestion(self, q): self.questions.remove(q)
    def publish(self):
        if not self.isPublished: self.isPublished, self.code = True, new_code()
        return self.code
    def attemptOf(self, email): return next((a for a in self.attempts if a.email == email), None)
    def startAttempt(self, student):
        """Opens the student's single attempt (exam mode starts). Returns the open or finished attempt."""
        a = self.attemptOf(student.email)
        if not a: a = ExamAttempt(student.name, student.email); self.attempts.append(a)
        return a
    def submitAttempt(self, student, answers):
        a = self.startAttempt(student)
        if a.submittedAt: return a                                       # one attempt per student
        answers = (list(answers) + [None] * len(self.questions))[:len(self.questions)]
        a.answers, a.total = answers, len(self.questions)
        a.score = sum(q.checkAnswer(x) for q, x in zip(self.questions, answers))
        a.submittedAt = datetime.now(); return a
    def _wrongCounts(self):
        done = [a for a in self.attempts if a.submittedAt]
        return [sum(not q.checkAnswer(a.answers[i]) for a in done) for i, q in enumerate(self.questions)]
    def mostMissedQuestions(self, n=3):
        """Indexes of the questions most students got wrong (most wrong first, only if someone missed it)."""
        w = self._wrongCounts()
        return [i for i in sorted(range(len(w)), key=lambda i: (-w[i], i)) if w[i] > 0][:n]
    def exportText(self, withAnswers=False):
        ar = self.language == "Arabic"
        L = self.LETTERS["Arabic" if ar else "English"]
        lab = (dict(name="الاسم", id="الرقم الجامعي", key="مفتاح الإجابة", ans="الإجابة", why="الشرح")
               if ar else dict(name="Name", id="Student ID", key="Answer key", ans="Answer", why="Why"))
        out = [self.title or "Exam", ""]
        if not withAnswers: out += [f"{lab['name']}: ____________________    {lab['id']}: ____________", ""]
        for i, q in enumerate(self.questions, 1):
            out.append(f"{i}. {q.questionText}")
            out += [f"   {L[k]}) {o}" for k, o in enumerate(q.options)]
            if withAnswers:
                k = q.options.index(q.correctAnswer) if q.correctAnswer in q.options else -1
                out.append(f"   ✔ {lab['ans']}: {L[k] + ') ' if k >= 0 else ''}{q.correctAnswer}")
                if q.explanation: out.append(f"   {lab['why']}: {q.explanation}")
            out.append("")
        return "\n".join(out).rstrip() + "\n"

class ExamAttempt:
    def __init__(self, studentName, email, answers=None, score=0, total=0):
        self.studentName, self.email, self.answers = studentName, email, answers or []
        self.score, self.total = score, total
        self.submittedAt = None                  # None while the exam is still open (exam mode)
        self.tabLeaveCount = 0
    def recordTabLeave(self):
        if not self.submittedAt: self.tabLeaveCount += 1
        return self.tabLeaveCount
    def to_dict(self):
        return dict(name=self.studentName, email=self.email, score=self.score, total=self.total,
                    tabLeaves=self.tabLeaveCount,
                    submittedAt=self.submittedAt.strftime("%Y-%m-%d %H:%M") if self.submittedAt else None)
