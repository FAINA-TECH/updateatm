import time
import os
import json

# Directory for storing target readings
TARGET_DIR = '/flash/mem'

# ========== Persistent Storage Functions ==========

def save_target_reading(address, value):
    """
    Save target reading for a specific device to its own JSON file.
    Example file: /flash/mem/target_13.json => {"13": 789}
    """
    try:
        # Ensure storage directory exists
        if "mem" not in os.listdir("/flash"):
            os.mkdir(TARGET_DIR)

        filename = TARGET_DIR + "/target_" + str(address) + ".json"
        data = {str(address): value}

        with open(filename, 'w') as f:
            json.dump(data, f)

        print("[Storage] Saved target for Addr %s: %s" % (address, value))

    except Exception as e:
        print("[Storage] ❌ Save Failed Addr %s: %s" % (address, str(e)))


def load_target_reading(address):
    """
    Load target reading for a specific device from its own file.
    Returns None if the file or value doesn't exist.
    """
    try:
        filename = TARGET_DIR + "/target_" + str(address) + ".json"

        try:
            os.stat(filename)
        except OSError:
            # File doesn't exist yet. This is normal for a new setup.
            # We return None so main.py knows to initialize it.
            return None

        with open(filename, 'r') as f:
            data = json.load(f)

        value = data.get(str(address))
        if value is not None:
            print("[Storage] Loaded target for Addr %s: %s" % (address, value))
            return value
        else:
            return None

    except Exception as e:
        print("[Storage] Load Error Addr %s: %s" % (address, str(e)))
        return None