from machine import UART
import machine
import time
import _thread
from meter_storage import *
import json
import globals

# Defensive init: older/unsynced globals.py files may not define this yet.
if not hasattr(globals, 'CANCEL_REQUEST'):
    globals.CANCEL_REQUEST = {}

# ========== VERSION HELPERS ==========
def get_software_version():
    """
    Reads the OTA-managed firmware version (same file ota_update.py writes to
    after a successful update). Read directly rather than importing
    ota_update.py, to avoid pulling in its GSM/curl dependencies here.
    """
    try:
        with open(getattr(globals, 'VERSION_FILE', '/flash/version.txt'), "r") as f:
            return f.read().strip()
    except:
        return "unknown"

# ========== UART CONFIG ==========
uart = UART(2, baudrate=9600, bits=8, parity=1, stop=1, tx=19, rx=18)

# ========== HELPER: CLEAR BUFFER ==========
def clear_uart_buffer(uart):
    """
    Reads pending data to ensure the line is silent before we speak.
    """
    try:
        while uart.any():
            uart.read() # Left open as requested
            time.sleep(0.01) # Yield to CPU
    except:
        pass
    time.sleep(0.05) 

# ========== HELPER: SMART READ ==========
def smart_read_modbus(uart, expected_bytes, timeout_attempts=15):
    for _ in range(timeout_attempts):
        if uart.any() >= expected_bytes:
            break
        time.sleep(0.1) 
    
    try:
        return uart.read(expected_bytes)
    except:
        return None

# ========== CRC Utils ==========
def calculate_crc(data):
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 0x0001:
                crc >>= 1
                crc ^= 0xA001
            else:
                crc >>= 1
    return crc

def verify_crc(frame):
    if not frame or len(frame) < 3:
        return False
    received_crc = frame[-2] | (frame[-1] << 8)
    return calculate_crc(frame[:-2]) == received_crc

# ========== MODBUS FUNCTIONS ==========
def build_modbus_request(address, function_code, register_address, register_count):
    frame = bytearray(6)
    frame[0] = address
    frame[1] = function_code
    frame[2] = (register_address >> 8) & 0xFF
    frame[3] = register_address & 0xFF
    frame[4] = (register_count >> 8) & 0xFF
    frame[5] = register_count & 0xFF
    crc = calculate_crc(frame)
    frame += bytearray([crc & 0xFF, (crc >> 8) & 0xFF])
    return frame

def write_single_register(uart, address, register_address, value):
    clear_uart_buffer(uart)
    frame = bytearray(9)
    frame[0] = address
    frame[1] = 0x10 
    frame[2] = (register_address >> 8) & 0xFF
    frame[3] = register_address & 0xFF
    frame[4] = 0x00
    frame[5] = 0x01
    frame[6] = 0x02
    frame[7] = (value >> 8) & 0xFF
    frame[8] = value & 0xFF
    crc = calculate_crc(frame)
    frame += bytearray([crc & 0xFF, (crc >> 8) & 0xFF])
    
    uart.write(frame)
    response = smart_read_modbus(uart, 8)
    return response and verify_crc(response)

# ========== DIAGNOSTIC FUNCTIONS ==========
def read_valve_status(uart, address):
    clear_uart_buffer(uart)
    request = build_modbus_request(address, 0x03, 0x0060, 0x01)
    uart.write(request)
    response = smart_read_modbus(uart, 7)
    
    if response and len(response) == 7 and verify_crc(response):
        status_bits = response[4] & 0x03 
        if status_bits == 0x01: return "Open"
        if status_bits == 0x02: return "Closed"
    return None

def get_valid_valve_status(uart, address, retries=5, delay=1):
    for attempt in range(retries):
        status = read_valve_status(uart, address)
        if status: return status
        time.sleep(delay)
    return "Unknown"

def read_general_status(uart, address):
    clear_uart_buffer(uart)
    request = build_modbus_request(address, 0x03, 0x0001, 0x01)
    uart.write(request)
    response = smart_read_modbus(uart, 7)
    
    if response and len(response) == 7 and verify_crc(response):
        st_val = (response[3] << 8) | response[4]
        return {
            "battery": "Low" if (st_val & 0x0001) else "Good",
            "pipe_empty": "EMPTY (No Water)" if (st_val & 0x0002) else "Full (Normal)",
            "sensor_error": bool(st_val & 0x0010)
        }
    return None

def get_valid_health_data(uart, address, retries=3, delay=0.5):
    for _ in range(retries):
        data = read_general_status(uart, address)
        if data: return data
        time.sleep(delay)
    return {"battery": "Unknown", "pipe_empty": "Unknown", "sensor_error": False}

