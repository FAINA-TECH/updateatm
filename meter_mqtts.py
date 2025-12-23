import network
import utime
import globals
import json
import machine

# Global Variables
MQTT_BROKER_HOST = globals.MQTT_BROKER_HOST
MQTT_BROKER_PORT = globals.MQTT_BROKER_PORT
MQTT_CLIENT_USERNAME = globals.MQTT_CLIENT_USERNAME
MQTT_CLIENT_PASSWORD = globals.MQTT_CLIENT_PASSWORD
MQTT_CLIENT_ID = globals.MQTT_CLIENT_ID
MQTT_PUB_TOPIC = globals.MQTT_PUB_TOPIC
MQTT_SUB_TOPICS = globals.MQTT_SUB_TOPICS

def get_device_Hex(deviceID):
    if '-' in deviceID:
        try:
            return int(deviceID.split('-')[-1], 10)
        except ValueError:
            return None
    return None

# ---------------- MQTT CALLBACKS ---------------- #

def conncb(task):
    print("[{}] Connected".format(task))

def disconncb(task):
    print("[{}] Disconnected".format(task))
    mqttInitialize(mqtt, MQTT_SUB_TOPICS)

def subscb(task):
    print("[{}] Subscribed".format(task))

def pubcb(pub):
    print("[{}] Published: {}".format(pub[0], pub[1]))

def datacb(msg):
    # This runs in the MQTT Thread. 
    # WE MUST NOT TOUCH UART HERE.
    print("[Data] Topic: {}, Msg: {}".format(msg[1], msg[2]))

    try:
        payload = json.loads(msg[2])
        message = payload.get('message')
        litres = payload.get('litres')
        deviceID = payload.get('deviceID')
        
        hex_address = get_device_Hex(deviceID)
        
        if not hex_address:
            print("Invalid Device ID")
            return

        # Add to Queue for Main Thread to process
        cmd_data = {
            "type": message,
            "addr": hex_address,
            "litres": litres,
            "device_id": deviceID
        }
        
        globals.CMD_QUEUE.append(cmd_data)
        print("queued: {}".format(message))

    except Exception as e:
        print("MQTT Parse Error: {}".format(e))

# ---------------- MQTT INITIALIZATION ---------------- #

mqtt = network.mqtt(
    MQTT_CLIENT_ID, MQTT_BROKER_HOST, user=MQTT_CLIENT_USERNAME, password=MQTT_CLIENT_PASSWORD,
    port=MQTT_BROKER_PORT, autoreconnect=True, clientid=MQTT_CLIENT_ID,
    connected_cb=conncb, disconnected_cb=disconncb, subscribed_cb=subscb,
    published_cb=pubcb, data_cb=datacb
)

def mqttInitialize(mqtt, topic_list):
    loopCount = 10
    mqtt.start()
    utime.sleep(2)

    while mqtt.status()[0] != 2 and loopCount > 0:
        print("MQTT Connecting...")
        utime.sleep(1)
        loopCount -= 1
        
    if mqtt.status()[0] != 2:
        print("MQTT Failed")
        return None
    
    print("MQTT Connected")
    for topic in topic_list:
        mqtt.subscribe(topic)
    return mqtt

def mqttPublish(mqtt, topic, message):
    try:
        mqtt.publish(topic, message)
    except:
        pass