import curl
import machine
import uos
import gc
import globals
from utime import sleep
from meter_gsm import gsmInitialization, gsmCheckStatus

# ====== Configuration ======
UPDATE_URL = globals.UPDATE_URL
VERSION_FILE = globals.VERSION_FILE

FILES_TO_UPDATE = [
    "boot.py",
    "main.py",
    "meter.py",
    "meter_gsm.py",
    "meter_mqtts.py",
    "meter_run.py",
    "meter_storage.py",
    "ota_update.py"
]

# ====== Utility Functions ======
def log(msg):
    print("[OTA] " + msg)

def file_exists(path):
    try:
        uos.stat(path)
        return True
    except OSError:
        return False

def ensure_temp_dir():
    """Ensure /flash/temp exists for storing temporary OTA files."""
    temp_dir = "/flash/temp/"
    try:
        uos.mkdir(temp_dir)
        # log("Created temp folder: " + temp_dir)
    except OSError:
        pass  # already exists
    return temp_dir

def get_local_version():
    try:
        with open(VERSION_FILE, "r") as f:
            return f.read().strip()
    except:
        return "0.0.0"

def save_local_version(version):
    try:
        with open(VERSION_FILE, "w") as f:
            f.write(version)
        log("Local version updated to " + version)
    except Exception as e:
        log("Failed to write version file: {}".format(e))

# ====== OTA Logic ======
def check_for_system_update():
    """
    Checks version.txt on server against local version.
    Returns the new version string if update available, else None.
    """
    try:
        res_code, hdr, body = curl.get(UPDATE_URL + "/version.txt")
        if res_code != 0:
            log("Could not fetch version info. curl error code {}".format(res_code))
            return None

        if "200" not in hdr:
            log("Invalid HTTP response for version check")
            return None

        server_version = body.strip()
        local_version = get_local_version()

        if server_version != local_version:
            log("New System Version: {} (Current: {})".format(server_version, local_version))
            return server_version
        else:
            log("System Firmware is up to date ({})".format(local_version))
            return None
    except Exception as e:
        log("Error checking for update: {}".format(e))
        return None

def download_file(fname, retries=3):
    """
    Downloads file from UPDATE_URL + fname using curl.
    Returns True if success.
    """
    temp_dir = ensure_temp_dir()
    url = UPDATE_URL + "/" + fname
    tmp_path = temp_dir + "/tmp_" + fname
    dest_path = "/flash/" + fname

    for attempt in range(1, retries + 1):
        try:
            log("Downloading [{}] (Attempt {}/{})".format(fname, attempt, retries))

            # Use LoBo-style curl.get() with file output
            res_code, hdr, body = curl.get(url, tmp_path)

            if res_code == 0 and "200" in hdr:
                # Safely replace the old file
                if file_exists(dest_path):
                    uos.remove(dest_path)
                uos.rename(tmp_path, dest_path)
                log("✅ Updated {}".format(fname))
                return True
            else:
                log("❌ Download failed {} (curl code {}, hdr: {})".format(fname, res_code, hdr))

        except Exception as e:
            log("⚠️ Error downloading {}: {}".format(fname, e))

        sleep(3)

    log("⚠️ Skipping {} after multiple failures".format(fname))
    return False

def download_and_replace_files(file_list):
    """
    Iterates through the list and downloads files.
    Returns True if the process completed (even if some files skipped, we treat as 'attempted update').
    """
    total = len(file_list)
    success_count = 0
    
    for i, fname in enumerate(file_list):
        log("Updating file {}/{}: {}".format(i + 1, total, fname))
        
        if download_file(fname):
            success_count += 1
        gc.collect()
        log("Free RAM after {}: {} bytes".format(fname, gc.mem_free()))        
        sleep(1)
    return True

def update_global_file(device_id, retries=3):
    """
    Safely update globals.py only for the correct device.
    Checks version number inside the remote file before replacing.
    Returns True if an update actually occurred.
    """
    temp_dir = ensure_temp_dir()
    fname = "globals.py"
    tmp_path = temp_dir + "/tmp_" + fname
    dest_path = "/flash/" + fname
    url = "{}/device_configs/{}_globals.py".format(UPDATE_URL, device_id)

    # --- Helper Inner Functions ---
    def get_version_from_file(file_path):
        """Extract GLOBAL_VERSION from a Python file if it exists."""
        try:
            with open(file_path, "r") as f:
                for line in f:
                    if "GLOBAL_VERSION" in line and "=" in line:
                        # Parse: GLOBAL_VERSION = "1.0" -> 1.0
                        return line.split("=")[1].strip().replace('"', "").replace("'", "")
        except:
            pass
        return "0.0.0"

    def is_newer(new_ver, old_ver):
        try:
            # Simple float comparison or string comparison
            # Assuming format "1.1" or "1.2"
            return float(new_ver) > float(old_ver)
        except:
            return new_ver != old_ver
    # ------------------------------

    current_version = get_version_from_file(dest_path)
    log("Checking globals.py (Current Config Version: {})".format(current_version))

    for attempt in range(1, retries + 1):
        try:
            # Download to temp
            res_code, hdr, body = curl.get(url, tmp_path)

            if res_code == 0 and "200" in hdr:
                new_version = get_version_from_file(tmp_path)
                
                if is_newer(new_version, current_version):
                    log("✅ New Config Found: {} (Old: {})".format(new_version, current_version))
                    
                    if file_exists(dest_path):
                        uos.remove(dest_path)
                    uos.rename(tmp_path, dest_path)
                    
                    log("✅ globals.py updated successfully.")
                    return True # Update Occurred
                else:
                    log("Config up to date (Server: {})".format(new_version))
                    if file_exists(tmp_path): 
                        uos.remove(tmp_path)
                    return False # No update needed
            else:
                if attempt == retries:
                    log("❌ Config Check Failed (Code {})".format(res_code))

        except Exception as e:
            log("⚠️ Config Check Error: {}".format(e))
        
        sleep(1)

    return False

# ====== MAIN RUN FUNCTION ======
def run_ota():
    # --- NEW: Safely disable WDT during entire OTA process ---
    machine.WDT(False)
    try:
        gc.collect()
        print("Free mem:", gc.mem_free())

        print("📡 Initializing GSM module for OTA...")
        
        if gsmCheckStatus() != 1:
            gsmInitialization()

        gc.collect()
        sleep(2)

        reboot_required = False
        
        # --- 1. Global Config Check ---
        log("--- Step 2: Device Configuration ---")
        device_id = globals.MQTT_CLIENT_ID 
        
        if update_global_file(device_id):
            reboot_required = True
            log("✅ Device configuration updated.")

        # --- 2. System Update Check ---
        log("--- Step 1: System Firmware ---")
        new_system_version = check_for_system_update()
        
        if new_system_version:
            log("Starting System Update...")
            if download_and_replace_files(FILES_TO_UPDATE):
                save_local_version(new_system_version)
                reboot_required = True
                log("✅ System files updated.")

        # --- 3. Final Decision ---
        if reboot_required:
            log("🔄 UPDATES APPLIED. REBOOTING IN 3 SECONDS...")
            sleep(3)
            machine.reset()
        else:
            log("✅ No updates found. Continuing normal boot.")
            
    finally:
        # Only reached if no update is applied or download crashes
        machine.WDT(True)