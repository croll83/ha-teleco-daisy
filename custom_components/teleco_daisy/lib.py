import asyncio
import logging
import time
from typing import Any, Literal

import aiohttp
from pydantic import BaseModel, ConfigDict

from . import daisy_local

_LOGGER = logging.getLogger(__name__)
base_url = "https://tmate.telecoautomation.com/"


class DaisyStatus(BaseModel):
    """for /status-device-list"""

    idInstallationDeviceStatusitem: int
    idDevicetypeStatusitemModel: int
    statusitemCode: str
    statusItem: str
    statusValue: str
    lowlevelStatusitem: None | str = None


class DaisyInstallation(BaseModel):
    activetimer: str
    firmwareVersion: str
    idInstallation: int
    idInstallationDevice: int
    instCode: str
    instDescription: str
    installationOrder: int
    latitude: float | None
    longitude: float | None
    weekend: str | None  # list[str]
    workdays: str | None  # list[str]

    def __str__(self):
        return f"DaisyInstallation fw{self.firmwareVersion}"


class DaisyBaseDevice(BaseModel):
    activetimer: str
    deviceCode: str
    deviceIndex: int
    deviceOrder: int
    directOnly: str | None = None
    favorite: str
    feedback: str
    idDevicemodel: int
    idDevicetype: int
    idInstallationDevice: int
    label: str
    remoteControlCode: str


class DaisyDevice(DaisyBaseDevice):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    client: "TelecoDaisy"
    installation: DaisyInstallation

    def __str__(self):
        return f'{self.__class__.__name__} "{self.label}"'

    async def command(self, params: dict, ignore_ack: bool = False):
        res = await self.client.feed_the_commands(
            installation=self.installation,
            commandsList=[
                {
                    "deviceCode": str(self.deviceIndex),
                    "idInstallationDevice": self.idInstallationDevice,
                }
                | params
            ],
            ignore_ack=ignore_ack,
        )
        # se eseguito in locale, i comandi non aggiornano la cache cloud:
        # per comandi singoli (non jog OPEN/STOP/CLOSE) forziamo un GET_FEEDBACK
        # così lo stato letto dal cloud resta fresco. I jog restano dead-reckoning.
        if isinstance(res, dict) and res.get("local"):
            cp = str(params.get("commandParam", "")).upper()
            if cp not in ("OPEN", "STOP", "CLOSE"):
                await self.client.refresh_local_feedback(
                    self.installation, self.idInstallationDevice,
                    self.deviceIndex, self.idDevicemodel)
        return res

    async def update_state(self) -> list[DaisyStatus]:
        return await self.client.status_device_list(self.installation, self)


class DaisyDeviceWithCommands(DaisyBaseDevice):
    class DeviceCommand(BaseModel):
        commandAction: str
        commandCode: str
        commandParam: str
        deviceIndex: int
        idDevicetypeCommandModel: int
        idInstallationDeviceCommand: int
        lowlevelCommand: str

    deviceCommandList: list[DeviceCommand]

    def __str__(self):
        return (
            f'DaisyDevice "{self.label}" '
            f"(deviceType: {self.idDevicetype}, deviceModel: {self.idDevicemodel})"
        )


class DaisyRoom(BaseModel):
    idInstallationRoom: int
    idRoomtype: int
    roomDescription: str
    roomOrder: int
    deviceList: list[DaisyDevice]

    def __str__(self):
        return f'DaisyRoom "{self.roomDescription}"'


class DaisyRoomWithCommands(DaisyRoom):
    deviceList: list[DaisyDeviceWithCommands]


# Le raffiche open/close devono viaggiare come treno radio contiguo per il
# singolo motore: se due cover trasmettono in parallelo i frame si
# interleavano nella coda della DaisyBox, la spaziatura per-motore raddoppia
# e il motore vede N jog invece della pressione lunga. Un lock globale
# serializza i treni (i frame singoli - stop/jog - passano liberi).
_burst_lock = asyncio.Lock()


