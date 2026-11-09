import os
from flask import Flask, request, jsonify, session, send_from_directory
from models import *
from services import AuthService, LLMService

app = Flask(__name__, static_folder="static")
app.secret_key = os.getenv("SECRET_KEY", "change-me")
app.config["MAX_CONTENT_LENGTH"] = CourseFile.MAX
auth, ai = AuthService(), LLMService()
FILES, SUMMARIES, QUIZZES, EXAMS = {}, {}, {}, {}      # in-memory only (no DB in the class diagram)
PUBLISHED = {}                                         # join code -> published Exam
MESSAGES = {}                                          # ChatMessage id -> ChatMessage (for replies)

def me(): return auth.validateSession(session.get("t"))
def err(m, c=400): return jsonify(error=m), c
def own(fid):
    f, u = FILES.get(fid), me()
    return f if f and u and f.owner == u.email else None
def instructor(): u = me(); return u if isinstance(u, Instructor) else None
def student(): u = me(); return u if isinstance(u, Student) else None
def my_exam(eid):
    e, u = EXAMS.get(eid), instructor()
    return e if e and u and e in u.exams else None

# ---- Exam mode: while a student has an open (not submitted) attempt, other features are blocked ----
def open_attempt(u):
    if not isinstance(u, Student): return None, None
    for code, e in PUBLISHED.items():
        a = e.attemptOf(u.email)
        if a and not a.submittedAt: return code, e
    return None, None
EXAM_LOCK = "You are taking an exam. Other features are locked until you submit it."
def locked(): return open_attempt(me())[0] is not None

@app.get("/")
def index(): return send_from_directory("static", "index.html")

@app.post("/api/login")
def login():
    j = request.json or {}
    r = auth.authenticateWithGoogle(j.get("credential"), j.get("name"), j.get("email"), j.get("role", "student"))
    if not r: return err("Login failed", 401)
    session["t"] = r[0]; return jsonify(name=r[1].name, role=r[1].role)

@app.post("/api/logout")
def logout():
    u = me()
    if u: u.logOut()
    session.clear(); return jsonify(ok=True)

@app.get("/api/me")
def whoami():
    u = me(); return jsonify(name=u.name, role=u.role) if u else err("no session", 401)

@app.route("/api/files", methods=["GET", "POST"])
def files():
    u = me()
    if not u: return err("Login first", 401)
    if request.method == "GET":
        return jsonify([dict(id=f.id, name=f.fileName, pages=len(f.pages)) for f in FILES.values() if f.owner == u.email])
    up = request.files.get("file")
    if not up: return err("No file")
    raw = up.read(); f = CourseFile(up.filename, len(raw), u.email)
    if not u.uploadFile(f): return err("Only PDF/PPT/PPTX up to 300 MB are accepted")
    f.raw = raw
    try: ai.processFile(f)
    except Exception as e:
        u.files.remove(f); return err(f"Could not read the file: {e}")
    FILES[f.id] = f; return jsonify(id=f.id, name=f.fileName, pages=len(f.pages))

@app.post("/api/files/<fid>/summary")
def summary(fid):
    if locked(): return err(EXAM_LOCK, 423)
    f = own(fid)
    if not f: return err("Not found", 404)
    j = request.json or {}; s = ai.summarize(f, int(j.get("from", 1)), int(j.get("to", len(f.pages))))
    SUMMARIES[s.id] = s; return jsonify(id=s.id, bulletPoints=s.bulletPoints, keyTerms=s.keyTerms)

@app.post("/api/summaries/<sid>/translate")
def tr_summary(sid):
    if locked(): return err(EXAM_LOCK, 423)
    s = SUMMARIES.get(sid)
    return jsonify(text=ai.translateSummary(s).translatedText) if s else err("Not found", 404)

@app.post("/api/files/<fid>/translate")
def tr_part(fid):
    if locked(): return err(EXAM_LOCK, 423)
    f = own(fid)
    if not f: return err("Not found", 404)
    j = request.json or {}
    return jsonify(text=ai.translatePart(f, int(j.get("from", 1)), int(j.get("to", len(f.pages)))).translatedText)

