import httpx
from fastapi.responses import Response
from .config import ELEVENLABS_API_KEY,ELEVENLABS_VOICE_ID,ELEVENLABS_MODEL

def synthesize(text):
    if not (ELEVENLABS_API_KEY and ELEVENLABS_VOICE_ID): return None
    url=f'https://api.elevenlabs.io/v1/text-to-speech/{ELEVENLABS_VOICE_ID}'
    r=httpx.post(url,headers={'xi-api-key':ELEVENLABS_API_KEY,'Content-Type':'application/json'},params={'output_format':'mp3_44100_128'},json={'text':text,'model_id':ELEVENLABS_MODEL,'language_code':'en'},timeout=30)
    r.raise_for_status(); return Response(content=r.content,media_type='audio/mpeg')
