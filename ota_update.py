import curl
import machine
import uos
import gc
import globals
from utime import sleep

# boot.py's crash-recovery path imports THIS module specifically because it
# must never depend on meter_net.py/meter_wifi.py (see boot.py's
# _attempt_recovery() docstring) - a bare `from meter_net import ...` here
# would break that guarantee transitively, since recovery imports this
# module to get at download_and_replace_files()/get_file_list(). Prefer
# meter_net when it's actually there (WiFi-aware, used by the normal
# main.py-driven update path); fall back to meter_gsm directly otherwise,
# under the same two names, so the rest of this file doesn't need to care
# which one it got.
try:
    from meter_net import netCheckStatus, netReconnect
except ImportError:
    from meter_gsm import gsmInitialization, gsmCheckStatus
    netCheckStatus = gsmCheckStatus
    netReconnect = gsmInitialization

# ====== Configuration ======
UPDATE_URL = globals.UPDATE_URL
VERSION_FILE = globals.VERSION_FILE
MANIFEST_URL = UPDATE_URL + "/manifest.txt"

# Fallback ONLY - used when manifest.txt can't be fetched (doesn't exist
# yet, or this particular request fails). The authoritative, current file
# list should always come from the server manifest instead - see
# get_file_list(). Keep this in sync with manifest.txt in the repo root
# anyway, since it's what every device without network falls back to.
FILES_TO_UPDATE = [
    "boot.py",
    "main.py",
    "meter.py",
    "meter_gsm.py",
    "meter_net.py",
    "meter_wifi.py",
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
    Moves tmp_path over dest_path. Returns True/False - never raises, so a
    commit-phase failure on one file can be handled by the caller instead
    of crashing the whole batch loop with files already committed before it
    and files still pending after it (previously: the fallback branch's
    second rename() call was unguarded, so if IT also failed, the exception
    propagated out of this function uncaught).

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
        return True
    except OSError:
        pass

    try:
        if file_exists(dest_path):
            uos.remove(dest_path)
        uos.rename(tmp_path, dest_path)
        return True
    except OSError as e:
        log("⚠️ Could not commit {} -> {}: {}".format(tmp_path, dest_path, e))
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

def _fetch_manifest():
    """
    Fetches the authoritative, CURRENT file list from the server, fresh,
    every call. Returns a list of filenames, or None if the manifest isn't
    reachable (doesn't exist yet, 404, network error, empty body) - callers
    must fall back to FILES_TO_UPDATE in that case, not treat None as "no
    files to update".

    Why this exists: FILES_TO_UPDATE is baked into THIS file, so a device
    is always reading its own, possibly-stale copy of that list - it has
    no way to find out about a filename added after its own ota_update.py
    was last updated, no matter how many times it checks. A device that
    updates main.py (because main.py already existed in its stale list)
    to a version that imports a brand new module (not in that same stale
    list) ends up with a main.py it can no longer boot - this is exactly
    what happened to a device in the field. A manifest fetched fresh from
    the server every time has no such blind spot, because it was never
    baked into any device's code in the first place.
    """
    try:
        res_code, hdr, body = curl.get(MANIFEST_URL)
        if res_code != 0 or "200" not in hdr:
            log("Manifest not available (code {}, hdr {}) - using built-in file list.".format(res_code, hdr))
            return None

        names = [line.strip() for line in body.split("\n") if line.strip()]
        if not names:
            log("Manifest fetched but empty - using built-in file list.")
            return None

        return names
    except Exception as e:
        log("Manifest fetch error: {} - using built-in file list.".format(e))
        return None

def get_file_list():
    """
    The ONE place anything in this codebase should get the list of files
    to update - never read FILES_TO_UPDATE directly (see _fetch_manifest()
    for why). Backward compatible by construction: if manifest.txt doesn't
    exist on the server at all, or this device simply can't reach it right
    now, this silently returns the same FILES_TO_UPDATE list callers would
    have used anyway - nothing breaks for a device or a server that
    predates manifests existing.
    """
    return _fetch_manifest() or FILES_TO_UPDATE

def _download_to_temp(fname, retries=3):
    """
    Downloads fname into the temp dir only - never touches the live file.
    Returns the temp path on success, or None after exhausting retries.
    """
    temp_dir = ensure_temp_dir()
    url = UPDATE_URL + "/" + fname
    # ensure_temp_dir() already returns a trailing slash - a second "/" here
    # produced "/flash/temp//tmp_fname" (harmless on this filesystem, but
    # wrong) on every single download, new files included.
    tmp_path = temp_dir + "tmp_" + fname

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
    committed = []
    for fname, tmp_path in staged.items():
        if not _replace_file(tmp_path, "/flash/" + fname):
            # Every file downloaded fine - this is a local filesystem write
            # failing mid-commit, which should be rare, but leaving it alone
            # here means continuing the loop with some files already on the
            # new version and the rest (including this one) still on the
            # old one - the exact kind of mixed, half-updated state that
            # strands a device with no way to reach this code again. Reset
            # immediately instead: boot.py's own GSM-only recovery doesn't
            # depend on anything that could be in that mixed state, and will
            # retry this whole batch from scratch on the very next boot.
            log("🛑 CRITICAL: commit failed on {} after already committing {}. "
                "Resetting so recovery can retry the full batch.".format(
                    fname, committed or "nothing yet"))
            machine.reset()
        committed.append(fname)
        log("✅ Updated {}".format(fname))

    return True

def verify_files_reachable(file_list):
    """
    Pre-flight only - never commits or touches any live file. Attempts to
    download every file in file_list (including ones that have never
    existed on this device before - new files are fetched exactly the same
    way as existing ones, there's no special case for them) to the temp
    dir, then deletes each temp file immediately whether it succeeded or
    not. Logs and returns a {fname: True/False} dict.

    Run this deliberately - from a REPL on a bench device, or just by
    hitting UPDATE_URL + "/" + fname for each file from any machine with
    network access - BEFORE trusting a real run_ota() cycle on a device
    you can't physically reach. download_and_replace_files() already
    guarantees it won't partially apply a batch where something's
    unreachable; this just lets you confirm that in advance, with nothing
    at stake, instead of finding out during a real update.
    """
    results = {}
    for fname in file_list:
        tmp_path = _download_to_temp(fname, retries=1)
        ok = tmp_path is not None
        results[fname] = ok
        if tmp_path and file_exists(tmp_path):
            try:
                uos.remove(tmp_path)
            except OSError:
                pass
        log("{} {}".format("✅" if ok else "❌", fname))

    failed = [f for f, ok in results.items() if not ok]
    if failed:
        log("Pre-flight FAILED for: {}".format(", ".join(failed)))
    else:
        log("Pre-flight OK: all {} files reachable.".format(len(file_list)))
    return results

def update_global_file(device_id, retries=3):
    """
    Safely update globals.py only for the correct device.
    Checks version number inside the remote file before replacing.
    Returns True if an update actually occurred.
    """
    temp_dir = ensure_temp_dir()
    fname = "globals.py"
    tmp_path = temp_dir + "tmp_" + fname
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
                        if not _replace_file(tmp_path, dest_path):
                            log("⚠️ Failed to commit new globals.py - keeping current one, will retry next check.")
                            return False
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

        print("📡 Ensuring connectivity for OTA...")

        if netCheckStatus() != 1:
            netReconnect()

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
            if download_and_replace_files(get_file_list()):
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
