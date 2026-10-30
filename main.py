import urllib.request
import urllib.error
import json
import os
import ssl
import time
import gzip
from io import BytesIO
from urllib.parse import urlparse
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE

base_dir = 'api'
os.makedirs(base_dir, exist_ok=True)

# 🔐 GitHub Secrets থেকে ইউআরএল গ্রহণ
API_EVENTS = os.environ.get('API_EVENTS', '').strip()
API_STREAMS_BASE = os.environ.get('API_STREAMS_BASE', '').strip()

if not API_EVENTS or not API_STREAMS_BASE:
    print("❌ Fatal: API_EVENTS বা API_STREAMS_BASE সিক্রেট পাওয়া যায়নি! GitHub Secrets চেক করুন।")
    exit(1)

origin = ""
if API_EVENTS:
    parsed = urlparse(API_EVENTS)
    origin = f"{parsed.scheme}://{parsed.netloc}"

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
    'Accept': 'application/json, text/plain, */*',
    'Accept-Language': 'en-US,en;q=0.9',
    'Origin': origin,
    'Referer': f"{origin}/" if origin else "",
    'Connection': 'close'
}

DEMO_PATTERNS = [
    'unified-streaming.com',
    'tears-of-steel',
    'test-streams.mux.dev',
    'bitmovin.com',
    'commondatastorage.googleapis.com',
    'sample.m3u8'
]

def clean_img(url):
    """'null', 'None' বা খালি স্ট্রিং ফিল্টার করে সঠিক ইমেজ ইউআরএল ফেরত দেয়"""
    if not url:
        return ""
    s = str(url).strip()
    if s.lower() in ("null", "none", "undefined", ""):
        return ""
    return s

def is_real_stream(url):
    if not url or not url.strip():
        return False
    url_lower = url.lower()
    for pattern in DEMO_PATTERNS:
        if pattern in url_lower:
            return False
    return True

def fetch_json(url, timeout=12):
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, context=ctx, timeout=timeout) as response:
            content = response.read()
            if response.info().get('Content-Encoding') == 'gzip' or content[:2] == b'\x1f\x8b':
                content = gzip.GzipFile(fileobj=BytesIO(content)).read()
            return json.loads(content.decode('utf-8'))
    except Exception:
        return None

def fetch_main_events_with_retry(retries=3, timeout=25):
    for attempt in range(1, retries + 1):
        print(f"📡 Requesting schedule from Source API (Attempt {attempt}/{retries})...")
        try:
            req = urllib.request.Request(API_EVENTS, headers=HEADERS)
            with urllib.request.urlopen(req, context=ctx, timeout=timeout) as response:
                content = response.read()
                if response.info().get('Content-Encoding') == 'gzip' or content[:2] == b'\x1f\x8b':
                    content = gzip.GzipFile(fileobj=BytesIO(content)).read()
                data = json.loads(content.decode('utf-8'))
                if data and data.get('status') == 'ok':
                    return data
        except Exception as e:
            print(f"  ⚠️ Attempt {attempt} failed: {e}")
            if attempt < retries:
                time.sleep(3)
    return None