@app.post("/api/files/<fid>/ask")
def ask(fid):
    """Ask AI Tutor. Optional: replyTo (id of an earlier AI answer), voice (question came from the microphone)."""
    if locked(): return err(EXAM_LOCK, 423)
    f, u = own(fid), me()
    if not f: return err("Not found", 404)
    j = request.json or {}; q = (j.get("q") or "").strip()
    if not q: return err("Write a question first.")
    rt = MESSAGES.get(j.get("replyTo") or "")
    if rt and rt.role != "ai": rt = None
    if isinstance(u, Student):
        reply = (u.askByVoice if j.get("voice") else u.askTutor)(ai, f, q, rt)
        mine = reply.replyTo
    else:                                   # instructors can use the tutor too (same flow, not stored on a Student)
        mine = ChatMessage("student", q, rt)
        text = ai.answer(f, q, rt.thread() if rt else [])
        if text.strip() == "NOT_RELATED": text = "This question is not related to your uploaded file, so I can't answer it."
        reply = ChatMessage("ai", text, mine)
    MESSAGES[mine.id] = mine; MESSAGES[reply.id] = reply
    return jsonify(answer=reply.text, id=reply.id, question=mine.to_dict())

@app.post("/api/files/<fid>/quiz")
def quiz(fid):
    if locked(): return err(EXAM_LOCK, 423)
    f = own(fid)
    if not f: return err("Not found", 404)
    n = max(5, min(10, int((request.json or {}).get("n", 5))))
    q = ai.generateQuiz(f, n); QUIZZES[q.id] = q
    return jsonify(id=q.id, questions=[dict(questionText=x.questionText, options=x.options) for x in q.questions])

@app.post("/api/quizzes/<qid>/submit")
def submit(qid):
    if locked(): return err(EXAM_LOCK, 423)
    q = QUIZZES.get(qid)
    if not q: return err("Not found", 404)
    ans = (request.json or {}).get("answers", []); score = q.submitAnswers(ans)
    return jsonify(score=score, total=len(q.questions),
                   review=[dict(correct=x.correctAnswer, explanation=x.explanation, ok=x.checkAnswer(a))
                           for x, a in zip(q.questions, ans + [None] * len(q.questions))])

# ---------------- Exam (Instructor) ----------------
def q_dict(q):
    return dict(questionText=q.questionText, options=q.options, correctAnswer=q.correctAnswer, explanation=q.explanation,
                source=q.source, isVerified=q.isVerified, evidencePage=q.evidencePage, verifyNote=q.verifyNote)
def exam_dict(e):
    return dict(id=e.id, title=e.title, language=e.language, published=e.isPublished, code=e.code,
                hasFile=bool(e.sourceFile), questions=[q_dict(x) for x in e.questions])

@app.post("/api/files/<fid>/exam")
def exam(fid):
    f, u = own(fid), instructor()
    if not f or not u: return err("Instructors only", 403)
    j = request.json or {}
    n = max(1, min(20, int(j.get("n", 5))))
    e = u.generateExam(ai, f, n, "Arabic" if j.get("language") == "Arabic" else "English"); EXAMS[e.id] = e
    return jsonify(exam_dict(e))

def editable(eid, i=None):
    e = my_exam(eid)
    if not e: return None, err("Not found", 404)
    if e.isPublished: return None, err("This exam is published and locked", 409)
    if i is not None and not 0 <= i < len(e.questions): return None, err("Question not found", 404)
    return e, None

@app.put("/api/exams/<eid>/questions/<int:i>")
def edit_q(eid, i):
    e, bad = editable(eid, i)
    if bad: return bad
    j = request.json or {}; q = e.questions[i]
    try:
        instructor().editQuestion(ai, e, q, j.get("text", q.questionText), j.get("options", q.options),
                                  j.get("correctAnswer", q.correctAnswer))
    except ValueError as x: return err(str(x))
    return jsonify(q_dict(q))

@app.delete("/api/exams/<eid>/questions/<int:i>")
def del_q(eid, i):
    e, bad = editable(eid, i)
    if bad: return bad
    instructor().deleteQuestion(e, e.questions[i]); return jsonify(ok=True)

@app.post("/api/exams/<eid>/questions")
def add_q(eid):
    """Add Own Question «include» Verify Answer Exists in Course File."""
    e, bad = editable(eid)
    if bad: return bad
    j = request.json or {}
    try: text, opts, correct = ExamQuestion.clean(j.get("text"), j.get("options"), j.get("correctAnswer"))
    except ValueError as x: return err(str(x))
    q = instructor().addOwnQuestion(ai, e, ExamQuestion(text, opts, correct, (j.get("explanation") or "").strip(),
                                                         source="Instructor"))
    return jsonify(q_dict(q))

