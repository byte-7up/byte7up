#!/usr/bin/env python3
import json
import os
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib import error, request
from urllib.parse import urlsplit

STATUSES_TO_BACKUP = {"EXPIRED", "DISABLED", "LIMITED"}

PORT = int(os.getenv("PORT", "3000"))
API_URL = os.getenv("RW_API_URL", "https://panel.example.com/api").rstrip("/")
API_TOKEN = os.getenv("RW_API_TOKEN", "YOUR_API_TOKEN")
BACKUP_SQUAD_UUID = os.getenv("BACKUP_SQUAD_UUID", "backup-squad-uuid")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DATA_FILE = os.path.join(BASE_DIR, "data", "original_squads.json")
DATA_FILE = os.getenv("DATA_PATH", DEFAULT_DATA_FILE)
WEBHOOK_PATH = os.getenv("WEBHOOK_PATH", "/api/v1/remnawave")


def normalize_path(path):
    if not path:
        return "/"

    normalized = path if path == "/" else path.rstrip("/")
    return normalized or "/"


def load_original_squads():
    if not os.path.exists(DATA_FILE):
        return {}

    try:
        with open(DATA_FILE, "r", encoding="utf-8") as file_obj:
            data = json.load(file_obj)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"Failed to load state file {DATA_FILE}: {exc}")
        return {}

    if not isinstance(data, dict):
        print(f"State file {DATA_FILE} must contain a JSON object")
        return {}

    return data


original_squads = load_original_squads()


def save_original_squads():
    directory = os.path.dirname(DATA_FILE)

    try:
        if directory:
            os.makedirs(directory, exist_ok=True)

        with open(DATA_FILE, "w", encoding="utf-8") as file_obj:
            json.dump(original_squads, file_obj, indent=2, ensure_ascii=False)
    except OSError as exc:
        print(f"Failed to save state file {DATA_FILE}: {exc}")
        return False

    return True


def patch_user_squad(user_uuid, squads):
    url = f"{API_URL}/users/{user_uuid}"
    data = json.dumps({"squadUuids": squads}).encode("utf-8")
    req = request.Request(
        url,
        data=data,
        method="PATCH",
        headers={
            "Authorization": f"Bearer {API_TOKEN}",
            "Content-Type": "application/json",
        },
    )

    try:
        with request.urlopen(req) as resp:
            body = resp.read().decode("utf-8", "replace")
            print(f"PATCH {user_uuid} -> {squads}, status={resp.status}, body={body}")
            return True
    except error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        print(
            f"HTTP error patching user {user_uuid}: "
            f"status={exc.code}, body={body}"
        )
    except Exception as exc:
        print(f"Error patching user {user_uuid}: {exc}")

    return False


class WebhookHandler(BaseHTTPRequestHandler):
    def _send_text(self, status_code, body):
        encoded_body = body.encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded_body)))
        self.end_headers()
        self.wfile.write(encoded_body)

    def do_POST(self):
        request_path = normalize_path(urlsplit(self.path).path)
        expected_path = normalize_path(WEBHOOK_PATH)

        if request_path != expected_path:
            print(f"Ignoring POST to unexpected path: {request_path}")
            self._send_text(404, "Not Found")
            return

        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length)

        try:
            payload = json.loads(body)
            user = payload["data"]
            event = payload.get("event", "")
            user_uuid = user["uuid"]
            username = user.get("username", user_uuid)
            status = user["status"]
            squad_uuids = user["squadUuids"]
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            print(f"Webhook parse error: {exc}")
            self._send_text(400, "Invalid payload")
            return

        print(f"Webhook received: {event} for {username}, status={status}")

        if status in STATUSES_TO_BACKUP:
            if user_uuid not in original_squads:
                original_squads[user_uuid] = list(squad_uuids)
                if not save_original_squads():
                    original_squads.pop(user_uuid, None)
                    self._send_text(500, "Failed to save original squads")
                    return

                print(f"Saved original squads for {username}: {squad_uuids}")

            if not patch_user_squad(user_uuid, [BACKUP_SQUAD_UUID]):
                self._send_text(502, "Failed to patch user")
                return

        elif status == "ACTIVE":
            squads_to_restore = original_squads.get(user_uuid)
            if squads_to_restore is None:
                print(f"No saved squads for {username}, nothing to restore")
            else:
                if not patch_user_squad(user_uuid, squads_to_restore):
                    self._send_text(502, "Failed to restore user squads")
                    return

                original_squads.pop(user_uuid, None)
                if not save_original_squads():
                    original_squads[user_uuid] = squads_to_restore
                    self._send_text(500, "Failed to update local state")
                    return

                print(f"Restored original squads for {username}: {squads_to_restore}")

        else:
            print(f"Ignoring status {status} for {username}")

        self._send_text(200, "OK")


def run():
    server = HTTPServer(("0.0.0.0", PORT), WebhookHandler)
    print(f"Webhook server running on port {PORT}, path {normalize_path(WEBHOOK_PATH)}")
    server.serve_forever()


if __name__ == "__main__":
    run()
