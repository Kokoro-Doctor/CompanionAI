# Companion App — Build Guide (Local → Production on AWS)

This is the complete, locked spec and step-by-step path from zero to a live app.
Hand this whole file to Claude Code as your starting brief.

---

## 1. What we're building

A kids' AI companion web app. Child picks a character persona (Pip, Luna, Bongo,
or Coco) → real-time chat with that persona → every reply is read aloud →
chatbot can search the web for real answers → mocked payment/queue flow at the
end.

## 2. Locked tech stack

| Layer | Choice |
|---|---|
| Backend | Python / FastAPI, WebSocket-based |
| LLM | Google Gemini (free tier), Google Search grounding enabled |
| TTS (voice out) | Edge-TTS, server-side, streamed in chunks, en-US voices |
| STT (voice in) | Browser-native `SpeechRecognition` Web API, en-US, client-side only |
| Frontend | Original HTML/CSS/JS design, rewired for WebSocket + audio streaming |
| Hosting | AWS EC2 free tier (t2.micro/t3.micro) |

## 3. Architecture

```
Browser
  ├─ Mic icon → SpeechRecognition (en-US) → text → same path as typed input
  ├─ WebSocket connection → sends {persona_id, message, history}
  ↓
FastAPI WebSocket handler (/ws/chat)
  ├─ Calls Gemini, streaming mode, Google Search grounding ON
  ├─ Sentence-chunks the streamed text as it arrives
  ├─ Each chunk → Edge-TTS synthesizes audio (en-US voice per persona)
  ├─ Sends back interleaved over the socket:
  │     {type:"text_chunk", text}
  │     {type:"audio_chunk", audio_base64}
  │   ...repeats until reply finishes...
  └─ {type:"done"}
  ↓
Browser: appends text live, queues + plays audio chunks back-to-back
```

Conversation history is kept **server-side**, per WebSocket session — not
trusted from the client.

## 4. Feature details

### Personas
Four characters, each with its own system prompt, color identity, and TTS
voice/rate/pitch (already defined in `personas.py`):
- **Pip the Fox** — Early Math & Counting — `en-US-AndrewNeural`
- **Luna the Owl** — Calm Presence & Check-Ins — `en-US-AvaNeural`
- **Bongo the Bear** — Movement & Active Play — `en-US-GuyNeural`
- **Coco the Cat** — Words & Imagination — `en-US-AnaNeural`

### Voice input
Mic icon on the chat bar. Click → starts `SpeechRecognition` with
`lang = "en-US"` → on result, auto-fills the input and sends through the same
path as typed text. If the browser doesn't support it (e.g. Firefox), hide or
disable the icon — typing still works fully. No backend involvement.

### Voice output
Every assistant reply is synthesized to audio and streamed back in chunks so
playback can start before the full reply is done generating.

### Web search
Handled via Gemini's built-in Google Search grounding tool — no separate
search API or key needed.

### Payment flow (mocked)
Existing card form UI stays as-is (no real charge). On submit, the success
modal changes to a **queue confirmation**: *"You're on the queue! You're
#[number]"* — number is a plausible mocked value, not a real count.

### Error handling
- Gemini rate-limited → friendly in-character message sent as a normal
  `text_chunk` (e.g. "I need a tiny breather, ask me again in a moment!"),
  never a raw error or silent failure.
- Edge-TTS fails for a chunk → text still displays; that turn just has no
  audio rather than blocking the whole reply.

### Explicitly out of scope (for this prototype)
Real payments, real user accounts/auth, persistent database, parent
dashboard/notifications, non-US locales.

---

## 5. Step-by-step: Local setup

### Step 0 — Get your free Gemini API key
1. Go to **https://aistudio.google.com/apikey**
2. Sign in with any Google account
3. Click **"Create API key"** → choose "Create API key in new project" if
   asked
4. Copy the key — this is your `GEMINI_API_KEY`. No credit card needed for the
   free tier.

### Step 1 — Install prerequisites on your machine
- **Python 3.10+** — check with `python3 --version`
- **VS Code** — https://code.visualstudio.com/download
- VS Code extensions to install: **Python** (Microsoft), **Pylance**

### Step 2 — Open the project in VS Code
```bash
mkdir companion-app && cd companion-app
code .
```
This opens an empty folder in VS Code. From here, hand the spec in this file
to Claude Code (open VS Code's integrated terminal, run `claude`, and paste
this whole document as your first instruction) to scaffold:

