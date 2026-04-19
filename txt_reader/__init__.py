import logging
import uuid
import time
import os
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers.network import get_url

from .const import DOMAIN, MAX_CHUNK_LENGTH
from .store import AudiobookStore
from .utils import get_book_chunks
from .view import TxtReaderStreamView

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    if DOMAIN not in hass.data:
        store = AudiobookStore(hass)
        await store.async_load()
        hass.data[DOMAIN] = {"sessions": {}, "store": store}
        hass.http.register_view(TxtReaderStreamView(hass))

        async def handle_play(call: ServiceCall):
            conf_entry = hass.config_entries.async_get_entry(call.data["config_entry"])
            if not conf_entry: return
            config = {**conf_entry.data, **conf_entry.options}
            if call.data.get("voice"): config["voice"] = call.data["voice"]
            
            player_id = call.data["entity_id"]
            file_path = call.data["file_path"]
            manual_idx = call.data.get("block_index")
            global_store = hass.data[DOMAIN]["store"]
            
            # Название книги
            book_title = os.path.basename(file_path).replace(".txt", "").capitalize()

            chunks = await hass.async_add_executor_job(get_book_chunks, file_path, MAX_CHUNK_LENGTH)
            if not chunks: return

            # Если указан индекс вручную, обновляем его в базе сразу
            if manual_idx is not None:
                global_store.save_progress(file_path, manual_idx)
                start_index = manual_idx
            else:
                start_index = global_store.get_progress(file_path)

            now, sessions = time.time(), hass.data[DOMAIN]["sessions"]
            
            # Очистка старых сессий (неактивных более 12 часов)
            for sid in list(sessions.keys()):
                if now - sessions[sid].get("last_accessed", now) > 43200:
                    sessions.pop(sid, None)

            session_id = uuid.uuid4().hex
            # ВАЖНО: Добавляем player_id в объект сессии
            sessions[session_id] = {
                "config": config,
                "file_path": file_path,
                "chunks": chunks,
                "store": global_store,
                "start_index": start_index,
                "last_accessed": now,
                "player_id": player_id  # <--- Ключевое дополнение
            }

            # Отправка команды плееру с метаданными
            await hass.services.async_call("media_player", "play_media", {
                "entity_id": player_id,
                "media_content_id": f"{get_url(hass)}/api/txt_reader/stream/{session_id}",
                "media_content_type": "music",
                "extra": {
                    "title": book_title,
                    "artist": "TXT Reader TTS"
                }
            })

        hass.services.async_register(DOMAIN, "play", handle_play)
    return True

async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    if not hass.config_entries.async_entries(DOMAIN):
        hass.services.async_remove(DOMAIN, "play")
        hass.data.pop(DOMAIN, None)
    return True