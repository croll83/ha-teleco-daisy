"""Config flow UI per Teleco Daisy (login all'account app Daisy / cloud tmate)."""
import logging
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import DOMAIN
from .lib import TelecoDaisy

_LOGGER = logging.getLogger(__name__)

AUTH_SCHEMA = vol.Schema(
    {vol.Required(CONF_USERNAME): str, vol.Required(CONF_PASSWORD): str}
)


class TelecoDaisyConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1
    MINOR_VERSION = 1
    CONNECTION_CLASS = config_entries.CONN_CLASS_CLOUD_POLL

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        errors: dict[str, str] = {}
        if user_input is not None:
            # ⚠️ il cloud Daisy rifiuta l'email con maiuscole ("Wrong user name
            # or password"): la normalizziamo a minuscolo per evitare l'hub morto.
            username = user_input[CONF_USERNAME].strip().lower()
            password = user_input[CONF_PASSWORD]
            client = TelecoDaisy(
                username, password, session=async_get_clientsession(self.hass)
            )
            try:
                await client.login()
                installations = await client.get_account_installation_list()
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Teleco Daisy: login fallito")
                errors["base"] = "invalid_auth"
            else:
                if not installations:
                    errors["base"] = "no_installations"
                else:
                    await self.async_set_unique_id(str(installations[0].idInstallation))
                    self._abort_if_unique_id_configured()
                    return self.async_create_entry(
                        title="Teleco Daisy",
                        data={CONF_USERNAME: username, CONF_PASSWORD: password},
                    )
        return self.async_show_form(
            step_id="user", data_schema=AUTH_SCHEMA, errors=errors
        )
