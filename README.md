# IU Arabic Courses Assistant — prototype
Python 3.10+ / Flask. Run:
    pip install -r requirements.txt
    export COMETAPI_KEY=...             # without it the AI replies in demo mode
    export GOOGLE_CLIENT_ID=...         # optional; without it a demo login is shown
    python app.py                       # http://127.0.0.1:5000
Files: models.py (class diagram classes), services.py (AuthService, AIService, LLMService), app.py (web routes),
static/index.html (UI + SpeechService). MAPPING.md shows where every class and use case is in the code.

## Features
Student: summary, translation, AI tutor (reply to an answer, ask by voice, listen to answers), quizzes,
take a published exam (exam mode locks the other features and counts leaving the page).
Instructor: generate exam questions in English or Arabic, edit questions and options, add own questions
(checked against the file), print or copy the exam (with or without answer key), publish with a code,
see results with the most-missed questions and how many times each student left the page.

## Deploy on Render (no cmd needed)
1. Push this folder to a GitHub repo (render.yaml must be at the repo root).
2. On render.com: New + -> Blueprint -> pick the repo.
3. Paste your CometAPI key when Render asks for COMETAPI_KEY, then Apply.
4. Open the https://....onrender.com link. That's it.
Note: the free plan sleeps after ~15 min idle (first visit takes ~1 min) and
uploaded files / exams are kept in memory, so they reset when it sleeps or redeploys.
