import network
import utime
import globals
import json
import machine
import _thread

# Global Variables
MQTT_BROKER_HOST = globals.MQTT_BROKER_HOST
MQTT_BROKER_PORT = globals.MQTT_BROKER_PORT
MQTT_CLIENT_USERNAME = globals.MQTT_CLIENT_USERNAME
MQTT_CLIENT_PASSWORD = globals.MQTT_CLIENT_PASSWORD
MQTT_CLIENT_ID = globals.MQTT_CLIENT_ID
MQTT_PUB_TOPIC = globals.MQTT_PUB_TOPIC
MQTT_SUB_TOPICS = globals.MQTT_SUB_TOPICS

# Defensive init: older/unsynced globals.py files may not define this yet.
if not hasattr(globals, 'CANCEL_REQUEST'):
    globals.CANCEL_REQUEST = {}

# QoS 1 (at-least-once), matching the backend's publish/subscribe QoS - kept
# the same on both ends deliberately, since MQTT only guarantees min(sender
# QoS, receiver QoS) anyway, so a mismatch here would silently cap delivery
# at the lower side. Not QoS 2: the broker/client four-way handshake adds
# latency this old REPL's serial link doesn't need, and the backend already
# tolerates the occasional duplicate QoS 1 can produce. Every publish/
# subscribe call tries qos=1 first and transparently falls back to the
# no-qos default if the native binding doesn't accept the keyword, rather
# than assuming and risking a crash.
MQTT_QOS = getattr(globals, 'MQTT_QOS', 1)

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
    for topic in MQTT_SUB_TOPICS:
        try:
            try:
                mqtt.subscribe(topic, qos=MQTT_QOS)
            except TypeError:
                mqtt.subscribe(topic)
            print("Subscribed to: {}".format(topic))
        except Exception as e:
            print("Sub failed for {}: {}".format(topic, e))

def disconncb(task):
    print("[{}] Disconnected. Auto-reconnect pending...".format(task))

def subscb(task):
    print("[{}] Subscribed".format(task))

def pubcb(pub):
    print("[{}] Published: {}".format(pub[0], pub[1]))

def datacb(msg):
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

        # --- IMMEDIATE CANCEL: set the flag directly, bypassing CMD_QUEUE. ---
        # A dispense in progress blocks the main loop from draining the queue,
        # so any command that means "stop the water" must land as a flag the
        # dispense loop itself polls every iteration - not wait in the queue.
        if message in ("cancel_dispense", "valve_close"):
            _thread.lock()
            globals.CANCEL_REQUEST[hex_address] = True
            _thread.unlock()
            print("[Cancel] Requested for addr {} (via {})".format(hex_address, message))

        # Add to Queue for Main Thread to process
        cmd_data = {
            "type": message,
            "addr": hex_address,
            "litres": litres,
            "device_id": deviceID
        }
        
        # --- THREAD LOCK: Safely write to the shared resource ---
        _thread.lock()
        globals.CMD_QUEUE.append(cmd_data)
        _thread.unlock()
        # --------------------------------------------------------
        
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
    return mqtt

def mqttPublish(mqtt, topic, message):
    try:
        mqtt.publish(topic, message, qos=MQTT_QOS)
    except TypeError:
        try:
            mqtt.publish(topic, message)
        except Exception as e:
            print("Publish Error:", e)
    except Exception as e:
        print("Publish Error:", e)