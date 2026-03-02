from machine import UART
import time
import struct
from meter import (
    calculate_crc, verify_crc, build_modbus_request, 
    smart_read_modbus, clear_uart_buffer,
    open_valve, close_valve, get_valid_volume
)

# ========== UART CONFIGURATION ==========
uart = UART(2, baudrate=9600, bits=8, parity=1, stop=1, tx=19, rx=18)

# ========== LOW-LEVEL HELPERS ==========

def read_register_raw(uart, device_addr, register_addr, count=1):
    """
    Generic function to read Modbus registers (FC 0x03).
    """
    clear_uart_buffer(uart)
    request = build_modbus_request(device_addr, 0x03, register_addr, count)
    uart.write(request)
    
    # Expected: [Addr][FC][ByteCount][Data...][CRC][CRC]
    expected = 5 + (2 * count)
    response = smart_read_modbus(uart, expected)
    
    if response and len(response) == expected and verify_crc(response):
        return response
    return None

def write_register_raw(uart, device_addr, register_addr, value):
    """
    Generic function to write a single register using FC 0x10.
    """
    clear_uart_buffer(uart)
    frame = bytearray(9)
    frame[0] = device_addr
    frame[1] = 0x10  # Function Code
    frame[2] = (register_addr >> 8) & 0xFF
    frame[3] = register_addr & 0xFF
    frame[4] = 0x00
    frame[5] = 0x01  # Quantity
    frame[6] = 0x02  # Byte Count
    frame[7] = (value >> 8) & 0xFF
    frame[8] = value & 0xFF
    
    crc = calculate_crc(frame)
    frame += bytearray([crc & 0xFF, (crc >> 8) & 0xFF])
    
    uart.write(frame)
    response = smart_read_modbus(uart, 8)
    return response and verify_crc(response)

def bcd_to_decimal(bcd_val):
    """
    Converts BCD (Binary Coded Decimal) to Integer.
    Ex: 0x0364 -> 364
    """
    return (bcd_val >> 12) * 1000 + ((bcd_val >> 8) & 0x0F) * 100 + ((bcd_val >> 4) & 0x0F) * 10 + (bcd_val & 0x0F)

# ========== DIAGNOSTIC FUNCTIONS ==========

def decode_st_register(val):
    """
    Decodes the State ST register (0x0017).
    Per Doc: "Second byte of status ST is first" (High Byte).
    High Byte = Second Byte Definition Table 
    Low Byte  = First Byte Definition Table
    """
    high_byte = (val >> 8) & 0xFF
    low_byte = val & 0xFF
    
    status = []
    
    # --- LOW BYTE (First Byte Table) ---
    # D2: Cell Voltage (1 = Underpressure/Low)
    if (low_byte >> 2) & 1: status.append("BATTERY: Low Voltage")
    # D3: Leakage (1 = Leakage)
    if (low_byte >> 3) & 1: status.append("ALERT: Leakage Detected")
    # D4: Pipe Burst (1 = Burst)
    if (low_byte >> 4) & 1: status.append("ALERT: Pipe Burst")
    # D5: Blank Pipe (1 = Empty/Air)
    if (low_byte >> 5) & 1: status.append("CRITICAL: Pipe Empty")
    # D6: Temp Error (1 = Error)
    if (low_byte >> 6) & 1: status.append("ERROR: Temp Sensor")
    
    # --- HIGH BYTE (Second Byte Table) ---
    # D1: Water Direction (0=Pos, 1=Rev)
    if (high_byte >> 1) & 1: status.append("FLOW: Reverse Direction")
    # D7: Sensor Anomaly
    if (high_byte >> 7) & 1: status.append("ERROR: Sensor Failure")

    if not status:
        return "System OK"
    return ", ".join(status)

