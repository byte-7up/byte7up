#!/usr/bin/env python3
import json
import os
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib import error, request
from urllib.parse import urlsplit

STATUSES_TO_BACKUP = {"EXPIRED", "DISABLED", "LIMITED"}
EVENTS_TO_BACKUP = {
    "user.disabled": "DISABLED",
    "user.expired": "EXPIRED",
    "user.limited": "LIMITED",
}
EVENTS_TO_RESTORE = {
    "user.enabled": "ACTIVE",
}

PORT = int(os.getenv("PORT", "3000"))
API_URL = os.getenv("RW_API_URL", "https://panel.example.com/api").rstrip("/")
API_TOKEN = os.getenv("RW_API_TOKEN", "YOUR_API_TOKEN")
BACKUP_SQUAD_UUID = os.getenv("BACKUP_SQUAD_UUID", "backup-squad-uuid")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DATA_FILE = os.path.join(BASE_DIR, "data", "original_squads.json")
DATA_FILE = os.getenv("DATA_PATH", DEFAULT_DATA_FILE)
WEBHOOK_PATH = os.getenv("WEBHOOK_PATH", "/api/v1/remnawave")


def log(message):
    print(message, flush=True)


def normalize_path(path):
    if not path:
        return "/"

    normalized = path if path == "/" else path.rstrip("/")
    return normalized or "/"


def preview_json(data):
    try:
        return json.dumps(data, ensure_ascii=False, default=str)
    except TypeError:
        return repr(data)


def build_patch_urls(user_uuid):
    base_urls = [API_URL]

    if not API_URL.endswith("/api"):
        base_urls.append(f"{API_URL}/api")

    urls = []
    for base_url in base_urls:
        urls.append(f"{base_url}/users")
        urls.append(f"{base_url}/users/{user_uuid}")

    unique_urls = []
    for url in urls:
        if url not in unique_urls:
            unique_urls.append(url)

    return unique_urls


def ensure_list(value):
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def extract_uuid_list(value):
    uuids = []

    for item in ensure_list(value):
        if isinstance(item, str):
            uuids.append(item)
            continue

        if isinstance(item, dict):
            uuid_value = item.get("uuid")
            if isinstance(uuid_value, str):
                uuids.append(uuid_value)

    return uuids


def extract_squad_uuids(user):
    candidate_keys = (
        "activeInternalSquads",
        "active_internal_squads",
        "internalSquads",
        "internal_squads",
        "internalSquadUuids",
        "internal_squad_uuids",
        "squadUuids",
        "squad_uuids",
        "squads",
    )

    for key in candidate_keys:
        value = user.get(key)
        squad_uuids = extract_uuid_list(value)
        if squad_uuids:
            return squad_uuids

    return []


def infer_status(event, user):
    status = user.get("status")
    if isinstance(status, str) and status:
        return status

    if event in EVENTS_TO_BACKUP:
        return EVENTS_TO_BACKUP[event]

    if event in EVENTS_TO_RESTORE:
        return EVENTS_TO_RESTORE[event]

    return ""


def load_original_squads():
    if not os.path.exists(DATA_FILE):
        return {}

    try:
        with open(DATA_FILE, "r", encoding="utf-8") as file_obj:
            data = json.load(file_obj)
    except (OSError, json.JSONDecodeError) as exc:
        log(f"Failed to load state file {DATA_FILE}: {exc}")
        return {}

    if not isinstance(data, dict):
        log(f"State file {DATA_FILE} must contain a JSON object")
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
        log(f"Failed to save state file {DATA_FILE}: {exc}")
        return False

    return True


def patch_user_squad(user_uuid, squads):
    payload_variants = (
        {"uuid": user_uuid, "activeInternalSquads": squads},
        {"uuid": user_uuid, "active_internal_squads": squads},
        {"uuid": user_uuid, "internalSquadUuids": squads},
        {"uuid": user_uuid, "internal_squad_uuids": squads},
        {"uuid": user_uuid, "squadUuids": squads},
        {"uuid": user_uuid, "squad_uuids": squads},
    )

    for url in build_patch_urls(user_uuid):
        for payload in payload_variants:
            data = json.dumps(payload).encode("utf-8")
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
                    log(
                        f"PATCH {url} with {preview_json(payload)}, "
                        f"status={resp.status}, body={body}"
                    )
                    return True
            except error.HTTPError as exc:
                body = exc.read().decode("utf-8", "replace")
                log(
                    f"HTTP error patching via {url} with {preview_json(payload)}: "
                    f"status={exc.code}, body={body}"
                )
            except Exception as exc:
                log(f"Error patching via {url} with {preview_json(payload)}: {exc}")

    return False


class WebhookHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        return

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
            log(f"Ignoring POST to unexpected path: {request_path}")
            self._send_text(404, "Not Found")
            return

        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length)

        try:
            payload = json.loads(body)
        except json.JSONDecodeError as exc:
            log(f"Webhook JSON decode error: {exc}; raw={body.decode('utf-8', 'replace')}")
            self._send_text(400, "Invalid payload")
            return

        if not isinstance(payload, dict):
            log(f"Ignoring webhook with unexpected root type: {type(payload).__name__}")
            self._send_text(200, "Ignored")
            return

        event = payload.get("event", "")
        user = payload.get("data")

        if not isinstance(user, dict):
            log(f"Ignoring webhook with unexpected data payload: {preview_json(payload)}")
            self._send_text(200, "Ignored")
            return

        user_uuid = user.get("uuid")
        if not isinstance(user_uuid, str) or not user_uuid:
            log(f"Ignoring webhook without user uuid: {preview_json(payload)}")
            self._send_text(200, "Ignored")
            return

        username = user.get("username") or user.get("email") or user_uuid
        status = infer_status(event, user)
        squad_uuids = extract_squad_uuids(user)

        log(
            f"Webhook received: event={event or 'unknown'}, "
            f"user={username}, status={status or 'unknown'}, squads={squad_uuids}"
        )

        if status in STATUSES_TO_BACKUP:
            if not squad_uuids:
                log(f"User {username} has no squads in payload, skipping backup")
                self._send_text(200, "Ignored")
                return

            if user_uuid not in original_squads:
                original_squads[user_uuid] = list(squad_uuids)
                if not save_original_squads():
                    original_squads.pop(user_uuid, None)
                    self._send_text(500, "Failed to save original squads")
                    return

                log(f"Saved original squads for {username}: {squad_uuids}")

            if not patch_user_squad(user_uuid, [BACKUP_SQUAD_UUID]):
                self._send_text(502, "Failed to patch user")
                return

        elif status == "ACTIVE":
            squads_to_restore = original_squads.get(user_uuid)
            if squads_to_restore is None:
                log(f"No saved squads for {username}, nothing to restore")
            else:
                if not patch_user_squad(user_uuid, squads_to_restore):
                    self._send_text(502, "Failed to restore user squads")
                    return

                original_squads.pop(user_uuid, None)
                if not save_original_squads():
                    original_squads[user_uuid] = squads_to_restore
                    self._send_text(500, "Failed to update local state")
                    return

                log(f"Restored original squads for {username}: {squads_to_restore}")

        else:
            log(f"Ignoring event={event or 'unknown'} status={status or 'unknown'} for {username}")

        self._send_text(200, "OK")


def run():
    server = HTTPServer(("0.0.0.0", PORT), WebhookHandler)
    log(
        f"Webhook server running on port {PORT}, path {normalize_path(WEBHOOK_PATH)}, "
        f"api_url={API_URL}"
    )
    server.serve_forever()


if __name__ == "__main__":
    run()
