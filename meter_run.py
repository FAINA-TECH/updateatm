from machine import UART
import time
from meter import *

# ========== UART CONFIG ==========
uart = UART(2, baudrate=9600, bits=8, parity=1, stop=1, tx=19, rx=18)  # UART2 on ESP32

# MODBUS Slave Addresses
# SLAVE_ADDRESSES = [1, 2, 3, 4, 5, 6]
SLAVE_ADDRESSES = [4]

# ========== MAIN LOOP ==========

def main():
    print("---- Test Starting ----")

    # Pass None for MQTT args to avoid errors since we are just testing hardware
#     try:
#         read_meter_only(uart, SLAVE_ADDRESSES, None, None, None)
#     except Exception as e:
#         print("Read Error: ", e)
# 
#     time.sleep(1)

    # valve_open expects a single integer, so we loop through the list
    print("Opening Valves...")
    for addr in SLAVE_ADDRESSES:
        try:
            # CORRECTED: Function name is open_valve (not valve_open)
            open_valve(uart, addr)
            print("Valve Open command sent to {}".format(addr))
        except Exception as e:
            print("Valve Error on {}: {}".format(addr, e))

    print("---- Test Complete Done ----\n")
    time.sleep(2)

# Run main loop
main()