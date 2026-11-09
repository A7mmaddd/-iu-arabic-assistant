"""Domain classes — class diagram + exam publishing extension (see MAPPING.md)."""
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
    def uploadFile(self, f):
        ok = f.validate()
        if ok: self.files.append(f)
        return ok
    def requestSummary(self, ai, f, a=1, b=None): return ai.summarize(f, a, b or len(f.pages))
    def requestTranslation(self, ai, f, a=1, b=None): return ai.translatePart(f, a, b or len(f.pages))
    def askTutor(self, ai, f, q): return ai.answer(f, q)
    def requestQuiz(self, ai, f, n=5): return ai.generateQuiz(f, n)
    def takeExam(self, exam, answers): return exam.submitAttempt(self, answers)   # Student --takes--> Exam (published)

class Instructor(User):
    role = "instructor"
    def __init__(self, name, email):
        super().__init__(name, email); self.exams = []
    def uploadFile(self, f):
        ok = f.validate()
        if ok: self.files.append(f)
        return ok
    def generateExam(self, ai, f, n=5):
        e = ai.generateExam(f, n); self.exams.append(e); return e   # Instructor --prepares--> Exam (0..*)
    def reviewQuestion(self, q): return q
    def editQuestion(self, q, text): q.edit(text)
    def deleteQuestion(self, exam, q): exam.removeQuestion(q)
    def publishExam(self, exam): return exam.publish()                          # returns the join code

class Page:
    def __init__(self, number, text):
        self.pageNumber, self.text = number, text
        self.isRTL = bool(re.search(r"[\u0600-\u06FF]", text))

class CourseFile:
    ALLOWED = {"pdf", "ppt", "pptx"}
    MAX = 300 * 1024 * 1024
    def __init__(self, fileName, size, owner):
        self.id, self.fileName, self.size, self.owner = new_id(), fileName, size, owner
        self.fileType = fileName.rsplit(".", 1)[-1].lower()
        self.uploadDate = datetime.now()
        self.retentionUntil = self.uploadDate + timedelta(days=180)  # one semester
        self.pages, self.raw, self._blob = [], b"", b""          # pages: CourseFile *-- Page (1..*)
        self.summaries, self.translations, self.quizzes = [], [], []   # CourseFile -> Summary / Translation / Quiz (0..*)
    def validate(self): return self.fileType in self.ALLOWED and 0 < self.size <= self.MAX
    def encrypt(self): self._blob = _fernet.encrypt(self.raw); self.raw = b""
    def getPages(self): return self.pages

class Summary:
    def __init__(self, bulletPoints, keyTerms, pageFrom, pageTo):
        self.id, self.bulletPoints, self.keyTerms = new_id(), bulletPoints, keyTerms
        self.pageFrom, self.pageTo, self.createdAt = pageFrom, pageTo, datetime.now()

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
    def __init__(self, *a, language="English"):
        super().__init__(*a); self.language = language
    @property
    def question(self): return self.questionText          # diagram attribute 'question'
    @question.setter
    def question(self, v): self.questionText = v
    def edit(self, newText): self.questionText = newText; return self

class Quiz:
    def __init__(self, questions, sourceFile=None):
        self.id, self.questions, self.score, self.createdAt = new_id(), questions, 0, datetime.now()
        self.sourceFile = sourceFile                         # Quiz --basedOn--> CourseFile
    def submitAnswers(self, answers):
        self.score = sum(q.checkAnswer(a) for q, a in zip(self.questions, answers)); return self.score

class Exam:
    def __init__(self, questions, language="English", title=""):
        self.id, self.questions, self.language, self.createdAt = new_id(), questions, language, datetime.now()
        self.title = title
        self.isPublished, self.code, self.attempts = False, None, []   # Exam 1 --> 0..* ExamAttempt
    def addQuestion(self, q): self.questions.append(q)
    def removeQuestion(self, q): self.questions.remove(q)
    def publish(self):
        if not self.isPublished: self.isPublished, self.code = True, new_code()
        return self.code
    def attemptOf(self, email): return next((a for a in self.attempts if a.email == email), None)
    def submitAttempt(self, student, answers):
        old = self.attemptOf(student.email)
        if old: return old                                              # one attempt per student
        score = sum(q.checkAnswer(a) for q, a in zip(self.questions, answers))
        a = ExamAttempt(student.name, student.email, answers, score, len(self.questions))
        self.attempts.append(a); return a

class ExamAttempt:
    def __init__(self, studentName, email, answers, score, total):
        self.studentName, self.email, self.answers = studentName, email, answers
        self.score, self.total, self.submittedAt = score, total, datetime.now()
    def to_dict(self):
        return dict(name=self.studentName, email=self.email, score=self.score, total=self.total,
                    submittedAt=self.submittedAt.strftime("%Y-%m-%d %H:%M"))