class DaisyCover(DaisyDevice):
    is_closed: bool | None = None

    # Ripetizioni frame per open/close: 1 = frame singolo. I motori lamelle
    # (es. model 31 TVBLADE) col frame singolo fanno un JOG (micro-passo);
    # la corsa continua richiede il frame ripetuto, come la pressione lunga
    # del telecomando. Lo stop resta sempre singolo.
    run_repeat: int = 1

    osc_map: dict[Literal["open", "stop", "close"], dict[str, Any]]

    async def update_state(self):
        stati = await super().update_state()
        for status in stati:
            if status.statusitemCode == "OPEN_CLOSE":
                match status.statusValue:
                    case "CLOSE":
                        self.is_closed = True
                    case "OPEN":
                        self.is_closed = False
                    case _:
                        self.is_closed = None
        return stati

    async def open_cover(self):
        return await self._open_stop_close("open")

    async def stop_cover(self):
        await self._open_stop_close("stop")

    async def close_cover(self):
        await self._open_stop_close("close")

    async def _open_stop_close(
        self,
        open_stop_close: Literal["open", "stop", "close"],
        repeat: int | None = None,
    ):
        reps = repeat if repeat is not None else (
            1 if open_stop_close == "stop" else self.run_repeat)
        res = None
        if reps <= 1:
            return await self.command(
                {"commandAction": "OPEN_STOP_CLOSE"} | self.osc_map[open_stop_close]
            )
        async with _burst_lock:
            # ignore_ack: attendere l'ack della box tra un frame e l'altro
            # (poll 0,5s+ per frame) dilata il treno radio oltre la finestra
            # di "pressione lunga" → il motore vede jog separati e si ferma
            # a metà corsa. I frame della raffica partono senza attesa ack.
            for i in range(reps):
                if i:
                    await asyncio.sleep(0.3)
                res = await self.command(
                    {"commandAction": "OPEN_STOP_CLOSE"} | self.osc_map[open_stop_close],
                    ignore_ack=True,
                )
        return res

    async def jog(self, direction: Literal["open", "close"]):
        """Singolo frame (~6° sui model 31). Senza attesa ack: nei conteggi
        multi-frame l'ack-poll dilaterebbe il treno."""
        return await self.command(
            {"commandAction": "OPEN_STOP_CLOSE"} | self.osc_map[direction],
            ignore_ack=True,
        )


class DaisySlatsCover(DaisyCover):
    position: int | None = None

    osc_map: dict[Literal["open", "stop", "close"], dict[str, Any]] = {
        "open": {"commandId": 94, "commandParam": "OPEN", "lowlevelCommand": "CH4"},
        "stop": {"commandId": 95, "commandParam": "STOP", "lowlevelCommand": "CH7"},
        "close": {"commandId": 96, "commandParam": "CLOSE", "lowlevelCommand": "CH1"},
    }

    async def open_cover(self, percent: Literal["33", "66", "100"] | None = None):
        if percent == "100" or percent is None:
            return await self._open_stop_close("open")

        percent_map = {
            "33": {"commandParam": "LEV2", "commandId": 97, "lowlevelCommand": "CH2"},
            "66": {"commandParam": "LEV3", "commandId": 98, "lowlevelCommand": "CH3"},
            "100": {"commandParam": "LEV4", "commandId": 99, "lowlevelCommand": "CH4"},
        }

        return await self.command({"commandAction": "LEVEL"} | percent_map[percent])

    async def update_state(self):
        stati = await super().update_state()
        for status in stati:
            if status.statusitemCode == "LEVEL":
                self.position = int(status.statusValue)
        return stati


class DaisyRetractableSlatsCover(DaisyCover):
    position: int | None = None

    osc_map: dict[Literal["open", "stop", "close"], dict[str, Any]] = {
        "open": {"commandId": 206, "commandParam": "OPEN", "lowlevelCommand": "CH5"},
        "stop": {"commandId": 207, "commandParam": "STOP", "lowlevelCommand": "CH7"},
        "close": {"commandId": 208, "commandParam": "CLOSE", "lowlevelCommand": "CH8"},
    }

    async def open_cover_tilt(
        self, percent: Literal["0", "33", "66", "100"] | None = None
    ):
        percent_map = {
            "0": {"commandParam": "LEV1", "commandId": 214, "lowlevelCommand": "CH1"},
            "33": {"commandParam": "LEV2", "commandId": 210, "lowlevelCommand": "CH2"},
            "66": {"commandParam": "LEV3", "commandId": 211, "lowlevelCommand": "CH3"},
            "100": {"commandParam": "LEV4", "commandId": 212, "lowlevelCommand": "CH4"},
        }

        return await self.command({"commandAction": "LEVEL"} | percent_map[percent])

    async def update_state(self):
        stati = await super().update_state()
        for status in stati:
            if status.statusitemCode == "LEVEL":
                self.position = int(status.statusValue)
        return stati


