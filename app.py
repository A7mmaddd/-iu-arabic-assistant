import os, hashlib, time
from datetime import timedelta
from flask import Flask, request, jsonify, session, send_from_directory
from models import *
from services import AuthService, LLMService, ProfileError
from store import Store, file_hash

app = Flask(__name__, static_folder="static")
app.secret_key = os.getenv("SECRET_KEY", "change-me")
app.config.update(MAX_CONTENT_LENGTH=CourseFile.MAX, PERMANENT_SESSION_LIFETIME=timedelta(days=30),
                  SESSION_COOKIE_SAMESITE="Lax", SESSION_COOKIE_SECURE=bool(os.getenv("RENDER")))
auth, ai, db = AuthService(), LLMService(), Store()
FILES, SUMMARIES, QUIZZES, EXAMS = {}, {}, {}, {}      # working copies in memory; files/summaries/chats are also in the DB
PUBLISHED = {}                                         # join code -> published Exam
MESSAGES = {}                                          # ChatMessage id -> ChatMessage (for replies)
CACHE_V = "v2"   # change this when the summary/translation prompts change, so old saved results are not reused

def me():
    if auth.clientId and not session.get("google"): return None   # a demo-login session is not accepted once Google is on
    u = auth.validateSession(session.get("t"))
    if u or not session.get("email") or not session.get("role"): return u   # no role yet = profile not filled in
    p = db.profile(session["email"])                                         # server restarted -> sign back in
    if not p:                                     # e.g. signed in before profiles existed -> ask for the profile
        session.pop("role", None); session.pop("t", None); return None
    token, u = auth.signIn(p, session["email"])
    session["t"] = token; return u
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

@app.get("/api/config")
def config():
    """What the login page needs: the Google client ID (public) and the default university name."""
    return jsonify(googleClientId=auth.clientId or None, defaultUniversity=auth.DEFAULT_UNIVERSITY)

def signed_in(profile):
    token, u = auth.signIn(profile, session["email"])
    session.update(t=token, name=u.name, role=u.role)
    return jsonify(name=u.name, role=u.role)

def need_profile():
    return jsonify(needProfile=True, name=session.get("gname") or "", email=session.get("email"))

@app.post("/api/login")
def login():
    """Step 1: Google says who the person is. If their profile is saved -> signed in; else the page asks for it."""
    j = request.json or {}
    try: name, email = auth.authenticateWithGoogle(j.get("credential"), j.get("name"), j.get("email"))
    except PermissionError as e: return err(str(e), 401)
    session.clear(); session.permanent = True
    session.update(email=email, gname=name, google=bool(auth.clientId))
    p = db.profile(email)
    return signed_in(p) if p else need_profile()

@app.post("/api/profile")
def save_profile():
    """Step 2 (first login only): name + university/staff number (decides student/instructor) + optional details."""
    if not session.get("email") or (auth.clientId and not session.get("google")): return err("Login first", 401)
    email, p = session["email"], db.profile(session["email"])
    if p: return signed_in(p)                       # already saved (e.g. two tabs)
    try: p = auth.checkProfile(email, request.json or {})
    except ProfileError as e: return jsonify(error=str(e), code=e.code), 400
    if not db.claim_id(p["universityId"], email):
        return jsonify(error="This number is already used by another account.", code="taken"), 400
    db.save_profile(email, p)
    return signed_in(p)

@app.post("/api/logout")
def logout():
    u = me()
    if u: u.logOut()
    session.clear(); return jsonify(ok=True)

@app.get("/api/status")
def status():
    """Shows if the AI key reached the server (never shows the key itself)."""
    names = sorted(k for k in os.environ if "GEMINI" in k.upper() or "API_KEY" in k.upper() or "COMET" in k.upper())
    return jsonify(aiReady=ai.client is not None, model=ai.LLMService, models=ai.models, keyNamesSeen=names,
                   database=db.enabled, databaseError=db.error or None, googleSignIn=bool(auth.clientId))

@app.get("/api/me")
def whoami():
    u = me()
    if u: return jsonify(name=u.name, role=u.role)
    if session.get("email") and not session.get("role") and not (auth.clientId and not session.get("google")):
        return need_profile()                       # signed in with Google but the first-login form is not done yet
    return err("no session", 401)

def file_id(email, h):
    """Same user + same file -> same id, also after a restart (keeps their chat linked to the file)."""
    return hashlib.sha256(f"{email}|{h}".encode()).hexdigest()[:12]

