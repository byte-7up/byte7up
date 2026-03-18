#!/usr/bin/env python3
import os
import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib import request

PORT = int(os.getenv("PORT", 3000))
API_URL = os.getenv("RW_API_URL", "https://panel.example.com/api")
API_TOKEN = os.getenv("RW_API_TOKEN", "YOUR_API_TOKEN")
BACKUP_SQUAD_UUID = os.getenv("BACKUP_SQUAD_UUID", "backup-squad-uuid")
DATA_FILE = os.getenv("DATA_PATH", "/data/original_squads.json")

# Загружаем оригинальные squad'ы
if os.path.exists(DATA_FILE):
    with open(DATA_FILE, "r") as f:
        original_squads = json.load(f)
else:
    original_squads = {}

def save_original_squads():
    os.makedirs(os.path.dirname(DATA_FILE), exist_ok=True)
    with open(DATA_FILE, "w") as f:
        json.dump(original_squads, f, indent=2)

def patch_user_squad(user_uuid, squads):
    url = f"{API_URL}/users/{user_uuid}"
    data = json.dumps({"squadUuids": squads}).encode("utf-8")
    req = request.Request(url, data=data, method="PATCH",
                          headers={
                              "Authorization": f"Bearer {API_TOKEN}",
                              "Content-Type": "application/json"
                          })
    try:
        with request.urlopen(req) as resp:
            body = resp.read().decode()
            print(f"PATCH {user_uuid} -> {squads}, status={resp.status}")
    except Exception as e:
        print(f"Error patching user {user_uuid}: {e}")

class WebhookHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        content_length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(content_length)
        try:
            payload = json.loads(body)
            user = payload["data"]
            event = payload.get("event", "")
            print(f"Webhook received: {event} for {user['username']}, status={user['status']}")

            # EXPIRED / DISABLED / LIMITED → backup
            if user["status"] in ["EXPIRED", "DISABLED", "LIMITED"]:
                if user["uuid"] not in original_squads:
                    original_squads[user["uuid"]] = user["squadUuids"]
                    save_original_squads()
                    print(f"Saved original squads for {user['username']}: {user['squadUuids']}")

                patch_user_squad(user["uuid"], [BACKUP_SQUAD_UUID])

            # ACTIVE → restore original squad
            elif user["status"] == "ACTIVE":
                squads_to_restore = original_squads.get(user["uuid"], user["squadUuids"])
                patch_user_squad(user["uuid"], squads_to_restore)
                original_squads.pop(user["uuid"], None)
                save_original_squads()

        except Exception as e:
            print(f"Webhook parse error: {e}")

        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

def run():
    server = HTTPServer(('', PORT), WebhookHandler)
    print(f"Webhook server running on port {PORT}")
    server.serve_forever()

if __name__ == "__main__":
    run()