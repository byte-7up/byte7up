#!/usr/bin/env python3
import json
import os
import tempfile
import zlib
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib import error, request
from urllib.parse import urlsplit


def getenv_int(name, default):
    value = os.getenv(name)
    if value in (None, ""):
        return default

    try:
        return int(value)
    except ValueError:
        print(f"Invalid integer for {name}: {value!r}, using {default}", flush=True)
        return default


STATUSES_TO_BACKUP = {"EXPIRED", "LIMITED"}
EVENTS_TO_BACKUP = {
    "user.expired": "EXPIRED",
    "user.limited": "LIMITED",
}
EVENTS_TO_RESTORE = {
    "user.enabled": "ACTIVE",
}
SUPPORTED_USER_EVENTS = set(EVENTS_TO_BACKUP) | set(EVENTS_TO_RESTORE) | {
    "user.modified",
}

PORT = int(os.getenv("PORT", "3000"))
API_URL = os.getenv("RW_API_URL", "https://panel.example.com/api").rstrip("/")
API_TOKEN = os.getenv("RW_API_TOKEN", "YOUR_API_TOKEN")
BACKUP_SQUAD_UUID = os.getenv("BACKUP_SQUAD_UUID", "backup-squad-uuid")
TEMP_ACTIVE_DAYS = getenv_int("TEMP_ACTIVE_DAYS", 3)
TEMP_ACTIVE_TRAFFIC_LIMIT_MB = max(0, getenv_int("TEMP_ACTIVE_TRAFFIC_LIMIT_MB", 300))
TEMP_ACTIVE_TRAFFIC_LIMIT_BYTES = (
    TEMP_ACTIVE_TRAFFIC_LIMIT_MB * 1024 * 1024
    if TEMP_ACTIVE_TRAFFIC_LIMIT_MB > 0
    else None
)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DATA_FILE = os.path.join(BASE_DIR, "data", "original_squads.json")
DATA_FILE = os.getenv("DATA_PATH", DEFAULT_DATA_FILE)
WEBHOOK_PATH = os.getenv("WEBHOOK_PATH", "/api/v1/remnawave")

SUBSCRIPTION_PROFILE_ALIASES = {
    "expire_at": (
        "expireAt",
        "expire_at",
        "subscriptionExpireAt",
        "subscription_expire_at",
    ),
    "traffic_limit_bytes": (
        "trafficLimitBytes",
        "traffic_limit_bytes",
    ),
    "traffic_limit_strategy": (
        "trafficLimitStrategy",
        "traffic_limit_strategy",
        "trafficResetStrategy",
        "traffic_reset_strategy",
    ),
    "hwid_device_limit": (
        "hwidDeviceLimit",
        "hwid_device_limit",
        "deviceLimit",
        "device_limit",
        "maxHwidDevices",
        "max_hwid_devices",
    ),
}


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


def parse_datetime_value(value):
    if value in (None, ""):
        return None

    if isinstance(value, (int, float)):
        timestamp = float(value)
        if timestamp > 1_000_000_000_000:
            timestamp /= 1000

        try:
            return datetime.fromtimestamp(timestamp, tz=timezone.utc)
        except (OSError, OverflowError, ValueError):
            return None

    if not isinstance(value, str):
        return None

    normalized = value.strip()
    if not normalized:
        return None

    if normalized.endswith("Z"):
        normalized = f"{normalized[:-1]}+00:00"

    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)

    return parsed.astimezone(timezone.utc)


def parse_int_value(value):
    if value in (None, "") or isinstance(value, bool):
        return None

    if isinstance(value, int):
        return value

    if isinstance(value, float):
        return int(value)

    if isinstance(value, str):
        normalized = value.strip()
        if not normalized:
            return None

        try:
            return int(normalized)
        except ValueError:
            return None

    return None


