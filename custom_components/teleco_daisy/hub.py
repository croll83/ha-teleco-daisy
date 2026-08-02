import logging
from datetime import timedelta

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .lib import TelecoDaisy

_LOGGER = logging.getLogger(__name__)


class TelecoDaisyHub:
    def __init__(self, hass: HomeAssistant, email: str, password: str):
        self.hass = hass

        self.client = TelecoDaisy(
            email=email, password=password, session=async_get_clientsession(hass)
        )

        self.devices = []
        self._installation = None

        self.coordinator = DataUpdateCoordinator(
            hass,
            _LOGGER,
            name="Teleco Daisy Update",
            update_method=self.async_update_data,
            update_interval=timedelta(seconds=60),
        )

    async def async_setup(self) -> bool:
        try:
            await self.client.login()
            installations = await self.client.get_account_installation_list()

            if not installations:
                _LOGGER.error("No installations found for this account.")
                return False

            self._installation = installations[0]
            for installation in installations:
                rooms = await self.client.get_room_list(installation)

                for room in rooms:
                    self.devices.extend(room.deviceList)

            # discovery box in LAN al setup (~12s: intervallo beacon ~10s).
            # Bloccante ma una tantum; garantisce la cache prima dei comandi.
            try:
                await self.client.prime_box_ip(self._installation.instCode)
            except Exception as e:  # noqa: BLE001
                _LOGGER.warning("teleco_daisy: discovery box LAN fallita: %s", e)
            return True

        except Exception as err:
            _LOGGER.error("Failed to connect to Teleco Daisy: %s", err)
            return False

    async def async_update_data(self):
        try:
            # (ri)scopri l'IP box in LAN quando la cache è vuota. Inline: l'update
            # gira ogni 60s a event-loop calmo (post-avvio), quindi si auto-ripara
            # anche se la discovery al setup ha mancato il beacon.
            if self._installation is not None and self.client._cached_box_ip() is None:
                await self.client.prime_box_ip(self._installation.instCode)
            for device in self.devices:
                await device.update_state()
            return self.devices
        except Exception as err:
            raise UpdateFailed(f"Error communicating with Daisy API: {err}") from err
