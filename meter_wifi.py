import network
import machine
import utime as time
import globals

# Self-defaulted (not a bare globals.WIFI_SSID read) so a device whose
# device_configs/*.py predates this file still boots fine with WiFi simply
# unusable (empty SSID) instead of an AttributeError - see
# check_globals_compat.py for why this matters.
WIFI_SSID = getattr(globals, 'WIFI_SSID', '')
WIFI_PASSWORD = getattr(globals, 'WIFI_PASSWORD', '')
WIFI_CONNECT_TIMEOUT_S = getattr(globals, 'WIFI_CONNECT_TIMEOUT_S', 20)

_sta_if = None

def _sta():
    global _sta_if
    if _sta_if is None:
        _sta_if = network.WLAN(network.STA_IF)
    return _sta_if

def wifiCheckStatus():
    """1/0 int, same contract as meter_gsm.gsmCheckStatus()."""
    try:
        return 1 if _sta().isconnected() else 0
    except Exception:
        return 0

def wifiConnect(ssid=None, password=None, timeout_s=None):
    """
    Blocking, bounded attempt to associate to an AP. Returns True/False -
    never raises, never resets the device itself (unlike gsmInitialization()):
    a missing/unreachable AP is an expected, recoverable case here, since the
    whole point is to fall back to GSM rather than treat it as fatal.
    """
    ssid = ssid or WIFI_SSID
    password = password or WIFI_PASSWORD
    timeout_s = timeout_s if timeout_s is not None else WIFI_CONNECT_TIMEOUT_S

    if not ssid:
        return False

    # Mirrors meter_gsm.gsmInitialization()'s WDT(False)/finally WDT(True) -
    # association can stall for a few seconds; don't let that alone trip
    # the hardware watchdog while it's enabled (e.g. during monitor_loop).
    machine.WDT(False)
    try:
        sta = _sta()
        sta.active(True)
        sta.connect(ssid, password)

        waited_ms = 0
        step_ms = 500
        timeout_ms = int(timeout_s * 1000)
        while not sta.isconnected() and waited_ms < timeout_ms:
            time.sleep_ms(step_ms)
            waited_ms += step_ms

        return sta.isconnected()
    except Exception as e:
        print("[WiFi] Connect error:", e)
        return False
    finally:
        machine.WDT(True)

def wifiDisconnect():
    """Best-effort teardown before handing off to GSM - never raises."""
    try:
        sta = _sta()
        sta.disconnect()
        sta.active(False)
    except Exception:
        pass
