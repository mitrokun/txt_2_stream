"""Config flow for TXT Reader."""
import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    OptionsFlowWithConfigEntry,
)
from homeassistant.const import CONF_NAME
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
    CONF_BUFFER_BLOCKS,
    CONF_HOST,
    CONF_PORT,
    CONF_VOICE,
    DEFAULT_BUFFER_BLOCKS,
    DEFAULT_HOST,
    DEFAULT_PORT,
    DOMAIN,
)


class TxtReaderConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for TXT Reader."""

    VERSION = 1

    async def async_step_user(self, user_input=None):
        """Handle the initial step."""
        if user_input is not None:
            return self.async_create_entry(
                title=user_input.get(CONF_NAME, "TXT Reader Server"),
                data=user_input,
            )

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_NAME, default="Main TTS Server"): str,
                    vol.Required(CONF_HOST, default=DEFAULT_HOST): str,
                    vol.Required(CONF_PORT, default=DEFAULT_PORT): int,
                    vol.Optional(CONF_VOICE): str,
                    vol.Required(
                        CONF_BUFFER_BLOCKS, default=DEFAULT_BUFFER_BLOCKS
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(min=1, max=10, mode="box")
                    ),
                }
            ),
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry):
        """Get the options flow for this handler."""
        return TxtReaderOptionsFlow(config_entry)


class TxtReaderOptionsFlow(OptionsFlowWithConfigEntry):
    """Handle an options flow for TXT Reader."""

    async def async_step_init(self, user_input=None):
        """Manage the options."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        conf = self.config_entry.data
        opts = self.options

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_HOST, default=opts.get(CONF_HOST, conf.get(CONF_HOST))
                    ): str,
                    vol.Required(
                        CONF_PORT, default=opts.get(CONF_PORT, conf.get(CONF_PORT))
                    ): int,
                    vol.Optional(
                        CONF_VOICE,
                        description={
                            "suggested_value": opts.get(CONF_VOICE, conf.get(CONF_VOICE))
                        },
                    ): str,
                    vol.Required(
                        CONF_BUFFER_BLOCKS,
                        default=opts.get(
                            CONF_BUFFER_BLOCKS,
                            conf.get(CONF_BUFFER_BLOCKS, DEFAULT_BUFFER_BLOCKS),
                        ),
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(min=1, max=10, mode="box")
                    ),
                }
            ),
        )