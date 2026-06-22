#!/usr/bin/env python3
import hashlib
import hmac
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


def getenv_bool_mode(name, default="auto"):
    value = os.getenv(name)
    if value in (None, ""):
        return default

    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on", "enabled"}:
        return "enabled"
    if normalized in {"0", "false", "no", "off", "disabled"}:
        return "disabled"
    if normalized == "auto":
        return "auto"

    print(f"Invalid boolean mode for {name}: {value!r}, using {default}", flush=True)
    return default


def getenv_first(*names, default=""):
    for name in names:
        value = os.getenv(name)
        if value not in (None, ""):
            return value.strip()

    return default


def parse_csv_value(value):
    if value in (None, ""):
        return []

    return [
        item.strip()
        for item in str(value).split(",")
        if item.strip()
    ]


def getenv_csv(name, default=""):
    return parse_csv_value(os.getenv(name, default))


UNSET = object()


STATUSES_TO_BACKUP = {"EXPIRED", "LIMITED"}
EVENTS_TO_BACKUP = {
    "user.expired": "EXPIRED",
    "user.limited": "LIMITED",
}
EVENTS_TO_RESTORE = {
    "user.enabled": "ACTIVE",
    "user.traffic_reset": "ACTIVE",
}
SUPPORTED_USER_EVENTS = set(EVENTS_TO_BACKUP) | set(EVENTS_TO_RESTORE) | {
    "user.modified",
}

PORT = getenv_int("PORT", 3040)
API_URL = os.getenv("RW_API_URL", "https://panel.example.com/api").rstrip("/")
API_TOKEN = os.getenv("RW_API_TOKEN", "YOUR_API_TOKEN")
API_CADDY_TOKEN = getenv_first(
    "RW_API_CADDY_TOKEN",
    default="",
)
API_COOKIE = getenv_first(
    "RW_API_COOKIE",
    default="",
)
API_CF_CLIENT_ID = getenv_first(
    "RW_API_CF_CLIENT_ID",
    default="",
)
API_CF_CLIENT_SECRET = getenv_first(
    "RW_API_CF_CLIENT_SECRET",
    default="",
)
API_INTERNAL_PROXY_HEADERS_MODE = getenv_bool_mode(
    "RW_API_INTERNAL_PROXY_HEADERS",
    default="auto",
)
BACKUP_SQUAD_UUIDS = (
    getenv_csv("BACKUP_SQUAD_UUIDS")
    or getenv_csv("BACKUP_SQUAD_UUID")
)
BACKUP_EXTERNAL_SQUAD_UUID = getenv_first("BACKUP_EXTERNAL_SQUAD_UUID", default="")
TEMP_ACTIVE_DAYS = getenv_int("TEMP_ACTIVE_DAYS", 3)
TEMP_ACTIVE_TRAFFIC_LIMIT_MB = max(0, getenv_int("TEMP_ACTIVE_TRAFFIC_LIMIT_MB", 300))
TEMP_ACTIVE_TRAFFIC_LIMIT_BYTES = (
    TEMP_ACTIVE_TRAFFIC_LIMIT_MB * 1024 * 1024
    if TEMP_ACTIVE_TRAFFIC_LIMIT_MB > 0
    else None
)
GIB_BYTES = 1024 * 1024 * 1024
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DATA_FILE = os.path.join(BASE_DIR, "data", "original_squads.json")
DATA_FILE = os.getenv("DATA_PATH", DEFAULT_DATA_FILE)
WEBHOOK_PATH = os.getenv("WEBHOOK_PATH", "/api/v1/remnawave")
WEBHOOK_SECRET = getenv_first(
    "WEBHOOK_SECRET_HEADER",
    "REMNAWAVE_WEBHOOK_SECRET",
    "WEBHOOK_SECRET",
    default="",
)
WEBHOOK_SIGNATURE_HEADER = getenv_first(
    "WEBHOOK_SIGNATURE_HEADER",
    default="X-Remnawave-Signature",
)
WEBHOOK_TIMESTAMP_HEADER = getenv_first(
    "WEBHOOK_TIMESTAMP_HEADER",
    default="X-Remnawave-Timestamp",
)
WEBHOOK_MAX_AGE_SECONDS = max(0, getenv_int("WEBHOOK_MAX_AGE_SECONDS", 300))
MAX_WEBHOOK_BODY_BYTES = max(0, getenv_int("MAX_WEBHOOK_BODY_BYTES", 1024 * 1024))

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