# ========== FLOW & VALVE CONTROL ==========
def read_cumulative_flow(uart, address):
    """
    0x000E holds the cumulative-flow total across 2 registers, low word
    first then high word (confirmed against the meter's own docs by the
    diagnostic in meter_run.py). This used to return only the low word,
    which is a plain 16-bit counter - correct up to 65,535 L, then it wraps
    back to 0 every 65,536 L while the real total keeps climbing in the
    high word we were throwing away. Both are combined here now.
    """
    clear_uart_buffer(uart)
    request = build_modbus_request(address, 0x03, 0x000E, 0x02)
    uart.write(request)
    response = smart_read_modbus(uart, 9)

    if response and len(response) == 9 and verify_crc(response):
        if response[0] == address:
            low_word = (response[3] << 8) | response[4]
            high_word = (response[5] << 8) | response[6]
            return (high_word << 16) | low_word
    return None

def open_valve(uart, device_address):
    write_single_register(uart, device_address, 0x0060, 0x0001)
    time.sleep(0.5)

def close_valve(uart, device_address):
    write_single_register(uart, device_address, 0x0060, 0x0002)
    time.sleep(0.5)

def get_valid_volume(uart, address, retries=5, delay=1):
    for attempt in range(retries):
        volume_value = read_cumulative_flow(uart, address)
        if volume_value is not None:
            return volume_value
        time.sleep(delay)
    return None

# =========== ATM DISPENSE LOGIC (CRITICAL) ============ #

# Independent backstop for an open valve, same _thread pattern as meter_gsm.py's
# GSM-init watchdog. Why this exists: previously, any exception raised inside
# the dispense loop below (e.g. uart.write() faulting on a bus glitch - not
# wrapped anywhere) propagated straight out of dispense_batch() with the valve
# left OPEN. The caller (main.py's monitor_loop) only has a generic
# try/except that logs "Loop Crash" and carries on - it never knew a valve
# needed closing, and nothing else was watching it. The device kept running
# fine (GSM/MQTT up, WDT fed), so no reboot ever happened to trigger
# close_all_valves_on_boot() either. Net effect: a non-fatal software fault
# could leave water flowing, undetected, until the next unrelated power
# cycle - this is almost certainly the "software crashing, meter left
# discharging" loss scenario. Two independent layers now close that gap:
# the try/finally below (catches exceptions) and this watchdog thread
# (catches a hang - the loop never raising, never returning, just stuck).
#
# This thread never touches the UART itself. uart is a single shared
# peripheral the main thread is already mid-transaction on
# (clear_uart_buffer/write/smart_read_modbus inside the loop below have no
# locking, same as open_valve/close_valve) - a second thread calling
# close_valve() straight into that would race the main thread's own
# reads/writes on the same bus with no synchronization, which on this
# port's _thread implementation is a direct path to a corrupted/hung UART
# or heap, i.e. new instability, not a fix for it. Instead this reuses the
# existing remote-cancel mechanism (globals.CANCEL_REQUEST, already
# cooperatively polled every loop iteration below, already lock-guarded
# the same way meter_mqtts.py/main.py guard their own shared dicts) so the
# valve is still only ever closed from the one thread that owns the UART.
# machine.reset() is the only hardware-adjacent call this thread makes if
# that cooperative path doesn't land in time - a full reset touches no
# Python-level shared state, so it's safe to call from any thread.
_dispense_watchdog_active = {}
_DISPENSE_WATCHDOG_COOPERATIVE_GRACE_SEC = 15

def _dispense_watchdog_thread(uart, address, deadline_sec):
    waited = 0
    step = 1
    while waited < deadline_sec:
        if not _dispense_watchdog_active.get(address):
            return  # dispense_batch() already exited (any path) - stand down
        time.sleep(step)
        waited += step
    if not _dispense_watchdog_active.get(address):
        return

    print("[WATCHDOG] Dispense on addr {} exceeded {}s - requesting cooperative close".format(address, deadline_sec))
    _thread.lock()
    globals.CANCEL_REQUEST[address] = True
    _thread.unlock()

    # Give the main thread a chance to see CANCEL_REQUEST on its next
    # iteration and close the valve itself (same thread as every other
    # UART access). Only reset if it's truly stuck and never gets there.
    waited = 0
    while waited < _DISPENSE_WATCHDOG_COOPERATIVE_GRACE_SEC:
        if not _dispense_watchdog_active.get(address):
            return  # dispense_batch() exited cooperatively - done
        time.sleep(step)
        waited += step

    if _dispense_watchdog_active.get(address):
        print("[WATCHDOG] Dispense on addr {} did not respond to cancel - forcing reset".format(address))
        _dispense_watchdog_active[address] = False
        machine.reset()

