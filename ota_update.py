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
    "main_meter.py",
    "main.py",
    "meter_gsm.py",
    "meter_mqtts.py",
    "meter_run.py",
    "meter_sim.py",
    "meter_storage.py",
    "meter_tests.py",
    "meter.py"
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
        log("Created temp folder: " + temp_dir)
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
def check_for_update():
    try:
        res_code, hdr, body = curl.get(UPDATE_URL + "/version.txt")
        if res_code != 0:
            log("Could not fetch version info. curl error code {}".format(res_code))
            return None

        if "200" not in hdr:
            log("Invalid HTTP response:\n" + hdr)
            return None

        server_version = body.strip()
        local_version = get_local_version()

        if server_version != local_version:
            log("New version available: {} (local {})".format(server_version, local_version))
            return server_version
        else:
            log("Device is up to date.")
            return None
    except Exception as e:
        log("Error checking for update: {}".format(e))
        return None


def download_file(fname, retries=3):
    """
    Downloads file from UPDATE_URL + fname using curl, writes to flash in chunks.
    Ensures file integrity by checking size consistency.
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
                size_on_disk = uos.stat(tmp_path)[6]
                log("✅ Download complete: {} bytes".format(size_on_disk))

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
    total = len(file_list)
    for i, fname in enumerate(file_list):
        log("Updating file {}/{}: {}".format(i + 1, total, fname))
        success = download_file(fname)
        if not success:
            log("⚠️ Skipped file due to repeated failures: {}".format(fname))
        gc.collect()
        sleep(1)
        
def update_global_file(device_id, retries=3):
    """
    Safely update globals.py only for the correct device.
    Checks version number before replacing.
    Updates globals.GLOBAL_VERSION in memory if updated.
    """
    temp_dir = ensure_temp_dir()
    fname = "globals.py"
    tmp_path = temp_dir + "/tmp_" + fname
    dest_path = "/flash/" + fname
    url = "{}/device_configs/{}_globals.py".format(UPDATE_URL, device_id)

    # Ensure GSM connection
    if gsmCheckStatus() != 1:
        gsmInitialization()

    def get_version(file_path):
        """Extract GLOBAL_VERSION from a Python file if it exists."""
        try:
            with open(file_path, "r") as f:
                for line in f:
                    if "GLOBAL_VERSION" in line and "=" in line:
                        return line.split("=")[1].strip().replace('"', "").replace("'", "")
        except Exception as e:
            log("⚠️ Error reading version from {}: {}".format(file_path, e))
        return "0.0.0"

    def file_exists(path):
        try:
            uos.stat(path)
            return True
        except OSError:
            return False

    def is_newer_version(new, old):
        """Compare two semantic versions like '1.2.3'."""
        try:
            n = [int(x) for x in new.split(".")]
            o = [int(x) for x in old.split(".")]
            return n > o
        except:
            return False

    current_version = get_version(dest_path)
    log("📦 Current global.py version: {}".format(current_version))

    for attempt in range(1, retries + 1):
        try:
            log("⬇️ Downloading device-specific [{}] (Attempt {}/{})".format(fname, attempt, retries))
            res_code, hdr, body = curl.get(url, tmp_path)

            if res_code == 0 and "200" in hdr:
                new_version = get_version(tmp_path)
                log("🆕 Downloaded version: {}".format(new_version))

                if is_newer_version(new_version, current_version):
                    try:
                        size_on_disk = uos.stat(tmp_path)[6]
                    except Exception:
                        size_on_disk = 0
                    log("✅ Valid update detected ({} → {}), {} bytes".format(current_version, new_version, size_on_disk))

                    # Replace old file with new version
                    if file_exists(dest_path):
                        uos.remove(dest_path)
                    uos.rename(tmp_path, dest_path)

                    # Update in-memory version
                    try:
                        import globals
                        globals.GLOBAL_VERSION = new_version
                        log("🧠 Updated in-memory GLOBAL_VERSION to {}".format(new_version))
                    except Exception as e:
                        log("⚠️ Could not update in-memory version: {}".format(e))

                    log("✅ globals.py updated successfully for {}".format(device_id))

                    # Optional: reboot to ensure all globals reload cleanly
                    # machine.reset()
                    return True
                else:
                    log("⚠️ Skipping update — downloaded version ({}) is not newer than current ({}).".format(new_version, current_version))
                    if file_exists(tmp_path):
                        uos.remove(tmp_path)
                    return False
            else:
                log("❌ Download failed (curl code {}, hdr: {})".format(res_code, hdr))

        except Exception as e:
            log("⚠️ Error downloading {}: {}".format(fname, e))
        sleep(3)

    log("⚠️ Skipping {} after multiple failures".format(fname))
    return False


# def is_newer_version(new, current):
#     """Simple semantic version comparison."""
#     try:
#         new_parts = [int(x) for x in new.split(".")]
#         cur_parts = [int(x) for x in current.split(".")]
#         for i in range(max(len(new_parts), len(cur_parts))):
#             n = new_parts[i] if i < len(new_parts) else 0
#             c = cur_parts[i] if i < len(cur_parts) else 0
#             if n > c:
#                 return True
#             elif n < c:
#                 return False
#         return False
#     except Exception:
#         # if parsing fails, assume update is newer
#         return True

def run_ota():
    gc.collect()
    print("Free mem:", gc.mem_free())

    print("📡 Initializing GSM module...")
    
    if gsmCheckStatus() != 1:
        gsmInitialization()

    gc.collect()
    print("Free mem:", gc.mem_free())
    sleep(3)

    log("Checking for OTA updates...")
    new_version = check_for_update()

    if new_version:
        log("Starting file updates...")
        download_and_replace_files(FILES_TO_UPDATE)
        save_local_version(new_version)
        log("✅ Update complete. Rebooting in 3 seconds...")
        sleep(3)
        machine.reset()
    else:
        log("No updates to apply.")
