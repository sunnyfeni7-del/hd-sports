import urllib.request
import json
import os
import sys
import ssl
import time
import gzip
from io import BytesIO
from datetime import datetime, timezone, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler
import threading

# রেন্ডার কনসোলে ইনস্ট্যান্ট লগ দেখানোর জন্য
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(line_buffering=True)

def log(msg):
    timestamp = datetime.now().strftime("%I:%M:%S %p")
    print(f"[{timestamp}] {msg}", flush=True)

# SSL ভেরিফিকেশন বাইপাস
ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "").strip()
REPO_OWNER = os.environ.get("REPO_OWNER", "sunnyfeni7-del").strip()
REPO_NAME = os.environ.get("REPO_NAME", "hd-sports").strip()

API_EVENTS = 'https://vortextv.modsdone.com/cricfy.php/events'

# 🚀 VortexTV-এর জন্য ১০০% সঠিক হেডার (টাইমআউট রোধে Connection: close অত্যন্ত জরুরি)
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
    'Accept': 'application/json, text/plain, */*',
    'Accept-Language': 'en-US,en;q=0.9',
    'Origin': 'https://vortextv.modsdone.com',
    'Referer': 'https://vortextv.modsdone.com/',
    'Connection': 'close'
}

def parse_date(date_str):
    if not date_str:
        return None
    clean = date_str.split('+')[0].strip()
    for fmt in ("%Y/%m/%d %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(clean, fmt)
        except ValueError:
            continue
    return None

def fetch_events_with_retry(retries=3, timeout=25):
    """৩ বার তাৎক্ষণিক চেষ্টা করবে যাতে সার্ভার স্লো হলেও ডাটা মিস না হয়"""
    for attempt in range(1, retries + 1):
        try:
            log(f"📡 সোর্স API থেকে ডাটা আনা হচ্ছে (চেষ্টা: {attempt}/{retries})...")
            req = urllib.request.Request(API_EVENTS, headers=HEADERS)
            with urllib.request.urlopen(req, context=ctx, timeout=timeout) as res:
                content = res.read()
                # সার্ভার যদি কমপ্রেসড (gzip) ডাটা পাঠায়
                if res.info().get('Content-Encoding') == 'gzip':
                    content = gzip.GzipFile(fileobj=BytesIO(content)).read()
                
                data = json.loads(content.decode('utf-8'))
                if data and data.get('status') == 'ok':
                    return data
        except Exception as e:
            log(f"  ⚠️ চেষ্টা {attempt} ব্যর্থ: {e}")
            if attempt < retries:
                time.sleep(3) # ৩ সেকেন্ড বিরতি দিয়ে আবার চেষ্টা করবে
    return None

def trigger_github_action(reason):
    if not GITHUB_TOKEN:
        log("❌ GITHUB_TOKEN পাওয়া যায়নি! Action ট্রিগার বাতিল।")
        return

    url = f"https://api.github.com/repos/{REPO_OWNER}/{REPO_NAME}/dispatches"
    payload = json.dumps({
        "event_type": "source_data_changed",
        "client_payload": {"reason": reason}
    }).encode('utf-8')

    req = urllib.request.Request(url, data=payload, headers={
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "Render-Watcher"
    }, method="POST")

    try:
        with urllib.request.urlopen(req, timeout=15) as res:
            if res.status in [200, 204]:
                log(f"🚀 [GitHub Action Triggered] কারণ: {reason}")
            else:
                log(f"⚠️ Dispatch failed: HTTP {res.status}")
    except Exception as e:
        log(f"❌ Failed to trigger GitHub Action: {e}")

def get_current_event_states():
    data = fetch_events_with_retry(retries=3, timeout=25)
    if not data:
        return None, {}

    events = data.get('data', [])
    state_map = {}
    summary_counts = {"Live": 0, "Upcoming": 0, "Ended": 0}
    now = datetime.now(timezone.utc).replace(tzinfo=None)

    for ev in events:
        slug = ev.get('slug')
        if not slug:
            continue

        info = ev.get('eventInfo', {})
        api_status = str(info.get('Status') or ev.get('status') or '').strip().lower()

        # ১. সোর্স API যদি ended/finish বলে দেয়
        if api_status in ["finish", "finished", "ended", "completed", "closed"]:
            status = "Ended"
        else:
            start = parse_date(info.get('startTime'))
            # ২. ম্যাচ শুরুর ১৫ মিনিট আগে পৌঁছালে অথবা সোর্স API লাইভ দেখালে
            if start and (now >= (start - timedelta(minutes=15)) or api_status == "live"):
                status = "Live"
            else:
                status = "Upcoming"

        state_map[slug] = status
        summary_counts[status] = summary_counts.get(status, 0) + 1

    return state_map, summary_counts

def detect_changes(previous_state, current_state):
    if previous_state is None:
        return "Initial Startup Sync"

    prev_slugs = set(previous_state.keys())
    curr_slugs = set(current_state.keys())

    added = curr_slugs - prev_slugs
    if added:
        return f"New event(s) added (+{len(added)})"

    removed = prev_slugs - curr_slugs
    if removed:
        return f"Event(s) removed (-{len(removed)})"

    status_changed = []
    for slug in curr_slugs:
        if current_state[slug] != previous_state.get(slug):
            status_changed.append(f"{slug} ({previous_state.get(slug)} ➔ {current_state[slug]})")

    if status_changed:
        return f"Status updated: {', '.join(status_changed)}"

    return None

class HealthServer(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Watcher is actively monitoring API!")
    def log_message(self, format, *args):
        pass

def run_health_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), HealthServer)
    log(f"🌐 Health server listening on port {port}")
    server.serve_forever()

def watcher_loop():
    log("👀 Watcher service started! Monitoring API every 5 minutes...")
    previous_state = None

    while True:
        try:
            current_state, counts = get_current_event_states()
            
            if current_state is not None:
                total_events = len(current_state)
                log(f"📊 চেক সম্পন্ন: মোট {total_events} টি ইভেন্ট পাওয়া গেছে | Live: {counts['Live']}, Upcoming: {counts['Upcoming']}, Ended: {counts['Ended']}")

                change_reason = detect_changes(previous_state, current_state)
                
                if change_reason:
                    log(f"🔔 পরিবর্তন ধরা পড়েছে! ➔ {change_reason}")
                    trigger_github_action(change_reason)
                    previous_state = current_state
                else:
                    log("💤 আগের তালিকার সাথে কোনো পরিবর্তন নেই। GitHub রিকোয়েস্ট পাঠানো হয়নি।")
            else:
                log("⚠️ সোর্স এপিআই থেকে ডাটা পাওয়া যায়নি। ৫ মিনিট পর আবার চেষ্টা করা হবে...")

        except Exception as e:
            log(f"❌ Watcher Loop Error: {e}")

        log("⏳ পরবর্তী চেকের জন্য ৫ মিনিট (৩০০ সেকেন্ড) অপেক্ষা করা হচ্ছে...\n")
        time.sleep(300)

if __name__ == '__main__':
    threading.Thread(target=run_health_server, daemon=True).start()
    watcher_loop()