def dispense_batch(uart, address, liters_to_dispense):
    start_vol = get_valid_volume(uart, address)
    if start_vol is None:
        return {"status": "failed", "reason": "initial_read_error", "dispensed": 0}

    target_vol = start_vol + liters_to_dispense

    save_target_reading(address, target_vol)
    open_valve(uart, address)
    time.sleep(1)

    # Deadline = time to deliver liters_to_dispense at a conservative MINIMUM
    # flow rate (globals.MIN_FLOW_RATE_LPM, liters/min - tune this to the real
    # meter's worst-case slow flow), plus a flat grace period. Defaults are
    # intentionally generous (never fire during a legitimate slow dispense);
    # tighten both once real flow-rate data is available.
    MIN_FLOW_RATE_LPM = getattr(globals, 'MIN_FLOW_RATE_LPM', 1.0)
    WATCHDOG_GRACE_SEC = getattr(globals, 'DISPENSE_WATCHDOG_GRACE_SEC', 300)
    deadline_sec = (liters_to_dispense / MIN_FLOW_RATE_LPM) * 60 + WATCHDOG_GRACE_SEC

    _dispense_watchdog_active[address] = True
    try:
        _thread.start_new_thread(_dispense_watchdog_thread, (uart, address, deadline_sec))
    except TypeError:
        _thread.start_new_thread("DispenseWatchdog", _dispense_watchdog_thread, (uart, address, deadline_sec))

    last_vol = start_vol
    consecutive_errors = 0
    max_errors = 5

    try:
        while True:
            # --- NEW: Feed the WDT while dispensing! ---
            machine.resetWDT()
            # -------------------------------------------

            # --- CANCEL CHECK: remote abort takes priority over the target ---
            # Same lock convention as the writers (meter_mqtts.py's datacb,
            # the watchdog thread above) - this dict is now written from
            # two other threads, not just read here.
            _thread.lock()
            cancel_requested = globals.CANCEL_REQUEST.pop(address, None)
            _thread.unlock()
            if cancel_requested:
                close_valve(uart, address)
                cancelled_vol = read_cumulative_flow(uart, address)
                if cancelled_vol is None:
                    cancelled_vol = last_vol
                save_target_reading(address, cancelled_vol)
                print("[ATM] Batch Cancelled")
                return {"status": "cancelled", "dispensed": (cancelled_vol - start_vol), "final_reading": cancelled_vol, "reason": "remote_cancel"}

            current_vol = read_cumulative_flow(uart, address)

            if current_vol is None:
                consecutive_errors += 1
                print("[ATM] Read Error {}/{}".format(consecutive_errors, max_errors))
                if consecutive_errors >= max_errors:
                    close_valve(uart, address)
                    return {"status": "failed", "reason": "meter_timeout", "dispensed": (last_vol - start_vol)}
                time.sleep(1)
                continue

            consecutive_errors = 0

            # CRITICAL FIX: Keep track of the last known good volume for error reporting
            last_vol = current_vol

            if current_vol >= target_vol:
                close_valve(uart, address)
                save_target_reading(address, current_vol)
                print("[ATM] Batch Complete")
                return {"status": "completed", "dispensed": (current_vol - start_vol), "final_reading": current_vol}

            time.sleep(1)
    except Exception as e:
        # The gap this closes: previously an exception here left the valve
        # OPEN and propagated to a caller that doesn't know to close it.
        print("[ATM] Dispense crashed: {} - forcing valve closed".format(e))
        try:
            close_valve(uart, address)
        except Exception:
            pass
        return {"status": "failed", "reason": "exception:{}".format(e), "dispensed": (last_vol - start_vol)}
    finally:
        _dispense_watchdog_active[address] = False

def cancel_pending_target(uart, address):
    """
    Clears any pending/stale target by resetting it to the current cumulative
    reading. Safe to call whether or not a batch is currently active - if one
    is active, its loop will independently detect CANCEL_REQUEST and stop itself.
    """
    globals.CANCEL_REQUEST.pop(address, None)
    current_vol = get_valid_volume(uart, address)
    if current_vol is None:
        return None
    save_target_reading(address, current_vol)
    return current_vol

# =========== UPDATED REPORTING (ATM + HEALTH) ============ #
def read_meter_only(uart, addresses, publish_func, mqtt_client, mqtt_topic):
    for address in addresses:
        cumulative = get_valid_volume(uart, address)
        if cumulative is None: continue
        
        target = load_target_reading(address)
        status_msg = "idle"

        if target and target > cumulative:
             status_msg = "interrupted_batch_detected"

        valve_state = get_valid_valve_status(uart, address, retries=2)
        health = get_valid_health_data(uart, address, retries=2)

        # --- MODE: what the ATM is currently doing ---
        if valve_state == "Open":
            mode = "dispensing"
        elif status_msg == "interrupted_batch_detected":
            mode = "interrupted"
        else:
            mode = "idle"

        payload_dict = {
            "type": "device_report",
            "device": address,
            "cumulative_flow_L": cumulative,
            "target_reading": target,
            "mode": mode,
            "status": status_msg,
            "valve_status": valve_state,
            "battery": health["battery"],
            "pipe": health["pipe_empty"],
            "software_version": get_software_version(),
            "globals_version": getattr(globals, 'GLOBAL_VERSION', 'unknown')
        }
        
        try:
            publish_func(mqtt_client, mqtt_topic, json.dumps(payload_dict))
        except:
            pass