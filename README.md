# Companion App

Kids' AI companion web app — pick a character (Pip, Luna, Bongo, or Coco), chat in real-time, hear every reply read aloud, and ask questions that search the web.

## Quick start (local)

```bash
# 1. Install dependencies
cd backend
python -m venv venv
venv\Scripts\activate        # Windows
pip install -r requirements.txt

# 2. Add your Gemini API key
copy .env.example .env
# Edit .env and set GEMINI_API_KEY=your_key_here

# 3. Start the backend
uvicorn main:app --reload --port 8000

# 4. Open the frontend
# Open frontend/index.html in Chrome or Edge
```

Get a free Gemini API key at https://aistudio.google.com/apikey — no credit card needed.

## Stack

- **Backend**: Python / FastAPI / WebSocket
- **LLM**: Google Gemini 2.0 Flash with Google Search grounding
- **TTS**: Edge-TTS (server-side, streamed)
- **STT**: Browser SpeechRecognition API (Chrome/Edge)
- **Frontend**: Vanilla HTML/CSS/JS
