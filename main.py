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
    uart, load_target_reading, save_target_reading,
    cancel_pending_target
)
from ota_update import *

# ============ CONFIGURATION ============ #
SLAVE_ADDRESSES = getattr(globals, 'SLAVE_ADDRESSES', [])
MQTT_PUB_TOPIC = getattr(globals, 'MQTT_PUB_TOPIC', '')
MQTT_SUB_TOPICS = getattr(globals, 'MQTT_SUB_TOPICS', [])
LOG_FILE = "system.log"
led = Pin(13, Pin.OUT)

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
            file_mode = 'a'
            try:
                if os.stat(LOG_FILE)[6] > 51200: 
                    file_mode = 'w' 
            except OSError:
                pass
            
            with open(LOG_FILE, file_mode) as f:
                f.write("[{}] {}\n".format(timestamp, msg))
    except:
        pass

def safe_gc():
    """Explicitly cleans memory."""
    try:
        gc.collect()
    except:
        pass

def publish_dispense_report(dev_id, result):
    """
    Reports the outcome of a dispense_batch() call back to the server, whether
    it completed, was cancelled, or failed - used by every call site so no path
    (including the boot-time auto-resume) can finish silently.
    """
    if result['status'] == 'completed':
        status_msg = "dispense_complete"
    elif result['status'] == 'cancelled':
        status_msg = "dispense_cancelled"
    else:
        status_msg = "dispense_failed"

    payload = {
        "type": "device_report",
        "device": dev_id,
        "status": status_msg,
        "dispensed": result.get('dispensed', 0),
        "final_reading": result.get('final_reading'),
        "reason": result.get('reason')
    }
    mqtt_client = getattr(meter_mqtts, 'mqtt', None)
    if mqtt_client is not None:
        meter_mqtts.mqttPublish(mqtt_client, MQTT_PUB_TOPIC, ujson.dumps(payload))

def check_for_update_on_start():
    try:
        sys_log("Checking for OTA Updates...", "INFO")
        run_ota()
    except Exception as e:
        sys_log("OTA Error: {}".format(e), "ERROR")
        
def close_all_valves_on_boot():
    """
    Forces all valves closed on startup to guarantee a safe initial state.
    """
    sys_log("Securing valves (Force Close)...", "INFO")
    for addr in SLAVE_ADDRESSES:
        try:
            machine.resetWDT() # Feed WDT during the loop
            close_valve(uart, addr)
            time.sleep(0.5) # Give the RS485 bus and mechanical valve time to settle
        except Exception as e:
            sys_log("Valve close err Addr {}: {}".format(addr, e), "ERROR")

# ============ MAIN MONITOR LOOP ============ #