def format_datetime_value(value):
    return (
        value.astimezone(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def datetimes_equal(left, right):
    if left is None or right is None:
        return False

    return int(left.timestamp()) == int(right.timestamp())


def normalize_json_value(value):
    if isinstance(value, str):
        parsed_datetime = parse_datetime_value(value)
        if parsed_datetime is not None:
            return format_datetime_value(parsed_datetime)

    if isinstance(value, dict):
        return {
            key: normalize_json_value(value[key])
            for key in sorted(value)
        }

    if isinstance(value, list):
        normalized_items = [normalize_json_value(item) for item in value]
        return sorted(
            normalized_items,
            key=lambda item: json.dumps(item, ensure_ascii=False, sort_keys=True),
        )

    return value


def build_patch_urls(user_uuid):
    base_urls = [API_URL]

    if not API_URL.endswith("/api"):
        base_urls.append(f"{API_URL}/api")

    urls = []
    for base_url in base_urls:
        urls.append(f"{base_url}/users/")
        urls.append(f"{base_url}/users")

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


def extract_expire_at(user):
    candidate_keys = (
        "expireAt",
        "expire_at",
        "subscriptionExpireAt",
        "subscription_expire_at",
    )

    for key in candidate_keys:
        expire_at = parse_datetime_value(user.get(key))
        if expire_at is not None:
            return expire_at

    return None


def extract_subscription_profile(user):
    profile = {}

    for canonical_key, aliases in SUBSCRIPTION_PROFILE_ALIASES.items():
        for alias in aliases:
            if alias not in user:
                continue

            value = user.get(alias)
            if value is None:
                continue

            if canonical_key == "expire_at":
                parsed_expire_at = parse_datetime_value(value)
                if parsed_expire_at is None:
                    continue
                profile[canonical_key] = format_datetime_value(parsed_expire_at)
            else:
                profile[canonical_key] = normalize_json_value(value)
            break

    return profile


def extract_used_traffic_bytes(user):
    candidate_values = [
        user.get("usedTrafficBytes"),
        user.get("used_traffic_bytes"),
    ]

    nested_user_traffic = user.get("userTraffic") or user.get("user_traffic")
    if isinstance(nested_user_traffic, dict):
        candidate_values.extend(
            (
                nested_user_traffic.get("usedTrafficBytes"),
                nested_user_traffic.get("used_traffic_bytes"),
            )
        )

    for candidate_value in candidate_values:
        used_traffic_bytes = parse_int_value(candidate_value)
        if used_traffic_bytes is not None:
            return max(0, used_traffic_bytes)

    return None


def build_subscription_profile_from_expire_at(expire_at):
    if expire_at is None:
        return {}

    return {"expire_at": format_datetime_value(expire_at)}


def profile_matches_reference(current_profile, reference_profile):
    if not current_profile or not reference_profile:
        return False

    for key, value in current_profile.items():
        if key not in reference_profile:
            return False
        if reference_profile[key] != value:
            return False

    return True


def infer_status(event, user):
    status = user.get("status")
    if isinstance(status, str) and status:
        return status

    if event in EVENTS_TO_BACKUP:
        return EVENTS_TO_BACKUP[event]

    if event in EVENTS_TO_RESTORE:
        return EVENTS_TO_RESTORE[event]

    return ""


def normalize_squad_uuids(squad_uuids):
    normalized = {
        squad_uuid
        for squad_uuid in ensure_list(squad_uuids)
        if isinstance(squad_uuid, str) and squad_uuid
    }
    return sorted(normalized)


def squads_match(current_squads, target_squads):
    return normalize_squad_uuids(current_squads) == normalize_squad_uuids(target_squads)


def build_user_state(original_squads, status="", expire_at=None):
    user_state = {
        "original_squads": normalize_squad_uuids(original_squads),
    }

    if status:
        user_state["original_status"] = status

    if expire_at is not None:
        user_state["original_expire_at"] = format_datetime_value(expire_at)
        user_state["original_subscription_profile"] = (
            build_subscription_profile_from_expire_at(expire_at)
        )

    return user_state


def normalize_user_state(user_uuid, raw_state):
    if isinstance(raw_state, list):
        return build_user_state(raw_state)

    if not isinstance(raw_state, dict):
        log(
            f"Ignoring invalid state for user {user_uuid}: "
            f"expected object or list, got {type(raw_state).__name__}"
        )
        return None

    original_squads = []
    for key in ("original_squads", "originalSquads", "squads"):
        if key in raw_state:
            original_squads = raw_state.get(key)
            break

    user_state = build_user_state(
        original_squads,
        status=raw_state.get("original_status", ""),
        expire_at=parse_datetime_value(raw_state.get("original_expire_at")),
    )

    temporary_active_until = parse_datetime_value(
        raw_state.get("temporary_active_until")
        or raw_state.get("temporaryActiveUntil")
        or raw_state.get("temporary_access_until")
    )
    if temporary_active_until is not None:
        user_state["temporary_active_until"] = format_datetime_value(temporary_active_until)
        user_state["temporary_subscription_profile"] = (
            build_subscription_profile_from_expire_at(temporary_active_until)
        )

    original_subscription_profile = raw_state.get("original_subscription_profile")
    if isinstance(original_subscription_profile, dict):
        user_state["original_subscription_profile"] = normalize_json_value(
            original_subscription_profile
        )

    temporary_subscription_profile = raw_state.get("temporary_subscription_profile")
    if isinstance(temporary_subscription_profile, dict):
        user_state["temporary_subscription_profile"] = normalize_json_value(
            temporary_subscription_profile
        )

    return user_state


def load_user_states():
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

    normalized_states = {}

    for user_uuid, raw_state in data.items():
        if not isinstance(user_uuid, str) or not user_uuid:
            log(f"Ignoring invalid user id in state file: {preview_json(user_uuid)}")
            continue

        normalized_state = normalize_user_state(user_uuid, raw_state)
        if normalized_state is not None:
            normalized_states[user_uuid] = normalized_state

    return normalized_states


user_states = load_user_states()


def save_user_states():
    directory = os.path.dirname(DATA_FILE)
    target_dir = directory or "."
    temp_path = None

    try:
        os.makedirs(target_dir, exist_ok=True)

        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=target_dir,
            delete=False,
        ) as file_obj:
            temp_path = file_obj.name
            json.dump(user_states, file_obj, indent=2, ensure_ascii=False)
            file_obj.flush()
            os.fsync(file_obj.fileno())

        os.replace(temp_path, DATA_FILE)
    except OSError as exc:
        log(f"Failed to save state file {DATA_FILE}: {exc}")
        if temp_path:
            try:
                os.remove(temp_path)
            except OSError:
                pass
        return False

    return True


def extract_response_user(response_body):
    try:
        payload = json.loads(response_body)
    except (TypeError, json.JSONDecodeError):
        return None

    if not isinstance(payload, dict):
        return None

    response_user = payload.get("response")
    if isinstance(response_user, dict):
        return response_user

    data_user = payload.get("data")
    if isinstance(data_user, dict):
        return data_user

    return payload if "uuid" in payload else None


def patch_user(user_uuid, payload_variants, response_validator=None):
    for url in build_patch_urls(user_uuid):
        for payload in payload_variants:
            request_payload = {"uuid": user_uuid}
            request_payload.update(payload)
            data = json.dumps(request_payload).encode("utf-8")
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
                    response_user = extract_response_user(body)

                    if response_validator is not None:
                        is_valid = response_validator(request_payload, response_user)
                        if not is_valid:
                            log(
                                f"PATCH {url} with {preview_json(request_payload)} "
                                f"returned 200 but did not apply expected changes; "
                                f"trying next payload variant"
                            )
                            continue

                    log(
                        f"PATCH {url} with {preview_json(request_payload)}, "
                        f"status={resp.status}, body={body}"
                    )
                    return True
            except error.HTTPError as exc:
                body = exc.read().decode("utf-8", "replace")
                log(
                    f"HTTP error patching via {url} with {preview_json(request_payload)}: "
                    f"status={exc.code}, body={body}"
                )
            except Exception as exc:
                log(
                    f"Error patching via {url} with "
                    f"{preview_json(request_payload)}: {exc}"
                )

    return False


def patch_user_squad(user_uuid, squads):
    payload_variants = (
        {"activeInternalSquads": squads},
    )
    return patch_user(user_uuid, payload_variants)


def patch_user_access(user_uuid, expire_at, traffic_limit_bytes=None, traffic_limit_strategy=""):
    formatted_expire_at = format_datetime_value(expire_at)
    primary_payload = {"status": "ACTIVE", "expireAt": formatted_expire_at}
    fallback_payload = {"status": "ACTIVE", "expire_at": formatted_expire_at}

    if traffic_limit_bytes is not None:
        primary_payload["trafficLimitBytes"] = traffic_limit_bytes
        fallback_payload["traffic_limit_bytes"] = traffic_limit_bytes

        if traffic_limit_strategy:
            primary_payload["trafficLimitStrategy"] = traffic_limit_strategy
            fallback_payload["traffic_limit_strategy"] = traffic_limit_strategy

    payload_variants = (
        primary_payload,
        fallback_payload,
    )
    return patch_user(user_uuid, payload_variants)


def patch_user_traffic_settings(user_uuid, traffic_limit_bytes=None, traffic_limit_strategy=""):
    primary_payload = {}
    fallback_payload = {}

    if traffic_limit_bytes is not None:
        primary_payload["trafficLimitBytes"] = traffic_limit_bytes
        fallback_payload["traffic_limit_bytes"] = traffic_limit_bytes

    if traffic_limit_strategy:
        primary_payload["trafficLimitStrategy"] = traffic_limit_strategy
        fallback_payload["traffic_limit_strategy"] = traffic_limit_strategy

    if not primary_payload:
        return True

    payload_variants = (
        primary_payload,
        fallback_payload,
    )
    return patch_user(user_uuid, payload_variants)


def get_temporary_expire_at_offset_seconds(user_uuid):
    # Add a tiny deterministic offset so our temporary expireAt is easy to
    # recognize later and does not rely on webhook timing.
    return zlib.crc32(user_uuid.encode("utf-8")) % 53 + 7


def calculate_temporary_expire_at(user_uuid, current_expire_at):
    minimum_expire_at = datetime.now(timezone.utc) + timedelta(days=TEMP_ACTIVE_DAYS)
    if current_expire_at is None:
        base_expire_at = minimum_expire_at
    else:
        base_expire_at = max(current_expire_at, minimum_expire_at)

    return base_expire_at.replace(microsecond=0) + timedelta(
        seconds=get_temporary_expire_at_offset_seconds(user_uuid)
    )


def calculate_temporary_traffic_limit_bytes(original_profile, used_traffic_bytes):
    if TEMP_ACTIVE_TRAFFIC_LIMIT_BYTES is None:
        return None

    original_traffic_limit_bytes = parse_int_value(
        (original_profile or {}).get("traffic_limit_bytes")
    )

    if used_traffic_bytes is None:
        baseline_traffic_bytes = max(0, original_traffic_limit_bytes or 0)
    else:
        baseline_traffic_bytes = max(0, used_traffic_bytes)

    return baseline_traffic_bytes + TEMP_ACTIVE_TRAFFIC_LIMIT_BYTES


def build_temporary_subscription_profile(
    original_profile,
    temporary_expire_at,
    traffic_limit_bytes=None,
    traffic_limit_strategy="",
):
    temporary_profile = dict(original_profile)
    temporary_profile["expire_at"] = format_datetime_value(temporary_expire_at)
    if traffic_limit_bytes is not None:
        temporary_profile["traffic_limit_bytes"] = traffic_limit_bytes
        if traffic_limit_strategy:
            temporary_profile["traffic_limit_strategy"] = traffic_limit_strategy
    return temporary_profile


def build_original_access_restore_settings(user_state, current_profile):
    if not current_profile:
        return {}

    original_profile = user_state.get("original_subscription_profile") or {}
    temporary_profile = user_state.get("temporary_subscription_profile") or {}
    settings_to_restore = {}

    current_traffic_limit_bytes = parse_int_value(current_profile.get("traffic_limit_bytes"))
    temporary_traffic_limit_bytes = parse_int_value(
        temporary_profile.get("traffic_limit_bytes")
    )
    if (
        temporary_traffic_limit_bytes is not None
        and current_traffic_limit_bytes == temporary_traffic_limit_bytes
        and "traffic_limit_bytes" in original_profile
    ):
        settings_to_restore["traffic_limit_bytes"] = parse_int_value(
            original_profile.get("traffic_limit_bytes")
        ) or 0

    current_traffic_limit_strategy = current_profile.get("traffic_limit_strategy")
    temporary_traffic_limit_strategy = temporary_profile.get("traffic_limit_strategy")
    if (
        temporary_traffic_limit_strategy
        and current_traffic_limit_strategy == temporary_traffic_limit_strategy
        and "traffic_limit_strategy" in original_profile
    ):
        settings_to_restore["traffic_limit_strategy"] = (
            original_profile.get("traffic_limit_strategy") or ""
        )

    return settings_to_restore


def should_restore_original_squads(user_state, current_profile):
    if not current_profile:
        return False

    temporary_profile = user_state.get("temporary_subscription_profile") or {}
    if profile_matches_reference(current_profile, temporary_profile):
        return False

    original_profile = user_state.get("original_subscription_profile") or {}
    if profile_matches_reference(current_profile, original_profile):
        return False

    if temporary_profile or original_profile:
        return True

    if "expire_at" not in current_profile:
        return False

    original_expire_at = parse_datetime_value(user_state.get("original_expire_at"))
    if original_expire_at is None:
        return True

    return current_profile["expire_at"] != format_datetime_value(original_expire_at)


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

        if isinstance(event, str) and event and not event.startswith("user."):
            log(f"Ignoring non-user event={event}")
            self._send_text(200, "Ignored")
            return

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
        expire_at = extract_expire_at(user)
        subscription_profile = extract_subscription_profile(user)
        used_traffic_bytes = extract_used_traffic_bytes(user)

        if event and event not in SUPPORTED_USER_EVENTS:
            log(f"Ignoring unsupported user event={event} for {username}")
            self._send_text(200, "Ignored")
            return

        log(
            f"Webhook received: event={event or 'unknown'}, "
            f"user={username}, status={status or 'unknown'}, "
            f"squads={squad_uuids}, expireAt={format_datetime_value(expire_at) if expire_at else 'unknown'}, "
            f"subscriptionProfile={preview_json(subscription_profile)}"
        )

        should_handle_backup = (
            event in EVENTS_TO_BACKUP
            or (not event and status in STATUSES_TO_BACKUP)
        )

        if should_handle_backup:
            target_squads = [BACKUP_SQUAD_UUID]
            user_state = user_states.get(user_uuid)
            already_on_backup = squads_match(squad_uuids, target_squads)

            if not squad_uuids:
                log(f"User {username} has no squads in payload, skipping backup")
                self._send_text(200, "Ignored")
                return

            if user_state is None:
                if already_on_backup:
                    log(
                        f"User {username} already has target squads {target_squads}, "
                        f"but original squads are not saved"
                    )
                    self._send_text(200, "Ignored")
                    return

                user_state = build_user_state(squad_uuids, status=status, expire_at=expire_at)
                if subscription_profile:
                    user_state["original_subscription_profile"] = dict(subscription_profile)
                user_states[user_uuid] = user_state
                if not save_user_states():
                    user_states.pop(user_uuid, None)
                    self._send_text(500, "Failed to save original squads")
                    return

                log(f"Saved original squads for {username}: {squad_uuids}")
            else:
                state_changed = False

                if not user_state.get("original_squads") and not already_on_backup:
                    user_state["original_squads"] = list(normalize_squad_uuids(squad_uuids))
                    state_changed = True

                if status and not user_state.get("original_status"):
                    user_state["original_status"] = status
                    state_changed = True

                if expire_at is not None and not user_state.get("original_expire_at"):
                    user_state["original_expire_at"] = format_datetime_value(expire_at)
                    state_changed = True

                if subscription_profile and not user_state.get("original_subscription_profile"):
                    user_state["original_subscription_profile"] = dict(subscription_profile)
                    state_changed = True

                if state_changed and not save_user_states():
                    self._send_text(500, "Failed to update local state")
                    return

            if TEMP_ACTIVE_DAYS > 0:
                temporary_active_until = parse_datetime_value(
                    user_state.get("temporary_active_until")
                )
                if temporary_active_until is None:
                    temporary_expire_at = calculate_temporary_expire_at(
                        user_uuid,
                        expire_at,
                    )
                    user_state["temporary_active_until"] = format_datetime_value(
                        temporary_expire_at
                    )
                    original_profile = user_state.get("original_subscription_profile") or {}
                    temporary_traffic_limit_bytes = calculate_temporary_traffic_limit_bytes(
                        original_profile,
                        used_traffic_bytes,
                    )
                    temporary_traffic_limit_strategy = (
                        "NO_RESET"
                        if temporary_traffic_limit_bytes is not None
                        else (original_profile.get("traffic_limit_strategy") or "")
                    )
                    user_state["temporary_subscription_profile"] = (
                        build_temporary_subscription_profile(
                            original_profile,
                            temporary_expire_at,
                            traffic_limit_bytes=temporary_traffic_limit_bytes,
                            traffic_limit_strategy=temporary_traffic_limit_strategy,
                        )
                    )

                    if not patch_user_access(
                        user_uuid,
                        temporary_expire_at,
                        traffic_limit_bytes=temporary_traffic_limit_bytes,
                        traffic_limit_strategy=temporary_traffic_limit_strategy,
                    ):
                        user_state.pop("temporary_active_until", None)
                        user_state.pop("temporary_subscription_profile", None)
                        self._send_text(502, "Failed to activate user temporarily")
                        return

                    if not save_user_states():
                        self._send_text(500, "Failed to update local state")
                        return

                    log(
                        f"Temporarily activated {username} until "
                        f"{user_state['temporary_active_until']}"
                    )
                else:
                    log(
                        f"Temporary ACTIVE access already granted for {username} until "
                        f"{user_state['temporary_active_until']}, leaving current state"
                    )

            if already_on_backup:
                log(
                    f"User {username} already has target squads {target_squads}, "
                    f"skipping squad patch"
                )
            elif not patch_user_squad(user_uuid, target_squads):
                log(
                    f"Failed to switch {username} to backup squads {target_squads}; "
                    f"acknowledging webhook to avoid retry loop"
                )
                self._send_text(200, "Failed to patch user")
                return

        else:
            user_state = user_states.get(user_uuid)
            should_try_restore = (
                user_state is not None
                and (
                    status == "ACTIVE"
                    or event == "user.modified"
                )
            )

            if not should_try_restore:
                log(
                    f"Ignoring event={event or 'unknown'} status={status or 'unknown'} "
                    f"for {username}"
                )
                self._send_text(200, "OK")
                return

            if user_state is None:
                log(f"No saved squads for {username}, nothing to restore")
            else:
                squads_to_restore = user_state.get("original_squads") or []
                if not squads_to_restore:
                    log(f"Saved state for {username} has no original squads, nothing to restore")
                    self._send_text(200, "Ignored")
                    return

                real_subscription_change_detected = should_restore_original_squads(
                    user_state,
                    subscription_profile,
                )

                if not real_subscription_change_detected:
                    log(
                        f"User {username} has no real subscription change yet; "
                        f"keeping backup squad until a real subscription change is detected"
                    )
                    self._send_text(200, "Ignored")
                    return
                restore_access_settings = build_original_access_restore_settings(
                    user_state,
                    subscription_profile,
                )

                if squads_match(squad_uuids, squads_to_restore):
                    if restore_access_settings and not patch_user_traffic_settings(
                        user_uuid,
                        traffic_limit_bytes=restore_access_settings.get("traffic_limit_bytes"),
                        traffic_limit_strategy=restore_access_settings.get(
                            "traffic_limit_strategy", ""
                        ),
                    ):
                        log(
                            f"Failed to restore original access settings for {username}: "
                            f"{preview_json(restore_access_settings)}; "
                            f"acknowledging webhook to retry on the next user event"
                        )
                        self._send_text(200, "Failed to restore user access settings")
                        return

                    user_states.pop(user_uuid, None)
                    if not save_user_states():
                        user_states[user_uuid] = user_state
                        self._send_text(500, "Failed to update local state")
                        return

                    log(
                        f"Original squads already restored for {username}: "
                        f"{squads_to_restore}"
                    )
                else:
                    if not patch_user_squad(user_uuid, squads_to_restore):
                        log(
                            f"Failed to restore original squads for {username}: "
                            f"{squads_to_restore}; acknowledging webhook to avoid retry loop"
                        )
                        self._send_text(200, "Failed to restore user squads")
                        return

                    if restore_access_settings and not patch_user_traffic_settings(
                        user_uuid,
                        traffic_limit_bytes=restore_access_settings.get("traffic_limit_bytes"),
                        traffic_limit_strategy=restore_access_settings.get(
                            "traffic_limit_strategy", ""
                        ),
                    ):
                        log(
                            f"Failed to restore original access settings for {username}: "
                            f"{preview_json(restore_access_settings)}; "
                            f"acknowledging webhook to retry on the next user event"
                        )
                        self._send_text(200, "Failed to restore user access settings")
                        return

                    user_states.pop(user_uuid, None)
                    if not save_user_states():
                        user_states[user_uuid] = user_state
                        self._send_text(500, "Failed to update local state")
                        return

                    log(f"Restored original squads for {username}: {squads_to_restore}")

        self._send_text(200, "OK")


def run():
    server = HTTPServer(("0.0.0.0", PORT), WebhookHandler)
    log(
        f"Webhook server running on port {PORT}, path {normalize_path(WEBHOOK_PATH)}, "
        f"api_url={API_URL}, temp_active_days={TEMP_ACTIVE_DAYS}, "
        f"temp_active_traffic_limit_mb={TEMP_ACTIVE_TRAFFIC_LIMIT_MB}"
    )
    server.serve_forever()


if __name__ == "__main__":
    run()
