import socket
import machine
import time
import sys
import gsm
import globals

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
#MODEM_RST = 5
MODEM_PWRKEY_PIN = 4
MODEM_TX = 27
MODEM_RX = 26
LED_PIN = 13

def gsmInitialization():
    # Power on the GSM module
    global MODEM_RST
    
    GSM_POWER = machine.Pin(MODEM_POWER_PIN, machine.Pin.OUT)
    GSM_POWER.value(1)

    LED = machine.Pin(LED_PIN, machine.Pin.OUT)
    LED.value(1)

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
    
    # --- CHANGED: Set Debug to False to hide hex dumps ---
    gsm.debug(False) 

    gsm.start(tx=MODEM_TX, rx=MODEM_RX, apn=GSM_APN,
              user=GSM_USER, password=GSM_PASS, roaming=True)
    
    for retry in range(20):
        if gsm.atcmd('AT'):
            break
        else:
            sys.stdout.write('.')
            time.sleep_ms(5000)
    else:
        #raise Exception("Modem not responding!")
        print("Modem not responding!")
        machine.reset()
    print()

    print("Connecting to GSM...")
    gsm.connect()

    while gsm.status()[0] != 1:
        pass

    print('IP:', gsm.ifconfig()[0])
    # GSM connection is complete.
    # You can now use modules like urequests, uPing, etc.
    # Let's try socket API:
    print("Connected !")

def gsmCheckStatus():
    gsmconnectivity = gsm.status()[0]
    return gsmconnectivity