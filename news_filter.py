import os
import json
import time
from datetime import datetime, timezone, timedelta
import urllib.request
import urllib.error

import config

CACHE_FILE = os.path.join(os.path.dirname(__file__), "logs", "ff_calendar_cache.json")
API_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"

def _fetch_calendar():
    """Fetch structured JSON calendar from ForexFactory"""
    req = urllib.request.Request(
        API_URL, 
        headers={'User-Agent': 'Mozilla/5.0'}
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            data = json.loads(response.read().decode('utf-8'))
            
            os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
            with open(CACHE_FILE, 'w') as f:
                json.dump({"timestamp": time.time(), "data": data}, f)
            return data
    except Exception as e:
        print(f"[NEWS] Failed to fetch live currency calendar: {e}")
        return _read_cache()

def _read_cache():
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, 'r') as f:
                cached = json.load(f)
                return cached.get("data", [])
        except:
            return []
    return []

def get_events():
    """Returns list of events, utilizing short-term cache"""
    if os.path.exists(CACHE_FILE):
        file_age = time.time() - os.path.getmtime(CACHE_FILE)
        # 6 hour cache ttl to avoid spamming the public API
        if file_age < 6 * 3600:
            return _read_cache()
    
    return _fetch_calendar()

def is_news_safe(symbol: str) -> tuple[bool, str]:
    """Returns True if it's safe to trade (outside Red impact window), False otherwise."""
    if not getattr(config, "NEWS_FILTER_ENABLED", False):
        return True, ""
        
    events = get_events()
    now_utc = datetime.now(timezone.utc)
    
    if len(symbol) >= 6:
        base = symbol[:3].upper()
        quote = symbol[3:6].upper()
    else:
        return True, ""
    
    buffer_before = timedelta(minutes=getattr(config, "NEWS_BUFFER_MINUTES_BEFORE", 30))
    buffer_after = timedelta(minutes=getattr(config, "NEWS_BUFFER_MINUTES_AFTER", 30))
    
    for event in events:
        impact = event.get("impact", "")
        if impact != "High":
            continue
            
        currency = event.get("country", "").upper()
        if currency not in [base, quote]:
            continue
            
        date_str = event.get("date", "")
        if not date_str:
            continue
            
        try:
            # Parse standard ISO timezone format (e.g., 2026-04-10T08:30:00-04:00)
            event_dt = datetime.fromisoformat(date_str).astimezone(timezone.utc)
        except ValueError:
            continue
            
        start_buffer = event_dt - buffer_before
        end_buffer = event_dt + buffer_after
        
        if start_buffer <= now_utc <= end_buffer:
            title = event.get("title", "Major Economic Event")
            diff_mins = (event_dt - now_utc).total_seconds() / 60.0
            
            if diff_mins > 0:
                msg = f"News buffer active: {title} ({currency}) in {diff_mins:.0f}m"
            else:
                msg = f"News buffer active: {title} ({currency}) passed {-diff_mins:.0f}m ago"
            
            return False, msg
            
    return True, ""