SENSITIVE_LOG_KEYS = {
    "trojanPassword",
    "trojan_password",
    "ssPassword",
    "ss_password",
    "vlessUuid",
    "vless_uuid",
    "subscriptionUrl",
    "subscription_url",
    "shortUuid",
    "short_uuid",
    "password",
    "token",
    "accessToken",
    "access_token",
    "refreshToken",
    "refresh_token",
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


def redact_log_value(value):
    if isinstance(value, dict):
        return {
            key: "***REDACTED***" if key in SENSITIVE_LOG_KEYS else redact_log_value(item)
            for key, item in value.items()
        }

    if isinstance(value, list):
        return [redact_log_value(item) for item in value]

    return value


def preview_response_body(body):
    try:
        payload = json.loads(body)
    except (TypeError, json.JSONDecodeError):
        return body[:1000]

    return preview_json(redact_log_value(payload))


def normalize_signature(value):
    if not isinstance(value, str):
        return ""

    signature = value.strip()
    if signature.lower().startswith("sha256="):
        signature = signature.split("=", 1)[1].strip()

    return signature.lower()


def validate_webhook_signature(headers, body):
    if not WEBHOOK_SECRET:
        return False, "WEBHOOK_SECRET_HEADER is not configured"

    received_signature = normalize_signature(headers.get(WEBHOOK_SIGNATURE_HEADER))
    if not received_signature:
        return False, f"missing {WEBHOOK_SIGNATURE_HEADER} header"

    if len(received_signature) != 64 or any(
        char not in "0123456789abcdef"
        for char in received_signature
    ):
        return False, "invalid webhook signature format"

    expected_signature = hmac.new(
        WEBHOOK_SECRET.encode("utf-8"),
        body,
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(received_signature, expected_signature):
        return False, "invalid webhook signature"

    if WEBHOOK_MAX_AGE_SECONDS > 0:
        timestamp = parse_datetime_value(headers.get(WEBHOOK_TIMESTAMP_HEADER))
        if timestamp is None:
            return False, f"missing or invalid {WEBHOOK_TIMESTAMP_HEADER} header"

        age_seconds = abs((datetime.now(timezone.utc) - timestamp).total_seconds())
        if age_seconds > WEBHOOK_MAX_AGE_SECONDS:
            return False, "webhook timestamp is outside allowed age"

    return True, ""


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


def build_api_headers():
    headers = {
        "Authorization": f"Bearer {API_TOKEN}",
        "Content-Type": "application/json",
    }

    if API_CADDY_TOKEN:
        headers["X-Api-Key"] = API_CADDY_TOKEN

    if API_COOKIE:
        headers["Cookie"] = API_COOKIE

    if API_CF_CLIENT_ID:
        headers["CF-Access-Client-Id"] = API_CF_CLIENT_ID

    if API_CF_CLIENT_SECRET:
        headers["CF-Access-Client-Secret"] = API_CF_CLIENT_SECRET

    if should_add_internal_proxy_headers():
        headers["x-forwarded-proto"] = "https"
        headers["x-forwarded-for"] = "127.0.0.1"

    return headers


def should_add_internal_proxy_headers():
    if API_INTERNAL_PROXY_HEADERS_MODE == "enabled":
        return True

    if API_INTERNAL_PROXY_HEADERS_MODE == "disabled":
        return False

    parsed_api_url = urlsplit(API_URL)
    hostname = (parsed_api_url.hostname or "").lower()

    return parsed_api_url.scheme == "http" and hostname in {
        "remnawave",
        "localhost",
        "127.0.0.1",
    }


def describe_api_proxy_auth():
    enabled = []

    if API_CADDY_TOKEN:
        enabled.append("x-api-key")
    if API_COOKIE:
        enabled.append("cookie")
    if API_CF_CLIENT_ID or API_CF_CLIENT_SECRET:
        enabled.append("cloudflare-access")
    if should_add_internal_proxy_headers():
        enabled.append("internal-proxy-headers")

    return ",".join(enabled) if enabled else "disabled"


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


def normalize_external_squad_uuid(value):
    if isinstance(value, str):
        normalized = value.strip()
        return normalized or None

    if isinstance(value, dict):
        return normalize_external_squad_uuid(value.get("uuid"))

    return None


def extract_external_squad_uuid(user):
    candidate_keys = (
        "externalSquadUuid",
        "external_squad_uuid",
        "externalSquad",
        "external_squad",
    )

    for key in candidate_keys:
        if key not in user:
            continue

        return normalize_external_squad_uuid(user.get(key))

    return None


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


def external_squads_match(current_squad, target_squad):
    return normalize_external_squad_uuid(current_squad) == normalize_external_squad_uuid(
        target_squad
    )


def target_squads_match(
    current_squads,
    target_squads,
    current_external_squad,
    target_external_squad=UNSET,
):
    internal_matches = not target_squads or squads_match(current_squads, target_squads)
    external_matches = (
        target_external_squad is UNSET
        or external_squads_match(current_external_squad, target_external_squad)
    )
    return internal_matches and external_matches


def build_user_state(original_squads, status="", expire_at=None, external_squad=UNSET):
    user_state = {
        "original_squads": normalize_squad_uuids(original_squads),
    }

    if external_squad is not UNSET:
        user_state["original_external_squad"] = normalize_external_squad_uuid(
            external_squad
        )

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

    original_external_squad = UNSET
    for key in (
        "original_external_squad",
        "originalExternalSquad",
        "external_squad",
        "externalSquad",
    ):
        if key in raw_state:
            original_external_squad = raw_state.get(key)
            break

    user_state = build_user_state(
        original_squads,
        status=raw_state.get("original_status", ""),
        expire_at=parse_datetime_value(raw_state.get("original_expire_at")),
        external_squad=original_external_squad,
    )

    original_subscription_profile = raw_state.get("original_subscription_profile")
    if isinstance(original_subscription_profile, dict):
        user_state["original_subscription_profile"] = normalize_json_value(
            original_subscription_profile
        )

    temporary_active_until = parse_datetime_value(
        raw_state.get("temporary_active_until")
        or raw_state.get("temporaryActiveUntil")
        or raw_state.get("temporary_access_until")
    )
    if temporary_active_until is not None:
        user_state["temporary_active_until"] = format_datetime_value(temporary_active_until)
        temporary_subscription_profile = dict(
            user_state.get("original_subscription_profile") or {}
        )
        temporary_subscription_profile["expire_at"] = format_datetime_value(
            temporary_active_until
        )
        user_state["temporary_subscription_profile"] = temporary_subscription_profile

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
                headers=build_api_headers(),
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
                        f"status={resp.status}, body={preview_response_body(body)}"
                    )
                    return True
            except error.HTTPError as exc:
                body = exc.read().decode("utf-8", "replace")
                log(
                    f"HTTP error patching via {url} with {preview_json(request_payload)}: "
                    f"status={exc.code}, body={preview_response_body(body)}"
                )
            except Exception as exc:
                log(
                    f"Error patching via {url} with "
                    f"{preview_json(request_payload)}: {exc}"
                )

    return False


def patch_user_squad(user_uuid, squads=UNSET, external_squad=UNSET):
    payload = {}

    if squads is not UNSET:
        payload["activeInternalSquads"] = normalize_squad_uuids(squads)

    if external_squad is not UNSET:
        payload["externalSquadUuid"] = normalize_external_squad_uuid(external_squad)

    if not payload:
        return True

    payload_variants = (payload,)

    def response_validator(request_payload, response_user):
        if not isinstance(response_user, dict):
            return True

        if "activeInternalSquads" in request_payload:
            response_has_internal_squads = any(
                key in response_user
                for key in (
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
            )
            if response_has_internal_squads and not squads_match(
                extract_squad_uuids(response_user),
                request_payload["activeInternalSquads"],
            ):
                return False

        if "externalSquadUuid" in request_payload:
            response_has_external_squad = any(
                key in response_user
                for key in (
                    "externalSquadUuid",
                    "external_squad_uuid",
                    "externalSquad",
                    "external_squad",
                )
            )
            if response_has_external_squad and not external_squads_match(
                extract_external_squad_uuid(response_user),
                request_payload["externalSquadUuid"],
            ):
                return False

        return True

    return patch_user(user_uuid, payload_variants, response_validator=response_validator)


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

    desired_limit_bytes = baseline_traffic_bytes + TEMP_ACTIVE_TRAFFIC_LIMIT_BYTES
    return ceil_traffic_limit_to_gib_bytes(desired_limit_bytes)


def ceil_traffic_limit_to_gib_bytes(traffic_limit_bytes):
    traffic_limit_bytes = parse_int_value(traffic_limit_bytes)
    if traffic_limit_bytes is None or traffic_limit_bytes <= 0:
        return None

    return ((traffic_limit_bytes + GIB_BYTES - 1) // GIB_BYTES) * GIB_BYTES


def round_traffic_limit_to_remnashop_gb_bytes(traffic_limit_bytes):
    # Remnashop 0.7.5 syncs Remnawave bytes through integer GB with ROUND_HALF_UP.
    traffic_limit_bytes = parse_int_value(traffic_limit_bytes)
    if traffic_limit_bytes is None or traffic_limit_bytes <= 0:
        return None

    rounded_gb = (traffic_limit_bytes + GIB_BYTES // 2) // GIB_BYTES
    if rounded_gb <= 0:
        return None

    return rounded_gb * GIB_BYTES


def traffic_limit_matches_temporary(current_traffic_limit_bytes, temporary_traffic_limit_bytes):
    current_traffic_limit_bytes = parse_int_value(current_traffic_limit_bytes)
    temporary_traffic_limit_bytes = parse_int_value(temporary_traffic_limit_bytes)
    if current_traffic_limit_bytes is None or temporary_traffic_limit_bytes is None:
        return False

    if current_traffic_limit_bytes == temporary_traffic_limit_bytes:
        return True

    return current_traffic_limit_bytes == round_traffic_limit_to_remnashop_gb_bytes(
        temporary_traffic_limit_bytes
    )


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
        traffic_limit_matches_temporary(
            current_traffic_limit_bytes,
            temporary_traffic_limit_bytes,
        )
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


def should_restore_after_traffic_reset(user_state):
    original_status = user_state.get("original_status")
    if not isinstance(original_status, str):
        return False

    return original_status.upper() == "LIMITED"


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

        try:
            content_length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            log("Webhook request has invalid Content-Length")
            self._send_text(400, "Invalid Content-Length")
            return

        if MAX_WEBHOOK_BODY_BYTES and content_length > MAX_WEBHOOK_BODY_BYTES:
            log(
                f"Webhook request body too large: {content_length} bytes, "
                f"limit={MAX_WEBHOOK_BODY_BYTES}"
            )
            self._send_text(413, "Payload Too Large")
            return

        body = self.rfile.read(content_length)

        is_valid_webhook, validation_error = validate_webhook_signature(
            self.headers,
            body,
        )
        if not is_valid_webhook:
            log(f"Webhook authentication failed: {validation_error}")
            self._send_text(401, "Unauthorized")
            return

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
        external_squad_uuid = extract_external_squad_uuid(user)
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
            f"squads={squad_uuids}, externalSquad={external_squad_uuid or 'none'}, "
            f"expireAt={format_datetime_value(expire_at) if expire_at else 'unknown'}, "
            f"subscriptionProfile={preview_json(subscription_profile)}"
        )

        should_handle_backup = (
            event in EVENTS_TO_BACKUP
            or (not event and status in STATUSES_TO_BACKUP)
        )
        target_squads = normalize_squad_uuids(BACKUP_SQUAD_UUIDS)
        target_external_squad = (
            normalize_external_squad_uuid(BACKUP_EXTERNAL_SQUAD_UUID)
            if BACKUP_EXTERNAL_SQUAD_UUID
            else UNSET
        )

        if should_handle_backup:
            if not target_squads and target_external_squad is UNSET:
                log("No backup internal or external squad is configured, skipping backup")
                self._send_text(500, "Backup squad is not configured")
                return

            user_state = user_states.get(user_uuid)
            already_on_backup = target_squads_match(
                squad_uuids,
                target_squads,
                external_squad_uuid,
                target_external_squad,
            )

            if target_squads and not squad_uuids and user_state is None:
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

                user_state = build_user_state(
                    squad_uuids,
                    status=status,
                    expire_at=expire_at,
                    external_squad=(
                        external_squad_uuid
                        if target_external_squad is not UNSET
                        else UNSET
                    ),
                )
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

                if (
                    target_external_squad is not UNSET
                    and "original_external_squad" not in user_state
                    and not already_on_backup
                ):
                    user_state["original_external_squad"] = normalize_external_squad_uuid(
                        external_squad_uuid
                    )
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

                    if not save_user_states():
                        user_state.pop("temporary_active_until", None)
                        user_state.pop("temporary_subscription_profile", None)
                        self._send_text(500, "Failed to update local state")
                        return

                    if not patch_user_access(
                        user_uuid,
                        temporary_expire_at,
                        traffic_limit_bytes=temporary_traffic_limit_bytes,
                        traffic_limit_strategy=temporary_traffic_limit_strategy,
                    ):
                        user_state.pop("temporary_active_until", None)
                        user_state.pop("temporary_subscription_profile", None)
                        save_user_states()
                        self._send_text(502, "Failed to activate user temporarily")
                        return

                    log(
                        f"Temporarily activated {username} until "
                        f"{user_state['temporary_active_until']}, "
                        f"traffic_limit_bytes="
                        f"{temporary_traffic_limit_bytes if temporary_traffic_limit_bytes is not None else 'unchanged'}"
                    )
                else:
                    log(
                        f"Temporary ACTIVE access already granted for {username} until "
                        f"{user_state['temporary_active_until']}, leaving current state"
                    )

            if already_on_backup:
                log(
                    f"User {username} already has target squads {target_squads} "
                    f"and external squad "
                    f"{target_external_squad if target_external_squad is not UNSET else 'unchanged'}, "
                    f"skipping squad patch"
                )
            elif not patch_user_squad(
                user_uuid,
                target_squads if target_squads else UNSET,
                target_external_squad,
            ):
                log(
                    f"Failed to switch {username} to backup squads {target_squads} "
                    f"and external squad "
                    f"{target_external_squad if target_external_squad is not UNSET else 'unchanged'}; "
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
                external_to_restore = (
                    user_state.get("original_external_squad")
                    if (
                        BACKUP_EXTERNAL_SQUAD_UUID
                        and "original_external_squad" in user_state
                    )
                    else UNSET
                )

                if not squads_to_restore and external_to_restore is UNSET:
                    log(
                        f"Saved state for {username} has no original squads, "
                        f"nothing to restore"
                    )
                    self._send_text(200, "Ignored")
                    return

                traffic_reset_restore_detected = (
                    event == "user.traffic_reset"
                    and should_restore_after_traffic_reset(user_state)
                )
                real_subscription_change_detected = (
                    traffic_reset_restore_detected
                    or should_restore_original_squads(
                        user_state,
                        subscription_profile,
                    )
                )

                if traffic_reset_restore_detected:
                    log(
                        f"Traffic reset detected for limited user {username}; "
                        "restoring original squads"
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
                internal_backup_managed = bool(target_squads)
                external_backup_managed = target_external_squad is not UNSET
                internal_squads_are_original = (
                    not internal_backup_managed
                    or not squads_to_restore
                    or squads_match(squad_uuids, squads_to_restore)
                )
                external_squad_is_original = (
                    not external_backup_managed
                    or external_to_restore is UNSET
                    or external_squads_match(external_squad_uuid, external_to_restore)
                )
                internal_squads_are_backup = (
                    not internal_backup_managed
                    or squads_match(squad_uuids, target_squads)
                )
                external_squad_is_backup = (
                    not external_backup_managed
                    or external_squads_match(external_squad_uuid, target_external_squad)
                )
                internal_squads_are_remnashop_assigned = (
                    internal_backup_managed
                    and bool(squad_uuids)
                    and not internal_squads_are_original
                    and not internal_squads_are_backup
                )
                external_squad_is_remnashop_assigned = (
                    external_backup_managed
                    and external_squad_uuid is not None
                    and not external_squad_is_original
                    and not external_squad_is_backup
                )
                already_restored = (
                    internal_squads_are_original
                    and external_squad_is_original
                )
                remnashop_assigned_new_squads = (
                    internal_squads_are_remnashop_assigned
                    or external_squad_is_remnashop_assigned
                )
                squads_patch = (
                    squads_to_restore
                    if (
                        internal_backup_managed
                        and squads_to_restore
                        and not internal_squads_are_original
                        and not internal_squads_are_remnashop_assigned
                    )
                    else UNSET
                )
                external_patch = (
                    external_to_restore
                    if (
                        external_backup_managed
                        and external_to_restore is not UNSET
                        and not external_squad_is_original
                        and not external_squad_is_remnashop_assigned
                    )
                    else UNSET
                )

                if (
                    remnashop_assigned_new_squads
                    and squads_patch is UNSET
                    and external_patch is UNSET
                ):
                    if (
                        restore_access_settings
                        and not patch_user_traffic_settings(
                            user_uuid,
                            traffic_limit_bytes=restore_access_settings.get(
                                "traffic_limit_bytes"
                            ),
                            traffic_limit_strategy=restore_access_settings.get(
                                "traffic_limit_strategy", ""
                            ),
                        )
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
                        f"Detected Remnashop-assigned squads for {username}: "
                        f"current_squads={squad_uuids}, "
                        f"current_external_squad={external_squad_uuid or 'none'}; "
                        f"leaving them unchanged and clearing saved backup state"
                    )
                    self._send_text(200, "OK")
                    return

                if already_restored:
                    if (
                        restore_access_settings
                        and not patch_user_traffic_settings(
                            user_uuid,
                            traffic_limit_bytes=restore_access_settings.get(
                                "traffic_limit_bytes"
                            ),
                            traffic_limit_strategy=restore_access_settings.get(
                                "traffic_limit_strategy", ""
                            ),
                        )
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
                        f"{squads_to_restore}, external squad="
                        f"{external_to_restore if external_to_restore is not UNSET else 'unchanged'}"
                    )
                else:
                    if not patch_user_squad(
                        user_uuid,
                        squads_patch,
                        external_patch,
                    ):
                        log(
                            f"Failed to restore backup-managed squads for {username}: "
                            f"squads={squads_patch if squads_patch is not UNSET else 'unchanged'}, "
                            f"external squad="
                            f"{external_patch if external_patch is not UNSET else 'unchanged'}; "
                            f"acknowledging webhook to avoid retry loop"
                        )
                        self._send_text(200, "Failed to restore user squads")
                        return

                    if (
                        restore_access_settings
                        and not patch_user_traffic_settings(
                            user_uuid,
                            traffic_limit_bytes=restore_access_settings.get(
                                "traffic_limit_bytes"
                            ),
                            traffic_limit_strategy=restore_access_settings.get(
                                "traffic_limit_strategy", ""
                            ),
                        )
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

                    if remnashop_assigned_new_squads:
                        log(
                            f"Preserved Remnashop-assigned squads for {username}: "
                            f"current_squads={squad_uuids}, "
                            f"current_external_squad={external_squad_uuid or 'none'}; "
                            f"restored backup-managed parts: "
                            f"squads={squads_patch if squads_patch is not UNSET else 'unchanged'}, "
                            f"external squad="
                            f"{external_patch if external_patch is not UNSET else 'unchanged'}"
                        )
                    else:
                        log(
                            f"Restored original squads for {username}: {squads_to_restore}, "
                            f"external squad="
                            f"{external_to_restore if external_to_restore is not UNSET else 'unchanged'}"
                        )

        self._send_text(200, "OK")


def run():
    server = HTTPServer(("0.0.0.0", PORT), WebhookHandler)
    log(
        f"Webhook server running on port {PORT}, path {normalize_path(WEBHOOK_PATH)}, "
        f"api_url={API_URL}, temp_active_days={TEMP_ACTIVE_DAYS}, "
        f"temp_active_traffic_limit_mb={TEMP_ACTIVE_TRAFFIC_LIMIT_MB}, "
        f"backup_squads={BACKUP_SQUAD_UUIDS}, "
        f"backup_external_squad={BACKUP_EXTERNAL_SQUAD_UUID or 'disabled'}, "
        f"webhook_auth={'enabled' if WEBHOOK_SECRET else 'missing-secret'}, "
        f"api_proxy_auth={describe_api_proxy_auth()}, "
        "remnashop_squad_policy=preserve-assigned-squads"
    )
    server.serve_forever()


if __name__ == "__main__":
    run()