def add_file(u, h, name, texts):
    f = CourseFile(name, 1, u.email)
    f.id, f.hash = file_id(u.email, h), h
    f.pages = [Page(i + 1, t) for i, t in enumerate(texts)]
    FILES[f.id] = f
    if f not in u.files: u.files.append(f)
    return f

def load_my_files(u):
    """After a restart the user's file list comes back from the database (page text is shared by file hash)."""
    if getattr(u, "_loaded", False): return
    u._loaded = True
    for h, info in db.user_files(u.email):
        if file_id(u.email, h) in FILES: continue
        texts = db.load_pages(h)
        if texts is not None: add_file(u, h, info.get("name", "file"), texts)

def file_json(f): return dict(id=f.id, name=f.fileName, pages=len(f.pages))

@app.route("/api/files", methods=["GET", "POST"])
def files():
    u = me()
    if not u: return err("Login first", 401)
    load_my_files(u)
    if request.method == "GET":
        return jsonify([file_json(f) for f in FILES.values() if f.owner == u.email])
    up = request.files.get("file")
    if not up: return err("No file")
    raw = up.read(); check = CourseFile(up.filename, len(raw), u.email)
    if not check.validate(): return err("Only PDF/PPT/PPTX up to 300 MB are accepted")
    h = file_hash(raw)
    if file_id(u.email, h) in FILES: return jsonify(file_json(FILES[file_id(u.email, h)]))   # uploaded again
    texts = db.load_pages(h)                         # someone already uploaded this exact file -> no need to read it
    if texts is None:
        check.raw = raw
        try: ai.processFile(check)
        except Exception as e: return err(f"Could not read the file: {e}")
        texts = [p.text for p in check.pages]
        db.save_pages(h, up.filename, check.fileType, texts)
    f = add_file(u, h, up.filename, texts)
    db.add_user_file(u.email, h, up.filename, len(texts))
    return jsonify(file_json(f))

@app.delete("/api/files/<fid>")
def delete_file(fid):
    """Delete a file uploaded by mistake: it leaves this user's list and their chat for it is deleted.
    Saved summaries stay (they are shared by everyone who uploads the same file). Published exams keep their questions."""
    if locked(): return err(EXAM_LOCK, 423)
    f, u = own(fid), me()
    if not f: return err("Not found", 404)
    FILES.pop(fid, None)
    if f in u.files: u.files.remove(f)
    for s in f.summaries: SUMMARIES.pop(s.id, None)
    for k in [k for k, m in MESSAGES.items() if getattr(m, "fileId", None) == fid]: MESSAGES.pop(k)
    db.remove_user_file(u.email, f.hash)
    return jsonify(ok=True)

def page_range(f, j):
    """The pages the user selected (from/to in the request), kept inside the file."""
    last = max(1, len(f.pages))
    frm = max(1, min(last, int(j.get("from") or 1))); to = max(1, min(last, int(j.get("to") or last)))
    return (frm, to) if frm <= to else (to, frm)

MAX_SUMMARY_PAGES = 15      # one summary request = at most 15 pages; the page asks for bigger ranges in parts

@app.post("/api/files/<fid>/summary")
def summary(fid):
    if locked(): return err(EXAM_LOCK, 423)
    f = own(fid)
    if not f: return err("Not found", 404)
    j = request.json or {}
    frm = max(1, int(j.get("from", 1)))
    to = min(int(j.get("to", len(f.pages))), frm + MAX_SUMMARY_PAGES - 1, len(f.pages))
    key = f"{CACHE_V}-{ai._lang(f, frm, to)}-{frm}-{to}"
    hit = db.cached(f.hash, "summaries", key)       # same file + same pages summarized before -> no AI call
    if hit:
        s = Summary(hit["bulletPoints"], hit["keyTerms"], frm, to, sections=hit["sections"], coverage=hit["coverage"])
        f.summaries.append(s)
    else:
        s = ai.summarize(f, frm, to)
        if s.coverage.get("failedPages") and not s.coverage.get("coveredPages"):   # AI failed for every page -> page offers "Try again"
            f.summaries.remove(s)
            return err("The AI is busy right now (Google servers or free-tier limit). Please try again in a minute.", 503)
        c = s.coverage
        if ai.client and not c.get("failedPages") and c.get("coveredPages"):     # only complete results are shared
            db.cache(f.hash, "summaries", key, dict(bulletPoints=s.bulletPoints, keyTerms=s.keyTerms,
                                                     sections=s.sections, coverage=c))
    s.fileHash, s.cacheKey = f.hash, key
    SUMMARIES[s.id] = s
    return jsonify(id=s.id, bulletPoints=s.bulletPoints, keyTerms=s.keyTerms, sections=s.sections,
                   coverage=s.coverage, pageFrom=frm, pageTo=to, cached=bool(hit))