class DaisyShadeCover(DaisyCover):
    pass


class DaisyAwningCover(DaisyCover):
    pass


class DaisyLight(DaisyDevice):
    is_on: bool | None = None
    brightness: int | None = None  # from 0 to 100

    async def update_state(self):
        stati = await super().update_state()
        for status in stati:
            if status.statusitemCode == "POWER":
                self.is_on = status.statusValue == "ON"
        return stati

    async def set_brightness(self, brightness: int):
        raise NotImplementedError

    async def _turn_on(self, specific_params: dict):
        return await self.command(
            {"commandAction": "POWER", "commandParam": "ON"} | specific_params
        )

    async def _turn_off(self, specific_params: dict):
        return await self.command(
            {"commandAction": "POWER", "commandParam": "OFF"} | specific_params
        )


class DaisyRGBLight(DaisyLight):
    rgb: tuple[int, int, int] | None = None

    async def update_state(self):
        stati = await super().update_state()
        for status in stati:
            if status.statusitemCode == "COLOR":
                val = status.statusValue
                self.brightness = int(val[1:4])
                self.rgb = (int(val[5:8]), int(val[9:12]), int(val[13:16]))
        return stati

    async def set_rgb_and_brightness(
        self, rgb: tuple[int, int, int] | None = None, brightness: int | None = None
    ):
        if brightness is None:
            brightness = self.brightness or 0
        if 0 > brightness or brightness > 100:
            raise ValueError("Brightness must be between 0 and 100")
        if rgb is None:
            rgb = self.rgb or (255, 255, 255)
        if any((c < 0 or c > 255) for c in rgb):
            raise ValueError("Color must be between 0 and 255")

        v = f"A{brightness:03d}R{rgb[0]:03d}G{rgb[1]:03d}B{rgb[2]:03d}"

        return await self.command(
            {
                "commandAction": "COLOR",
                "commandId": 137,
                "commandParam": v,
                "lowlevelCommand": None,
            }
        )

    async def turn_on(self):
        return await self._turn_on({"commandId": 138, "lowlevelCommand": None})

    async def turn_off(self):
        return await self._turn_off({"commandId": 138, "lowlevelCommand": None})


# class DaisyWhiteLight(DaisyLight):
#     def set_brightness(self, brightness: int):
#         if brightness is None:
#             brightness = self.brightness or 0
#         if 0 > brightness or brightness > 100:
#             raise ValueError("Brightness must be between 0 and 100")
#
#         v = f"A{brightness:03d}R255G255B255"
#
#         return self.command(
#             {
#                 "commandAction": "COLOR",
#                 "commandId": 146,
#                 "commandParam": v,
#                 "lowlevelCommand": "CH1",
#             }
#         )
#
#     def turn_on(self):
#         # https://github.com/andreasnuesslein/py-teleco-daisy/issues/10
#         if self.idDevicetype == 21 and self.idDevicemodel == 17:
#             return self._turn_on({"commandId": 40, "lowlevelCommand": "CH1"})
#         return self._turn_on({"commandId": 146, "lowlevelCommand": "CH1"})
#
#     def turn_off(self):
#         # https://github.com/andreasnuesslein/py-teleco-daisy/issues/10
#         if self.idDevicetype == 21 and self.idDevicemodel == 17:
#             return self._turn_off({"commandId": 41, "lowlevelCommand": "CH8"})
#         return self._turn_off({"commandId": 147, "lowlevelCommand": "CH8"})


