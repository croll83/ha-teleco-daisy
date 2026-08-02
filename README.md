# Teleco Daisy — Home Assistant integration

Integrazione **custom** (HACS) per la **Teleco DaisyBox**, la centralina che
controlla pergotende, tende, luci dimmerabili e motori a lamelle Teleco
Automation. Espone tutto come entità native Home Assistant (`cover`, `light`,
`climate`).

La caratteristica principale di questo fork è il **canale di comando locale in
LAN**: i comandi vengono inviati **direttamente alla DaisyBox** via TCP,
bypassando il cloud Teleco, con **fallback automatico al cloud** se la box non è
raggiungibile in rete locale. Risultato: comandi quasi istantanei e immuni ai
disservizi/latenze del cloud (i classici timeout 504 di `tmate.telecoautomation.com`).

> ℹ️ **Basato sul lavoro di [Andreas Nüßlein](https://github.com/andreasnuesslein)**
> ([`andreasnuesslein/hass_teleco_daisy`](https://github.com/andreasnuesslein/hass_teleco_daisy),
> MIT). Questo repo aggiunge il canale locale, la lettura di stato robusta e un
> config flow con validazione. Tutto il merito dell'integrazione originale è suo.

## Come funziona

- **Onboarding (cloud)**: al primo setup l'integrazione fa login all'account
  dell'app Daisy (portale `tmate.telecoautomation.com`) e scopre installazione,
  stanze e dispositivi. Le credenziali le fornisci tu dall'interfaccia HA.
- **Comandi (locale)**: la DaisyBox si annuncia in LAN con un beacon UDP in
  broadcast (porta 55555). L'integrazione la scopre da sola, ne memorizza l'IP e
  invia i comandi cifrati sul suo canale locale (TCP porta 400). Se la box non è
  in LAN o non risponde, il comando riparte automaticamente sul cloud.
- **Stato**: dopo un comando locale, la box viene sollecitata (`GET_FEEDBACK`) a
  ripubblicare il proprio stato, che viene poi letto — così l'entità resta
  aggiornata anche saltando il cloud per i comandi.

Nessuna configurazione extra per il canale locale: la scoperta della box è
**automatica e trasparente**. Se la box non è raggiungibile in LAN, l'integrazione
funziona esattamente come l'originale (solo cloud).

## Entità

- **cover** — pergotende / tende / motori a lamelle. Supporta apri / stop /
  chiudi e, sui motori con preset di livello, le posizioni native (LEV2/3/4).
- **light** — luci on/off e dimmerabili (preset di luminosità 25/50/75/100%).
- **climate** — se presenti nella tua installazione.

### Servizio extra: `teleco_daisy.slats_jog`

Per i motori a **lamelle jog-based** (che non hanno preset di livello ma solo
apri/stop/chiudi, es. modello 31) un frame singolo corrisponde a un micro-passo
(~6°) e la corsa continua è una raffica di frame ravvicinati. Il servizio
`teleco_daisy.slats_jog` permette di inviare un numero preciso di passi:

```yaml
service: teleco_daisy.slats_jog
target:
  entity_id: cover.lame_destro
data:
  direction: close   # open | close
  count: 5           # numero di passi
  spacing: 1.0       # secondi tra un passo e l'altro
```

Tipico uso: **ricette close-first** — si chiude a fine-corsa (reset posizione),
poi si aprono N passi per raggiungere un angolo assoluto senza feedback.

## Installazione

1. **HACS → menu ⋮ → Custom repositories** → aggiungi
   `https://github.com/croll83/ha-teleco-daisy` come categoria **Integration**.
2. Installa **Teleco Daisy** da HACS e **riavvia** Home Assistant.
3. **Impostazioni → Dispositivi e servizi → Aggiungi integrazione → Teleco Daisy**.
4. Inserisci **email** e **password** del tuo account app Daisy.
   L'email è trattata **case-insensitive** (viene normalizzata a minuscolo).

## Note e limitazioni

- **La DaisyBox è fragile sotto raffica**: sequenze di molti frame ravvicinati
  possono far crashare temporaneamente il suo server locale (si riprende da sola
  in ~2 minuti). Per i motori a lamelle usa spaziature generose (≥1 s) e non
  accumulare raffiche parallele su più motori contemporaneamente.
- Lo **stato** dei motori a lamelle one-way è una stima (nessun feedback di
  posizione dall'hardware).
- L'integrazione richiede comunque un **account cloud Daisy** per il setup
  iniziale (scoperta dispositivi); i **comandi** poi girano in locale quando
  possibile.

## Crediti

- **Componente originale**: [Andreas Nüßlein — `hass_teleco_daisy`](https://github.com/andreasnuesslein/hass_teleco_daisy) (MIT)
- **Canale locale LAN, lettura stato, config flow validato**: [croll83](https://github.com/croll83)

## Licenza

MIT — vedi [LICENSE](LICENSE). Copyright originale di Andreas Nüßlein preservato.
