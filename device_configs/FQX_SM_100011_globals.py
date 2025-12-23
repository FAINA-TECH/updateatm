# ============ DEVICE CONFIGURATION ============ #
GLOBAL_VERSION = "1.0.0"

# ====== Configuration ======
UPDATE_URL = "https://raw.githubusercontent.com/FAINA-TECH/updateatm/main"
VERSION_FILE = "/flash/version.txt"

# ============ MODBUS SLAVE ADDRESSES ============ #
SLAVE_ADDRESSES = [6]

# ============ MQTT CONFIGURATION ============ #
MQTT_BROKER_HOST = "152.42.139.67"
MQTT_BROKER_PORT = 18100
MQTT_CLIENT_ID = "FQX_SM_100011"
MQTT_CLIENT_USERNAME = "FQX_SM_100011"
MQTT_CLIENT_PASSWORD = "FQX_SM@100011"

MQTT_PUB_TOPIC = 'smartmeter/FQX_SM_100011/pub/controlcomm/message'

# ============ GSM CONFIGURATION ============ #
GSM_APN = 'safaricomiot'
GSM_USER = ''
GSM_PASS = ''

MQTT_SUB_TOPICS = [
    "smartmeter/FQX_SM_100011/sub/controlcomm/message"
]

timer = 180

# ============ COMMAND QUEUE (THREAD SAFE) ============ #
# MQTT thread puts commands here. Main thread executes them.
CMD_QUEUE = []
