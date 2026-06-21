import asyncio
import base64
import json
import os
import re
import tempfile
import traceback
from datetime import datetime

from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
import edge_tts
from groq import AsyncGroq, BadRequestError
from tavily import TavilyClient

from personas import get_persona

load_dotenv()

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

groq_client = AsyncGroq(api_key=os.getenv("GROQ_API_KEY"))
tavily_client = TavilyClient(api_key=os.getenv("TAVILY_API_KEY"))

MODEL = "openai/gpt-oss-120b"

SEARCH_TOOL = [
    {
        "type": "function",
        "function": {
            "name": "search_web",
            "description": (
                "Search the web for real-world information. ALWAYS call this tool when "
                "the user asks about: recommendations or 'best' options (best apps, "
                "platforms, websites, courses, products, places to learn something), "
                "current facts or news, specific services or resources, how-to questions "
                "requiring up-to-date answers, or anything you are not fully certain "
                "about from memory. Prefer calling this tool over guessing — when in "
                "doubt, search first."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The search query to look up.",
                    }
                },
                "required": ["query"],
            },
        },
    }
]

SENTENCE_RE = re.compile(r'(?<=[.!?])\s+')


def split_into_sentences(text: str) -> list[str]:
    parts = SENTENCE_RE.split(text.strip())
    return [p.strip() for p in parts if p.strip()]


async def synthesize_chunk(text: str, voice: str, rate: str, pitch: str) -> bytes | None:
    try:
        communicate = edge_tts.Communicate(text, voice=voice, rate=rate, pitch=pitch)
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
            tmp_path = tmp.name
        await communicate.save(tmp_path)
        with open(tmp_path, "rb") as f:
            audio_bytes = f.read()
        os.unlink(tmp_path)
        return audio_bytes
    except Exception:
        return None


async def stream_and_tts(aiter, websocket: WebSocket, voice: str, rate: str, pitch: str) -> str:
    """Drain a Groq streaming response: forward text_chunks to the socket and
    interleave Edge-TTS audio chunks sentence by sentence. Returns full reply text."""
    full_text = ""
    sentence_buffer = ""

    async for chunk in aiter:
        text_piece = chunk.choices[0].delta.content or ""
        if not text_piece:
            continue
        full_text += text_piece
        sentence_buffer += text_piece

        await websocket.send_json({"type": "text_chunk", "text": text_piece})

        sentences = split_into_sentences(sentence_buffer)
        if len(sentences) >= 2:
            to_speak = " ".join(sentences[:-1])
            sentence_buffer = sentences[-1]
            audio = await synthesize_chunk(to_speak, voice, rate, pitch)
            if audio:
                await websocket.send_json({
                    "type": "audio_chunk",
                    "audio_base64": base64.b64encode(audio).decode(),
                })

    if sentence_buffer.strip():
        audio = await synthesize_chunk(sentence_buffer.strip(), voice, rate, pitch)
        if audio:
            await websocket.send_json({
                "type": "audio_chunk",
                "audio_base64": base64.b64encode(audio).decode(),
            })

    return full_text