def cached_text(h, key, make):
    """Translation saved in the database by file hash; made by the AI only the first time."""
    hit = db.cached(h, "translations", key) if h else None
    if hit: return hit["text"]
    text = make()
    if h and ai.client and text.strip() and not text.startswith("[DEMO"): db.cache(h, "translations", key, {"text": text})
    return text

@app.post("/api/summaries/<sid>/translate")
def tr_summary(sid):
    if locked(): return err(EXAM_LOCK, 423)
    s = SUMMARIES.get(sid)
    if not s: return err("Not found", 404)
    key = f"summary-{getattr(s, 'cacheKey', s.id)}"
    return jsonify(text=cached_text(getattr(s, "fileHash", None), key, lambda: ai.translateSummary(s).translatedText))

@app.post("/api/files/<fid>/translate")
def tr_part(fid):
    if locked(): return err(EXAM_LOCK, 423)
    f = own(fid)
    if not f: return err("Not found", 404)
    frm, to = page_range(f, request.json or {})
    key = f"{CACHE_V}-pages-{frm}-{to}"
    return jsonify(text=cached_text(f.hash, key, lambda: ai.translatePart(f, frm, to).translatedText))

@app.post("/api/files/<fid>/ask")
def ask(fid):
    """Ask AI Tutor. Optional: replyTo (id of an earlier AI answer), voice (question came from the microphone)."""
    if locked(): return err(EXAM_LOCK, 423)
    f, u = own(fid), me()
    if not f: return err("Not found", 404)
    j = request.json or {}; q = (j.get("q") or "").strip()
    if not q: return err("Write a question first.")
    rt = MESSAGES.get(j.get("replyTo") or "")
    if rt and (rt.role != "ai" or getattr(rt, "fileId", None) != fid): rt = None   # only this user's chat for this file
    if isinstance(u, Student):
        reply = (u.askByVoice if j.get("voice") else u.askTutor)(ai, f, q, rt)
        mine = reply.replyTo
    else:                                   # instructors can use the tutor too (same flow, not stored on a Student)
        mine = ChatMessage("student", q, rt)
        text = ai.answer(f, q, rt.thread() if rt else [])
        if text.strip() == "NOT_RELATED": text = "This question is not related to your uploaded file, so I can't answer it."
        reply = ChatMessage("ai", text, mine)
    for m in (mine, reply): m.fileId = fid; MESSAGES[m.id] = m
    if not reply.text.startswith("[DEMO"):
        db.add_chat(u.email, f.hash, dict(mine.to_dict(), at=time.time()), dict(reply.to_dict(), at=time.time()))
    return jsonify(answer=reply.text, id=reply.id, question=mine.to_dict())

@app.get("/api/files/<fid>/chat")
def chat_history(fid):
    """This user's own saved chat for this file (nobody else's)."""
    f, u = own(fid), me()
    if not f: return err("Not found", 404)
    out, objs = [], {}
    for d in db.chat(u.email, f.hash):
        m = MESSAGES.get(d["id"]) or ChatMessage(d["role"], d["text"], objs.get(d.get("replyTo")))
        m.id, m.fileId = d["id"], fid
        objs[m.id] = MESSAGES[m.id] = m             # rebuilt so "reply to this answer" still works after a restart
        out.append(m.to_dict())
    return jsonify(out)

@app.post("/api/files/<fid>/quiz")
def quiz(fid):
    if locked(): return err(EXAM_LOCK, 423)
    f = own(fid)
    if not f: return err("Not found", 404)
    j = request.json or {}
    n = max(5, min(10, int(j.get("n", 5))))
    frm, to = page_range(f, j)
    q = ai.generateQuiz(f, n, j.get("language"), frm, to); QUIZZES[q.id] = q
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
    frm, to = page_range(f, j)
    e = u.generateExam(ai, f, n, "Arabic" if j.get("language") == "Arabic" else "English", frm, to); EXAMS[e.id] = e
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
