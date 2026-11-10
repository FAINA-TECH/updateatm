# This file is executed on every boot (including wake-boot from deepsleep)
import sys
sys.path[1] = '/flash/lib'

import machine
import utime

_FAIL_COUNT_FILE = '/flash/boot_fail_count.txt'
_MAX_FAST_RETRIES = 5      # this many boot failures in a row retry almost immediately
_BACKOFF_STEP_S = 300      # then back off by 5 more minutes per extra failure...
_MAX_BACKOFF_S = 1800      # ...capped at 30 minutes, but never stop trying entirely -
                           # this device can't reach MQTT to be told to retry, so giving
                           # up permanently would mean only a site visit fixes it.

def _read_fail_count():
    try:
        with open(_FAIL_COUNT_FILE, 'r') as f:
            return int(f.read().strip())
    except Exception:
        return 0

def _write_fail_count(n):
    try:
        with open(_FAIL_COUNT_FILE, 'w') as f:
            f.write(str(n))
    except Exception:
        pass

def _attempt_recovery():
    """
    Best-effort: connect over GSM and force a fresh pull of every application
    file, bypassing the normal version-match gate - a device stuck on a
    mismatched file set (e.g. a past partial OTA) may already have a
    version.txt that claims it's up to date, so the ordinary "is there a
    newer version" check would skip re-downloading. Never raises; any
    failure here just means the reboot below tries again later.
    """
    try:
        from meter_gsm import gsmInitialization
        from ota_update import (
            download_and_replace_files, FILES_TO_UPDATE,
            save_local_version, check_for_system_update,
        )

        print("[BOOT] Recovery: connecting GSM...")
        gsmInitialization()  # blocks until connected, or resets the device itself

        print("[BOOT] Recovery: forcing a full file re-sync...")
        if download_and_replace_files(FILES_TO_UPDATE):
            new_version = check_for_system_update()
            if new_version:
                save_local_version(new_version)
            print("[BOOT] Recovery: files restored.")
        else:
            print("[BOOT] Recovery: download failed (server unreachable or a file 404'd). Nothing changed.")

    except Exception as e:
        print("[BOOT] Recovery attempt itself failed:", e)

try:
    import main
    _write_fail_count(0)   # module set imported cleanly - this failure class is behind us
    main.main()

except Exception as e:
    fail_count = _read_fail_count() + 1
    _write_fail_count(fail_count)
    print("[BOOT] main.py failed to start (failure #{}): {}".format(fail_count, e))

    _attempt_recovery()

    if fail_count <= _MAX_FAST_RETRIES:
        delay = 5
    else:
        delay = min(_BACKOFF_STEP_S * (fail_count - _MAX_FAST_RETRIES), _MAX_BACKOFF_S)

    print("[BOOT] Rebooting in {}s (failure #{})...".format(delay, fail_count))
    utime.sleep(delay)
    machine.reset()
