from meter_gsm import gsmInitialization, gsmCheckStatus
import meter_mqtts 
from meter import (
    dispense_batch, read_meter_only, get_valid_volume,
    open_valve, close_valve, 
    uart, load_target_reading, save_target_reading
)
from ota_update import *
from machine import UART, Pin
from utime import sleep, time, localtime
import _thread
import globals
import machine
import gc
import json
import os

# ============ CONFIGURATION ============ #
SLAVE_ADDRESSES = globals.SLAVE_ADDRESSES
MQTT_PUB_TOPIC = globals.MQTT_PUB_TOPIC
MQTT_SUB_TOPICS = globals.MQTT_SUB_TOPICS
LOG_FILE = "system_error.log"

# Status LED (Pin 13)
led = Pin(13, Pin.OUT)

# Global tick to track system health
last_alive_tick = time() 

# ============ UTILITIES ============ #
def sys_log(msg, level="INFO"):
    """Logs messages to console and file (only errors)."""
    try:
        t = localtime()
        timestamp = "{:02d}-{:02d} {:02d}:{:02d}:{:02d}".format(t[1], t[2], t[3], t[4], t[5])
        formatted_msg = "[{}] [{}] {}".format(timestamp, level, msg)
        print(formatted_msg)

        if level == "ERROR" or level == "BOOT":
            with open(LOG_FILE, 'a') as f:
                f.write(formatted_msg + '\n')
    except Exception:
        print(msg) 

def safe_gc():
    """Forces garbage collection to prevent fragmentation crashes."""
    gc.collect()
    if gc.mem_free() < 10240:
        sys_log("Low RAM. Rebooting.", "ERROR")
        sleep(2)
        machine.reset()

def check_scheduled_restart():
    """Checks for OTA updates at Midnight instead of rebooting."""
    t = localtime()
    # If year > 2024 (time synced) and it is Midnight (00:0X)
    if t[0] > 2024 and t[3] == 0 and 0 <= t[4] < 2: 
        try:
            update_global_file(globals.MQTT_CLIENT_ID, retries=3)
            run_ota()
        except:
            pass
        sleep(60) # Avoid repeating in same minute

# ============ RECOVERY & INIT LOGIC ============ #
def check_for_interrupted_jobs():
    """
    Runs on boot. 
    1. Checks if a target file exists.
    2. If MISSING: Reads meter and initializes file (Syncs Target = Current).
    3. If EXISTS and Target > Current: Resumes dispensing.
    """
    print("[Recovery] Checking device state...")
    for addr in SLAVE_ADDRESSES:
        try:
            saved_target = load_target_reading(addr)
            
            # --- AUTO-INITIALIZATION ---
            if saved_target is None:
                print("[Init] No saved state for Addr {}. Reading meter...".format(addr))
                current_vol = get_valid_volume(uart, addr)
                
                if current_vol is not None:
                    # Initialize: Set Target = Current (No debt)
                    save_target_reading(addr, current_vol)
                    print("[Init] Device initialized. Volume: {} L".format(current_vol))
                else:
                    print("[Init] ❌ Failed to read meter at Addr {}. Cannot init.".format(addr))
                continue
            
            # --- RECOVERY CHECK ---
            current_vol = get_valid_volume(uart, addr)
            if current_vol is None: continue
            
            if saved_target > current_vol:
                remaining = saved_target - current_vol
                print("[Recovery] Resuming batch for Addr {}. Remaining: {} L".format(addr, remaining))
                
                # Notify Backend
                if meter_mqtts.mqtt is not None:
                     meter_mqtts.mqttPublish(meter_mqtts.mqtt, MQTT_PUB_TOPIC, json.dumps({
                        "type": "device_report", "device": globals.MQTT_CLIENT_ID, 
                        "status": "resuming_batch", "remaining": remaining
                    }))
                
                # Resume Dispense (Blocking)
                dispense_batch(uart, addr, remaining)
                
            else:
                print("[Recovery] Addr {} clean (Target {} <= Curr {}).".format(addr, saved_target, current_vol))
                
        except Exception as e:
            print("[Recovery] Error on Addr {}: {}".format(addr, e))

