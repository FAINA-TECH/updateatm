import ujson
import utime as time
import urequests 
from machine import UART, Pin
import machine
import os
import _thread
import gc
import globals
from meter_gsm import gsmInitialization, gsmCheckStatus
import meter_mqtts 
from meter import (
    dispense_batch, read_meter_only, get_valid_volume,
    open_valve, close_valve, get_valid_valve_status,
    uart, load_target_reading, save_target_reading
)
from ota_update import *

# ============ CONFIGURATION ============ #
SLAVE_ADDRESSES = getattr(globals, 'SLAVE_ADDRESSES', [])
MQTT_PUB_TOPIC = getattr(globals, 'MQTT_PUB_TOPIC', '')
MQTT_SUB_TOPICS = getattr(globals, 'MQTT_SUB_TOPICS', [])
LOG_FILE = "system_error.log"

# Status LED (Pin 13)
led = Pin(13, Pin.OUT)

# --- HEARTBEAT GLOBALS ---
last_alive_tick = time.time()  # Watchdog tick

# ============ MEMORY & LOGGING HELPERS ============ #

def sys_log(msg, level="INFO"):
    """
    Logs messages. 
    Optimization: Minimal string formatting to save RAM.
    """
    try:
        t = time.localtime()
        timestamp = "{:02d}:{:02d}:{:02d}".format(t[3], t[4], t[5])
        print("[{}] [{}] {}".format(timestamp, level, msg))
        
        if level == "ERROR" or level == "BOOT":
            with open(LOG_FILE, 'a') as f:
                f.write("[{}] {}\n".format(timestamp, msg))
    except:
        pass

def safe_gc():
    """Explicitly cleans memory."""
    try:
        gc.collect()
    except:
        pass
    
def check_for_update_on_start():
    try:
        sys_log("Checking for OTA Updates...", "INFO")
        update_global_file(globals.MQTT_CLIENT_ID, retries=3)
        run_ota()
    except Exception as e:
        sys_log("OTA Error: {}".format(e), "ERROR")

# ============ SUPERVISOR (WATCHDOG) ============ #

def supervisor_thread():
    """
    The 'Heart Monitor'. 
    Reboots if Main Loop hangs for > 10 mins.
    """
    global last_alive_tick
    WDT_TIMEOUT = 600 # 10 minutes
    
    sys_log("❤️ Supervisor Started", "INFO")
    
    while True:
        # Check if the main loop has frozen
        if (time.time() - last_alive_tick) > WDT_TIMEOUT:
            sys_log("❌ FATAL: Main Loop Frozen! Rebooting...", "ERROR")
            time.sleep(1)
            machine.reset()
        time.sleep(10)

# ============ FEATURE: CIU HEALTH CHECK ============ #

def perform_ciu_health_check(uart):
    """
    Optimized CIU Health Check.
    - Zero Leakage: socket.close() guaranteed in finally block.
    """
    safe_gc() # Clean before alloc
    sys_log("Starting CIU Check...", "INFO")

    target_url = getattr(globals, 'CIU_CALLBACK_URL', None)
    addresses = getattr(globals, 'SLAVE_ADDRESSES', [])
    dev_id = getattr(globals, 'MQTT_CLIENT_ID', 'UNKNOWN')
    
    if not target_url or not addresses:
        sys_log("CIU Error: Missing Config", "ERROR")
        return

    slave_results = []
    
    # 1. Iterate Slaves
    for addr in addresses:
        v_stat = get_valid_valve_status(uart, addr, retries=1)
        is_online = (v_stat is not None and v_stat != "Unknown")
        
        slave_results.append({
            "slave_address": addr,
            "connection_status": "online" if is_online else "check connection",
            "valve_status": v_stat if is_online else "unknown"
        })

    # 2. Build Payload
    payload = {
        "main_device_id": dev_id,
        "report_type": "ciu_health_check",
        "slaves": slave_results
    }

    # 3. Send HTTP POST
    response = None
    try:
        sys_log("Posting to {}...".format(target_url), "DEBUG")
        response = urequests.post(target_url, json=payload)
        
        if 200 <= response.status_code < 300:
            sys_log("CIU Sent. Status: {}".format(response.status_code), "INFO")
        else:
            sys_log("CIU Failed. Status: {}".format(response.status_code), "ERROR")
            
    except Exception as e:
        sys_log("CIU Error: {}".format(e), "ERROR")
        
    finally:
        # CRITICAL: Close socket
        if response:
            try:
                response.close()
            except:
                pass
        
        # Release memory
        slave_results = None
        payload = None
        response = None
        safe_gc()

