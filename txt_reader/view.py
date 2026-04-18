"""HTTP View for TXT Reader with block-duration pacing."""
import asyncio
import logging
import time

from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from wyoming.audio import AudioChunk, AudioStart
from wyoming.client import AsyncTcpClient
from wyoming.tts import SynthesizeChunk, SynthesizeStart, SynthesizeStop, SynthesizeStopped, SynthesizeVoice

from .const import DOMAIN
from .utils import create_wav_header

_LOGGER = logging.getLogger(__name__)


class TxtReaderStreamView(HomeAssistantView):
    """View to stream processed text-to-speech audio."""

    url = "/api/txt_reader/stream/{session_id}"
    name = "api:txt_reader:stream"
    requires_auth = False

    def __init__(self, hass):
        self.hass = hass

    async def get(self, request, session_id):
        """Handle GET request for audio streaming."""
        sessions = self.hass.data[DOMAIN].get("sessions", {})
        session = sessions.get(session_id)
        if not session:
            return web.Response(status=404, text="Expired")

        session["last_accessed"] = time.time()
        config = session["config"]
        file_path = session["file_path"]
        chunks = session["chunks"]
        store = session["store"]

        start_index = session.get("start_index", store.get_progress(file_path))
        session.pop("start_index", None)

        response = web.StreamResponse()
        response.content_type = "audio/wav"
        await response.prepare(request)

        # Queue for pre-fetched audio blocks
        ready_blocks = asyncio.Queue(maxsize=1)
        state = {"bytes_per_sec": 44100, "header_sent": False, "stop": False}

        async def text_feeder():
            """Producer task: fetch audio data from Wyoming server."""
            try:
                for i in range(start_index, len(chunks)):
                    if state["stop"]:
                        break
                    audio_data = bytearray()
                    async with AsyncTcpClient(config["host"], config["port"]) as client:
                        voice = SynthesizeVoice(name=config["voice"]) if config.get("voice") else None
                        await client.write_event(SynthesizeStart(voice=voice).event())
                        await client.write_event(SynthesizeChunk(text=chunks[i]).event())
                        await client.write_event(SynthesizeStop().event())
                        
                        while True:
                            event = await client.read_event()
                            if event is None:
                                break
                            if AudioStart.is_type(event.type):
                                info = AudioStart.from_event(event)
                                state["bytes_per_sec"] = info.rate * info.width * info.channels
                            elif AudioChunk.is_type(event.type):
                                audio_data.extend(AudioChunk.from_event(event).audio)
                            elif SynthesizeStopped.is_type(event.type):
                                break
                    
                    await ready_blocks.put({'idx': i, 'data': bytes(audio_data)})
            except Exception:
                pass
            finally:
                await ready_blocks.put(None)

        feeder_task = asyncio.create_task(text_feeder())

        # Audio streaming logic
        bytes_sent = 0
        start_time = time.time()
        current_idx = start_index

        try:
            while True:
                block = await ready_blocks.get()
                if block is None:
                    break
                
                current_idx = block['idx']
                store.save_progress(file_path, current_idx)
                session["current_block"] = current_idx

                if not state["header_sent"]:
                    header = create_wav_header(int(state["bytes_per_sec"] / 2), 16, 1)
                    await response.write(header)
                    state["header_sent"] = True

                # Stream audio chunks with rate-limiting to match playback speed
                audio_bytes = block['data']
                chunk_size = 4096 
                
                for i in range(0, len(audio_bytes), chunk_size):
                    chunk = audio_bytes[i:i + chunk_size]
                    await response.write(chunk)
                    bytes_sent += len(chunk)

                    # Throttle stream if buffer builds up to prevent connection timeout
                    total_audio_sec = bytes_sent / state["bytes_per_sec"]
                    real_elapsed_sec = time.time() - start_time
                    
                    if total_audio_sec > (real_elapsed_sec + 10):
                        await asyncio.sleep(0.1)
        
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        finally:
            state["stop"] = True
            if not feeder_task.done():
                feeder_task.cancel()
            store.save_progress(file_path, current_idx)
        
        return response