def monitor_loop():
    """
    Main Logic. Runs in Main Thread.
    Uses Non-Blocking Timers for responsiveness.
    """
    sys_log("Monitor Loop Started", "INFO")
    
    UPLOAD_INTERVAL = getattr(globals, 'UPLOAD_INTERVAL', 180) # Default 3 mins
    last_upload_time = time.time()
    
    # --- RECOVERY CHECK ---
    check_for_interrupted_jobs()

    while True:
        try:
            current_time = time.time()
            
            # --- FEED THE LOBO HARDWARE WATCHDOG ---
            machine.resetWDT() 
            # ---------------------------------------

            # ===============================================
            # 1. INSTANT COMMAND PROCESSING (Priority 1)
            # ===============================================
            if globals.CMD_QUEUE:
                sys_log("Processing Queue...", "DEBUG")
                
                while globals.CMD_QUEUE:
                    machine.resetWDT() # Keep alive during heavy processing
                    
                    # --- THREAD LOCK: Safely read/remove from the shared resource ---
                    _thread.lock()
                    cmd_item = globals.CMD_QUEUE.pop(0)
                    _thread.unlock()
                    # ----------------------------------------------------------------
                    
                    cmd = cmd_item.get('cmd') or cmd_item.get('type')
                    addr = cmd_item.get('addr')
                    dev_id = cmd_item.get('dev_id') or getattr(globals, 'MQTT_CLIENT_ID', 'UNKNOWN')
                    
                    sys_log("CMD: {}".format(cmd), "INFO")
                    
                    mqtt_ready = (hasattr(meter_mqtts, 'mqtt') and meter_mqtts.mqtt is not None)

                    # --- A. DISPENSE (ATM LOGIC - BLOCKING) ---
                    if cmd == "success" and addr:
                        litres = cmd_item.get('litres')
                        # Validate before dispensing: a missing/None/non-numeric litres would
                        # otherwise raise TypeError on the ">" comparison below, which the outer
                        # loop swallows silently - no water moves, but no report reaches the
                        # server either. Report a clean failure instead.
                        if not isinstance(litres, (int, float)) or isinstance(litres, bool) or litres <= 0:
                            sys_log("Invalid litres in 'success' cmd: {}".format(litres), "ERROR")
                            publish_dispense_report(dev_id, {
                                "status": "failed", "dispensed": 0,
                                "final_reading": None, "reason": "invalid_litres"
                            })
                        else:
                            if mqtt_ready:
                                meter_mqtts.mqttPublish(meter_mqtts.mqtt, MQTT_PUB_TOPIC, ujson.dumps({
                                    "type": "device_report", "device": dev_id, "status": "dispense_started", "amount": litres
                                }))

                            result = dispense_batch(uart, addr, litres)
                            publish_dispense_report(dev_id, result)

                            safe_gc()

                    # --- B. MANUAL / DIAGNOSTIC COMMANDS ---
                    elif cmd == "check_status" and mqtt_ready:
                        read_meter_only(uart, SLAVE_ADDRESSES, meter_mqtts.mqttPublish, meter_mqtts.mqtt, MQTT_PUB_TOPIC)
                    
                    elif cmd == "check_update":
                        check_for_update_on_start()
                    
                    elif cmd == "valve_open" and addr:
                        open_valve(uart, addr)
                        if mqtt_ready:
                            meter_mqtts.mqttPublish(meter_mqtts.mqtt, MQTT_PUB_TOPIC, ujson.dumps({"type": "device_report", "device": dev_id, "status": "valve_force_open"}))
                    
                    elif cmd == "valve_close" and addr:
                        close_valve(uart, addr)
                        # Clear any CANCEL_REQUEST left over from this same message if no
                        # batch was active to consume it - otherwise it would wrongly abort
                        # the next legitimate dispense the instant it starts.
                        globals.CANCEL_REQUEST.pop(addr, None)
                        if mqtt_ready:
                            meter_mqtts.mqttPublish(meter_mqtts.mqtt, MQTT_PUB_TOPIC, ujson.dumps({"type": "device_report", "device": dev_id, "status": "valve_force_closed"}))

                    # --- C. CANCEL / RESUME (Safety controls for interrupted/oversized batches) ---
                    elif cmd == "cancel_dispense" and addr:
                        # If a batch is actively running, meter_mqtts.datacb already set
                        # CANCEL_REQUEST directly so it aborts on its own. This call handles
                        # the idle case: a stale/oversized target with nothing running.
                        final_reading = cancel_pending_target(uart, addr)
                        publish_dispense_report(dev_id, {
                            "status": "cancelled", "dispensed": 0,
                            "final_reading": final_reading, "reason": "idle_reset"
                        })

                    elif cmd == "resume_dispense" and addr:
                        saved = load_target_reading(addr)
                        curr = get_valid_volume(uart, addr)
                        if saved is not None and curr is not None and saved > curr:
                            rem = saved - curr
                            result = dispense_batch(uart, addr, rem)
                            publish_dispense_report(dev_id, result)
                        elif mqtt_ready:
                            meter_mqtts.mqttPublish(meter_mqtts.mqtt, MQTT_PUB_TOPIC, ujson.dumps({
                                "type": "device_report", "device": dev_id, "status": "resume_nothing_pending"
                            }))

            # ===============================================
            # 2. SCHEDULED UPLOAD (Priority 2)
            # ===============================================
            if (current_time - last_upload_time) >= UPLOAD_INTERVAL:
                mqtt_client = getattr(meter_mqtts, 'mqtt', None)
                
                if mqtt_client and gsmCheckStatus() == 1:
                    sys_log("Scheduled Upload...", "INFO")
                    try:
                        read_meter_only(uart, SLAVE_ADDRESSES, meter_mqtts.mqttPublish, mqtt_client, MQTT_PUB_TOPIC)
                    except Exception as e:
                        sys_log("Upload Err: {}".format(e), "ERROR")
                
                last_upload_time = current_time
                safe_gc()
                
            if gsmCheckStatus() != 1:
                sys_log("GSM Lost. Attempting graceful reconnect...", "WARNING")
                try:
                    gsmInitialization()
                    machine.WDT(True) 
                    sys_log("GSM Reconnected Successfully", "INFO")
                except Exception as e:
                    sys_log("Reconnect failed: {}. Forcing reboot.".format(e), "ERROR")
                    time.sleep(5)
                    machine.reset()

        except Exception as e:
            sys_log("Loop Crash: {}".format(e), "ERROR")
            safe_gc()
            time.sleep(5)

        time.sleep(1)

# ============ HELPERS ============ #

def check_for_interrupted_jobs():
    """
    Resumes interrupted batches on boot.
    Safety cap: a leftover larger than MAX_AUTO_RESUME_LITERS is left alone
    (valve stays closed from close_all_valves_on_boot) and waits for an explicit
    remote "resume_dispense" or "cancel_dispense" instead of auto-reopening the valve.
    """
    sys_log("Checking interrupted jobs...", "INFO")
    max_auto_resume = getattr(globals, 'MAX_AUTO_RESUME_LITERS', 1000)
    for addr in SLAVE_ADDRESSES:
        try:
            machine.resetWDT() # Feed WDT during loop
            saved = load_target_reading(addr)
            if saved is None:
                curr = get_valid_volume(uart, addr)
                if curr is not None: save_target_reading(addr, curr)
                continue

            curr = get_valid_volume(uart, addr)
            if curr is not None and saved > curr:
                rem = saved - curr
                if rem > max_auto_resume:
                    sys_log("Large interrupted batch ({} L) on Addr {} - awaiting remote confirmation".format(rem, addr), "WARNING")
                    continue
                sys_log("Resuming Batch: {} L".format(rem), "WARNING")
                result = dispense_batch(uart, addr, rem)
                dev_id = getattr(globals, 'MQTT_CLIENT_ID', 'UNKNOWN')
                publish_dispense_report(dev_id, result)
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
    
    close_all_valves_on_boot()

    try:
        sys_log("Init GSM...", "INFO")
        gsmInitialization() # NOTE: ensure WDT is disabled inside this function in meter_gsm.py
        
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
        led.value(1)
        
        check_for_update_on_start() # NOTE: ensure WDT is disabled inside this function in ota_update.py

        # 1. Start MQTT Listener 
        _thread.start_new_thread("MqttListener", meter_mqtts.mqttInitialize, (meter_mqtts.mqtt, MQTT_SUB_TOPICS,))
        
        # 2. Enable Hardware WDT and start loop
        machine.WDT(True)
        time.sleep(2) 
        monitor_loop() 

    except Exception as e:
        sys_log("Crit Fail: {}".format(e), "ERROR")
        time.sleep(10)
        machine.reset()

if __name__ == "__main__":
    main()