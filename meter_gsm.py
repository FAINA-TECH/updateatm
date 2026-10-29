import socket
import machine
import time
import sys
import gsm
import globals
import _thread

SIM800L_IP5306_VERSION_20190610 = 0
SIM800L_AXP192_VERSION_20200327 = 1
SIM800C_AXP192_VERSION_20200609 = 2
SIM800L_IP5306_VERSION_20200811 = 3

# Please change to the version you use here, the default version is IP5306
board_type = SIM800L_IP5306_VERSION_20200811

# APN credentials (replace with yours)
GSM_APN = globals.GSM_APN
GSM_USER = globals.GSM_USER
GSM_PASS = globals.GSM_PASS

UART_BAUD = 115200

# defaule use SIM800L_IP5306_VERSION_20190610
MODEM_POWER_PIN = 23
MODEM_PWRKEY_PIN = 4
MODEM_TX = 27
MODEM_RX = 26

# Independent outer deadline for the whole of gsmInitialization(), on top of
# the WDT(False)/True handling above. Field/bench evidence (residential
# fork, 2026-10-03): the native gsm.start()/connect() call can block for an
# unbounded time inside its own C-level AT handshake - it restarts the
# ENTIRE sequence from scratch on any AT+CREG? response other than
# "registered" (0,1), including 0,2 ("searching"), a normal, temporary
# state during real-world registration - and separately, an established
# connection was observed dropping and reconnecting repeatedly on its own.
# Because all of this lives inside a single blocking native call, neither
# the WDT-rearm above nor the bounded status-wait loop below can help if
# that call simply never returns - they only run once it does. A
# machine.Timer was tried first but failed twice on bench hardware with
# undocumented errors specific to this LoBo port's Timer API; _thread is
# the proven mechanism here (same pattern as residential's own
# supervisor_thread()), so that's what backstops this instead.
GSM_INIT_DEADLINE_MS = getattr(globals, 'GSM_INIT_DEADLINE_MS', 180000)  # 3 min
_gsm_init_done = True

def _gsm_watchdog_thread(deadline_ms):
    waited_ms = 0
    step_ms = 250
    while waited_ms < deadline_ms:
        if _gsm_init_done:
            return  # gsmInitialization() returned normally - stand down
        time.sleep_ms(step_ms)
        waited_ms += step_ms
    if not _gsm_init_done:
        print("[WATCHDOG] GSM init exceeded {}ms deadline - forcing reset".format(deadline_ms))
        machine.reset()

def gsmInitialization():
    # Power on the GSM module
    global MODEM_RST

    # --- NEW: Disable WDT during slow network init ---
    # --- FIX: this is now guaranteed to be re-enabled in the `finally`
    # below, however this function returns or raises. Previously nothing
    # re-armed it if gsmInitialization() hung or errored before reaching
    # the caller's own machine.WDT(True) call - a bad PPP negotiation left
    # the device frozen with no watchdog to recover it, requiring a manual
    # power cycle.
    machine.WDT(False)
    global _gsm_init_done
    _gsm_init_done = False
    try:
        _thread.start_new_thread(_gsm_watchdog_thread, (GSM_INIT_DEADLINE_MS,))
    except TypeError:
        _thread.start_new_thread("GsmWatchdog", _gsm_watchdog_thread, (GSM_INIT_DEADLINE_MS,))
    try:
        GSM_POWER = machine.Pin(MODEM_POWER_PIN, machine.Pin.OUT)
        GSM_POWER.value(1)

        if True:
            MODEM_RST = machine.Pin(5, machine.Pin.OUT)
            MODEM_RST.value(1)

        GSM_PWR = machine.Pin(MODEM_PWRKEY_PIN, machine.Pin.OUT)
        GSM_PWR.value(1)
        time.sleep_ms(200)
        GSM_PWR.value(0)
        time.sleep_ms(1000)
        GSM_PWR.value(1)

        # Init PPPoS
        gsm.debug(True)  # Uncomment this to see more logs, investigate issues, etc.

        gsm.start(tx=MODEM_TX, rx=MODEM_RX, apn=GSM_APN,
                  user=GSM_USER, password=GSM_PASS, roaming=True)

        for retry in range(20):
            if gsm.atcmd('AT'):
                break
            else:
                sys.stdout.write('.')
                time.sleep_ms(5000)
        else:
            print("Modem not responding!")
            machine.reset()
        print()

        print("Connecting to GSM...")
        gsm.connect()

        # --- FIX: this used to be `while gsm.status()[0] != 1: pass` - an
        # unbounded busy-loop with the watchdog disabled (see above). If
        # gsm.connect() reports success but status() never reaches 1 (seen
        # in the field on a bad PPP negotiation), the device hung forever
        # with nothing able to recover it. Bounded like the AT-command
        # retry above: give up and reset rather than hang indefinitely.
        for retry in range(60):  # ~5 minutes at 5s/retry
            if gsm.status()[0] == 1:
                break
            time.sleep_ms(5000)
        else:
            print("GSM status never reached connected - resetting")
            machine.reset()

        print('IP:', gsm.ifconfig()[0])
        print("Connected !")
    finally:
        # Guarantee WDT turns back on even if initialization hangs/errors
        machine.WDT(True)
        _gsm_init_done = True

def gsmCheckStatus():
    gsmconnectivity = gsm.status()[0]
    return gsmconnectivity