def parse_date(date_str):
    if not date_str:
        return None
    clean = str(date_str).strip()
    if clean.endswith('Z'):
        clean = clean[:-1]
    clean = clean.split('+')[0].strip().replace('T', ' ')
    for fmt in ("%Y/%m/%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(clean, fmt)
        except ValueError:
            continue
    return None

def parse_utc_to_bst(utc_str):
    dt_utc = parse_date(utc_str)
    if dt_utc:
        dt_bst = dt_utc + timedelta(hours=6)
        return dt_bst.strftime("%b %d, %Y, %I:%M:%S %p")
    return ""

def calculate_event_status(start_utc, end_utc, api_status, has_channels):
    api_status_str = str(api_status).strip().lower() if api_status else ""

    if api_status_str in ["finish", "finished", "ended", "completed", "closed"]:
        return "Ended"

    start = parse_date(start_utc)
    now = datetime.now(timezone.utc).replace(tzinfo=None)

    if not start:
        return "Live" if (has_channels and api_status_str == "live") else "Upcoming"

    early_live_threshold = start - timedelta(minutes=15)

    if now >= early_live_threshold or api_status_str == "live":
        return "Live" if has_channels else "Upcoming"

    return "Upcoming"

def parse_stream_url_and_headers(raw_link):
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

def extract_raw_streams(stream_payload):
    if not stream_payload:
        return []
    if isinstance(stream_payload, list):
        return stream_payload
    if isinstance(stream_payload, dict):
        data = stream_payload.get('data')
        if isinstance(data, dict):
            urls = data.get('streamUrls') or data.get('streams') or []
            if urls:
                return urls
        elif isinstance(data, list):
            return data
        
        if 'streamUrls' in stream_payload and isinstance(stream_payload['streamUrls'], list):
            return stream_payload['streamUrls']
        if 'streams' in stream_payload and isinstance(stream_payload['streams'], list):
            return stream_payload['streams']
    return []

def process_channels(raw_streams):
    channels = []
    for stream in raw_streams:
        if not isinstance(stream, dict):
            continue
            
        raw_link = stream.get('link') or stream.get('webLink') or stream.get('url') or ''
        clean_url, headers = parse_stream_url_and_headers(raw_link)
        
        if not is_real_stream(clean_url):
            continue

        channels.append({
            "channel_name": stream.get('title') or stream.get('name') or 'Live Channel',
            "stream_type": detect_stream_type(clean_url, stream.get('type')),
            "url": clean_url,
            "raw_link": raw_link,
            "headers": headers,
            "drm_key": extract_drm_key(stream)
        })
    return channels

def get_status_priority(event):
    status = str(event.get("status", "")).strip().lower()
    if status == "live":
        return 0
    elif status == "upcoming":
        return 1
    return 2

def process_single_match(event):
    title = event.get('title', '')
    slug = event.get('slug', '')
    category = event.get('category', 'Live Events')
    event_info = event.get('eventInfo', {})

    team_a_name = event_info.get("teamA") or (title.split(' vs ')[0] if ' vs ' in title else title)
    team_b_name = event_info.get("teamB") or (title.split(' vs ')[1] if ' vs ' in title else "")
    
    # 🖼️ ইমেজ ও লোগো হ্যান্ডলিং (কখনো "null" আসবে না)
    raw_event_image = clean_img(event.get('image')) or clean_img(event_info.get('image'))
    team_a_flag = clean_img(event_info.get("teamAFlag")) or raw_event_image
    team_b_flag = clean_img(event_info.get("teamBFlag")) or raw_event_image
    
    # লোগো না থাকলে বা null থাকলে team_a এর ফ্ল্যাগ/ইমেজকে লোগো হিসেবে সেট করবে
    logo = raw_event_image or team_a_flag or team_b_flag

    stream_base = API_STREAMS_BASE if API_STREAMS_BASE.endswith('/') else f"{API_STREAMS_BASE}/"
    stream_payload = fetch_json(f"{stream_base}{slug}", timeout=10)
    raw_stream_urls = extract_raw_streams(stream_payload)
    channels = process_channels(raw_stream_urls)
    has_real_channels = len(channels) > 0

    raw_start_utc = event_info.get("startTime", "")
    raw_end_utc = event_info.get("endTime", "")
    api_raw_status = event_info.get("Status") or event.get("status", "")

    final_status = calculate_event_status(
        start_utc=raw_start_utc,
        end_utc=raw_end_utc,
        api_status=api_raw_status,
        has_channels=has_real_channels
    )

    print(f"👉 [{final_status}] {title} | Channels: {len(channels)}")

    return {
        "title": title,
        "sport": event_info.get("eventCat", category),
        "sub_category": event_info.get("eventName", title),
        "status": final_status,
        "is_hot": str(event_info.get("isHot", "0")).lower() in ("1", "true"),
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

def run_sync():
    payload = fetch_main_events_with_retry(retries=3, timeout=25)
    
    if not payload or payload.get('status') != 'ok':
        print("❌ API unresponsive or offline after 3 attempts. Sync cancelled.")
        return

    match_list = payload.get('data', [])
    if not match_list:
        print("⚠️ API returned 0 active matches.")
        return

    print(f"🎉 Successfully fetched {len(match_list)} events from source! Processing streams...")

    with ThreadPoolExecutor(max_workers=10) as executor:
        consolidated_data = list(executor.map(process_single_match, match_list))

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
        
    print(f"\n⚡ Sync completed! Total {len(consolidated_data)} matches saved to: {events_file}")

if __name__ == '__main__':
    run_sync()
