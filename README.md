# IU Arabic Courses Assistant — prototype
Python 3.10+ / Flask. Run:
    pip install -r requirements.txt
    export GEMINI_API_KEY=...           # from aistudio.google.com; without it the AI replies in demo mode
    export LLM_MODELS=gemini-3.5-flash-lite,gemini-3.1-flash-lite,gemini-3.8-flash   # optional; this is the default
    export GOOGLE_CLIENT_ID=...         # optional; Google sign-in (without it a demo login is shown)
    export FIREBASE_CREDENTIALS='{...}' # optional; Firestore database (or put firebase-key.json in this folder)
    export INSTRUCTOR_EMAILS=a@x,b@x    # optional; only these Google accounts may register a 6-digit staff number
    export ALLOWED_EMAIL_DOMAIN=...     # optional; only these Google-account domains can sign in
    python app.py                       # http://127.0.0.1:5000
Files: models.py (class diagram classes), services.py (AuthService, AIService, LLMService), app.py (web routes),
store.py (Firestore database),
static/index.html (UI + SpeechService). MAPPING.md shows where every class and use case is in the code.

## Features
Student: summary (15 pages per part; bigger ranges are done in parts that appear one by one,
with a warning when pages could not be read), delete an uploaded file, translation, AI tutor (reply to an answer, ask by voice, listen to answers), quizzes,
take a published exam (exam mode locks the other features and counts leaving the page).
Instructor: generate exam questions in English or Arabic, edit questions and options, add own questions
(checked against the file), print or copy the exam (with or without answer key), publish with a code,
see results with the most-missed questions and how many times each student left the page.
English exams show an Arabic translation under every question (instructor only, never sent to students);
the instructor can write a question or options in Arabic and they are translated to English on save.
A "Guide" button (between dark mode and language) shows a short note next to every visible button for 5 seconds.

## Deploy on Render (no cmd needed)
1. Push this folder to a GitHub repo (render.yaml must be at the repo root).
2. On render.com: New + -> Blueprint -> pick the repo.
3. Paste your Gemini API key (from Google AI Studio) when Render asks for GEMINI_API_KEY, then Apply.
4. Open the https://....onrender.com link. That's it.
Note: the free plan sleeps after ~15 min idle (first visit takes ~1 min). With FIREBASE_CREDENTIALS set,
profiles, files, summaries, translations and chats are kept in the database; quizzes and exams are still in memory.

## Scanned PDFs (pages that are pictures)
Pages with no real text are read by Gemini in the background (scan.py): upload finishes at once, a bar shows
"reading pages: x of y", and the pages a user asks for are read first. Reading is 5 pages per request, 3 at a time;
the text is saved to the database after every batch, so it is read only once (also for other users of the same file).
If the server restarts in the middle, uploading the file again carries on from where it stopped. Pages the AI could
not read after 3 tries are listed in the summary warning; uploading the file again tries them again.

## First login (profile)
After Google sign-in, a new user fills a short form once: full name (required), university/staff number
(required: 9 digits = student, 6 digits = instructor), university e-mail (optional: students @stu.iu.edu.sa,
staff @iu.edu.sa) and university (optional, default "الجامعة الإسلامية"). It is saved to their Google account,
so the next sign-in goes straight in. One number can only be registered by one Google account.

## Database (Firebase / Cloud Firestore)
- The same file uploaded by different students is recognised by its SHA-256 hash: its text, summaries and
  translations are saved once and reused (no new AI call). Quizzes and exam questions are always made fresh.
- "New summary" / "New translation" asks the AI again for that user only; the saved copy is not replaced,
  so other users still get the first one.
- Each user's file list and AI-tutor chat are saved under that user only; nobody else can read their chat.
- Without FIREBASE_CREDENTIALS everything still works, but only in memory (resets when the server restarts).
- Never put the Firebase key file on GitHub (`firebase-key.json` is in .gitignore).

## Show up in Google search
The site already has a description, `/robots.txt` and `/sitemap.xml`. To get it into Google:
1. Open search.google.com/search-console -> Add property -> "URL prefix" -> paste your https://....onrender.com link.
2. Choose the "HTML tag" method and copy only the code inside `content="..."`.
3. On Render: Environment -> set `GOOGLE_SITE_VERIFICATION` to that code -> Save (it redeploys), then press Verify.
4. In Search Console: Sitemaps -> enter `sitemap.xml` -> Submit. Then URL Inspection -> Request indexing.
It usually appears in results within a few days to a few weeks. Links to the site from other pages
(e.g. the university or course page) help it rank higher.
