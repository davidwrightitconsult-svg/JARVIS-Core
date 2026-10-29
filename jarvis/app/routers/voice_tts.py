"""Voice TTS endpoint — securely proxies to ElevenLabs."""
import os
import httpx
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

router = APIRouter(prefix="/api/voice", tags=["voice"])

ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY")
ELEVENLABS_VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID", "0R6Lfp4qCdzDNeJvoGCf")

class SynthesizeRequest(BaseModel):
    text: str

@router.post("/synthesize")
async def synthesize(req: SynthesizeRequest):
    if not req.text or not req.text.strip():
        raise HTTPException(status_code=400, detail="text is required")
    
    if not ELEVENLABS_API_KEY:
        raise HTTPException(status_code=500, detail="ElevenLabs API key not configured")

    url = f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVENLABS_VOICE_ID}/stream"
    headers = {
        "xi-api-key": ELEVENLABS_API_KEY,
        "Content-Type": "application/json"
    }
    payload = {
        "text": req.text.strip()[:1000],
        "model_id": "eleven_turbo_v2_5",  # The fastest conversational model
        "voice_settings": {
            "stability": 0.5,
            "similarity_boost": 0.75,
            "style": 0.0,
            "use_speaker_boost": True
        }
    }

    # We use an async client to stream the audio chunks back to the frontend
    async def stream_audio():
        async with httpx.AsyncClient() as client:
            async with client.stream("POST", url, json=payload, headers=headers, timeout=30.0) as response:
                if response.status_code != 200:
                    error_body = await response.aread()
                    print(f"ElevenLabs Error {response.status_code}: {error_body}")
                    return
                async for chunk in response.aiter_bytes():
                    yield chunk

    return StreamingResponse(stream_audio(), media_type="audio/mpeg")
