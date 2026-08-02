from __future__ import annotations

from typing import Any

from homeassistant.components.cover import (
    ATTR_POSITION,
    ATTR_TILT_POSITION,
    CoverDeviceClass,
    CoverEntity,
    CoverEntityFeature,
)
import asyncio
import logging

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_platform
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .lib import (
    DaisyAwningCover,
    DaisyCover,
    DaisyRetractableSlatsCover,
    DaisyShadeCover,
    DaisySlatsCover,
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    hub = hass.data[DOMAIN][config_entry.entry_id]

    async_add_entities(
        [
            TelecoDaisyCover(hub.coordinator, device)
            for device in hub.devices
            if isinstance(device, DaisyCover)
        ]
    )

    # jog: micro-passi contati per l'angolo fine delle lamelle (frame singoli)
    platform = entity_platform.async_get_current_platform()
    # count: 1 frame ≈ 6° sui motori model 31 (quantizzati a frame);
    # 25 frame close = reset a saturazione da qualsiasi posizione ignota.
    platform.async_register_entity_service(
        "slats_jog",
        {
            vol.Required("direction"): vol.In(["open", "close"]),
            vol.Optional("count", default=1): vol.All(vol.Coerce(int), vol.Range(min=1, max=40)),
            vol.Optional("spacing", default=0.4): vol.All(vol.Coerce(float), vol.Range(min=0.3, max=10.0)),
        },
        "async_slats_jog",
    )


class TelecoDaisyCover(CoordinatorEntity, CoverEntity):
    def __init__(self, coordinator, cover: DaisyCover) -> None:
        super().__init__(coordinator)

        self._cover = cover

        self._attr_unique_id = str(cover.idInstallationDevice)
        self._attr_name = cover.label

        if isinstance(cover, DaisySlatsCover):
            self._attr_device_class = CoverDeviceClass.BLIND
            self._attr_supported_features = (
                CoverEntityFeature.OPEN
                | CoverEntityFeature.CLOSE
                | CoverEntityFeature.STOP
                | CoverEntityFeature.OPEN_TILT
                | CoverEntityFeature.CLOSE_TILT
                | CoverEntityFeature.SET_TILT_POSITION
                | CoverEntityFeature.STOP_TILT
            )
        elif isinstance(cover, DaisyShadeCover):
            self._attr_device_class = CoverDeviceClass.SHADE
            self._attr_supported_features = (
                CoverEntityFeature.OPEN
                | CoverEntityFeature.CLOSE
                | CoverEntityFeature.STOP
            )
        elif isinstance(cover, DaisyAwningCover):
            self._attr_device_class = CoverDeviceClass.AWNING
            self._attr_supported_features = (
                CoverEntityFeature.OPEN
                | CoverEntityFeature.CLOSE
                | CoverEntityFeature.STOP
            )
        elif isinstance(cover, DaisyRetractableSlatsCover):
            self._attr_device_class = CoverDeviceClass.AWNING
            self._attr_supported_features = (
                CoverEntityFeature.OPEN
                | CoverEntityFeature.CLOSE
                | CoverEntityFeature.STOP
                | CoverEntityFeature.OPEN_TILT
                | CoverEntityFeature.CLOSE_TILT
                | CoverEntityFeature.SET_TILT_POSITION
                | CoverEntityFeature.STOP_TILT
            )

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self._attr_unique_id)},
            name=self._attr_name,
            manufacturer="Teleco Automation",
        )

    @property
    def is_closed(self) -> bool | None:
        return self._cover.is_closed

    @property
    def current_cover_position(self) -> int | None:
        return getattr(self._cover, "position", None)

    @property
    def current_cover_tilt_position(self) -> int | None:
        return getattr(self._cover, "position", None)

    # @property
    # def is_closing(self) -> bool:
    #     """Return if the cover is closing or not."""
    #     return self._roller.moving < 0
    #
    # @property
    # def is_opening(self) -> bool:
    #     """Return if the cover is opening or not."""
    #     return self._roller.moving > 0
    #

    async def async_slats_jog(self, direction: str, count: int = 1,
                              spacing: float = 1.0) -> None:
        """`count` jog LOCALI affidabili (~5°/jog sui model 31), con la disciplina
        dell'app: ACK-wait per ogni jog + settle prima del successivo (la box fa
        un comando alla volta; connettersi mentre trasmette la radio dà DENY).

        Robustezza:
        - 'deny' (box occupata: contesa con app o radio in corso) → backoff e
          RIPROVA lo stesso jog (non lo conta), fino a max_deny tentativi.
        - pausa di drain ogni `group` jog (la box va in DENY dopo ~20 frame di
          fila; stiamo sotto con margine).
        - 'cloud' (nessuna box in LAN) → esce: le corse fluide vanno sul cloud,
          non ha senso fare jog cloud uno a uno.
        Serializzato col _burst_lock per non interleavare due motori."""
        from .lib import _burst_lock
        cover = self._cover
        cmd = {
            "deviceCode": str(cover.deviceIndex),
            "idInstallationDevice": cover.idInstallationDevice,
            "commandAction": "OPEN_STOP_CLOSE",
        } | cover.osc_map[direction]

        group = 15          # pausa di drain ogni 15 jog (soglia box ~20)
        drain = 6.0
        max_deny = 5        # tentativi max consecutivi su DENY prima di arrendersi
        async with _burst_lock:
            done = 0
            deny_streak = 0
            while done < count:
                status = await cover.client.local_jog(cover.installation, cmd)
                if status in ("ack", "noack"):
                    done += 1
                    deny_streak = 0
                    if done < count:
                        await asyncio.sleep(spacing)   # settle: radio finita
                        if done % group == 0:
                            await asyncio.sleep(drain)  # drain sotto la soglia box
                elif status == "deny":
                    deny_streak += 1
                    if deny_streak > max_deny:
                        _LOGGER.warning(
                            "slats_jog: DENY persistente dopo %d jog su %d "
                            "(box occupata) — mi fermo", done, count)
                        return
                    await asyncio.sleep(2.0 * deny_streak)  # backoff crescente
                else:  # 'cloud': niente box in LAN
                    _LOGGER.warning(
                        "slats_jog: box non in LAN — jog locali non disponibili "
                        "(fatti %d su %d)", done, count)
                    return

    async def async_open_cover(self, **kwargs: Any) -> None:
        await self._cover.open_cover()
        await self._update_state()

    async def async_close_cover(self, **kwargs: Any) -> None:
        await self._cover.close_cover()
        await self._update_state()

    async def async_stop_cover(self, **kwargs: Any) -> None:
        await self._cover.stop_cover()
        await self._update_state()

    async def async_open_cover_tilt(self, **kwargs: Any) -> None:
        await self._cover.open_cover()
        await self._update_state()

    async def async_close_cover_tilt(self, **kwargs: Any) -> None:
        await self._cover.close_cover()
        await self._update_state()

    async def async_stop_cover_tilt(self, **kwargs: Any) -> None:
        await self._cover.stop_cover()
        await self._update_state()

    async def async_set_cover_position(self, **kwargs: Any) -> None:
        await self._async_set_cover_position(kwargs[ATTR_POSITION])

    async def async_set_cover_tilt_position(self, **kwargs: Any) -> None:
        await self._async_set_cover_position(kwargs[ATTR_TILT_POSITION])

    async def _async_set_cover_position(self, position: int) -> None:
        if position <= 15:
            await self._cover.close_cover()
        elif 15 < position <= 48:
            await self._cover.open_cover("33")
        elif 48 < position <= 81:
            await self._cover.open_cover("66")
        else:
            await self._cover.open_cover("100")
        await self._update_state()

    async def _update_state(self):
        await self._cover.update_state()
        self.async_write_ha_state()
