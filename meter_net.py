"""
Connectivity orchestrator - the only module main.py/ota_update.py should
call to get online. Picks WiFi vs GSM, and swaps to whichever one is
working when the current one drops.

Gated entirely by globals.WIFI_MODE (default off/missing = False): with it
off, netInitialization()/netCheckStatus()/netReconnect() are a thin,
behavior-identical pass-through to the existing GSM-only functions, so
every device that hasn't opted in is completely unaffected by this file
existing.

NOTE: boot.py's crash-recovery path (_attempt_recovery()) deliberately does
NOT use this module - it imports meter_gsm directly instead. That path only
runs when main.py itself failed to import, so it must not depend on
anything that a bad OTA could plausibly have broken (this file included).
GSM-only there is intentional, not an oversight.
"""
import socket
import globals
import meter_wifi
from meter_gsm import gsmInitialization, gsmCheckStatus

WIFI_MODE = getattr(globals, 'WIFI_MODE', False)
MQTT_BROKER_HOST = getattr(globals, 'MQTT_BROKER_HOST', '')
MQTT_BROKER_PORT = getattr(globals, 'MQTT_BROKER_PORT', 0)
NET_CHECK_TIMEOUT_S = getattr(globals, 'NET_CHECK_TIMEOUT_S', 5)

_current = None  # 'wifi' or 'gsm' - which transport is active right now

def _has_internet(timeout_s=NET_CHECK_TIMEOUT_S):
    """
    A real reachability check, not just link state - wlan.isconnected()
    is True the moment the AP hands out an IP, even if that AP's own
    upstream internet (or just our broker) is down. Tests against the
    broker specifically rather than some arbitrary external host, since
    that's the only reachability this device actually needs.
    """
    if not MQTT_BROKER_HOST or not MQTT_BROKER_PORT:
        return True  # nothing configured to check against - don't block on it
    try:
        addr = socket.getaddrinfo(MQTT_BROKER_HOST, MQTT_BROKER_PORT)[0][-1]
        s = socket.socket()
        s.settimeout(timeout_s)
        s.connect(addr)
        s.close()
        return True
    except Exception as e:
        print("[Net] Broker unreachable:", e)
        return False

def _try_wifi():
    if not WIFI_MODE:
        return False
    print("[Net] Trying WiFi...")
    if meter_wifi.wifiConnect() and _has_internet():
        print("[Net] WiFi OK")
        return True
    print("[Net] WiFi unavailable or no internet - falling back to GSM")
    meter_wifi.wifiDisconnect()
    return False

def _connect():
    """
    WiFi first if allowed and it actually works, otherwise GSM.
    gsmInitialization() keeps its own existing contract: it blocks until
    connected, or resets the device itself if GSM can't be established -
    so this still ends in a reboot if NEITHER transport works, same
    guarantee the device had before WiFi mode existed.
    """
    global _current
    if _try_wifi():
        _current = 'wifi'
        return
    gsmInitialization()
    _current = 'gsm'

def netInitialization():
    """Call once at boot, in place of gsmInitialization()."""
    _connect()

def netCheckStatus():
    """Mirrors gsmCheckStatus()'s 1/0 contract, for whichever transport is active."""
    if _current == 'wifi':
        return meter_wifi.wifiCheckStatus()
    return gsmCheckStatus()

def netReconnect():
    """Call when netCheckStatus() != 1 - tries WiFi (if allowed), then GSM."""
    _connect()

def netCurrentTransport():
    return _current
