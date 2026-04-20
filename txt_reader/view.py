"""HTTP View for TXT Reader with unified Pause, Pacing and Precise Progress."""
import asyncio
import logging
import time

from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.const import STATE_PLAYING, STATE_IDLE, STATE_ON
from wyoming.audio import AudioChunk, AudioStart
from wyoming.client import AsyncTcpClient
from wyoming.tts import SynthesizeChunk, SynthesizeStart, SynthesizeStop, SynthesizeStopped, SynthesizeVoice

from .const import DOMAIN
from .utils import create_wav_header

_LOGGER = logging.getLogger(__name__)

ACTIVE_STATES = (STATE_PLAYING, STATE_IDLE, STATE_ON, "buffering")

MAX_PAUSE_TIMEOUT = 3600  # 1 час

class TxtReaderStreamView(HomeAssistantView):
    """View to stream audio with precise byte-level pacing and ear-accurate progress."""

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
        config, file_path, chunks, store = session["config"], session["file_path"], session["chunks"], session["store"]
        player_id = session.get("player_id")
        timer_sec = session.get("timer_sec")

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

        def is_player_active():
            if not player_id: return True
            if (time.time() - stream_start_time) < 5: return True
            p_state = self.hass.states.get(player_id)
            return p_state is not None and p_state.state in ACTIVE_STATES

        async def text_feeder():
            try:
                for i in range(start_index, len(chunks)):
                    if state["stop"]: break
                    while not is_player_active():
                        if state["stop"]: return
                        await asyncio.sleep(1.5)

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
            except Exception: pass
            finally: await ready_blocks.put(None)

        feeder_task = asyncio.create_task(text_feeder())
        feeder_task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)

        bytes_sent = 0
        stream_start_time = time.time()
        current_playing_idx = start_index
        playback_timeline =[]

        try:
            while True:
                now = time.time()
                real_elapsed = now - stream_start_time
                
                while playback_timeline and real_elapsed > playback_timeline[0][1]:
                    finished_idx, _ = playback_timeline.pop(0)
                    current_playing_idx = finished_idx + 1
                    store.save_progress(file_path, current_playing_idx)
                    session["current_block"] = current_playing_idx

                while not is_player_active():
                    if state["stop"]: break
                    p_start = time.time()
                    await asyncio.sleep(1.5)
                    stream_start_time += (time.time() - p_start)

                if state["stop"]: break

                block = await ready_blocks.get()
                if block is None: 
                    # Если дошли до конца файла естественным образом, фиксируем последний блок
                    if not state["stop"]:
                        current_playing_idx = len(chunks) - 1
                    break
                
                block_dur = len(block['data']) / state["bytes_per_sec"]
                last_end = playback_timeline[-1][1] if playback_timeline else (bytes_sent / state["bytes_per_sec"])
                playback_timeline.append((block['idx'], last_end + block_dur))

                if not state["header_sent"]:
                    await response.write(create_wav_header(int(state["bytes_per_sec"]/2), 16, 1))
                    state["header_sent"] = True

                audio_bytes, chunk_size = block['data'], 4096
                for i in range(0, len(audio_bytes), chunk_size):
                    if not is_player_active():
                        pause_started_at = time.time()
                        while not is_player_active():
                            if state["stop"]: break
                            if (time.time() - pause_started_at) > MAX_PAUSE_TIMEOUT:
                                _LOGGER.info("Stream closed due to 1h pause timeout")
                                state["stop"] = True
                                break
                            p_inner_start = time.time()
                            await asyncio.sleep(1.5)
                            stream_start_time += (time.time() - p_inner_start)

                    if state["stop"]: break
                    
                    chunk = audio_bytes[i:i+chunk_size]
                    await response.write(chunk)
                    bytes_sent += len(chunk)

                    sent_sec = bytes_sent / state["bytes_per_sec"]
                    
                    if timer_sec and sent_sec >= timer_sec:
                        _LOGGER.info("Sleep timer reached (%s min). Stopping stream.", timer_sec // 60)
                        state["stop"] = True
                        break

                    real_elapsed = time.time() - stream_start_time
                    limit = max(lead_time_limit, initial_burst_seconds)
                    if sent_sec > (real_elapsed + limit):
                        await asyncio.sleep(min(sent_sec - (real_elapsed + limit), 0.5))

                if state["stop"]:
                    break
        
        except (ConnectionResetError, BrokenPipeError, asyncio.CancelledError):
            pass
        finally:
            state["stop"] = True
            if not feeder_task.done(): feeder_task.cancel()
            store.save_progress(file_path, current_playing_idx)
        
        return response