class DaisyWhite4LevelLight(DaisyLight):
    brightness_map: dict[Literal[25, 50, 75, 100], dict[str, Any]]

    async def set_brightness(self, brightness: int):
        if brightness is None:
            brightness = self.brightness or 0
        if 0 > brightness or brightness > 100:
            raise ValueError("Brightness must be between 0 and 100")

        if brightness is None or brightness == 0:
            return await self.turn_off()

        if 1 <= brightness <= 37:
            vals = self.brightness_map[25]
        elif 38 <= brightness <= 62:
            vals = self.brightness_map[50]
        elif 63 <= brightness <= 87:
            vals = self.brightness_map[75]
        else:  # 76-100
            vals = self.brightness_map[100]

        return await self.command({"commandAction": "LEVEL"} | vals)

    async def turn_on(self):
        if self.idDevicetype == 21 and self.idDevicemodel == 17:
            return await self._turn_on({"commandId": 40, "lowlevelCommand": "CH1"})
        if self.idDevicetype == 21 and self.idDevicemodel == 34:
            return await self._turn_on({"commandId": 146, "lowlevelCommand": "CH1"})

        # legacy, without devicemodelmatching
        return await self._turn_on({"commandId": 146, "lowlevelCommand": "CH1"})

    async def turn_off(self):
        # https://github.com/andreasnuesslein/py-teleco-daisy/issues/10
        if self.idDevicetype == 21 and self.idDevicemodel == 17:
            return await self._turn_off({"commandId": 41, "lowlevelCommand": "CH8"})
        if self.idDevicetype == 21 and self.idDevicemodel == 34:
            return await self._turn_on({"commandId": 147, "lowlevelCommand": "CH8"})

        # legacy, without devicemodelmatching
        return await self._turn_off({"commandId": 147, "lowlevelCommand": "CH8"})

    async def update_state(self):
        stati = await super().update_state()
        for status in stati:
            if status.statusitemCode == "POWER":
                self.is_on = status.statusValue == "ON"
            elif status.statusitemCode == "LEVEL":
                try:
                    self.brightness = {
                        "25": 25,
                        "50": 50,
                        "75": 75,
                        "100": 100,
                    }[status.statusValue]
                except KeyError:
                    self.brightness = 50
        return stati


class DaisyHeater4CH(DaisyDevice):
    async def turn_on(self):
        return await self.command(
            {
                "commandAction": "POWER",
                "commandParam": "ON",
                "lowlevelCommand": "CH1",
                "commandId": 58,
            }
        )

    async def turn_off(self):
        return await self.command(
            {
                "commandAction": "POWER",
                "commandParam": "OFF",
                "lowlevelCommand": "CH4",
                "commandId": 59,
            }
        )

    async def set_level(self, level: Literal["50", "75", "100"]):
        if level == "50":
            cmd = {
                # "idDevicetypeCommandModel": 60,
                "commandId": 60,
                "commandParam": "LEV2",
                "lowlevelCommand": "CH3",
            }
        elif level == "75":
            cmd = {
                # "idDevicetypeCommandModel": 61,
                "commandId": 61,
                "commandParam": "LEV3",
                "lowlevelCommand": "CH2",
            }
        else:
            cmd = {
                # "idDevicetypeCommandModel": 62,
                "commandId": 62,
                "commandParam": "LEV4",
                "lowlevelCommand": "CH1",
            }

        return await self.command({"commandAction": "LEVEL"} | cmd)