@app.get("/api/exams")
def my_exams():
    u = instructor()
    if not u: return err("Instructors only", 403)
    return jsonify([dict(id=e.id, title=e.title, language=e.language, createdAt=e.createdAt.strftime("%Y-%m-%d %H:%M"),
                         questions=len(e.questions), published=e.isPublished, code=e.code,
                         attempts=len(u.viewResults(e))) for e in reversed(u.exams)])

@app.get("/api/exams/<eid>")
def get_exam(eid):
    e = my_exam(eid)
    return jsonify(exam_dict(e)) if e else err("Not found", 404)

@app.post("/api/exams/<eid>/publish")
def publish(eid):
    e = my_exam(eid)
    if not e: return err("Not found", 404)
    if not e.questions: return err("Add at least one question before publishing")
    code = instructor().publishExam(e); PUBLISHED[code] = e
    return jsonify(code=code)

@app.get("/api/exams/<eid>/export")
def export(eid):
    """Print / Copy Exam. ?answers=1 gives the answer-key version; ?for=print or copy."""
    e, u = my_exam(eid), instructor()
    if not e: return err("Not found", 404)
    withAnswers = request.args.get("answers") == "1"
    text = (u.printExam if request.args.get("for") == "print" else u.copyExam)(e, withAnswers)
    return jsonify(text=text, title=e.title, language=e.language, rtl=e.language == "Arabic")

@app.get("/api/exams/<eid>/results")
def results(eid):
    """View Exam Results «include» Show Most-Missed Questions, Show Tab-Leave Count per Student."""
    e, u = my_exam(eid), instructor()
    if not e: return err("Not found", 404)
    done = u.viewResults(e); wrong = e._wrongCounts()
    return jsonify(total=len(e.questions), attempts=[a.to_dict() for a in done],
                   inProgress=sum(1 for a in e.attempts if not a.submittedAt),
                   mostMissed=[dict(index=i, text=e.questions[i].questionText, wrong=wrong[i], of=len(done))
                               for i in e.mostMissedQuestions(5)])

# ---------------- Take Published Exam (Student) ----------------
def take_dict(e):
    return dict(title=e.title, language=e.language, code=e.code,
                questions=[dict(questionText=x.questionText, options=x.options) for x in e.questions])

@app.post("/api/published/join")
def join_exam():
    """Validate Exam Code & Single Attempt, then open the attempt (exam mode starts)."""
    u = student()
    if not u: return err("Students only", 403)
    code = ((request.json or {}).get("code") or "").strip().upper()
    e = PUBLISHED.get(code)
    if not e: return err("No published exam with this code", 404)
    a = e.attemptOf(u.email)
    if a and a.submittedAt: return err("You have already taken this exam", 409)
    oc, _ = open_attempt(u)
    if oc and oc != code: return err("Finish the exam you already started first (code " + oc + ").", 409)
    e.startAttempt(u)
    return jsonify(take_dict(e))

@app.get("/api/published/active")
def active_exam():
    """Lets the page come back to an exam the student already opened (e.g. after a refresh)."""
    code, e = open_attempt(me())
    return jsonify(take_dict(e) if e else None)

@app.post("/api/published/<code>/leave")
def tab_leave(code):
    u, e = student(), PUBLISHED.get(code.upper())
    a = e.attemptOf(u.email) if (u and e) else None
    if not a or a.submittedAt: return jsonify(ok=False)
    return jsonify(ok=True, count=a.recordTabLeave())

@app.post("/api/published/<code>/submit")
def submit_exam(code):
    u, e = student(), PUBLISHED.get(code.upper())
    if not u: return err("Students only", 403)
    if not e: return err("Not found", 404)
    old = e.attemptOf(u.email)
    if old and old.submittedAt: return err("You have already taken this exam", 409)
    ans = (request.json or {}).get("answers", []); ans = ans if isinstance(ans, list) else []
    a = u.takeExam(e, ans)
    return jsonify(score=a.score, total=a.total,
                   review=[dict(correct=x.correctAnswer, explanation=x.explanation, ok=x.checkAnswer(g))
                           for x, g in zip(e.questions, a.answers)])

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)), debug=not os.getenv("RENDER"))