@app.websocket("/ws/chat")
async def chat_ws(websocket: WebSocket):
    await websocket.accept()
    history: list[dict] = []  # OpenAI-format: {"role": ..., "content": ...}

    try:
        while True:
            raw = await websocket.receive_text()
            data = json.loads(raw)

            persona_id = data.get("persona_id", "pip")
            user_message = data.get("message", "").strip()
            if not user_message:
                continue

            persona = get_persona(persona_id)
            if not persona:
                await websocket.send_json({"type": "text_chunk", "text": "Unknown persona."})
                await websocket.send_json({"type": "done"})
                continue

            history.append({"role": "user", "content": user_message})

            groq_messages = [
                {"role": "system", "content": persona["system_prompt"]}
            ] + history

            tts_voice = persona["tts_voice"]
            tts_rate = persona["tts_rate"]
            tts_pitch = persona["tts_pitch"]

            try:
                print(
                    f"[{datetime.now().strftime('%H:%M:%S.%f')[:-3]}] "
                    f"Groq call — persona={persona_id!r} msg={user_message[:60]!r}"
                )

                # ── First call: NON-streaming for reliable tool_call detection ────────
                # llama-3.3-70b-versatile in streaming mode outputs tool calls as raw
                # "<function=name>args</function>" text instead of delta.tool_calls.
                # stream=False bypasses that and returns proper message.tool_calls.
                first_resp = await groq_client.chat.completions.create(
                    model=MODEL,
                    messages=groq_messages,
                    tools=SEARCH_TOOL,
                    tool_choice="auto",
                    stream=False,
                    max_tokens=1024,
                    timeout=30,
                )

                choice = first_resp.choices[0]
                raw_tool_calls = choice.message.tool_calls
                full_reply = ""

                if raw_tool_calls:
                    # ── Tool call path ────────────────────────────────────────────────
                    tc = raw_tool_calls[0]
                    tool_call_id   = tc.id
                    tool_call_name = tc.function.name
                    tool_call_args = tc.function.arguments
                    print(f"  → tool_call: {tool_call_name}({tool_call_args[:80]})")

                    query = json.loads(tool_call_args).get("query", user_message)
                    print(f"  → search_web query={query!r}")
                    print(f"  → calling Tavily (10 s timeout)...")

                    try:
                        search_response = await asyncio.wait_for(
                            asyncio.to_thread(lambda: tavily_client.search(query=query, max_results=3)),
                            timeout=10.0,
                        )
                    except asyncio.TimeoutError:
                        print(f"  ✗ Tavily timed out (10 s) for query={query!r}")
                        full_reply = "Hmm, my web search helper is taking too long right now — try asking me again in a moment!"
                        await websocket.send_json({"type": "text_chunk", "text": full_reply})
                    else:
                        results = search_response.get("results", [])
                        print(f"  ✓ Tavily returned {len(results)} result(s)")
                        results_text = "\n".join(
                            f"{i + 1}. {r['title']} ({r['url']}): {r['content']}"
                            for i, r in enumerate(results)
                        ) or "No results found."

                        groq_messages += [
                            {
                                "role": "assistant",
                                "content": None,
                                "tool_calls": [{
                                    "id": tool_call_id,
                                    "type": "function",
                                    "function": {
                                        "name": tool_call_name,
                                        "arguments": tool_call_args,
                                    },
                                }],
                            },
                            {
                                "role": "tool",
                                "tool_call_id": tool_call_id,
                                "content": results_text,
                            },
                        ]

                        print(f"  → second Groq call with {len(results)} search result(s)")
                        second_stream = await groq_client.chat.completions.create(
                            model=MODEL,
                            messages=groq_messages,
                            stream=True,
                            max_tokens=512,
                            timeout=30,
                        )
                        full_reply = await stream_and_tts(
                            second_stream, websocket, tts_voice, tts_rate, tts_pitch
                        )

                else:
                    # ── Direct answer path ────────────────────────────────────────────
                    full_reply = choice.message.content or ""
                    print(f"  → direct answer ({len(full_reply)} chars)")
                    if full_reply:
                        await websocket.send_json({"type": "text_chunk", "text": full_reply})
                        for sentence in split_into_sentences(full_reply):
                            if sentence.strip():
                                audio = await synthesize_chunk(sentence.strip(), tts_voice, tts_rate, tts_pitch)
                                if audio:
                                    await websocket.send_json({
                                        "type": "audio_chunk",
                                        "audio_base64": base64.b64encode(audio).decode(),
                                    })

                history.append({"role": "assistant", "content": full_reply})

            except BadRequestError as e:
                print(f"  ✗ Groq BadRequestError: {e}")
                if "tool_use_failed" in str(e).lower():
                    friendly = "I got a bit muddled trying to look that up — could you ask me again?"
                else:
                    friendly = "Oops, something went fuzzy on my end! Try asking again."
                await websocket.send_json({"type": "text_chunk", "text": friendly})
            except Exception:
                traceback.print_exc()
                err_str = traceback.format_exc().lower()
                if "rate" in err_str or "limit" in err_str or "quota" in err_str:
                    friendly = persona["name"] + " needs a tiny breather — ask me again in a moment!"
                else:
                    friendly = "Oops, something went fuzzy on my end! Try asking again."
                await websocket.send_json({"type": "text_chunk", "text": friendly})

            await websocket.send_json({"type": "done"})

    except WebSocketDisconnect:
        pass


app.mount("/", StaticFiles(directory="../frontend", html=True), name="frontend")
