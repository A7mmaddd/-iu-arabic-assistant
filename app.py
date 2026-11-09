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

def me(): return auth.validateSession(session.get("t"))
def need_user():
    u = me()
    return u if u else (None)
def err(m, c=400): return jsonify(error=m), c
def own(fid):
    f, u = FILES.get(fid), me()
    return f if f and u and f.owner == u.email else None

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
    f = own(fid)
    if not f: return err("Not found", 404)
    j = request.json or {}; s = ai.summarize(f, int(j.get("from", 1)), int(j.get("to", len(f.pages))))
    SUMMARIES[s.id] = s; return jsonify(id=s.id, bulletPoints=s.bulletPoints, keyTerms=s.keyTerms)

@app.post("/api/summaries/<sid>/translate")
def tr_summary(sid):
    s = SUMMARIES.get(sid)
    return jsonify(text=ai.translateSummary(s).translatedText) if s else err("Not found", 404)

@app.post("/api/files/<fid>/translate")
def tr_part(fid):
    f = own(fid)
    if not f: return err("Not found", 404)
    j = request.json or {}
    return jsonify(text=ai.translatePart(f, int(j.get("from", 1)), int(j.get("to", len(f.pages)))).translatedText)

@app.post("/api/files/<fid>/ask")
def ask(fid):
    f = own(fid)
    if not f: return err("Not found", 404)
    a = ai.answer(f, (request.json or {}).get("q", ""))
    if a.strip() == "NOT_RELATED":
        a = "This question is not related to your uploaded file, so I can't answer it."
    return jsonify(answer=a)

@app.post("/api/files/<fid>/quiz")
def quiz(fid):
    f = own(fid)
    if not f: return err("Not found", 404)
    n = max(5, min(10, int((request.json or {}).get("n", 5))))
    q = ai.generateQuiz(f, n); QUIZZES[q.id] = q
    return jsonify(id=q.id, questions=[dict(questionText=x.questionText, options=x.options) for x in q.questions])

@app.post("/api/quizzes/<qid>/submit")
def submit(qid):
    q = QUIZZES.get(qid)
    if not q: return err("Not found", 404)
    ans = (request.json or {}).get("answers", []); score = q.submitAnswers(ans)
    return jsonify(score=score, total=len(q.questions),
                   review=[dict(correct=x.correctAnswer, explanation=x.explanation, ok=x.checkAnswer(a))
                           for x, a in zip(q.questions, ans + [None] * len(q.questions))])

def instructor(): u = me(); return u if isinstance(u, Instructor) else None
def student(): u = me(); return u if isinstance(u, Student) else None
def my_exam(eid):
    e, u = EXAMS.get(eid), instructor()
    return e if e and u and e in u.exams else None

@app.post("/api/files/<fid>/exam")
def exam(fid):
    f = own(fid)
    if not f or not instructor(): return err("Instructors only", 403)
    e = instructor().generateExam(ai, f, int((request.json or {}).get("n", 5))); e.title = f.fileName; EXAMS[e.id] = e
    return jsonify(id=e.id, title=e.title, published=e.isPublished, code=e.code, questions=[x.to_dict() for x in e.questions])

@app.put("/api/exams/<eid>/questions/<int:i>")
def edit_q(eid, i):
    e = EXAMS.get(eid)
    if not e or not instructor(): return err("Instructors only", 403)
    if e.isPublished: return err("This exam is published and locked", 409)
    e.questions[i].edit((request.json or {}).get("text", "")); return jsonify(ok=True)

@app.delete("/api/exams/<eid>/questions/<int:i>")
def del_q(eid, i):
    e = EXAMS.get(eid)
    if not e or not instructor(): return err("Instructors only", 403)
    if e.isPublished: return err("This exam is published and locked", 409)
    e.removeQuestion(e.questions[i]); return jsonify(ok=True)

# ---- Publish exam (Instructor) / Take published exam (Student) ----
@app.get("/api/exams")
def my_exams():
    u = instructor()
    if not u: return err("Instructors only", 403)
    return jsonify([dict(id=e.id, title=e.title, createdAt=e.createdAt.strftime("%Y-%m-%d %H:%M"), questions=len(e.questions),
                         published=e.isPublished, code=e.code, attempts=len(e.attempts)) for e in reversed(u.exams)])

@app.get("/api/exams/<eid>")
def get_exam(eid):
    e = my_exam(eid)
    if not e: return err("Not found", 404)
    return jsonify(id=e.id, title=e.title, published=e.isPublished, code=e.code, questions=[x.to_dict() for x in e.questions])

@app.post("/api/exams/<eid>/publish")
def publish(eid):
    e = my_exam(eid)
    if not e: return err("Not found", 404)
    if not e.questions: return err("Add at least one question before publishing")
    code = instructor().publishExam(e); PUBLISHED[code] = e
    return jsonify(code=code)

@app.get("/api/exams/<eid>/results")
def results(eid):
    e = my_exam(eid)
    if not e: return err("Not found", 404)
    return jsonify(total=len(e.questions), attempts=[a.to_dict() for a in e.attempts])

@app.post("/api/published/join")
def join_exam():
    u = student()
    if not u: return err("Students only", 403)
    e = PUBLISHED.get(((request.json or {}).get("code") or "").strip().upper())
    if not e: return err("No published exam with this code", 404)
    if e.attemptOf(u.email): return err("You have already taken this exam", 409)
    return jsonify(title=e.title, questions=[dict(questionText=x.questionText, options=x.options) for x in e.questions])

@app.post("/api/published/<code>/submit")
def submit_exam(code):
    u, e = student(), PUBLISHED.get(code.upper())
    if not u: return err("Students only", 403)
    if not e: return err("Not found", 404)
    if e.attemptOf(u.email): return err("You have already taken this exam", 409)
    ans = (request.json or {}).get("answers", []); ans = ans if isinstance(ans, list) else []
    a = u.takeExam(e, ans)
    return jsonify(score=a.score, total=a.total,
                   review=[dict(correct=x.correctAnswer, explanation=x.explanation, ok=x.checkAnswer(g))
                           for x, g in zip(e.questions, ans + [None] * len(e.questions))])

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)), debug=not os.getenv("RENDER"))