def create_specific_device(dev):
    match dev:
        case {"idDevicetype": 21, "idDevicemodel": 17}:
            dev["brightness_map"] = {
                25: {"commandParam": "LEV1", "commandId": 42, "lowlevelCommand": "CH4"},
                50: {"commandParam": "LEV2", "commandId": 43, "lowlevelCommand": "CH3"},
                75: {"commandParam": "LEV3", "commandId": 44, "lowlevelCommand": "CH2"},
                100: {
                    "commandParam": "LEV4",
                    "commandId": 45,
                    "lowlevelCommand": "CH1",
                },
            }
            return DaisyWhite4LevelLight(**dev)

        case {"idDevicetype": 21, "idDevicemodel": 20}:
            return DaisyHeater4CH(**dev)

        case {"idDevicetype": 21, "idDevicemodel": 34}:
            dev["brightness_map"] = {
                25: {
                    "commandParam": "LEV1",
                    "commandId": 141,
                    "lowlevelCommand": "CH4",
                },
                50: {
                    "commandParam": "LEV2",
                    "commandId": 142,
                    "lowlevelCommand": "CH3",
                },
                75: {
                    "commandParam": "LEV3",
                    "commandId": 143,
                    "lowlevelCommand": "CH2",
                },
                100: {
                    "commandParam": "LEV4",
                    "commandId": 144,
                    "lowlevelCommand": "CH1",
                },
            }
            return DaisyWhite4LevelLight(**dev)

        case {"idDevicetype": 22, "idDevicemodel": 21}:
            dev["osc_map"] = {
                "open": {
                    "commandId": 63,
                    "commandParam": "OPEN",
                    "lowlevelCommand": "CH5",
                },
                "stop": {
                    "commandId": 64,
                    "commandParam": "STOP",
                    "lowlevelCommand": "CH7",
                },
                "close": {
                    "commandId": 65,
                    "commandParam": "CLOSE",
                    "lowlevelCommand": "CH8",
                },
            }
            return DaisyAwningCover(**dev)

        case {"idDevicetype": 22, "idDevicemodel": 25}:
            dev["osc_map"] = {
                "open": {
                    "commandId": 75,
                    "commandParam": "OPEN",
                    "lowlevelCommand": "CH5",
                },
                "stop": {
                    "commandId": 76,
                    "commandParam": "STOP",
                    "lowlevelCommand": "CH7",
                },
                "close": {
                    "commandId": 77,
                    "commandParam": "CLOSE",
                    "lowlevelCommand": "CH8",
                },
            }
            return DaisyShadeCover(**dev)

        case {"idDevicetype": 22, "idDevicemodel": 31}:
            dev["osc_map"] = {
                "open": {
                    "commandId": 111,
                    "commandParam": "OPEN",
                    "lowlevelCommand": "CH5",
                },
                "stop": {
                    "commandId": 112,
                    "commandParam": "STOP",
                    "lowlevelCommand": "CH7",
                },
                "close": {
                    "commandId": 113,
                    "commandParam": "CLOSE",
                    "lowlevelCommand": "CH8",
                },
            }
            # lamelle pergola bioclimatica: frame singolo = jog → serve la
            # raffica per la corsa continua
            dev["run_repeat"] = 6
            return DaisyShadeCover(**dev)

        case {"idDevicetype": 23, "idDevicemodel": 32}:
            return DaisyRGBLight(**dev)
        case {"idDevicetype": 24, "idDevicemodel": 27}:
            return DaisySlatsCover(**dev)
        case {"idDevicetype": 24, "idDevicemodel": 44}:
            return DaisyRetractableSlatsCover(**dev)

        case _:
            return DaisyDevice(**dev)


