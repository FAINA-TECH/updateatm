from machine import UART
import time
from meter_storage import *
import json

# ========== UART CONFIG ==========
uart = UART(2, baudrate=9600, bits=8, parity=1, stop=1, tx=19, rx=18)

# ========== HELPER: CLEAR BUFFER ==========
def clear_uart_buffer(uart):
    """
    Reads all pending data to ensure the line is silent before we speak.
    """
    try:
        while uart.any():
            uart.read()
            time.sleep(0.01) # Yield to CPU
    except:
        pass
    time.sleep(0.05) 

# ========== HELPER: SMART READ ==========
def smart_read_modbus(uart, expected_bytes, timeout_attempts=15):
    """
    Waits for 'expected_bytes' to arrive in the buffer.
    """
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
    # Wait for response (8 bytes for Write command)
    response = smart_read_modbus(uart, 8)
    return response and verify_crc(response)

# ========== NEW: DIAGNOSTIC FUNCTIONS (Ported from Residential) ==========

def read_valve_status(uart, address):
    """
    Performs a single Modbus read of the equipment control register (0x0060).
    """
    clear_uart_buffer(uart)
    request = build_modbus_request(address, 0x03, 0x0060, 0x01)
    uart.write(request)
    response = smart_read_modbus(uart, 7)
    
    if response and len(response) == 7 and verify_crc(response):
        status_bits = response[4] & 0x03 # D1:D0
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
    """
    Reads the General Status Register (ST) at 0x0001 for hardware health.
    """
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
    clear_uart_buffer(uart)
    request = build_modbus_request(address, 0x03, 0x000E, 0x02)
    uart.write(request)
    response = smart_read_modbus(uart, 9)
    
    if response and len(response) == 9 and verify_crc(response):
        if response[0] == address:
            return (response[3] << 8) | response[4]
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

def dispense_batch(uart, address, liters_to_dispense):
    """
    ATM MODE: Opens valve, monitors flow closely, closes at target.
    PERSISTENCE: Saves target to file so we can recover on power loss.
    Returns: {"status": "completed"|"failed", "dispensed": float, "final_reading": float}
    """
    print("[ATM] Starting Batch: {} Liters for Addr {}".format(liters_to_dispense, address))
    
    # 1. Get Initial Reading
    start_vol = get_valid_volume(uart, address)
    if start_vol is None:
        return {"status": "failed", "reason": "initial_read_error", "dispensed": 0}
    
    # 2. Calculate Target
    target_vol = start_vol + liters_to_dispense
    print("[ATM] Start: {} L | Target: {} L".format(start_vol, target_vol))
    
    # 3. SAVE TARGET (Persistence)
    save_target_reading(address, target_vol)
    
    # 4. Open Valve
    open_valve(uart, address)
    time.sleep(1) # Give valve time to move
    
    # 5. High-Frequency Monitoring Loop
    last_vol = start_vol
    consecutive_errors = 0
    max_errors = 5
    
    while True:
        # Read Meter
        current_vol = read_cumulative_flow(uart, address)
        
        # --- Error Handling ---
        if current_vol is None:
            consecutive_errors += 1
            print("[ATM] Read Error {}/{}".format(consecutive_errors, max_errors))
            if consecutive_errors >= max_errors:
                close_valve(uart, address)
                return {"status": "failed", "reason": "meter_timeout", "dispensed": (last_vol - start_vol)}
            time.sleep(1)
            continue
        
        # Reset error count if successful read
        consecutive_errors = 0 
        last_vol = current_vol
        
        print("[ATM] Progress: {} / {} L".format(current_vol, target_vol))
        
        # --- Check Target ---
        if current_vol >= target_vol:
            # Target Reached
            close_valve(uart, address)
            
            # CLEAR DEBT (Set target to current so we don't resume on reboot)
            save_target_reading(address, current_vol)
            
            print("[ATM] Batch Complete")
            return {"status": "completed", "dispensed": (current_vol - start_vol), "final_reading": current_vol}
        
        # --- Wait (Close Knit Monitoring) ---
        time.sleep(1) 

# =========== UPDATED REPORTING (ATM + HEALTH) ============ #

def read_meter_only(uart, addresses, publish_func, mqtt_client, mqtt_topic):
    """
    PASSIVE MODE: Reads meter + HEALTH STATS and uploads.
    Does NOT auto-close valves (ATM logic handles valves in dispense_batch).
    """
    for address in addresses:
        # 1. Read Flow
        cumulative = get_valid_volume(uart, address)
        if cumulative is None: continue
        
        # 2. Check Interruption Status
        target = load_target_reading(address)
        status_msg = "idle"
        
        if target and target > cumulative:
             status_msg = "interrupted_batch_detected"
        
        # 3. NEW: Read Health Data (Battery, Valve Status, etc)
        valve_state = get_valid_valve_status(uart, address, retries=2)
        health = get_valid_health_data(uart, address, retries=2)

        # 4. Build Comprehensive Payload
        payload_dict = {
            "type": "device_report", 
            "device": address, 
            "cumulative_flow_L": cumulative, 
            "status": status_msg,
            "valve_status": valve_state,
            "battery": health["battery"],
            "pipe": health["pipe_empty"]
        }
        
        # 5. Publish
        try:
            publish_func(mqtt_client, mqtt_topic, json.dumps(payload_dict))
        except:
            pass