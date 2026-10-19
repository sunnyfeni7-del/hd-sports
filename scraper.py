import urllib.request
import json
import os
import ssl
from datetime import datetime, timezone, timedelta

# SSL certificate bypass (যদি প্রয়োজন হয়)
ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE

base_dir = 'api'
os.makedirs(base_dir, exist_ok=True)

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept': 'application/json',
    'Origin': 'https://vortextv.modsdone.com',
    'Referer': 'https://vortextv.modsdone.com/'
}

API_EVENTS = 'https://vortextv.modsdone.com/cricfy.php/events'
API_STREAMS_BASE = 'https://vortextv.modsdone.com/cricfy.php/streams/'

def fetch_json(url):
    """Helper to download and parse JSON with 15s timeout."""
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, context=ctx, timeout=15) as response:
            return json.loads(response.read().decode('utf-8'))
    except Exception as e:
        print(f"  ⚠️ Error requesting {url}: {str(e)}")
        return None

def parse_date(date_str):
    """Helper to parse datetime from multiple formats."""
    if not date_str:
        return None
    clean = date_str.split('+')[0].strip()
    for fmt in ("%Y/%m/%d %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(clean, fmt)
        except ValueError:
            continue
    return None

def parse_utc_to_bst(utc_str):
    """Converts raw UTC string to BST formatted string."""
    dt_utc = parse_date(utc_str)
    if dt_utc:
        dt_bst = dt_utc + timedelta(hours=6)
        return dt_bst.strftime("%b %d, %Y, %I:%M:%S %p")
    return ""

def auto_detect_status(start_utc, end_utc):
    """Checks current time to determine match status."""
    start = parse_date(start_utc)
    if not start:
        return "Upcoming"
    
    end = parse_date(end_utc) or start
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    
    if start <= now <= end:
        return "Live"
    elif now > end:
        return "Finish"
    return "Upcoming"

def parse_stream_url_and_headers(raw_link):
    """Extracts clean URL and Headers from pipe-separated URLs."""
    if not raw_link:
        return "", {}
        
    if '|' in raw_link:
        parts = raw_link.split('|', 1)
        clean_url = parts[0].strip()
        headers_str = parts[1].strip()
        headers = {}
        for param in headers_str.split('&'):
            if '=' in param:
                k, v = param.split('=', 1)
                headers[k.strip()] = v.strip()
        return clean_url, headers
        
    return raw_link.strip(), {}

def detect_stream_type(url, raw_type=None):
    url_lower = url.lower()
    if '.mpd' in url_lower:
        return "DASH"
    if '.m3u8' in url_lower:
        return "HLS"
    if '.mp4' in url_lower:
        return "MP4"
    if str(raw_type) == "1":
        return "DASH"
    return "HLS"

def extract_drm_key(stream):
    for key in ['drm_key', 'drmKey', 'key', 'clearKey']:
        if stream.get(key):
            return stream[key]
            
    if stream.get('k1') and stream.get('k2'):
        return f"{stream['k1']}:{stream['k2']}"
        
    api_val = stream.get('api', '')
    if api_val and ':' in api_val and len(api_val) >= 32:
        return api_val
        
    return None

def process_channels(raw_streams):
    channels = []
    for stream in raw_streams:
        channel_name = stream.get('title', '')
        raw_link = stream.get('link', '')
        
        clean_url, headers = parse_stream_url_and_headers(raw_link)
        stream_type = detect_stream_type(clean_url, stream.get('type'))
        drm_key = extract_drm_key(stream)

        channels.append({
            "channel_name": channel_name,
            "stream_type": stream_type,
            "url": clean_url,
            "raw_link": raw_link,
            "headers": headers,
            "drm_key": drm_key
        })
    return channels

def get_status_priority(event):
    status = str(event.get("status", "")).strip().lower()
    if status == "live":
        return 0
    elif status == "upcoming":
        return 1
    return 2

def run_sync():
    print("📡 Requesting schedule from VortexTV API...")
    payload = fetch_json(API_EVENTS)
    
    if not payload or payload.get('status') != 'ok':
        print("❌ API unresponsive or offline. Sync cancelled.")
        return

    match_list = payload.get('data', [])
    if not match_list:
        print("⚠️ API returned 0 active matches. Freezing previous database cache!")
        return

    print(f"🎉 Connection Successful! Found {len(match_list)} events. Fetching feeds...")

    consolidated_data = []
    for event in match_list:
        title = event.get('title', '')
        slug = event.get('slug', '')
        category = event.get('category', 'Live Events')
        event_info = event.get('eventInfo', {})

        team_a_name = event_info.get("teamA") or (title.split(' vs ')[0] if ' vs ' in title else title)
        team_b_name = event_info.get("teamB") or (title.split(' vs ')[1] if ' vs ' in title else "")
        team_a_flag = event_info.get("teamAFlag") or event.get('image', '')
        team_b_flag = event_info.get("teamBFlag") or event.get('image', '')
        logo = event.get('image') or team_a_flag

        raw_start_utc = event_info.get("startTime", "")
        raw_end_utc = event_info.get("endTime", "")
        detected_status = auto_detect_status(raw_start_utc, raw_end_utc)
        final_status = event_info.get("Status") or detected_status
        is_hot = str(event_info.get("isHot", "0")).lower() in ("1", "true")

        # Fetch individual stream feeds
        stream_payload = fetch_json(f"{API_STREAMS_BASE}{slug}")
        raw_stream_urls = []
        if stream_payload and stream_payload.get('status') == 'ok':
            raw_stream_urls = stream_payload.get('data', {}).get('streamUrls', [])
            
        channels = process_channels(raw_stream_urls)

        match_entry = {
            "title": title,
            "sport": event_info.get("eventCat", category),
            "sub_category": event_info.get("eventName", title),
            "status": final_status,
            "is_hot": is_hot,
            "slug": slug,
            "schedule": {
                "timezone": "BST (UTC+6)",
                "start_time_bst": parse_utc_to_bst(raw_start_utc),
                "end_time_bst": parse_utc_to_bst(raw_end_utc),
                "raw_start_utc": raw_start_utc
            },
            "teams": {
                "team_a": {"name": team_a_name, "flag": team_a_flag},
                "team_b": {"name": team_b_name, "flag": team_b_flag}
            },
            "logo": logo,
            "channels": channels,
            "total_channels": len(channels)
        }
        consolidated_data.append(match_entry)

    consolidated_data.sort(key=get_status_priority)

    final_output = {
        "last_updated": datetime.now(timezone.utc).isoformat(),
        "event_count": len(consolidated_data),
        "events": {
            "events": consolidated_data
        }
    }

    events_file = os.path.join(base_dir, 'events.json')
    with open(events_file, 'w', encoding='utf-8') as f:
        json.dump(final_output, f, indent=2, ensure_ascii=False)
        
    print(f"\n✅ Sync completed! Total {len(consolidated_data)} events saved to: {events_file}")

if __name__ == '__main__':
    run_sync()
