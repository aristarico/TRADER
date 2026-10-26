import time
import urllib.request
import json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

_time_offset_seconds = 0
_time_synced = False
_last_sync_attempt = 0
_SYNC_COOLDOWN = 60  # wait 60 seconds before retrying on failure

NY_TZ = ZoneInfo("America/New_York")

def sync():
    """
    Fetch exact time from an online time API (WorldTimeAPI).
    Calculates the offset between the local machine clock and the true online time.
    """
    global _time_offset_seconds, _time_synced, _last_sync_attempt
    
    current_time = time.time()
    if not _time_synced and (current_time - _last_sync_attempt < _SYNC_COOLDOWN):
        return  # Do not spam the API on failure
        
    _last_sync_attempt = current_time
    print("[TIME SYNC] Fetching true New York time from worldclockapi.com...")
    
    try:
        # Using worldclockapi.com as it handles connections better than worldtimeapi.org
        req = urllib.request.Request(
            "http://worldclockapi.com/api/json/est/now",
            headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
        )
        with urllib.request.urlopen(req, timeout=5) as response:
            data = json.loads(response.read().decode())
            # worldclockapi returns "currentDateTime": "2026-05-23T16:34-04:00"
            api_time_str = data['currentDateTime']
            dt = datetime.fromisoformat(api_time_str)
            api_time = dt.timestamp()
            
            local_time = time.time()
            _time_offset_seconds = api_time - local_time
            _time_synced = True
            print(f"[TIME SYNC] Success! Online offset: {_time_offset_seconds:+.2f}s")
    except Exception as e:
        print(f"[TIME SYNC] Failed to sync online time: {e}. Retrying in {_SYNC_COOLDOWN}s. Using local time for now.")
        _time_offset_seconds = 0
        _time_synced = False

def get_utc_now() -> datetime:
    """Returns the true UTC datetime, offset corrected."""
    if not _time_synced:
        # We will attempt one sync if it wasn't done explicitly
        sync()
    corrected_timestamp = time.time() + _time_offset_seconds
    return datetime.fromtimestamp(corrected_timestamp, tz=timezone.utc)

def get_ny_now() -> datetime:
    """Returns the true New York datetime, offset corrected."""
    utc_now = get_utc_now()
    return utc_now.astimezone(NY_TZ)
