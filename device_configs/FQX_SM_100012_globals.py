# ============ USER CONFIGURATION (EDIT HERE) ============ #
GLOBAL_VERSION = "1.0.1"

# 1. Device Identification
DEVICE_ID = "FQX_SM_100012" 

# 2. Modbus Slave IDs (List all connected meters here)
SLAVE_ADDRESSES = [38] 

# 3. Connection Settings
MQTT_BROKER_HOST = "152.42.139.67"
MQTT_BROKER_PORT = 18100
UPDATE_URL = "https://raw.githubusercontent.com/FAINA-TECH/updateatm/main"
CIU_CALLBACK_URL = "https://backend2.mteja.co.ke/mqttcomms/callback/ciu-check"

# 4. GSM Settings
GSM_APN = 'safaricomiot'
GSM_USER = ''
GSM_PASS = ''

# ============ AUTOMATED CONFIGURATION (DO NOT EDIT) ============ #

# 1. Auto-Generate MQTT Credentials
MQTT_CLIENT_ID = DEVICE_ID
MQTT_CLIENT_USERNAME = DEVICE_ID

# Auto-generate Password
try:
    _prefix, _suffix = DEVICE_ID.rsplit('_', 1)
    MQTT_CLIENT_PASSWORD = "{}@{}".format(_prefix, _suffix)
except ValueError:
    # Fallback if no underscore found
    MQTT_CLIENT_PASSWORD = DEVICE_ID 

# 2. Auto-Generate Publication Topic
# Topic: smartmeter/FQX_SM_100010/pub/controlcomm/message
MQTT_PUB_TOPIC = "smartmeter/{}/pub/controlcomm/message".format(DEVICE_ID)

# 3. Auto-Generate Subscription Topics (One per Slave Address)
MQTT_SUB_TOPICS = []
for addr in SLAVE_ADDRESSES:
    topic = "smartmeter/{}-{}/sub/controlcomm/message".format(DEVICE_ID, addr)
    MQTT_SUB_TOPICS.append(topic)

# 4. Other Globals
VERSION_FILE = "/flash/version.txt"
timer = 180
CMD_QUEUE = []

CHECK_INTERVAL = 180   # 3 Minutes
UPLOAD_INTERVAL = 3600 # 1 Hour
RESPONSIVE_SLEEP = 5   # Sleep cycle duration