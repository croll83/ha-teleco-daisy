"""
Canale LOCALE LAN per Teleco DaisyBox (reverse dall'app com.telecoautomation.daisy).
Bypassa il cloud: comandi via TCP :400 cifrati. Discovery box via beacon UDP 55555.

Vedi memoria wagmi-teleco-daisy. La box è fragile sotto raffica → un comando per
connessione, spaziatura gestita a monte (coordinator/burst_lock).
"""
from __future__ import annotations

import asyncio
import base64
import logging
import re
import socket
import time

_LOGGER = logging.getLogger(__name__)

KEY = "d3Cr1pTam1St0CaZzOSe61nGrad0"          # chiave XOR (28 char) da encrypt()
IDX = "abcdefghijklmnopqrstuvwxyz0123456789"
CMD_PORT = 400
BEACON_PORT = 55555
BOARD_CODE = {16: "LMP", 17: "DIM", 18: "HEA", 19: "FAN", 20: "HEQ", 21: "PER",
              22: "CAN", 23: "SER", 24: "SOL", 25: "TEN", 26: "RGB", 27: "PEG",
              28: "AUD", 31: "PEQ", 32: "COP", 33: "DYP", 34: "DMS",
              43: "GAK", 44: "RAS", 45: "ARS", 46: "CRP"}


def _encrypt(plaintext: str, n: int = 7) -> bytes:
    rot = KEY[n:] + KEY[:n]
    v = str(int(time.time()))
    s = chr(int(v[0:2]))
    for i, c in enumerate(plaintext):
        if i == 50:   s += chr(int(v[2:4]))
        elif i == 70: s += chr(int(v[4:6]))
        elif i == 90: s += chr(int(v[6:8]))
        s += c
    s += chr(int(v[8:10]))
    xored = bytes((ord(ch) ^ ord(rot[i % len(KEY)])) & 0xFF for i, ch in enumerate(s))
    return ("M" + IDX[n] + base64.b64encode(xored).decode("ascii")).encode("ascii")


def _command_action(command_param: str) -> str:
    p = (command_param or "").upper()
    if p in ("ON", "OFF"):
        return "POWER"
    if p in ("OPEN", "STOP", "CLOSE"):
        return "OPEN_STOP_CLOSE"
    return "LEVEL"


def _convert_for_dm(inst_code: str, cmd: dict) -> str:
    """cmd = dict cloud del componente: deviceCode, idInstallationDevice, commandId,
    commandParam, lowlevelCommand (+ opz. commandAction)."""
    coac = cmd.get("commandAction") or _command_action(cmd.get("commandParam", ""))
    return ('{"idIn":"%s","idSc":0,"isSc":false,"cL":[{"idInDe":%d,"deCo":"%s","cmId":%d,'
            '"coAc":"%s","coPa":"%s","low":"%s","coSta":0}]}') % (
        inst_code, int(cmd["idInstallationDevice"]), str(cmd["deviceCode"]),
        int(cmd["commandId"]), coac, cmd.get("commandParam", ""),
        cmd.get("lowlevelCommand", "") or "")


async def discover_box_ip(inst_code: str, timeout: float = 6.0) -> str | None:
    """Ascolta il beacon UDP :55555 e ritorna l'IP della box con MAC = inst_code."""
    want_mac = inst_code.upper()
    loop = asyncio.get_running_loop()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind(("", BEACON_PORT))
    except OSError as e:
        _LOGGER.debug("daisy_local: bind beacon fallito: %s", e)
        sock.close()
        return None
    sock.setblocking(False)
    try:
        end = loop.time() + timeout
        while loop.time() < end:
            try:
                data = await asyncio.wait_for(loop.sock_recv(sock, 512), timeout=end - loop.time())
            except (asyncio.TimeoutError, ConnectionError):
                break
            # NB: il beacon NON è JSON valido ("time":0000 con zeri iniziali) →
            # estraggo mac/ip con regex invece di json.loads.
            text = data.decode("utf-8", "ignore")
            mac_m = re.search(r'"mac"\s*:\s*"([^"]+)"', text)
            ip_m = re.search(r'"ip"\s*:\s*"([^"]+)"', text)
            if mac_m and ip_m and mac_m.group(1).replace(":", "").upper() == want_mac:
                return ip_m.group(1)
    finally:
        sock.close()
    return None


async def send_command(box_ip: str, inst_code: str, cmd: dict, timeout: float = 6.0,
                       ignore_ack: bool = False) -> bool:
    """Invia un comando cifrato su :400. True se la box lo accetta (ACK o esecuzione).
    Solleva su errore di connessione (→ il chiamante fa fallback cloud).

    Due regimi, verificati sul campo (2026-08-02/03):
    - ignore_ack=True (RAFFICA, corsa continua): scrivi il frame e chiudi SUBITO,
      NIENTE read. Su box sana la chiusura è ~10ms → i frame a 0,3s si concatenano
      e il motore fa la corsa fluida (uomo-presente, come il telecomando tenuto).
      Aspettare l'ack qui dilata il treno oltre il deadman del motore (~1,5s) → jog
      separati. NB: se la box è DEGRADATA chiude lenta (~1s) → la raffica torna a
      spezzarsi in jog: sintomo che serve power-cycle+riposo, non una modifica codice.
    - ignore_ack=False (AZIONE SINGOLA: jog fine, luci, GET_FEEDBACK): attendi l'ACK
      pieno (la box lo manda a elaborazione conclusa, ~1,9s). Chiudere prima qui
      scarta il frame e, ripetuto, impianta il :400."""
    payload = _encrypt(_convert_for_dm(inst_code, cmd))
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(box_ip, CMD_PORT), timeout=timeout)
    try:
        writer.write(payload)
        await writer.drain()
        if ignore_ack:
            return True           # raffica: non leggere, chiudi subito (vedi sopra)
        try:
            resp = await asyncio.wait_for(reader.read(64), timeout=timeout)
        except asyncio.TimeoutError:
            resp = b""            # no-ACK entro il timeout = chiudiamo comunque
        return b"ACK" in resp or resp == b""
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass


async def refresh_feedback(box_ip: str, inst_code: str, id_installation_device: int,
                           device_index, id_devicemodel: int) -> None:
    """GET_FEEDBACK locale: forza la box a ripubblicare lo stato al cloud (letture fresche)."""
    code = BOARD_CODE.get(id_devicemodel)
    if not code:
        return
    await send_command(box_ip, inst_code, {
        "idInstallationDevice": id_installation_device, "deviceCode": str(device_index),
        "commandId": 122, "commandAction": "GET_FEEDBACK", "commandParam": code,
        "lowlevelCommand": ""})
