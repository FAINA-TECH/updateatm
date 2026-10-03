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

def _replace_file(tmp_path, dest_path):
    """
    Moves tmp_path over dest_path.

    Tries a plain rename() first: on littlefs/FAT this overwrites an existing
    destination as a single filesystem operation, so there is never a moment
    where dest_path doesn't exist. The old code did uos.remove(dest_path)
    then uos.rename(tmp_path, dest_path) as two separate steps - if the
    device lost power or reset between them (GSM transmit bursts are a known
    brownout trigger on these boards), dest_path was left permanently
    deleted with nothing to replace it. If this fork's rename() refuses to
    overwrite an existing file, we fall back to the old two-step behaviour
    (no worse than before, just no longer guaranteed atomic).
    """
    try:
        uos.rename(tmp_path, dest_path)
    except OSError:
        if file_exists(dest_path):
            uos.remove(dest_path)
        uos.rename(tmp_path, dest_path)

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

def _download_to_temp(fname, retries=3):
    """
    Downloads fname into the temp dir only - never touches the live file.
    Returns the temp path on success, or None after exhausting retries.
    """
    temp_dir = ensure_temp_dir()
    url = UPDATE_URL + "/" + fname
    tmp_path = temp_dir + "/tmp_" + fname

    for attempt in range(1, retries + 1):
        try:
            log("Downloading [{}] (Attempt {}/{})".format(fname, attempt, retries))

            # Use LoBo-style curl.get() with file output
            res_code, hdr, body = curl.get(url, tmp_path)

            if res_code == 0 and "200" in hdr:
                return tmp_path
            else:
                log("❌ Download failed {} (curl code {}, hdr: {})".format(fname, res_code, hdr))

        except Exception as e:
            log("⚠️ Error downloading {}: {}".format(fname, e))

        sleep(3)

    log("⚠️ Giving up on {} after {} attempts".format(fname, retries))
    return None

def download_file(fname, retries=3):
    """
    Downloads fname and replaces the live copy immediately. Kept for single-file
    use; the multi-file system update below stages everything before committing
    instead of calling this per file.
    """
    tmp_path = _download_to_temp(fname, retries)
    if tmp_path is None:
        return False
    _replace_file(tmp_path, "/flash/" + fname)
    log("✅ Updated {}".format(fname))
    return True

def download_and_replace_files(file_list):
    """
    Two-phase system update: download every file in file_list to the temp dir
    first. Only if ALL of them succeed do we commit (rename each into place).

    The old version replaced each file as soon as it downloaded and always
    returned True regardless of how many actually succeeded, so a mid-batch
    network drop could leave the device running a permanent mix of old and
    new modules while believing (via save_local_version, called unconditionally
    by the caller) that it was fully up to date - it would never retry the
    ones that failed.

    Now: nothing on disk changes unless every file downloaded successfully,
    so a failed batch is always safe to retry wholesale on the next check.
    Returns True only when every file was downloaded and committed.
    """
    total = len(file_list)
    staged = {}   # fname -> temp path, only populated once download succeeds

    for i, fname in enumerate(file_list):
        log("Downloading file {}/{}: {}".format(i + 1, total, fname))
        tmp_path = _download_to_temp(fname)
        if tmp_path is None:
            log("⚠️ Update aborted: {} failed to download. No files were changed.".format(fname))
            return False
        staged[fname] = tmp_path
        gc.collect()
        sleep(1)

    log("All {} files downloaded - committing...".format(total))
    for fname, tmp_path in staged.items():
        _replace_file(tmp_path, "/flash/" + fname)
        log("✅ Updated {}".format(fname))

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
        """
        Extract GLOBAL_VERSION from a Python file. Returns None if the file is
        missing, unreadable, or has no GLOBAL_VERSION line - previously this
        fell back to the string "0.0.0", which made a corrupt/truncated
        download indistinguishable from a real, older version.
        """
        try:
            with open(file_path, "r") as f:
                for line in f:
                    if "GLOBAL_VERSION" in line and "=" in line:
                        # Parse: GLOBAL_VERSION = "1.0" -> 1.0
                        return line.split("=")[1].strip().replace('"', "").replace("'", "")
        except OSError:
            pass
        return None

    def _parse_version(v):
        """'3.0.0' -> (3, 0, 0). None if v is missing or not a clean dotted-int version."""
        if not v:
            return None
        try:
            return tuple(int(p) for p in v.split("."))
        except ValueError:
            return None

    def is_newer(new_ver, old_ver):
        """
        True only when both sides parse as versions and new_ver > old_ver.

        The old version used float(new_ver) > float(old_ver), but every real
        GLOBAL_VERSION here is 3-part ("3.0.0"), and float() rejects a string
        with more than one '.'  - so that comparison always raised and fell
        into `except: return new_ver != old_ver`. A version that failed to
        parse (e.g. "0.0.0" from a corrupt download) is simply *different*
        from the current one, so that fallback said "yes, newer" and applied
        it. Now an unparsable version means "unknown" and is rejected rather
        than treated as newer, and a real x.y.z compare is used instead of
        the always-broken float() one.
        """
        a = _parse_version(new_ver)
        b = _parse_version(old_ver)
        if a is None or b is None:
            return False
        return a > b
    # ------------------------------

    current_version = get_version_from_file(dest_path)
    log("Checking globals.py (Current Config Version: {})".format(current_version or "unknown"))

    # --- NEW: Safely disable WDT during slow network request ---
    machine.WDT(False)
    try:
        for attempt in range(1, retries + 1):
            try:
                # Download to temp
                res_code, hdr, body = curl.get(url, tmp_path)

                if res_code == 0 and "200" in hdr:
                    new_version = get_version_from_file(tmp_path)

                    if new_version is None:
                        log("⚠️ Downloaded config unreadable (no GLOBAL_VERSION found) - rejecting, keeping current globals.py")
                        if file_exists(tmp_path): uos.remove(tmp_path)
                        return False  # No update applied; retried on the next check

                    if is_newer(new_version, current_version):
                        log("✅ New Config Found: {} (Old: {})".format(new_version, current_version or "unknown"))
                        _replace_file(tmp_path, dest_path)
                        log("✅ globals.py updated successfully.")
                        return True # Update Occurred
                    else:
                        log("Config up to date (Server: {})".format(new_version))
                        if file_exists(tmp_path): uos.remove(tmp_path)
                        return False # No update needed
                else:
                    if attempt == retries:
                        log("❌ Config Check Failed (Code {})".format(res_code))

            except Exception as e:
                log("⚠️ Config Check Error: {}".format(e))

            sleep(1)

        return False
    finally:
        # Guarantee WDT turns back on even if download crashes
        machine.WDT(True)

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
            else:
                log("⚠️ System update failed - no files changed, version not bumped. Will retry next check.")

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