class TelecoDaisy:
    idAccount: int | None = None
    idSession: str | None = None

    def __init__(self, email, password, session: aiohttp.ClientSession | None = None):
        self.email = email
        self.password = password
        self._session = session
        self._close_session = False

        self._auth = aiohttp.BasicAuth("teleco", "tmate20")

        # canale locale LAN (:400): IP box scoperto via beacon, con cache
        self.local_enabled = True
        self._box_ip: str | None = None
        self._box_ip_ts: float = 0.0
        self._box_ip_ttl = 300.0
        self._local_lock = asyncio.Lock()

    def _cached_box_ip(self) -> str | None:
        """IP box in cache (non bloccante). None se scaduto/mai scoperto."""
        if not self.local_enabled or not self._box_ip:
            return None
        if (time.monotonic() - self._box_ip_ts) >= self._box_ip_ttl:
            return None
        return self._box_ip

    async def prime_box_ip(self, inst_code: str) -> str | None:
        """Discovery box via beacon (12s > intervallo beacon ~10s), aggiorna cache.
        Da lanciare in BACKGROUND (al setup + periodicamente): NON bloccare i comandi."""
        if not self.local_enabled:
            return None
        if self._cached_box_ip():
            return self._box_ip
        try:
            ip = await daisy_local.discover_box_ip(inst_code, timeout=12.0)
        except Exception as e:  # noqa: BLE001
            _LOGGER.debug("daisy_local: discovery fallita: %s", e)
            return None
        if ip:
            self._box_ip, self._box_ip_ts = ip, time.monotonic()
            _LOGGER.info("daisy_local: box trovata in LAN a %s (comandi diretti :400)", ip)
        return ip

    async def refresh_local_feedback(self, installation, id_installation_device,
                                     device_index, id_devicemodel) -> None:
        """GET_FEEDBACK locale per rinfrescare lo stato cloud dopo un comando locale."""
        ip = self._box_ip if self.local_enabled else None
        if not ip:
            return
        try:
            async with self._local_lock:
                await daisy_local.refresh_feedback(
                    ip, installation.instCode, id_installation_device,
                    device_index, id_devicemodel)
        except Exception as e:  # noqa: BLE001
            _LOGGER.debug("daisy_local: refresh_feedback fallito: %s", e)

    async def local_jog(self, installation, cmd: dict) -> str:
        """Un singolo jog LOCALE su :400 con ACK-wait. Ritorna:
          'ack'|'deny'|'noack' (vedi daisy_local.send_command_status), oppure
          'cloud' se non c'è la box in LAN (cache IP vuota) → il chiamante decide.
        La box fa un comando alla volta: 'deny' = occupata, va riprovato."""
        box_ip = self._cached_box_ip()
        if not box_ip:
            return "cloud"
        async with self._local_lock:
            try:
                return await daisy_local.send_command_status(
                    box_ip, installation.instCode, cmd)
            except Exception as e:  # noqa: BLE001
                _LOGGER.warning("daisy_local: jog locale fallito (%s)", e)
                self._box_ip = None  # forza ri-discovery
                return "cloud"

    async def cloud_slats_run(self, installation, cmd: dict, count: int,
                              group: int = 7, group_pause: float = 6.0,
                              frame_gap: float = 0.35) -> int:
        """Corsa 'fluida' via CLOUD (6°/frame): la box accoda i frame e li scarica
        via radio ravvicinati → il motore concatena (corsa continua vera). Manda
        `count` frame in GRUPPI da `group` con pausa, e controlla `nodeActive`
        prima di ogni gruppo. NB: run lunghi non spezzati (es. 25 close + 23 open
        di fila) mandano in crash la box o bannano l'account → i gruppi + pausa lo
        evitano. Ritorna quanti frame sono stati mandati."""
        sent = 0
        while sent < count:
            try:
                if not await self.get_installation_is_active(installation):
                    _LOGGER.warning(
                        "cloud_slats_run: box nodeActive=False, stop a %d/%d",
                        sent, count)
                    return sent
            except Exception as e:  # noqa: BLE001
                _LOGGER.warning(
                    "cloud_slats_run: nodestatus fallito (%s), proseguo cauto", e)
            n = min(group, count - sent)
            for _ in range(n):
                res = await self._tmate20_post(
                    "teleco/services/tmate20/feedthecommands/",
                    json={"commandsList": [cmd],
                          "idInstallation": installation.instCode,
                          "idScenario": 0, "isScenario": False})
                if res.get("MessageID") != "WS-000":
                    _LOGGER.warning(
                        "cloud_slats_run: risposta %s a %d/%d, stop",
                        res.get("MessageID"), sent, count)
                    return sent
                sent += 1
                await asyncio.sleep(frame_gap)
            if sent < count:
                await asyncio.sleep(group_pause)
        return sent

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
            self._close_session = True
        return self._session

    async def close(self):
        """Clean up the session if we created it."""
        if self._session and self._close_session:
            await self._session.close()

    async def _tmate20_post(self, url, json: dict | None = None) -> dict:
        payload = {"idSession": self.idSession}
        if json:
            payload |= json

        session = await self._get_session()
        async with session.post(base_url + url, json=payload, auth=self._auth) as req:
            req.raise_for_status()  # Good practice to raise on 400/500 errors
            return await req.json()

    async def _post(self, url, json: dict | None = None, unauth=False) -> dict:
        if unauth:
            _json = json
        else:
            _json = {"idSession": self.idSession, "idAccount": self.idAccount}
            if json:
                _json |= json

        session = await self._get_session()
        async with session.post(base_url + url, json=_json, auth=self._auth) as req:
            req.raise_for_status()
            req_json = await req.json()

        if req_json["codEsito"] != "S":
            raise Exception(req_json)
        return req_json["valRisultato"]

    async def login(self):
        login = await self._post(
            "teleco/services/account-login",
            {"email": self.email, "pwd": self.password},
            unauth=True,
        )
        self.idAccount = login["idAccount"]
        self.idSession = login["idSession"]

    async def get_account_installation_list(self) -> list[DaisyInstallation]:
        req = await self._post("teleco/services/account-installation-list")
        return [DaisyInstallation(**inst) for inst in req["installationList"]]

    async def get_installation_is_active(self, installation: DaisyInstallation):
        res = await self._tmate20_post(
            "teleco/services/tmate20/nodestatus/",
            {"idInstallation": installation.instCode},
        )
        return res["nodeActive"]

    async def get_room_configuration_list(self, installation: DaisyInstallation):
        req = await self._post(
            "teleco/services/room-configuration-list",
            {"idInstallation": installation.idInstallation},
        )
        return [DaisyRoomWithCommands(**dr) for dr in req["roomList"]]

    async def get_room_list(self, installation: DaisyInstallation) -> list[DaisyRoom]:
        room_list = await self._post(
            "teleco/services/room-list",
            {"idInstallation": installation.idInstallation},
        )

        rooms = []
        for room in room_list["roomList"]:
            device_list = []
            for dv in room["deviceList"]:
                dv["installation"] = installation
                dv["client"] = self
                device_list += [create_specific_device(dv)]
            rooms += [DaisyRoom(**room | {"deviceList": device_list})]

        return rooms

    async def status_device_list(
        self, installation: DaisyInstallation, device: DaisyDevice
    ) -> list[DaisyStatus]:
        status_device_list = await self._post(
            "teleco/services/status-device-list",
            {
                "idInstallation": installation.idInstallation,
                "idInstallationDevice": device.idInstallationDevice,
            },
        )

        return [DaisyStatus(**x) for x in status_device_list["statusitemList"]]

    async def _scenario_list(self, installation: DaisyInstallation):
        req = await self._post(
            "teleco/services/scenario-list",
            {
                "idInstallation": installation.idInstallation,
            },
        )
        return req

    async def _command_scenario_list(
        self, installation: DaisyInstallation, szenario_id
    ):
        req = await self._post(
            "teleco/services/command-scenario-list",
            json={
                "idInstallation": installation.idInstallation,
                "idInstallationScenario": szenario_id,
            },
        )
        return req

    async def feed_the_commands(
        self,
        installation: DaisyInstallation,
        commandsList: list[dict],
        ignore_ack=False,
    ):
        # --- canale LOCALE :400 (bypassa il cloud) con fallback cloud ------
        box_ip = self._cached_box_ip()
        if box_ip:
            try:
                async with self._local_lock:
                    for cmd in commandsList:
                        await daisy_local.send_command(
                            box_ip, installation.instCode, cmd, ignore_ack=ignore_ack)
                return {"success": None, "local": True}
            except Exception as e:  # noqa: BLE001
                _LOGGER.warning(
                    "daisy_local: invio locale fallito (%s), fallback cloud", e)
                self._box_ip = None  # forza ri-discovery al prossimo giro

        res = await self._tmate20_post(
            "teleco/services/tmate20/feedthecommands/",
            json={
                "commandsList": commandsList,
                "idInstallation": installation.instCode,
                "idScenario": 0,
                "isScenario": False,
            },
        )
        if res["MessageID"] != "WS-000":
            raise Exception(res)

        if ignore_ack:
            return {"success": None}

        return await self._get_ack(installation, res["ActionReference"])

    async def _get_ack(self, installation: DaisyInstallation, action_reference: str):
        res = await self._tmate20_post(
            "teleco/services/tmate20/getackcommand/",
            json={
                "id": action_reference,
                "idInstallation": installation.instCode,
                "idSession": self.idSession,
            },
        )
        if res["MessageID"] != "WS-300":
            raise AssertionError()
        if res["MessageText"] == "RCV":
            await asyncio.sleep(0.5)
            return await self._get_ack(installation, action_reference)
        if res["MessageText"] == "PROC":
            return {"success": True}
        return {"success": False}