# ============ COMMAND PROCESSOR (ATM LOGIC) ============ #
def process_command_queue():
    """
    Executes commands. If a dispense command arrives, it BLOCKS 
    until water is finished, effectively queuing other commands.
    """
    # Loop as long as there are items in the queue
    while len(globals.CMD_QUEUE) > 0:
        try:
            item = globals.CMD_QUEUE.pop(0)
            cmd = item['type']
            addr = item['addr'] 
            dev_id = item['device_id']
            
            print("Processing CMD: {} for Addr {}".format(cmd, addr))
            
            # --- PAYMENT RECEIVED (START DISPENSE) ---
            if cmd == "success": 
                litres = item.get('litres', 0)
                
                if litres > 0:
                    # 1. Notify Backend: Started
                    meter_mqtts.mqttPublish(meter_mqtts.mqtt, MQTT_PUB_TOPIC, json.dumps({
                        "type": "device_report", "device": dev_id, "status": "dispense_started", "amount": litres
                    }))
                    
                    # 2. Run the Blocking Dispense Loop
                    result = dispense_batch(uart, addr, litres)
                    
                    # 3. Report Final Status
                    if result['status'] == 'completed':
                         meter_mqtts.mqttPublish(meter_mqtts.mqtt, MQTT_PUB_TOPIC, json.dumps({
                            "type": "device_report", 
                            "device": dev_id, 
                            "status": "dispense_complete",
                            "dispensed": result['dispensed'],
                            "final_reading": result.get('final_reading')
                        }))
                    else:
                        # REPORT FAILURE
                        meter_mqtts.mqttPublish(meter_mqtts.mqtt, MQTT_PUB_TOPIC, json.dumps({
                            "type": "device_report", 
                            "device": dev_id, 
                            "status": "dispense_failed",
                            "reason": result.get('reason'),
                            "dispensed_before_fail": result.get('dispensed')
                        }))
                        
            # --- MANUAL OVERRIDES ---
            elif cmd == "valve_open":
                open_valve(uart, addr)
                meter_mqtts.mqttPublish(meter_mqtts.mqtt, MQTT_PUB_TOPIC, json.dumps({
                    "type": "device_report", "device": dev_id, "status": "valve_force_open"
                }))

            elif cmd == "valve_close":
                close_valve(uart, addr)
                meter_mqtts.mqttPublish(meter_mqtts.mqtt, MQTT_PUB_TOPIC, json.dumps({
                    "type": "device_report", "device": dev_id, "status": "valve_force_closed"
                }))
                
        except Exception as e:
            print("Queue Error: {}".format(e))
        
        # Small sleep between queue items
        sleep(1)

# ============ SUPERVISOR THREAD (WDT MANAGER) ============ #
def supervisor_thread():
    """
    Feeds the Hardware Watchdog.
    Reboots if Main Thread hangs for > 20 minutes.
    """
    try:
        machine.WDT(True) # Enable LoBo Fixed WDT (approx 15s)
        sys_log("WDT Enabled")
    except:
        sys_log("WDT Init Fail")

    while True:
        # Check if Main Thread has reported alive recently
        if (time() - last_alive_tick) < 1200: # 20 Minutes Limit
            machine.resetWDT() # Feed the dog
        else:
            print("System HUNG > 20 mins. Allowing WDT Reboot...")
            # We intentionally STOP feeding. Hardware resets in ~15s.
        
        # Blink LED to show life
        led.value(not led.value())
        
        # Must sleep LESS than hardware timeout (15s)
        sleep(5) 

# ============ MONITOR THREAD (MAIN WORKER) ============ #
def monitor_loop():
    global last_alive_tick
    
    # Wait for other threads to stabilize
    sleep(10)
    
    # --- RECOVERY / INIT CHECK ON STARTUP ---
    check_for_interrupted_jobs()

    while True:
        try:
            # 1. Report Alive
            last_alive_tick = time()
            safe_gc()
            check_scheduled_restart()

            # 2. Process pending MQTT commands (Active Dispensing)
            process_command_queue()

            # 3. Check Connection
            if gsmCheckStatus() != 1:
                sys_log("GSM Lost. Rebooting.")
                machine.reset()

            # 4. Upload Idle Data (Only if MQTT is connected)
            if meter_mqtts.mqtt is not None and meter_mqtts.mqtt.status()[0] == 2:
                try:
                    read_meter_only(
                        uart, 
                        SLAVE_ADDRESSES, 
                        meter_mqtts.mqttPublish, 
                        meter_mqtts.mqtt, 
                        MQTT_PUB_TOPIC
                    )
                except Exception as e:
                    print("Upload Err:", e)

            # Sleep Logic (Idle Time)
            print("Sleeping {}s...".format(globals.timer))
            last_alive_tick = time() 
            sleep(globals.timer)
            last_alive_tick = time() # Update immediately on wake

        except Exception as e:
            sys_log("Loop Crash: " + str(e))
            sleep(5)
            machine.reset()

# ============ MAIN EXECUTION ============ #
def main():
    gc.enable()
    
    sys_log("Booting...", "BOOT")
    led.value(1)
    sleep(2)
    led.value(0)
    
    safe_gc()

    try:
        sys_log("Initializing GSM...", "INFO")
        gsmInitialization()
        
        wait = 0
        while gsmCheckStatus() != 1:
            print("Waiting for GSM...")
            led.value(not led.value()) 
            # We must feed WDT manually here if enabled early, 
            # but supervisor isn't running yet, so we are safe.
            sleep(1)
            wait += 1
            if wait > 120: 
                 sys_log("GSM Timeout. Rebooting.", "ERROR")
                 machine.reset()
        
        sys_log("GSM Connected.", "INFO")
        led.value(0)

        # 1. Start MQTT Listener (Receives -> Queue)
        _thread.start_new_thread("MqttListener", meter_mqtts.mqttInitialize, (meter_mqtts.mqtt, MQTT_SUB_TOPICS,))
        
        # 2. Start Supervisor (Feeds WDT)
        _thread.start_new_thread("Supervisor", supervisor_thread, ())

        # 3. Start Monitor (Consumes Queue + Reads UART)
        sleep(5) 
        _thread.start_new_thread("MeterMonitor", monitor_loop, ())

        sys_log("System Running", "INFO")
        
        # Keep Main Thread Alive
        while True:
            sleep(10)

    except Exception as e:
        sys_log("Main Crash: {}".format(e), "ERROR")
        sleep(5)
        machine.reset()

if __name__ == "__main__":
    main()