"""HTTP View for TXT Reader with Silent Pause and Auto-Resume."""
import asyncio
import logging
import time

from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.const import STATE_PLAYING
from wyoming.audio import AudioChunk, AudioStart
from wyoming.client import AsyncTcpClient
from wyoming.tts import SynthesizeChunk, SynthesizeStart, SynthesizeStop, SynthesizeStopped, SynthesizeVoice

from .const import DOMAIN
from .utils import create_wav_header

_LOGGER = logging.getLogger(__name__)


class TxtReaderStreamView(HomeAssistantView):
    """View to stream audio that stays open during pause and resumes automatically."""

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
        player_id = session.get("player_id")

        buffer_setting = config.get("buffer_blocks", 2)
        lead_time_limit = buffer_setting * 12.0
        initial_burst_seconds = 15.0 

        start_index = session.get("start_index", store.get_progress(file_path))
        session.pop("start_index", None)

        response = web.StreamResponse()
        response.content_type = "audio/wav"
        await response.prepare(request)

        ready_blocks = asyncio.Queue(maxsize=1)
        state = {"bytes_per_sec": 44100, "header_sent": False, "stop": False}

        def is_player_playing():
            """Check if the target player is in playing state."""
            if not player_id:
                return True
            # Allow start-up buffer
            if (time.time() - stream_start_time) < 5:
                return True
            p_state = self.hass.states.get(player_id)
            return p_state is not None and p_state.state == STATE_PLAYING

        async def text_feeder():
            """Producer task: synthesizes text only when player is active."""
            try:
                for i in range(start_index, len(chunks)):
                    if state["stop"]:
                        break

                    # ПАУЗА СИНТЕЗА: Ждем возобновления, прежде чем идти в Wyoming
                    while not is_player_playing():
                        if state["stop"]: return
                        await asyncio.sleep(1.0)

                    audio_data = bytearray()
                    async with AsyncTcpClient(config["host"], config["port"]) as client:
                        voice = SynthesizeVoice(name=config["voice"]) if config.get("voice") else None
                        await client.write_event(SynthesizeStart(voice=voice).event())
                        await client.write_event(SynthesizeChunk(text=chunks[i]).event())
                        await client.write_event(SynthesizeStop().event())
                        
                        while True:
                            event = await client.read_event()
                            if event is None: break
                            if AudioStart.is_type(event.type):
                                info = AudioStart.from_event(event)
                                state["bytes_per_sec"] = info.rate * info.width * info.channels
                            elif AudioChunk.is_type(event.type):
                                audio_data.extend(AudioChunk.from_event(event).audio)
                            elif SynthesizeStopped.is_type(event.type):
                                break
                    
                    if not state["stop"]:
                        await ready_blocks.put({'idx': i, 'data': bytes(audio_data)})
            except Exception:
                pass
            finally:
                await ready_blocks.put(None)

        feeder_task = asyncio.create_task(text_feeder())
        
        def _handle_task_result(task):
            try:
                if not task.cancelled(): task.exception()
            except Exception: pass
        feeder_task.add_done_callback(_handle_task_result)

        bytes_sent = 0
        stream_start_time = time.time()
        current_idx = start_index

        try:
            while True:
                # ПАУЗА ПОТОКА: Ждем возобновления перед следующим блоком
                if not is_player_playing():
                    _LOGGER.info("Pacer: Waiting for resume...")
                    p_start = time.time()
                    while not is_player_playing():
                        if state["stop"]: break
                        await asyncio.sleep(1.0)
                    # Корректируем время, чтобы не было рывка
                    stream_start_time += (time.time() - p_start)

                block = await ready_blocks.get()
                if block is None: break
                
                current_idx = block['idx']
                store.save_progress(file_path, current_idx)
                session["current_block"] = current_idx

                if not state["header_sent"]:
                    header = create_wav_header(int(state["bytes_per_sec"] / 2), 16, 1)
                    await response.write(header)
                    state["header_sent"] = True

                audio_bytes = block['data']
                chunk_size = 4096 
                
                for i in range(0, len(audio_bytes), chunk_size):
                    # ПАУЗА ВНУТРИ БЛОКА: Замораживаем передачу байтов
                    if not is_player_playing():
                        _LOGGER.debug("Pacer: Pause in middle of block %s. Standing by...", current_idx)
                        p_inner_start = time.time()
                        while not is_player_playing():
                            if state["stop"]: break
                            await asyncio.sleep(1.0)
                        stream_start_time += (time.time() - p_inner_start)
                        _LOGGER.info("Pacer: Resumed exactly where left off.")

                    chunk = audio_bytes[i:i + chunk_size]
                    await response.write(chunk)
                    bytes_sent += len(chunk)

                    # Логика капельницы
                    total_audio_sec = bytes_sent / state["bytes_per_sec"]
                    real_elapsed_sec = time.time() - stream_start_time
                    current_limit = max(lead_time_limit, initial_burst_seconds)
                    
                    if total_audio_sec > (real_elapsed_sec + current_limit):
                        wait_time = total_audio_sec - (real_elapsed_sec + current_limit)
                        await asyncio.sleep(min(wait_time, 0.5))
        
        except (ConnectionResetError, asyncio.CancelledError):
            _LOGGER.debug("Pacer: Stream connection closed by player.")
        finally:
            state["stop"] = True
            if not feeder_task.done():
                feeder_task.cancel()
            store.save_progress(file_path, current_idx)
        
        return response