def get_full_diagnostics(uart, addr):
    print("\n--- DIAGNOSTIC REPORT FOR DEVICE %s ---" % addr)
    
    # 1. Read Flow (0x000E - 2 Regs)
    flow_raw = read_register_raw(uart, addr, 0x000E, 2)
    if flow_raw:
        reg_e = (flow_raw[3] << 8) | flow_raw[4]
        reg_f = (flow_raw[5] << 8) | flow_raw[6]
        
        # Calculating based on Little Endian Word Order
        flow_val = (reg_f << 16) | reg_e
        print("[-] Cumulative Flow: %s L" % flow_val)
    else:
        print("[!] Failed to read Flow (0x000E)")

    # 2. Read Instantaneous Flow (0x0014 - 2 Regs)
    # Docs Page 8: 0014H-0015H
    inst_raw = read_register_raw(uart, addr, 0x0014, 2)
    if inst_raw:
        # Assuming same Little Endian Word Order
        i_reg1 = (inst_raw[3] << 8) | inst_raw[4]
        i_reg2 = (inst_raw[5] << 8) | inst_raw[6]
        inst_val = (i_reg2 << 16) | i_reg1
        print("[-] Instant Flow:    %s units" % inst_val)
    else:
        print("[!] Failed to read Inst. Flow (0x0014)")

    # 3. Read Battery Voltage (0x0016 - 1 Reg, BCD)
    # Docs Page 8: 0016H, Unit 0.01V, BCD
    volt_raw = read_register_raw(uart, addr, 0x0016, 1) 
    if volt_raw:
        bcd_val = (volt_raw[3] << 8) | volt_raw[4]
        decimal_val = bcd_to_decimal(bcd_val)
        volt_val = decimal_val * 0.01
        print("[-] Battery Voltage: %.2f V" % volt_val)
    else:
        print("[!] Failed to read Battery (0x0016)")

    # 4. Read State ST (0x0017 - 1 Reg)
    # Docs Page 8: 0017H
    st_raw = read_register_raw(uart, addr, 0x0017, 1)
    if st_raw:
        st_val = (st_raw[3] << 8) | st_raw[4]
        print("[-] System Status:   %s" % decode_st_register(st_val))
    else:
        print("[!] Failed to read Status ST (0x0017)")

    # 5. Read Temperature (0x0018 - 1 Reg)
    # Docs Page 8: 0018H, Unit 0.01C
    temp_raw = read_register_raw(uart, addr, 0x0018, 1)
    if temp_raw:
        temp_val = ((temp_raw[3] << 8) | temp_raw[4]) * 0.01
        print("[-] Water Temp:      %.2f C" % temp_val)
    else:
        print("[!] Failed to read Temp (0x0018)")

    # 6. Read Valve State (0x0060 - 1 Reg)
    valve_raw = read_register_raw(uart, addr, 0x0060, 1)
    if valve_raw:
        v_bits = valve_raw[4] & 0x03
        state = "OPEN" if v_bits == 1 else ("CLOSED" if v_bits == 2 else "UNKNOWN")
        print("[-] Valve Position:  %s" % state)
    else:
        print("[!] Failed to read Valve Pos (0x0060)")
        
    print("------------------------------------------")

# ========== NETWORK TOOLS ==========

def scan_network(uart):
    print("\n--- SCANNING NETWORK (1-100) ---")
    found = []
    for addr in range(1, 101):
        res = read_register_raw(uart, addr, 0x0000, 1)
        if res:
            detected_id = res[4] 
            print("[+] Found Device: %s" % detected_id)
            found.append(detected_id)
    if not found: print("No devices found.")
    return found

def change_id(uart, old_id, new_id):
    print("\n[CONFIG] Changing ID %s -> %s..." % (old_id, new_id))
    if write_register_raw(uart, old_id, 0x0000, new_id):
        print("[SUCCESS] ID Changed. Please Power Cycle Meter.")
    else:
        print("[FAIL] Could not change ID.")

def set_maintenance_cycle(uart, addr, days):
    """
    Sets 'Regular on and off valve cycle' (0x0006).
    Range 7-90 days.
    """
    if not (7 <= days <= 90):
        print("[ERROR] Days must be between 7 and 90.")
        return
    
    print("\n[CONFIG] Setting Valve Maint Cycle to %s days..." % days)
    if write_register_raw(uart, addr, 0x0006, days):
        print("[SUCCESS] Cycle updated.")
    else:
        print("[FAIL] Update failed.")

# ========== MENU ==========

def main_menu():
    while True:
        print("\n=== 485 ULTRASONIC METER TOOL ===")
        print("1. Scan Network (Find IDs)")
        print("2. Full Diagnostic Report")
        print("3. Valve Control (Open/Close)")
        print("4. Change Meter ID")
        print("5. Configure Maintenance Cycle")
        print("6. Exit")
        
        c = input("Choice: ")
        
        if c == '1':
            scan_network(uart)
            
        elif c == '2':
            val = input("Enter Meter ID (Default 1): ")
            a = int(val) if val else 1
            get_full_diagnostics(uart, a)
            
        elif c == '3':
            val = input("Enter Meter ID (Default 1): ")
            a = int(val) if val else 1
            act = input("Action (o=open, c=close): ")
            if act == 'o': 
                open_valve(uart, a)
                print("Command Sent: OPEN")
            elif act == 'c': 
                close_valve(uart, a)
                print("Command Sent: CLOSE")
            
        elif c == '4':
            curr = int(input("Current ID: "))
            new = int(input("New ID (1-247): "))
            change_id(uart, curr, new)
            
        elif c == '5':
            val = input("Enter Meter ID (Default 1): ")
            a = int(val) if val else 1
            d = int(input("Enter Cycle Days (7-90): "))
            set_maintenance_cycle(uart, a, d)
            
        elif c == '6':
            break

if __name__ == "__main__":
    main_menu()
