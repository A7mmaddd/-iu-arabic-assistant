# IU Arabic Courses Assistant — prototype
Python 3.10+ / Flask. Run:
    pip install -r requirements.txt
    export GEMINI_API_KEY=...           # from aistudio.google.com; without it the AI replies in demo mode
    export LLM_MODELS=gemini-3.5-flash-lite,gemini-3.1-flash-lite,gemini-3.8-flash   # optional; this is the default
    export GOOGLE_CLIENT_ID=...         # optional; without it a demo login is shown
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

## Deploy on Render (no cmd needed)
1. Push this folder to a GitHub repo (render.yaml must be at the repo root).
2. On render.com: New + -> Blueprint -> pick the repo.
3. Paste your Gemini API key (from Google AI Studio) when Render asks for GEMINI_API_KEY, then Apply.
4. Open the https://....onrender.com link. That's it.
Note: the free plan sleeps after ~15 min idle (first visit takes ~1 min) and
uploaded files / exams are kept in memory, so they reset when it sleeps or redeploys.

## Database (Firebase / Cloud Firestore)
- The same file uploaded by different students is recognised by its SHA-256 hash: its text, summaries and
  translations are saved once and reused (no new AI call). Quizzes and exam questions are always made fresh.
- Each user's file list and AI-tutor chat are saved under that user only; nobody else can read their chat.
- Without FIREBASE_CREDENTIALS everything still works, but only in memory (resets when the server restarts).
- Never put the Firebase key file on GitHub (`firebase-key.json` is in .gitignore).