# ============ MAIN MONITOR LOOP ============ #

def monitor_loop():
    """
    Main Logic. Runs in Main Thread.
    Uses Non-Blocking Timers for responsiveness.
    """
    global last_alive_tick
    sys_log("Monitor Loop Started", "INFO")
    
    # Config
    UPLOAD_INTERVAL = getattr(globals, 'timer', 180) # Default 3 mins
    
    # Timers
    last_upload_time = time.time()
    last_ota_check = time.time()
    
    # --- RECOVERY CHECK ---
    check_for_interrupted_jobs()

    while True:
        try:
            current_time = time.time()
            last_alive_tick = current_time # Feed Supervisor

            # ===============================================
            # 1. INSTANT COMMAND PROCESSING (Priority 1)
            # ===============================================
            if globals.CMD_QUEUE:
                sys_log("Processing Queue...", "DEBUG")
                
                # Process all pending commands
                while globals.CMD_QUEUE:
                    last_alive_tick = time.time() # Keep alive during heavy processing
                    cmd_item = globals.CMD_QUEUE.pop(0)
                    
                    cmd = cmd_item.get('cmd') or cmd_item.get('type')
                    addr = cmd_item.get('addr')
                    dev_id = cmd_item.get('dev_id') or getattr(globals, 'MQTT_CLIENT_ID', 'UNKNOWN')
                    
                    sys_log("CMD: {}".format(cmd), "INFO")
                    
                    mqtt_ready = (hasattr(meter_mqtts, 'mqtt') and meter_mqtts.mqtt is not None)

                    # --- A. DISPENSE (ATM LOGIC - BLOCKING) ---
                    if cmd == "success" and addr:
                        litres = cmd_item.get('litres', 0)
                        if litres > 0:
                            # Notify Start
                            if mqtt_ready:
                                meter_mqtts.mqttPublish(meter_mqtts.mqtt, MQTT_PUB_TOPIC, ujson.dumps({
                                    "type": "device_report", "device": dev_id, "status": "dispense_started", "amount": litres
                                }))
                            
                            # RUN DISPENSE (This blocks until finished)
                            result = dispense_batch(uart, addr, litres)
                            
                            # Notify End
                            status_msg = "dispense_complete" if result['status'] == 'completed' else "dispense_failed"
                            payload = {
                                "type": "device_report", 
                                "device": dev_id, 
                                "status": status_msg,
                                "dispensed": result.get('dispensed', 0),
                                "final_reading": result.get('final_reading'),
                                "reason": result.get('reason')
                            }
                            if mqtt_ready:
                                meter_mqtts.mqttPublish(meter_mqtts.mqtt, MQTT_PUB_TOPIC, ujson.dumps(payload))
                            
                            # Clean up after heavy operation
                            safe_gc()

                    # --- B. MANUAL / DIAGNOSTIC COMMANDS ---
                    elif cmd == "check_status" and mqtt_ready:
                        read_meter_only(uart, SLAVE_ADDRESSES, meter_mqtts.mqttPublish, meter_mqtts.mqtt, MQTT_PUB_TOPIC)
                    
                    elif cmd == "check_update":
                        check_for_update_on_start()
                    
                    elif cmd == "ciu_health_check":
                         perform_ciu_health_check(uart)
                    
                    elif cmd == "valve_open" and addr:
                        open_valve(uart, addr)
                        if mqtt_ready:
                            meter_mqtts.mqttPublish(meter_mqtts.mqtt, MQTT_PUB_TOPIC, ujson.dumps({"type": "device_report", "device": dev_id, "status": "valve_force_open"}))
                    
                    elif cmd == "valve_close" and addr:
                        close_valve(uart, addr)
                        if mqtt_ready:
                            meter_mqtts.mqttPublish(meter_mqtts.mqtt, MQTT_PUB_TOPIC, ujson.dumps({"type": "device_report", "device": dev_id, "status": "valve_force_closed"}))

            # ===============================================
            # 2. SCHEDULED UPLOAD (Priority 2)
            # ===============================================
            if (current_time - last_upload_time) >= UPLOAD_INTERVAL:
                mqtt_client = getattr(meter_mqtts, 'mqtt', None)
                
                # Only try if connected
                if mqtt_client and gsmCheckStatus() == 1:
                    sys_log("Scheduled Upload...", "INFO")
                    try:
                        read_meter_only(uart, SLAVE_ADDRESSES, meter_mqtts.mqttPublish, mqtt_client, MQTT_PUB_TOPIC)
                    except Exception as e:
                        sys_log("Upload Err: {}".format(e), "ERROR")
                
                last_upload_time = current_time
                safe_gc() # Clean after upload

            # ===============================================
            # 3. MAINTENANCE (Priority 3)
            # ===============================================
            # Check OTA at midnight
            t = time.localtime()
            if t[3] == 0 and 0 <= t[4] < 2 and (current_time - last_ota_check) > 3600:
                check_scheduled_restart()
                last_ota_check = current_time

            # Connection Check
            if gsmCheckStatus() != 1:
                sys_log("GSM Lost. Rebooting.", "ERROR")
                time.sleep(2)
                machine.reset()

        except Exception as e:
            sys_log("Loop Crash: {}".format(e), "ERROR")
            safe_gc()
            time.sleep(5)

        # ===============================================
        # 4. RESPONSIVE SLEEP
        # ===============================================
        # Sleep shortly (1s) to allow loop to repeat quickly.
        # This replaces the old blocking 'sleep(globals.timer)'
        time.sleep(1)

