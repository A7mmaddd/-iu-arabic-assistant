# IU Arabic Courses Assistant — prototype
Python 3.10+ / Flask. Run:
    pip install -r requirements.txt
    export ANTHROPIC_API_KEY=...        # without it the AI replies in demo mode
    export GOOGLE_CLIENT_ID=...         # optional; without it a demo login is shown
    python app.py                       # http://127.0.0.1:5000
Files: models.py (class diagram classes), services.py (AuthService, AIService, LLMService), app.py (web routes), static/index.html (UI).

## Deploy on Render (no cmd needed)
1. Push this folder to a GitHub repo (render.yaml must be at the repo root).
2. On render.com: New + -> Blueprint -> pick the repo.
3. Paste your CometAPI key when Render asks for COMETAPI_KEY, then Apply.
4. Open the https://....onrender.com link. That's it.
Note: the free plan sleeps after ~15 min idle (first visit takes ~1 min) and
uploaded files / exams are kept in memory, so they reset when it sleeps or redeploys.
