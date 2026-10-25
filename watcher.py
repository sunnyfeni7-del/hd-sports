import urllib.request
import json
import os
import ssl
import time
from datetime import datetime, timezone, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler
import threading

# SSL কনটেক্সট
ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE

# কনফিগারেশন (Render Environment Variables থেকে নিবে)
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "").strip()
REPO_OWNER = os.environ.get("REPO_OWNER", "sunnyfeni7-del").strip()
REPO_NAME = os.environ.get("REPO_NAME", "hd-sports").strip()

API_EVENTS = 'https://vortextv.modsdone.com/cricfy.php/events'
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)',
    'Accept': 'application/json'
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

def fetch_json(url):
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, context=ctx, timeout=15) as res:
            return json.loads(res.read().decode('utf-8'))
    except Exception as e:
        print(f"⚠️ API Fetch Error: {e}")
        return None

def trigger_github_action(reason):
    """GitHub Action ট্রিগার করার জন্য repository_dispatch রিকোয়েস্ট পাঠায়।"""
    if not GITHUB_TOKEN:
        print("❌ Error: GITHUB_TOKEN সেট করা নেই! রিকোয়েস্ট পাঠানো যাচ্ছে না।")
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
                print(f"🚀 [GitHub Action Triggered] কারণ: {reason}")
            else:
                print(f"⚠️ Dispatch failed with HTTP status: {res.status}")
    except Exception as e:
        print(f"❌ Failed to trigger GitHub Action: {e}")

def get_current_event_states():
    """API থেকে ইভেন্ট তালিকা এনে তাদের বর্তমান স্ট্যাটাস ম্যাপ তৈরি করে।"""
    data = fetch_json(API_EVENTS)
    if not data or data.get('status') != 'ok':
        return None

    events = data.get('data', [])
    state_map = {}
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
            # ২. ম্যাচ শুরুর ১৫ মিনিট আগে পৌঁছালে অথবা API লাইভ দেখালে
            if start and (now >= (start - timedelta(minutes=15)) or api_status == "live"):
                status = "Live"
            else:
                status = "Upcoming"

        state_map[slug] = status

    return state_map

def detect_changes(previous_state, current_state):
    """আগের স্টেট এবং নতুন স্টেটের মধ্যে পার্থক্য শনাক্ত করে।"""
    if previous_state is None:
        return "Initial Startup Sync"

    prev_slugs = set(previous_state.keys())
    curr_slugs = set(current_state.keys())

    # ইভেন্ট যোগ হয়েছে কি না
    added = curr_slugs - prev_slugs
    if added:
        return f"New event(s) added: {len(added)}"

    # ইভেন্ট মুছে ফেলা হয়েছে কি না
    removed = prev_slugs - curr_slugs
    if removed:
        return f"Event(s) removed: {len(removed)}"

    # কোনো ম্যাচের স্ট্যাটাস (Upcoming -> Live বা Live -> Ended) পরিবর্তন হয়েছে কি না
    status_changed = []
    for slug in curr_slugs:
        if current_state[slug] != previous_state.get(slug):
            status_changed.append(f"{slug}: {previous_state.get(slug)} -> {current_state[slug]}")

    if status_changed:
        return f"Status transition: {', '.join(status_changed)}"

    return None

# Render Free Web Service-এর জন্য হেলথ সার্ভার
class HealthServer(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Watcher is actively monitoring API!")
    def log_message(self, format, *args):
        pass

def run_health_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), HealthServer)
    print(f"🌐 Health server running on port {port}")
    server.serve_forever()

def watcher_loop():
    print("👀 Watcher service started! Monitoring every 5 minutes...")
    previous_state = None

    while True:
        try:
            current_state = get_current_event_states()
            if current_state is not None:
                change_reason = detect_changes(previous_state, current_state)
                if change_reason:
                    print(f"🔔 পরিবর্তন ধরা পড়েছে! -> {change_reason}")
                    trigger_github_action(change_reason)
                    previous_state = current_state
                else:
                    print(f"[{datetime.now().strftime('%H:%M:%S')}] কোনো পরিবর্তন নেই। স্লিপিং...")
            else:
                print("⚠️ API রেসপন্স দেয়নি, পরবর্তী চেকের জন্য অপেক্ষা করছি...")
        except Exception as e:
            print(f"❌ Watcher Error: {e}")

        # ৫ মিনিট (৩০০ সেকেন্ড) অপেক্ষা
        time.sleep(300)

if __name__ == '__main__':
    # হেলথ সার্ভার আলাদা থ্রেডে চালু
    threading.Thread(target=run_health_server, daemon=True).start()
    # মূল ওয়াচার লুপ চালু
    watcher_loop()