# ============ HELPERS ============ #

def check_for_interrupted_jobs():
    """Resumes interrupted batches on boot."""
    sys_log("Checking interrupted jobs...", "INFO")
    for addr in SLAVE_ADDRESSES:
        try:
            saved = load_target_reading(addr)
            if saved is None:
                # Init if new
                curr = get_valid_volume(uart, addr)
                if curr is not None: save_target_reading(addr, curr)
                continue
            
            # Resume if pending
            curr = get_valid_volume(uart, addr)
            if curr is not None and saved > curr:
                rem = saved - curr
                sys_log("Resuming Batch: {} L".format(rem), "WARNING")
                dispense_batch(uart, addr, rem)
        except:
            pass

def check_for_update_on_start():
    try:
        sys_log("Checking OTA...", "INFO")
        run_ota()
    except Exception as e:
        sys_log("OTA Err: {}".format(e), "ERROR")

def check_scheduled_restart():
    try:
        update_global_file(globals.MQTT_CLIENT_ID, retries=3)
        run_ota()
    except:
        pass

# ============ ENTRY POINT ============ #
def main():
    gc.enable()
    sys_log("Booting ATM...", "BOOT")
    led.value(1)
    time.sleep(2)
    led.value(0)
    safe_gc()

    try:
        # 1. Network
        sys_log("Init GSM...", "INFO")
        gsmInitialization()
        
        wait = 0
        while gsmCheckStatus() != 1:
            print("Waiting GSM...")
            led.value(not led.value())
            time.sleep(1)
            wait += 1
            if wait > 120: 
                 sys_log("GSM Timeout", "ERROR")
                 machine.reset()
        
        sys_log("GSM OK", "INFO")
        led.value(0)
        
        # 2. Setup
        check_for_update_on_start()

        # 3. Threads

        # 1. Start MQTT Listener (Receives -> Queue)
        _thread.start_new_thread("MqttListener", meter_mqtts.mqttInitialize, (meter_mqtts.mqtt, MQTT_SUB_TOPICS,))
        
        # 2. Start Supervisor (Feeds WDT)
        _thread.start_new_thread("Supervisor", supervisor_thread, ())

        # 4. Main Loop
        time.sleep(5) 
        monitor_loop() 

    except Exception as e:
        sys_log("Crit Fail: {}".format(e), "ERROR")
        time.sleep(10)
        machine.reset()

if __name__ == "__main__":
    main()