```
companion-app/
├── backend/
│   ├── main.py              # FastAPI app + WebSocket handler
│   ├── personas.py          # persona definitions (already drafted)
│   └── requirements.txt
├── frontend/
│   └── index.html           # adapted from original prototype
├── deploy/
│   ├── setup_ec2.sh         # one-shot EC2 provisioning script
│   └── companion.service    # systemd unit file
├── .env.example
└── README.md
```

### Step 3 — Set up the backend locally
```bash
cd companion-app/backend
python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

`requirements.txt` should contain:
```
fastapi
uvicorn[standard]
google-genai
edge-tts
python-multipart
python-dotenv
```

### Step 4 — Add your API key
Create `backend/.env` (copy from `.env.example`):
```
GEMINI_API_KEY=your_key_here
```

### Step 5 — Run it locally
```bash
uvicorn main:app --reload --port 8000
```
Open `frontend/index.html` directly in a browser, or serve it via a simple
static server pointing its WebSocket URL at `ws://localhost:8000/ws/chat`.

### Step 6 — Test the full flow locally
- Pick a persona
- Type a question → confirm text streams in + audio plays
- Click the mic icon → speak a question → confirm it transcribes and sends
- Ask something requiring current info (e.g. "what's the weather today") →
  confirm search grounding kicks in
- Click "Sign up" → "Reserve for $1" → fill mock card form → confirm the
  queue-number confirmation shows

---

## 6. Step-by-step: Deploy to AWS EC2 (free tier)

### Step 1 — Launch an EC2 instance
1. AWS Console → EC2 → **Launch Instance**
2. Name: `companion-app`
3. AMI: **Ubuntu Server 22.04 LTS** (free tier eligible)
4. Instance type: **t2.micro** or **t3.micro** (free tier eligible)
5. Key pair: create a new one, download the `.pem` file, keep it safe
6. Network settings → Edit security group rules → **Add rule**:
   - Type: Custom TCP, Port: **8000**, Source: Anywhere (0.0.0.0/0)
   - (Also keep the default SSH rule on port 22)
7. Launch instance

### Step 2 — Connect via SSH
```bash
chmod 400 your-key.pem
ssh -i your-key.pem ubuntu@<your-ec2-public-ip>
```

### Step 3 — Run the setup script
Upload your project to the instance (simplest: `git clone` your repo, or
`scp` the folder), then:
```bash
cd companion-app
chmod +x deploy/setup_ec2.sh
./deploy/setup_ec2.sh
```
This script (to be generated by Claude Code) should:
- Install Python, pip, venv
- Install project dependencies
- Set `GEMINI_API_KEY` as an environment variable (prompt for it or read from
  an uploaded `.env`)
- Install and enable the `companion.service` systemd unit so the app starts
  on boot and restarts if it crashes
- Start the service

### Step 4 — Verify it's live
Visit `http://<your-ec2-public-ip>:8000` in a browser. Test the full flow
again exactly as in local Step 6.

### Step 5 — (Optional, later) Put a domain + HTTPS in front
Not required for a working prototype, but if you want a clean URL later:
point a domain at the EC2 IP and put Nginx + Let's Encrypt in front of port
8000. Skip this for today's goal.

---

## 7. Known risks to watch for

- **Edge-TTS is an unofficial library.** It usually works fine on EC2's
  unrestricted network, but if Microsoft changes the endpoint it depends on,
  TTS could break until the library is updated. Text chat will keep working
  regardless.
- **Gemini free tier has rate limits.** Fine for a prototype/demo; if you hit
  limits during testing, wait a minute and retry.
- **Browser mic support varies.** Works well on Chrome/Edge; degrades
  gracefully (typing still works) elsewhere.
- **EC2 free tier is 750 hrs/month for 12 months** from account creation —
  confirm your account is still within that window so this stays free.

---

## 8. Quick checklist

- [ ] Got free Gemini API key from aistudio.google.com
- [ ] Installed Python 3.10+, VS Code
- [ ] Scaffolded project with Claude Code using this spec
- [ ] `pip install -r requirements.txt` locally
- [ ] Added `GEMINI_API_KEY` to `.env`
- [ ] Ran locally, tested chat + voice in/out + search + payment flow
- [ ] Launched EC2 t2.micro/t3.micro, opened port 8000
- [ ] Deployed via setup script, systemd service running
- [ ] Verified live app at `http://<ec2-ip>:8000`
