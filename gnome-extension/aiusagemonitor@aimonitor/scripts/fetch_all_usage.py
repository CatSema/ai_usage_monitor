#!/usr/bin/env python3
"""
AI Usage Monitor - Data fetcher

Reads usage data for:
  - Claude Code
  - OpenAI Codex
  - Gemini CLI

Outputs a single JSON object to stdout.

Usage:
  python3 fetch_all_usage.py
  python3 fetch_all_usage.py claude
  python3 fetch_all_usage.py codex
  python3 fetch_all_usage.py gemini
"""

import base64
import glob
import json
import os
import re
import selectors
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
import urllib.error
import urllib.parse
import urllib.request


result = {}

# Optional provider filter: python3 fetch_all_usage.py [claude|codex|gemini]
_only = sys.argv[1] if len(sys.argv) > 1 else None


# Gemini CLI PUBLIC OAuth client.
# These installed-app credentials ship with Gemini CLI and are not a user secret.
# They are needed when Gemini credential files omit client_id/client_secret.
GEMINI_CLI_OAUTH_CLIENT_ID = "681255809395-oo8ft2oprdrnp9e3aqf6av3hmdib135j.apps.googleusercontent.com"
GEMINI_CLI_OAUTH_CLIENT_SECRET = "GOCSPX" "-4uHgMPm-1o7Sk-geV6Cu5clXFsxl"


# ─────────────────────────────────────────────────────────────────────────────
# Common helpers
# ─────────────────────────────────────────────────────────────────────────────

def unix_to_iso(ts):
    """Convert Unix timestamp to ISO 8601 UTC string."""
    if ts is None:
        return None

    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat()
    except Exception:
        return str(ts)


def iso_now():
    """Return current UTC time as ISO 8601 string."""
    return datetime.now(timezone.utc).isoformat()


def read_http_error_body(err):
    """Read and decode HTTP error body safely."""
    try:
        body = err.read()
        if isinstance(body, bytes):
            return body.decode('utf-8', errors='replace')
        return str(body or '')
    except Exception:
        return ''


def sanitize_error_text(text):
    """
    Redact sensitive tokens from error text.

    SECURITY:
    - Do not expose Bearer tokens.
    - Do not expose OpenAI API keys.
    - Do not expose JWT-like strings.
    """
    if not text:
        return ''

    value = str(text)

    value = re.sub(
        r'(?i)(bearer\s+)[a-z0-9\-\._~\+\/]+=*',
        r'\1<redacted>',
        value,
    )

    value = re.sub(
        r'sk-[A-Za-z0-9_\-]{8,}',
        '<redacted>',
        value,
    )

    value = re.sub(
        r'AIza[A-Za-z0-9_\-]{20,}',
        '<redacted>',
        value,
    )

    for name in (
        'ZAI_API_KEY', 'ZAI_KEY', 'ZHIPU_API_KEY', 'ZHIPUAI_API_KEY',
        'KIMI_API_KEY', 'KIMI_CODE_API_KEY', 'KIMI_KEY',
        'MINIMAX_KEY', 'MINIMAX_API_KEY', 'QWEN_API_KEY',
        'BAILIAN_TOKEN_PLAN_API_KEY', 'CURSOR_SESSION_TOKEN',
    ):
        secret = provider_environment().get(name, '')
        if len(secret) >= 8:
            value = value.replace(secret, '<redacted>')

    value = re.sub(
        r'eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+',
        '<redacted-jwt>',
        value,
    )

    return value


def extract_api_message(body):
    """Extract a useful API message from JSON/text error bodies."""
    if not body:
        return ''

    safe_body = sanitize_error_text(body)

    try:
        obj = json.loads(safe_body)
        if isinstance(obj, dict):
            if isinstance(obj.get('error'), dict):
                err = obj['error']
                return str(err.get('message') or err.get('status') or '').strip()
            return str(obj.get('message') or '').strip()
    except Exception:
        pass

    return safe_body.strip().splitlines()[0][:180]


def parse_retry_after(value):
    """
    Parse Retry-After header.

    Supports:
      - seconds
      - HTTP-date
    """
    if not value:
        return None

    raw = str(value).strip()
    if not raw:
        return None

    try:
        seconds = int(raw)
        if seconds < 0:
            return None

        return {
            'retry_after_seconds': seconds,
            'retry_after_time': (
                datetime.now(timezone.utc) + timedelta(seconds=seconds)
            ).isoformat(),
        }
    except Exception:
        pass

    try:
        dt = parsedate_to_datetime(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)

        dt = dt.astimezone(timezone.utc)
        seconds = max(0, int((dt - datetime.now(timezone.utc)).total_seconds()))

        return {
            'retry_after_seconds': seconds,
            'retry_after_time': dt.isoformat(),
        }
    except Exception:
        return None


def classify_http_failure(provider, code, body='', context=None):
    """
    Normalize HTTP failures into user-facing messages.

    SECURITY:
    Never exposes full error bodies that might contain sensitive data.
    """
    context = context or {}
    api_msg = extract_api_message(body)

    fail_reason = 'http_error'
    error = f'HTTP {code}'

    if code == 401:
        fail_reason = 'auth_required'
        error = 'Authentication required'
    elif code == 403:
        fail_reason = 'forbidden'
        error = 'Permission denied'
    elif code == 404:
        fail_reason = 'not_found'
        error = 'API endpoint not found'
    elif code == 429:
        fail_reason = 'rate_limited'
        error = 'Rate limited'
    elif 500 <= code <= 599:
        fail_reason = 'server_error'
        error = 'Provider service error'

    if api_msg:
        error = f'{error}: {api_msg}'

    return {
        'fail_reason': fail_reason,
        'http_code': code,
        'error': sanitize_error_text(error),
    }


def classify_exception_failure(err):
    """Normalize non-HTTP failures."""
    if isinstance(err, urllib.error.URLError):
        reason = getattr(err, 'reason', None)

        if isinstance(reason, (TimeoutError, socket.timeout)):
            return {
                'fail_reason': 'timeout',
                'error': 'Request timed out',
            }

        return {
            'fail_reason': 'network_error',
            'error': sanitize_error_text(f'Network error: {reason}'),
        }

    if isinstance(err, TimeoutError):
        return {
            'fail_reason': 'timeout',
            'error': 'Request timed out',
        }

    if isinstance(err, KeyError):
        return {
            'fail_reason': 'invalid_credentials',
            'error': sanitize_error_text(f'Missing credential field: {err}'),
        }

    return {
        'fail_reason': 'unknown_error',
        'error': sanitize_error_text(str(err)),
    }


def parse_iso8601(raw):
    """Parse ISO 8601 date safely."""
    if not raw or not isinstance(raw, str):
        return None

    value = raw.strip()
    if not value:
        return None

    try:
        if value.endswith('Z'):
            value = value[:-1] + '+00:00'

        dt = datetime.fromisoformat(value)

        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)

        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def base64url_decode(data):
    """Decode base64url string safely."""
    if not data:
        return None

    try:
        padded = data.replace('-', '+').replace('_', '/')
        padded += '=' * (-len(padded) % 4)
        return base64.b64decode(padded)
    except Exception:
        return None


def extract_account_id_from_jwt(token):
    """
    Extract ChatGPT/OpenAI account ID from JWT claims.

    Checks:
      - chatgpt_account_id
      - https://api.openai.com/auth.chatgpt_account_id
      - organizations[0].id
    """
    if not token or not isinstance(token, str):
        return None

    parts = token.split('.')
    if len(parts) != 3:
        return None

    payload = base64url_decode(parts[1])
    if not payload:
        return None

    try:
        claims = json.loads(payload.decode('utf-8', errors='replace'))
    except Exception:
        return None

    if isinstance(claims.get('chatgpt_account_id'), str):
        return claims['chatgpt_account_id']

    openai_auth = claims.get('https://api.openai.com/auth')
    if isinstance(openai_auth, dict):
        if isinstance(openai_auth.get('chatgpt_account_id'), str):
            return openai_auth['chatgpt_account_id']

    orgs = claims.get('organizations')
    if isinstance(orgs, list) and orgs:
        first = orgs[0]
        if isinstance(first, dict) and isinstance(first.get('id'), str):
            return first['id']

    return None


def decode_jwt_email(id_token):
    """Decode email from id_token JWT payload. Does not verify signature."""
    if not id_token or not isinstance(id_token, str):
        return ''

    parts = id_token.split('.')
    if len(parts) != 3:
        return ''

    payload = base64url_decode(parts[1])
    if not payload:
        return ''

    try:
        claims = json.loads(payload.decode('utf-8', errors='replace'))
        email = claims.get('email')
        return email if isinstance(email, str) else ''
    except Exception:
        return ''


def parse_bool_env(value):
    """Parse common boolean environment values."""
    if value is None:
        return False

    return str(value).strip().lower() in (
        '1',
        'true',
        'yes',
        'y',
        'on',
    )


def parse_simple_dotenv(path):
    """
    Parse a simple .env file without external dependencies.

    Supports:
      KEY=value
      KEY="value"
      KEY='value'
      export KEY=value

    Does not execute shell expressions.
    """
    data = {}

    if not path.exists():
        return data

    try:
        for raw_line in path.read_text(errors='replace').splitlines():
            line = raw_line.strip()

            if not line or line.startswith('#'):
                continue

            if line.startswith('export '):
                line = line[len('export '):].strip()

            if '=' not in line:
                continue

            key, value = line.split('=', 1)
            key = key.strip()
            value = value.strip()

            if not key:
                continue

            if (
                len(value) >= 2
                and (
                    (value.startswith('"') and value.endswith('"'))
                    or (value.startswith("'") and value.endswith("'"))
                )
            ):
                value = value[1:-1]

            data[key] = value

    except Exception:
        return {}

    return data


def write_json_atomic(path, data):
    """Replace a JSON credential file without exposing partial writes."""
    mode = path.stat().st_mode & 0o777
    fd, temp_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f'.{path.name}.',
        text=True,
    )

    try:
        with os.fdopen(fd, 'w') as temp_file:
            json.dump(data, temp_file, indent=2, ensure_ascii=False)
            temp_file.flush()
            os.fsync(temp_file.fileno())

        os.chmod(temp_name, mode)
        os.replace(temp_name, path)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


# ─────────────────────────────────────────────────────────────────────────────
# Codex helpers
# ─────────────────────────────────────────────────────────────────────────────

def codex_home_path():
    """
    Return Codex home path.

    Priority:
      1. CODEX_HOME
      2. ~/.codex
    """
    custom = os.environ.get('CODEX_HOME')
    if custom and custom.strip():
        return Path(custom).expanduser()

    return Path.home() / '.codex'


def read_chatgpt_base_url_from_codex_config():
    """
    Read chatgpt_base_url from Codex config.

    Path:
      ${CODEX_HOME:-~/.codex}/config.toml

    No TOML dependency is used.
    """
    config_path = codex_home_path() / 'config.toml'

    if not config_path.exists():
        return None

    try:
        for raw_line in config_path.read_text(errors='replace').splitlines():
            line = raw_line.split('#', 1)[0].strip()

            if not line or line.startswith('#'):
                continue

            if not line.startswith('chatgpt_base_url'):
                continue

            parts = line.split('=', 1)

            if len(parts) != 2:
                continue

            value = parts[1].strip().strip('"').strip("'").strip()
            return value or None
    except Exception:
        return None

    return None


def resolve_codex_usage_url():
    """
    Resolve Codex usage endpoint.

    Default:
      https://chatgpt.com/backend-api/wham/usage

    If chatgpt_base_url is set:
      - if it contains /backend-api -> append /wham/usage
      - otherwise -> append /api/codex/usage
    """
    default_base = 'https://chatgpt.com/backend-api'
    base = read_chatgpt_base_url_from_codex_config() or default_base
    base = base.rstrip('/')

    if (
        base.startswith('https://chatgpt.com')
        or base.startswith('https://chat.openai.com')
    ) and '/backend-api' not in base:
        base += '/backend-api'

    if '/backend-api' in base:
        return base + '/wham/usage'

    return base + '/api/codex/usage'


def load_opencode_auth_paths():
    """
    Return possible OpenCode auth.json paths.

    Linux priority:
      1. ${XDG_DATA_HOME}/opencode/auth.json
      2. ~/.local/share/opencode/auth.json

    Extra compatibility:
      3. OPENCODE_CONFIG_DIR/auth.json
      4. ${XDG_CONFIG_HOME}/opencode/auth.json
      5. ~/.config/opencode/auth.json
      6. ~/.opencode/auth.json
      7. macOS Application Support path
    """
    paths = []

    xdg_data_home = os.environ.get('XDG_DATA_HOME')
    if xdg_data_home and xdg_data_home.strip():
        paths.append(Path(xdg_data_home) / 'opencode' / 'auth.json')

    paths.append(Path.home() / '.local' / 'share' / 'opencode' / 'auth.json')

    opencode_config_dir = os.environ.get('OPENCODE_CONFIG_DIR')
    if opencode_config_dir and opencode_config_dir.strip():
        paths.append(Path(opencode_config_dir) / 'auth.json')

    xdg_config_home = os.environ.get('XDG_CONFIG_HOME')
    if xdg_config_home and xdg_config_home.strip():
        paths.append(Path(xdg_config_home) / 'opencode' / 'auth.json')

    paths.append(Path.home() / '.config' / 'opencode' / 'auth.json')
    paths.append(Path.home() / '.opencode' / 'auth.json')
    paths.append(Path.home() / 'Library' / 'Application Support' / 'opencode' / 'auth.json')

    unique = []
    seen = set()

    for path in paths:
        key = str(path)
        if key not in seen:
            unique.append(path)
            seen.add(key)

    return unique


def codex_credential_needs_refresh(credentials):
    """Check if Codex OAuth credentials should be refreshed."""
    refresh_token = credentials.get('refresh_token') or ''

    if not refresh_token:
        return False

    expires_at = credentials.get('expires_at')

    if isinstance(expires_at, datetime):
        return datetime.now(timezone.utc) + timedelta(seconds=60) >= expires_at

    last_refresh = credentials.get('last_refresh')

    if not isinstance(last_refresh, datetime):
        return True

    return datetime.now(timezone.utc) - last_refresh > timedelta(days=8)


def load_codex_credentials():
    """
    Load OpenAI Codex credentials from:
      1. ${CODEX_HOME:-~/.codex}/auth.json
      2. OpenCode auth.json

    Supported Codex auth.json formats:
      - {"OPENAI_API_KEY": "..."}
      - {"tokens": {"access_token": "...", "refresh_token": "...", ...}}
    """
    candidates = []

    codex_auth_path = codex_home_path() / 'auth.json'

    if codex_auth_path.exists():
        try:
            root = json.loads(codex_auth_path.read_text(errors='replace'))

            api_key = root.get('OPENAI_API_KEY')
            if (
                isinstance(api_key, str)
                and api_key.strip()
                and read_chatgpt_base_url_from_codex_config()
            ):
                candidates.append({
                    'source': 'codex_api_key',
                    'path': codex_auth_path,
                    'access_token': api_key.strip(),
                    'refresh_token': '',
                    'account_id': None,
                    'last_refresh': None,
                    'expires_at': None,
                })

            tokens = root.get('tokens')
            if isinstance(tokens, dict):
                access = tokens.get('access_token')
                refresh = tokens.get('refresh_token')

                if (
                    isinstance(access, str)
                    and access.strip()
                    and isinstance(refresh, str)
                    and refresh.strip()
                ):
                    account_id = (
                        tokens.get('account_id')
                        or extract_account_id_from_jwt(tokens.get('id_token'))
                        or extract_account_id_from_jwt(access)
                    )

                    candidates.append({
                        'source': 'codex_oauth',
                        'path': codex_auth_path,
                        'access_token': access.strip(),
                        'refresh_token': refresh.strip(),
                        'account_id': account_id,
                        'last_refresh': parse_iso8601(root.get('last_refresh')),
                        'expires_at': None,
                    })

        except Exception:
            pass

    for opencode_path in load_opencode_auth_paths():
        if not opencode_path.exists():
            continue

        try:
            root = json.loads(opencode_path.read_text(errors='replace'))
            oauth = root.get('openai')

            if not isinstance(oauth, dict):
                continue

            if oauth.get('type') != 'oauth':
                continue

            access = oauth.get('access')
            refresh = oauth.get('refresh')

            if not isinstance(access, str) or not access.strip():
                continue

            if not isinstance(refresh, str) or not refresh.strip():
                continue

            expires_at = None
            expires = oauth.get('expires')

            if isinstance(expires, (int, float)):
                expires_at = datetime.fromtimestamp(expires / 1000.0, tz=timezone.utc)

            candidates.append({
                'source': 'opencode_oauth',
                'path': opencode_path,
                'access_token': access.strip(),
                'refresh_token': refresh.strip(),
                'account_id': oauth.get('accountId') or extract_account_id_from_jwt(access),
                'last_refresh': None,
                'expires_at': expires_at,
            })

            break

        except Exception:
            continue

    return candidates


def save_refreshed_codex_credentials(credentials, refreshed):
    """
    Save refreshed OpenAI OAuth tokens back to their original auth file.

    SECURITY:
    - Only writes to files already used as credential sources.
    - Does not print tokens.
    """
    path = credentials.get('path')
    source = credentials.get('source')

    if not isinstance(path, Path) or not path.exists():
        return

    try:
        root = json.loads(path.read_text(errors='replace'))
    except Exception:
        return

    access_token = refreshed.get('access_token') or credentials.get('access_token')
    refresh_token = refreshed.get('refresh_token') or credentials.get('refresh_token')
    id_token = refreshed.get('id_token')
    expires_in = refreshed.get('expires_in')

    if source == 'codex_oauth':
        tokens = root.get('tokens')

        if not isinstance(tokens, dict):
            tokens = {}

        tokens['access_token'] = access_token
        tokens['refresh_token'] = refresh_token

        if id_token:
            tokens['id_token'] = id_token

        account_id = (
            credentials.get('account_id')
            or extract_account_id_from_jwt(id_token)
            or extract_account_id_from_jwt(access_token)
        )

        if account_id:
            tokens['account_id'] = account_id

        root['tokens'] = tokens
        root['last_refresh'] = iso_now()

    elif source == 'opencode_oauth':
        oauth = root.get('openai')

        if not isinstance(oauth, dict):
            oauth = {}

        oauth['type'] = 'oauth'
        oauth['access'] = access_token
        oauth['refresh'] = refresh_token

        account_id = (
            credentials.get('account_id')
            or extract_account_id_from_jwt(id_token)
            or extract_account_id_from_jwt(access_token)
        )

        if account_id:
            oauth['accountId'] = account_id

        if isinstance(expires_in, int):
            oauth['expires'] = int(
                (datetime.now(timezone.utc).timestamp() + expires_in) * 1000
            )

        root['openai'] = oauth

    else:
        return

    try:
        write_json_atomic(path, root)
    except Exception:
        return


def refresh_codex_token(credentials):
    """
    Refresh OpenAI Codex OAuth token.

    SECURITY:
    - Never outputs access/refresh tokens.
    - Error messages are safe.
    """
    refresh_token = credentials.get('refresh_token')

    if not refresh_token:
        raise RuntimeError('No refresh token found')

    token_url = 'https://auth.openai.com/oauth/token'

    body = json.dumps({
        'client_id': 'app_EMoamEEZ73f0CkXaXp7hrann',
        'grant_type': 'refresh_token',
        'refresh_token': refresh_token,
        'scope': 'openid profile email',
    }).encode()

    req = urllib.request.Request(
        token_url,
        data=body,
        headers={
            'Content-Type': 'application/json',
            'Accept': 'application/json',
            'User-Agent': 'AIUsageMonitor',
        },
        method='POST',
    )

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            decoded = json.loads(resp.read())

    except urllib.error.HTTPError as e:
        body_text = sanitize_error_text(read_http_error_body(e))
        body_lower = body_text.lower()

        if e.code == 429:
            retry_info = parse_retry_after(e.headers.get('Retry-After'))
            msg = 'Refresh rate limited'

            if retry_info and retry_info.get('retry_after_seconds') is not None:
                msg += f": retry after {retry_info['retry_after_seconds']} seconds"

            raise RuntimeError(msg)

        if e.code == 401:
            if 'refresh_token_reused' in body_lower:
                raise RuntimeError("Codex auth is stale: refresh token reused. Run 'codex login'.")

            raise RuntimeError("Codex token expired. Run 'codex login'.")

        raise RuntimeError(f'Token refresh failed: HTTP {e.code}')

    new_credentials = credentials.copy()
    new_credentials['access_token'] = decoded.get('access_token') or credentials.get('access_token')
    new_credentials['refresh_token'] = decoded.get('refresh_token') or credentials.get('refresh_token')
    new_credentials['account_id'] = (
        credentials.get('account_id')
        or extract_account_id_from_jwt(decoded.get('id_token'))
        or extract_account_id_from_jwt(new_credentials.get('access_token'))
    )
    new_credentials['last_refresh'] = datetime.now(timezone.utc)

    expires_in = decoded.get('expires_in')

    if isinstance(expires_in, int):
        new_credentials['expires_at'] = datetime.now(timezone.utc) + timedelta(seconds=expires_in)

    save_refreshed_codex_credentials(credentials, decoded)

    return new_credentials


def fetch_codex_usage(access_token, account_id=None):
    """Fetch Codex usage from ChatGPT/Codex usage endpoint."""
    req = urllib.request.Request(
        resolve_codex_usage_url(),
        headers={
            'Authorization': f'Bearer {access_token}',
            'Accept': 'application/json',
            'User-Agent': 'AIUsageMonitor',
        },
        method='GET',
    )

    if account_id:
        req.add_header('ChatGPT-Account-Id', account_id)

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())

    except urllib.error.HTTPError as e:
        e.retry_after_info = parse_retry_after(e.headers.get('Retry-After'))
        raise


def map_codex_window(window, rpc=False):
    """Normalize OAuth and app-server Codex rate-limit windows."""
    if not isinstance(window, dict):
        return None
    used = window.get('usedPercent') if rpc else window.get('used_percent')
    reset = window.get('resetsAt') if rpc else window.get('reset_at')
    seconds = None
    if rpc:
        minutes = flexible_number(window.get('windowDurationMins'))
        seconds = int(minutes * 60) if minutes is not None else None
    else:
        seconds = window.get('limit_window_seconds')
    used = flexible_number(used)
    if used is None:
        return None
    return {
        'used_pct': clamp_percentage(used),
        'reset_time': unix_to_iso(reset),
        'window_seconds': seconds,
    }


def normalize_codex_windows(primary, secondary):
    """Place Codex session and weekly windows by duration, not API position."""
    def role(window):
        seconds = (window or {}).get('window_seconds')
        if seconds == 5 * 3600:
            return 'session'
        if seconds == 7 * 86400:
            return 'weekly'
        return 'unknown'

    primary_role = role(primary)
    secondary_role = role(secondary)
    if primary and secondary:
        if primary_role == 'weekly' and secondary_role in ('session', 'unknown'):
            return secondary, primary
        return primary, secondary
    if primary:
        return (None, primary) if primary_role == 'weekly' else (primary, None)
    if secondary:
        return (secondary, None) if secondary_role in ('session', 'unknown') else (None, secondary)
    return None, None


def codex_extra_rate_windows(usage):
    """Lossily map model-specific Codex limits without affecting core windows."""
    raw_limits = usage.get('additional_rate_limits')
    if not isinstance(raw_limits, list):
        return []
    windows = []
    used_ids = set()
    for entry in raw_limits:
        if not isinstance(entry, dict):
            continue
        limit_name = str(entry.get('limit_name') or '').strip()
        metered_feature = str(entry.get('metered_feature') or '').strip()
        name = limit_name or metered_feature
        rate_limit = entry.get('rate_limit')
        if not name or not isinstance(rate_limit, dict):
            continue
        candidates = [
            ('primary', rate_limit.get('primary_window')),
            ('secondary', rate_limit.get('secondary_window')),
        ]
        is_spark = any('spark' in value.lower() for value in (limit_name, metered_feature))
        if not is_spark:
            candidates = [next((item for item in candidates if isinstance(item[1], dict)), (None, None))]
        for kind, raw_window in candidates:
            window = map_codex_window(raw_window)
            if not window:
                continue
            slug = re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-')
            window_id = f'codex-{slug}'
            title = name
            if is_spark:
                seconds = window.get('window_seconds') or 0
                weekly = seconds >= 6 * 86400 or kind == 'secondary'
                window_id = 'codex-spark-weekly' if weekly else 'codex-spark'
                title = 'Codex Spark Weekly' if weekly else 'Codex Spark 5-hour'
            if not window_id or window_id in used_ids:
                continue
            used_ids.add(window_id)
            windows.append({'id': window_id, 'title': title, **window})
    return windows


def codex_spend_control_window(usage):
    """Map Business/Team monthly individual credit limits when present."""
    rate_limit = usage.get('rate_limit') or {}
    spend_control = usage.get('spend_control') or usage.get('spendControl') or {}
    if not isinstance(rate_limit, dict):
        rate_limit = {}
    if not isinstance(spend_control, dict):
        spend_control = {}
    candidates = [
        usage.get('individual_limit') or usage.get('individualLimit'),
        rate_limit.get('individual_limit') or rate_limit.get('individualLimit'),
        spend_control.get('individual_limit') or spend_control.get('individualLimit'),
    ]
    details = next((value for value in candidates if isinstance(value, dict)), None)
    if not details:
        return None
    remaining_pct = flexible_number(
        details.get('remaining_percent', details.get('remainingPercent'))
    )
    used = flexible_number(details.get('used'))
    limit = flexible_number(details.get('limit'))
    if remaining_pct is not None:
        used_pct = 100 - remaining_pct
    elif used is not None and limit:
        used_pct = used / limit * 100
    else:
        return None
    reset = details.get('resets_at', details.get('resetsAt', details.get('reset_at')))
    return {
        'id': 'codex-monthly-credits',
        'title': 'Monthly credits',
        'used_pct': clamp_percentage(used_pct),
        'reset_time': unix_to_iso(reset),
        'window_seconds': None,
        'used': used,
        'limit': limit,
    }


def fetch_codex_reset_credits(access_token, account_id=None):
    """Read available reset credits without redeeming or modifying them."""
    usage_url = resolve_codex_usage_url()
    if '/wham/usage' in usage_url:
        url = usage_url.rsplit('/wham/usage', 1)[0] + '/wham/rate-limit-reset-credits'
    else:
        return None
    headers = {
        'Authorization': f'Bearer {access_token}',
        'Accept': 'application/json',
        'User-Agent': 'AIUsageMonitor',
        'OpenAI-Beta': 'codex-1',
        'originator': 'Codex Desktop',
    }
    if account_id:
        headers['ChatGPT-Account-ID'] = account_id
    payload = fetch_json_request(url, headers=headers, timeout=4)
    available_count = payload.get('available_count')
    if not isinstance(available_count, int) or available_count < 0:
        return None
    now = datetime.now(timezone.utc)
    expiries = []
    for credit in payload.get('credits') or []:
        if not isinstance(credit, dict) or credit.get('status') != 'available':
            continue
        expires = parse_iso8601(credit.get('expires_at'))
        if expires and expires > now:
            expiries.append(expires)
    return {
        'available_count': available_count,
        'next_expiry': min(expiries).isoformat() if expiries else None,
    }


def fetch_codex_monthly_spend_control(access_token, account_id):
    """Best-effort Business/Team monthly spend-control usage."""
    usage_url = resolve_codex_usage_url()
    if '/wham/usage' not in usage_url or not account_id:
        return None
    base = usage_url.rsplit('/wham/usage', 1)[0]
    encoded_account = urllib.parse.quote(str(account_id), safe='')
    url = (
        f'{base}/accounts/{encoded_account}'
        '/spend-controls/current-user/monthly-usage'
    )
    payload = fetch_json_request(
        url,
        headers={
            'Authorization': f'Bearer {access_token}',
            'ChatGPT-Account-Id': str(account_id),
            'Accept': 'application/json',
            'User-Agent': 'AIUsageMonitor',
        },
        timeout=4,
    )
    effective = payload.get('effective_monthly_limit') or {}
    if not isinstance(effective, dict):
        return None
    enforcement = str(effective.get('enforcement_mode') or '').lower()
    if enforcement in ('none', 'off', 'disabled', 'no_limit'):
        return None
    limit = flexible_number(effective.get('limit'))
    used = flexible_number(payload.get('current_month_usage')) or 0
    if not limit or limit <= 0:
        return None
    return {
        'id': 'codex-monthly-credits',
        'title': 'Monthly credits',
        'used_pct': clamp_percentage(max(0, used) / limit * 100),
        'reset_time': None,
        'window_seconds': None,
        'used': max(0, used),
        'limit': limit,
    }


def resolve_codex_executable():
    """Find Codex CLI in PATH and common Linux user-install locations."""
    resolved = shutil.which('codex')
    if resolved:
        return resolved
    home = Path.home()
    candidates = [
        home / '.local' / 'bin' / 'codex',
        home / '.npm-global' / 'bin' / 'codex',
        home / '.bun' / 'bin' / 'codex',
    ]
    candidates.extend(sorted((home / '.nvm' / 'versions' / 'node').glob('*/bin/codex'), reverse=True))
    return next((str(path) for path in candidates if path.is_file() and os.access(path, os.X_OK)), None)


def stop_rpc_process(process):
    """Bounded TERM-to-KILL teardown for a Codex app-server process group."""
    if process.poll() is not None:
        return
    try:
        process.stdin.close()
    except Exception:
        pass
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=0.5)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            pass


def rpc_read_message(process, selector, request_id, timeout):
    """Read one matching newline-delimited JSON-RPC response."""
    deadline = time.monotonic() + timeout
    buffer = b''
    while time.monotonic() < deadline:
        events = selector.select(max(0, deadline - time.monotonic()))
        if not events:
            break
        chunk = os.read(process.stdout.fileno(), 65536)
        if not chunk:
            break
        buffer += chunk
        if len(buffer) > 1024 * 1024:
            raise RuntimeError('Codex RPC response exceeded 1 MiB')
        while b'\n' in buffer:
            raw_line, buffer = buffer.split(b'\n', 1)
            try:
                message = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            if message.get('id') != request_id:
                continue
            if isinstance(message.get('error'), dict):
                text = sanitize_error_text(message['error'].get('message') or 'Codex RPC request failed')
                raise RuntimeError(text)
            result_data = message.get('result')
            if isinstance(result_data, dict):
                return result_data
            raise RuntimeError('Codex RPC returned an invalid result')
    raise TimeoutError('Codex app-server request timed out')


def rpc_send(process, payload):
    process.stdin.write(json.dumps(payload, separators=(',', ':')) + '\n')
    process.stdin.flush()


def fetch_codex_rpc_provider():
    """Read Codex limits through the CLI's sandboxed app-server RPC."""
    executable = resolve_codex_executable()
    if not executable:
        return None
    process = subprocess.Popen(
        [executable, '-s', 'read-only', '-a', 'untrusted', 'app-server'],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        bufsize=1,
        start_new_session=True,
    )
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    try:
        rpc_send(process, {
            'id': 1,
            'method': 'initialize',
            'params': {'clientInfo': {'name': 'ai-usage-monitor', 'version': '1.0'}},
        })
        rpc_read_message(process, selector, 1, 8)
        rpc_send(process, {'method': 'initialized', 'params': {}})
        rpc_send(process, {'id': 2, 'method': 'account/rateLimits/read', 'params': {}})
        limits_result = rpc_read_message(process, selector, 2, 4)
        rpc_send(process, {'id': 3, 'method': 'account/read', 'params': {}})
        try:
            account_result = rpc_read_message(process, selector, 3, 4)
        except Exception:
            account_result = {}
    finally:
        selector.close()
        stop_rpc_process(process)

    limits = limits_result.get('rateLimits') or {}
    primary = map_codex_window(limits.get('primary'), rpc=True)
    secondary = map_codex_window(limits.get('secondary'), rpc=True)
    primary, secondary = normalize_codex_windows(primary, secondary)
    account = account_result.get('account') or {}
    if not isinstance(account, dict):
        account = {}
    email = account.get('email') if isinstance(account, dict) else ''
    result_data = {
        'installed': True,
        'authenticated': True,
        'has_data': bool(primary or secondary),
        'source': 'cli_rpc',
        'credential_source': 'codex_cli',
        'five_hour_pct': primary.get('used_pct') if primary else None,
        'five_hour_reset': primary.get('reset_time') if primary else None,
        'five_hour_window_seconds': primary.get('window_seconds') if primary else None,
        'seven_day_pct': secondary.get('used_pct') if secondary else None,
        'seven_day_reset': secondary.get('reset_time') if secondary else None,
        'seven_day_window_seconds': secondary.get('window_seconds') if secondary else None,
        'plan_type': limits.get('planType') or limits.get('plan_type') or account.get('planType') or '',
        'account_label': email or '',
        'extra_rate_windows': [],
    }
    credits = limits.get('credits') or {}
    if isinstance(credits, dict):
        result_data['credits_balance'] = credits.get('balance')
        result_data['credits_unlimited'] = credits.get('unlimited') is True
    rate_limits_by_id = (
        limits_result.get('rateLimitsByLimitId')
        or limits_result.get('rate_limits_by_limit_id')
        or {}
    )
    candidates = [limits]
    if isinstance(rate_limits_by_id, dict):
        candidates.extend(value for value in rate_limits_by_id.values() if isinstance(value, dict))
    for candidate in candidates:
        individual = candidate.get('individualLimit') or candidate.get('individual_limit')
        monthly = codex_spend_control_window({'individual_limit': individual})
        if monthly:
            result_data['extra_rate_windows'].append(monthly)
            break
    has_credits = result_data.get('credits_unlimited') is True or result_data.get('credits_balance') is not None
    result_data['has_data'] = bool(primary or secondary or result_data['extra_rate_windows'] or has_credits)
    if not result_data['has_data']:
        return None
    return result_data


def map_codex_api_result(usage, credentials):
    """Map the OAuth usage payload and best-effort additive Codex data."""
    rate_limit = usage.get('rate_limit') or {}
    if not isinstance(rate_limit, dict):
        rate_limit = {}
    primary = map_codex_window(rate_limit.get('primary_window'))
    secondary = map_codex_window(rate_limit.get('secondary_window'))
    primary, secondary = normalize_codex_windows(primary, secondary)
    extra_windows = codex_extra_rate_windows(usage)
    spend_control = codex_spend_control_window(usage)
    account_id = credentials.get('account_id') or usage.get('account_id')
    plan = str(usage.get('plan_type') or '').lower()
    if (
        not spend_control
        and ('spend_control' in usage or 'spendControl' in usage)
        and plan in ('team', 'business', 'education', 'quorum', 'k12', 'enterprise', 'edu', 'free_workspace')
        and account_id
    ):
        try:
            spend_control = fetch_codex_monthly_spend_control(
                credentials['access_token'], account_id
            )
        except Exception:
            spend_control = None
    if spend_control:
        extra_windows.append(spend_control)
    result_data = {
        'installed': True,
        'authenticated': True,
        'has_data': bool(primary or secondary),
        'source': 'api_usage',
        'five_hour_pct': primary.get('used_pct') if primary else None,
        'seven_day_pct': secondary.get('used_pct') if secondary else None,
        'five_hour_reset': primary.get('reset_time') if primary else None,
        'seven_day_reset': secondary.get('reset_time') if secondary else None,
        'five_hour_window_seconds': primary.get('window_seconds') if primary else None,
        'seven_day_window_seconds': secondary.get('window_seconds') if secondary else None,
        'plan_type': usage.get('plan_type') or '',
        'account_id': account_id or '',
        'credential_source': credentials.get('source') or '',
        'extra_rate_windows': extra_windows,
    }
    credits = usage.get('credits') or {}
    if isinstance(credits, dict):
        result_data['credits_balance'] = flexible_number(credits.get('balance'))
        result_data['credits_unlimited'] = credits.get('unlimited') is True
    has_credits = result_data.get('credits_unlimited') is True or result_data.get('credits_balance') is not None
    result_data['has_data'] = bool(primary or secondary or extra_windows or has_credits)
    try:
        reset_credits = fetch_codex_reset_credits(
            credentials['access_token'],
            credentials.get('account_id') or usage.get('account_id'),
        )
        if reset_credits:
            result_data['reset_credits_available'] = reset_credits['available_count']
            result_data['reset_credits_next_expiry'] = reset_credits['next_expiry']
    except Exception:
        pass
    return result_data


def should_try_next_codex_credential(error_info):
    """Decide whether the next credential candidate should be tried."""
    fail_reason = error_info.get('fail_reason')
    error_text = (error_info.get('error') or '').lower()

    if fail_reason in ('auth_required', 'forbidden', 'invalid_credentials', 'auth_failed'):
        return True

    return (
        'refresh token reused' in error_text
        or 'token expired' in error_text
        or 'unauthorized' in error_text
        or 'invalid api key' in error_text
        or 'invalid_api_key' in error_text
        or '401' in error_text
        or '403' in error_text
    )


def read_codex_local_jsonl_fallback():
    """
    Old fallback:
    Read last token_count from ${CODEX_HOME:-~/.codex}/sessions/**/*.jsonl.

    This is less accurate than API usage, but useful if endpoint is unavailable.
    """
    codex_sessions_dir = codex_home_path() / 'sessions'

    if not codex_sessions_dir.exists():
        return {
            'installed': False,
            'authenticated': False,
            'has_data': False,
        }

    files = sorted(
        glob.glob(str(codex_sessions_dir / '**' / '*.jsonl'), recursive=True)
    )

    if not files:
        return {
            'installed': True,
            'authenticated': None,
            'has_data': False,
            'source': 'local_jsonl_fallback',
        }

    last_tc_payload = None
    last_model = ''

    for sf in reversed(files):
        main_in_file = None
        fallback_in_file = None

        try:
            with open(sf, errors='replace') as f:
                for line in f:
                    line = line.strip()

                    if not line:
                        continue

                    try:
                        obj = json.loads(line)

                        if obj.get('type') == 'event_msg':
                            payload = obj.get('payload') or {}

                            if payload.get('type') == 'token_count':
                                rate_limits = payload.get('rate_limits') or {}
                                primary = rate_limits.get('primary')

                                # Ignore buckets without a real primary usage value.
                                if isinstance(primary, dict) and primary.get('used_percent') is not None:
                                    if rate_limits.get('limit_id') == 'codex':
                                        main_in_file = payload
                                    else:
                                        fallback_in_file = payload

                        elif obj.get('type') == 'turn_context':
                            m = (obj.get('payload') or {}).get('model', '')
                            if m:
                                last_model = m

                    except json.JSONDecodeError:
                        continue

        except OSError:
            continue

        chosen = main_in_file or fallback_in_file

        if chosen:
            last_tc_payload = chosen
            break

    if not last_tc_payload:
        return {
            'installed': True,
            'authenticated': None,
            'has_data': False,
            'source': 'local_jsonl_fallback',
        }

    rl = last_tc_payload.get('rate_limits') or {}
    primary = rl.get('primary') or {}
    secondary = rl.get('secondary') or {}

    return {
        'installed': True,
        'authenticated': None,
        'has_data': True,
        'source': 'local_jsonl_fallback',
        'five_hour_pct': primary.get('used_percent', 0),
        'seven_day_pct': secondary.get('used_percent', 0),
        'five_hour_reset': unix_to_iso(primary.get('resets_at')),
        'seven_day_reset': unix_to_iso(secondary.get('resets_at')),
        'plan_type': rl.get('plan_type') or '',
        'model': last_model,
    }


def fetch_codex_provider():
    """
    Fetch OpenAI Codex usage.

    Main method:
      - API/OAuth usage endpoint

    Fallback:
      - local JSONL sessions
    """
    credentials_list = load_codex_credentials()

    if not credentials_list:
        try:
            rpc_result = fetch_codex_rpc_provider()
            if rpc_result:
                return rpc_result
        except Exception:
            pass
        local = read_codex_local_jsonl_fallback()

        if local.get('installed') and local.get('has_data'):
            local['warning'] = 'Codex auth not found, using local JSONL fallback'
            return local

        return {
            'installed': local.get('installed', False),
            'authenticated': False,
            'fail_reason': 'auth_required',
            'error': 'Codex auth not found',
        }

    last_error = None

    for candidate in credentials_list:
        credentials = candidate.copy()

        try:
            if codex_credential_needs_refresh(credentials):
                credentials = refresh_codex_token(credentials)

            usage = fetch_codex_usage(
                access_token=credentials['access_token'],
                account_id=credentials.get('account_id'),
            )

            return map_codex_api_result(usage, credentials)

        except urllib.error.HTTPError as e:
            body = read_http_error_body(e)
            err = classify_http_failure('codex', e.code, body)

            retry_info = getattr(e, 'retry_after_info', None)
            if retry_info:
                err.update(retry_info)

            last_error = err

            if should_try_next_codex_credential(err):
                continue

            break

        except Exception as e:
            err = classify_exception_failure(e)
            last_error = err

            if should_try_next_codex_credential(err):
                continue

            break

    if last_error and should_try_next_codex_credential(last_error):
        try:
            rpc_result = fetch_codex_rpc_provider()
            if rpc_result:
                rpc_result['warning'] = 'Codex OAuth failed, using CLI RPC'
                return rpc_result
        except Exception:
            pass

    local = read_codex_local_jsonl_fallback()

    if local.get('installed') and local.get('has_data'):
        local['warning'] = 'Codex API usage failed, using local JSONL fallback'

        if last_error:
            local['api_error'] = last_error

        return local

    return {
        'installed': True,
        'authenticated': False,
        **(last_error or {
            'fail_reason': 'unknown_error',
            'error': 'Failed to fetch Codex usage',
        }),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Gemini helpers
# ─────────────────────────────────────────────────────────────────────────────

def gemini_settings_path():
    """Return Gemini CLI settings path."""
    return Path.home() / '.gemini' / 'settings.json'


def gemini_oauth_path():
    """Return Gemini CLI OAuth credentials path."""
    return Path.home() / '.gemini' / 'oauth_creds.json'


def gemini_accounts_dir():
    """Return Gemini CLI additional accounts directory."""
    return Path.home() / '.gemini' / 'accounts'


def find_gemini_env_file():
    """
    Find Gemini CLI .env file.

    For monitor purposes:
      1. current/.gemini/.env and parent dirs
      2. ~/.gemini/.env
      3. ~/.env
    """
    paths = []

    try:
        current = Path.cwd().resolve()
        paths.append(current / '.gemini' / '.env')

        for parent in current.parents:
            paths.append(parent / '.gemini' / '.env')
    except Exception:
        pass

    paths.append(Path.home() / '.gemini' / '.env')
    paths.append(Path.home() / '.env')

    seen = set()

    for path in paths:
        key = str(path)

        if key in seen:
            continue

        seen.add(key)

        if path.exists():
            return path

    return None


def load_gemini_effective_env():
    """
    Load effective Gemini environment.

    Priority:
      1. Real process environment
      2. First Gemini .env file for keys not already present
    """
    effective = dict(os.environ)

    env_file = find_gemini_env_file()

    if env_file:
        dotenv_values = parse_simple_dotenv(env_file)

        for key, value in dotenv_values.items():
            if key not in effective:
                effective[key] = value

        effective['_GEMINI_ENV_FILE'] = str(env_file)

    return effective


def load_gemini_auth_type():
    """
    Read Gemini CLI auth type from ~/.gemini/settings.json.

    If file is missing, infer from env later.
    """
    path = gemini_settings_path()

    if not path.exists():
        return ''

    try:
        root = json.loads(path.read_text(errors='replace'))
        security = root.get('security') or {}
        auth = security.get('auth') or {}
        selected_type = auth.get('selectedType')

        if isinstance(selected_type, str) and selected_type.strip():
            return selected_type.strip()
    except Exception:
        pass

    return ''


def detect_gemini_auth_mode():
    """
    Detect Gemini auth mode.

    Supported auth_type:
      - oauth-personal
      - api-key
      - vertex-ai
    """
    env = load_gemini_effective_env()
    selected = load_gemini_auth_type().strip().lower()

    use_vertex = parse_bool_env(env.get('GOOGLE_GENAI_USE_VERTEXAI'))

    if use_vertex:
        return {
            'auth_type': 'vertex-ai',
            'env': env,
            'env_file': env.get('_GEMINI_ENV_FILE', ''),
        }

    if selected in ('vertex-ai', 'vertex_ai', 'vertex'):
        return {
            'auth_type': 'vertex-ai',
            'env': env,
            'env_file': env.get('_GEMINI_ENV_FILE', ''),
        }

    if selected in ('api-key', 'api_key', 'apikey', 'gemini-api-key'):
        return {
            'auth_type': 'api-key',
            'env': env,
            'env_file': env.get('_GEMINI_ENV_FILE', ''),
        }

    if env.get('GEMINI_API_KEY'):
        return {
            'auth_type': 'api-key',
            'env': env,
            'env_file': env.get('_GEMINI_ENV_FILE', ''),
        }

    if env.get('GOOGLE_API_KEY') and not selected:
        return {
            'auth_type': 'api-key',
            'env': env,
            'env_file': env.get('_GEMINI_ENV_FILE', ''),
        }

    if env.get('GOOGLE_API_KEY') and selected in ('api-key', 'api_key', 'apikey'):
        return {
            'auth_type': 'api-key',
            'env': env,
            'env_file': env.get('_GEMINI_ENV_FILE', ''),
        }

    return {
        'auth_type': 'oauth-personal',
        'env': env,
        'env_file': env.get('_GEMINI_ENV_FILE', ''),
    }


def load_gemini_credentials_from_path(path):
    """
    Load Gemini CLI OAuth credentials from selected JSON file.

    Supports:
      - access_token
      - refresh_token
      - client_id
      - client_secret
      - expiry_date in milliseconds
      - expiry in seconds
      - id_token for email label
    """
    if not path.exists():
        raise RuntimeError(f'Gemini OAuth credentials not found: {path}')

    root = json.loads(path.read_text(errors='replace'))

    access_token = root.get('access_token')
    if not isinstance(access_token, str) or not access_token.strip():
        raise RuntimeError('Gemini access token not found')

    expiry_date = None

    raw_expiry_date = root.get('expiry_date')
    if isinstance(raw_expiry_date, (int, float)):
        expiry_date = datetime.fromtimestamp(raw_expiry_date / 1000.0, tz=timezone.utc)

    raw_expiry = root.get('expiry')
    if expiry_date is None and isinstance(raw_expiry, (int, float)):
        expiry_date = datetime.fromtimestamp(raw_expiry, tz=timezone.utc)

    email = decode_jwt_email(root.get('id_token', ''))

    return {
        'path': path,
        'raw': root,
        'access_token': access_token.strip(),
        'refresh_token': root.get('refresh_token') or '',
        'client_id': root.get('client_id') or GEMINI_CLI_OAUTH_CLIENT_ID,
        'client_secret': root.get('client_secret') or GEMINI_CLI_OAUTH_CLIENT_SECRET,
        'expiry_date': expiry_date,
        'email': email,
    }


def load_gemini_credentials():
    """Load main Gemini CLI OAuth credentials."""
    return load_gemini_credentials_from_path(gemini_oauth_path())


def list_gemini_oauth_account_paths():
    """
    Return primary + additional Gemini OAuth account files.

    Primary:
      ~/.gemini/oauth_creds.json

    Additional:
      ~/.gemini/accounts/*.json
    """
    paths = []

    primary = gemini_oauth_path()
    if primary.exists():
        paths.append(primary)

    extra_dir = gemini_accounts_dir()

    if extra_dir.is_dir():
        for path in sorted(extra_dir.glob('*.json')):
            if path not in paths:
                paths.append(path)

    return paths


def gemini_token_needs_refresh(credentials):
    """Refresh Gemini token if it expires in the next 60 seconds."""
    expiry_date = credentials.get('expiry_date')

    if not isinstance(expiry_date, datetime):
        return False

    return datetime.now(timezone.utc) + timedelta(seconds=60) >= expiry_date


def refresh_gemini_token(creds_path, creds):
    """
    Refresh Gemini OAuth token using refresh_token.

    Returns:
      (success: bool, new_creds: dict | None, error_msg: str | None)

    SECURITY:
    This function handles sensitive credentials but never logs or outputs them.
    """
    try:
        refresh_token = creds.get('refresh_token')
        client_id = creds.get('client_id') or GEMINI_CLI_OAUTH_CLIENT_ID
        client_secret = creds.get('client_secret') or GEMINI_CLI_OAUTH_CLIENT_SECRET

        if not refresh_token:
            return False, None, 'No refresh token found'

        token_url = 'https://oauth2.googleapis.com/token'

        data = urllib.parse.urlencode({
            'client_id': client_id,
            'client_secret': client_secret,
            'refresh_token': refresh_token,
            'grant_type': 'refresh_token',
        }).encode()

        req = urllib.request.Request(
            token_url,
            data=data,
            headers={
                'Content-Type': 'application/x-www-form-urlencoded',
                'User-Agent': 'AIUsageMonitor',
            },
            method='POST',
        )

        with urllib.request.urlopen(req, timeout=10) as resp:
            token_data = json.loads(resp.read())

        new_raw = creds.copy()
        new_raw['access_token'] = token_data['access_token']

        if 'refresh_token' in token_data:
            new_raw['refresh_token'] = token_data['refresh_token']

        if 'id_token' in token_data:
            new_raw['id_token'] = token_data['id_token']

        if 'expires_in' in token_data:
            expires_at_seconds = (
                int(datetime.now(timezone.utc).timestamp())
                + int(token_data['expires_in'])
            )
            new_raw['expiry'] = expires_at_seconds
            new_raw['expiry_date'] = expires_at_seconds * 1000

        write_json_atomic(creds_path, new_raw)

        return True, new_raw, None

    except urllib.error.HTTPError as e:
        if e.code == 400:
            return False, None, 'Refresh token expired - please re-authenticate Gemini CLI'

        if e.code == 401:
            return False, None, 'Authentication failed - please re-authenticate Gemini CLI'

        return False, None, f'Token refresh failed (HTTP {e.code})'

    except Exception as e:
        err_type = type(e).__name__
        return False, None, f'Token refresh error: {err_type}'


def parse_gemini_reset_time(raw):
    """
    Gemini resetTime is usually ISO 8601 string.
    Return it normalized if possible, otherwise original value.
    """
    if not raw or not isinstance(raw, str):
        return None

    parsed = parse_iso8601(raw)

    if parsed:
        return parsed.isoformat()

    return raw


def make_gemini_window(bucket):
    """Convert Gemini quota bucket to usage window."""
    remaining_fraction = bucket.get('remainingFraction')

    if not isinstance(remaining_fraction, (int, float)):
        return None

    used_pct = max(0, min(100, round((1.0 - remaining_fraction) * 100, 2)))

    return {
        'model': bucket.get('modelId') or '',
        'used_pct': used_pct,
        'remaining_pct': round(100 - used_pct, 2),
        'reset_time': parse_gemini_reset_time(bucket.get('resetTime')),
        'window_seconds': 24 * 60 * 60,
    }


def map_gemini_buckets(buckets):
    """
    Map Gemini quota buckets into:
      - primary: most used Pro bucket
      - secondary: most used Flash bucket
      - fallback: most used bucket overall
      - model_windows: all model buckets
    """
    model_windows = []

    for bucket in buckets:
        window = make_gemini_window(bucket)

        if not window:
            continue

        model_windows.append(window)

    pro_candidates = [
        w for w in model_windows
        if 'pro' in (w.get('model') or '').lower()
    ]

    flash_candidates = [
        w for w in model_windows
        if 'flash' in (w.get('model') or '').lower()
    ]

    fallback_candidates = model_windows[:]

    def most_used(items):
        if not items:
            return None

        return sorted(items, key=lambda x: x.get('remaining_pct', 100))[0]

    primary = most_used(pro_candidates) or most_used(fallback_candidates)
    secondary = most_used(flash_candidates)

    gemini_3_windows = sorted(
        [
            w for w in model_windows
            if (w.get('model') or '').lower().startswith('gemini-3')
        ],
        key=lambda x: x.get('model') or '',
    )

    return {
        'primary': primary,
        'secondary': secondary,
        'model_windows': model_windows,
        'gemini_3_windows': gemini_3_windows,
    }


def fetch_gemini_load_code_assist(access_token):
    """Call Gemini Code Assist loadCodeAssist endpoint for oauth-personal mode."""
    base = 'https://cloudcode-pa.googleapis.com/v1internal'

    headers = {
        'Authorization': f'Bearer {access_token}',
        'Content-Type': 'application/json',
        'User-Agent': 'AIUsageMonitor',
    }

    body = json.dumps({
        'metadata': {
            'ideType': 'GEMINI_CLI',
            'pluginType': 'GEMINI',
        },
    }).encode()

    req = urllib.request.Request(
        f'{base}:loadCodeAssist',
        data=body,
        headers=headers,
        method='POST',
    )

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())

    except urllib.error.HTTPError as e:
        e.retry_after_info = parse_retry_after(e.headers.get('Retry-After'))
        raise


def fetch_gemini_quota(access_token, project_id=None):
    """
    Call Gemini retrieveUserQuota endpoint for oauth-personal mode.

    If project_id is missing, send {} instead of failing.
    """
    base = 'https://cloudcode-pa.googleapis.com/v1internal'

    headers = {
        'Authorization': f'Bearer {access_token}',
        'Content-Type': 'application/json',
        'User-Agent': 'AIUsageMonitor',
    }

    if project_id:
        body = json.dumps({
            'project': project_id,
        }).encode()
    else:
        body = b'{}'

    req = urllib.request.Request(
        f'{base}:retrieveUserQuota',
        data=body,
        headers=headers,
        method='POST',
    )

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())

    except urllib.error.HTTPError as e:
        e.retry_after_info = parse_retry_after(e.headers.get('Retry-After'))
        raise


def gemini_tier_label(tier_id):
    """Convert Gemini currentTier.id to readable label."""
    if tier_id == 'standard-tier':
        return 'Paid'

    if tier_id == 'free-tier':
        return 'Free'

    if tier_id == 'legacy-tier':
        return 'Legacy'

    return tier_id or ''


def fetch_gemini_oauth_account(creds_path):
    """
    Fetch Gemini Code Assist quota for one OAuth account.

    Returns account dict compatible with top-level Gemini fields.
    """
    retry_count = 0
    max_retries = 2
    email = ''
    account_label = creds_path.name

    while retry_count < max_retries:
        retry_count += 1

        try:
            credentials = load_gemini_credentials_from_path(creds_path)
            email = credentials.get('email') or email

            if gemini_token_needs_refresh(credentials):
                success, new_raw, refresh_error = refresh_gemini_token(
                    credentials['path'],
                    credentials['raw'],
                )

                if not success:
                    return {
                        'authenticated': False,
                        'auth_type': 'oauth-personal',
                        'account_file': str(creds_path),
                        'account_label': email or account_label,
                        'email': email,
                        'fail_reason': 'auth_failed',
                        'error': sanitize_error_text(refresh_error),
                        'retry_count': retry_count,
                    }

                credentials = load_gemini_credentials_from_path(creds_path)
                email = credentials.get('email') or email

            load_res = fetch_gemini_load_code_assist(credentials['access_token'])

            current_tier = load_res.get('currentTier') or {}
            tier_id = current_tier.get('id')
            tier_label = gemini_tier_label(tier_id)

            project_id = load_res.get('cloudaicompanionProject')

            quota_res = fetch_gemini_quota(
                access_token=credentials['access_token'],
                project_id=project_id,
            )

            raw_buckets = quota_res.get('buckets') or []

            buckets = [
                b for b in raw_buckets
                if not (b.get('modelId') or '').endswith('_vertex')
            ]

            mapped = map_gemini_buckets(buckets)
            primary = mapped.get('primary') or {}
            secondary = mapped.get('secondary') or {}
            has_usage = bool(primary)

            return {
                'authenticated': True,
                'auth_type': 'oauth-personal',
                'account_file': str(creds_path),
                'account_label': email or account_label,
                'email': email,
                'tier': tier_label,
                'tier_id': tier_id or '',
                'project_id': project_id or '',
                'has_usage': has_usage,
                'usage_supported': has_usage,
                'usage_note': '' if has_usage else 'No Gemini quota buckets were returned.',
                'used_pct': primary.get('used_pct', 0),
                'reset_time': primary.get('reset_time'),
                'model': primary.get('model', ''),
                'primary_model': primary.get('model', ''),
                'primary_used_pct': primary.get('used_pct', 0),
                'primary_reset_time': primary.get('reset_time'),
                'secondary_model': secondary.get('model', ''),
                'secondary_used_pct': secondary.get('used_pct') if secondary else None,
                'secondary_reset_time': secondary.get('reset_time') if secondary else None,
                'buckets': mapped.get('model_windows') or [],
                'gemini_3_buckets': mapped.get('gemini_3_windows') or [],
            }

        except urllib.error.HTTPError as e:
            body = read_http_error_body(e)

            try:
                credentials = load_gemini_credentials_from_path(creds_path)
            except Exception:
                credentials = {'raw': {}}

            if (
                e.code == 401
                and credentials.get('raw', {}).get('refresh_token')
                and retry_count < max_retries
            ):
                success, new_raw, refresh_error = refresh_gemini_token(
                    creds_path,
                    credentials['raw'],
                )

                if success:
                    continue

                return {
                    'authenticated': False,
                    'auth_type': 'oauth-personal',
                    'account_file': str(creds_path),
                    'account_label': email or account_label,
                    'email': email,
                    'fail_reason': 'auth_failed',
                    'error': sanitize_error_text(refresh_error),
                    'http_code': 401,
                    'retry_count': retry_count,
                }

            err = classify_http_failure('gemini', e.code, body)
            retry_info = getattr(e, 'retry_after_info', None)

            if retry_info:
                err.update(retry_info)

            return {
                'authenticated': False,
                'auth_type': 'oauth-personal',
                'account_file': str(creds_path),
                'account_label': email or account_label,
                'email': email,
                'retry_count': retry_count,
                **err,
            }

        except Exception as e:
            return {
                'authenticated': False,
                'auth_type': 'oauth-personal',
                'account_file': str(creds_path),
                'account_label': email or account_label,
                'email': email,
                'retry_count': retry_count,
                **classify_exception_failure(e),
            }

    return {
        'authenticated': False,
        'auth_type': 'oauth-personal',
        'account_file': str(creds_path),
        'account_label': email or account_label,
        'email': email,
        'retry_count': retry_count,
        'fail_reason': 'unknown_error',
        'error': 'Failed to fetch Gemini account usage',
    }


def fetch_gemini_oauth_provider(env_file=''):
    """
    Fetch Gemini CLI oauth-personal usage.

    Supports:
      - ~/.gemini/oauth_creds.json
      - ~/.gemini/accounts/*.json
    """
    account_paths = list_gemini_oauth_account_paths()

    if not account_paths:
        return {
            'installed': False,
            'authenticated': False,
            'auth_type': 'oauth-personal',
            'env_file': env_file,
            'fail_reason': 'auth_required',
            'error': 'Gemini OAuth credentials not found',
        }

    accounts = [
        fetch_gemini_oauth_account(path)
        for path in account_paths
    ]

    first = next(
        (account for account in accounts if account.get('authenticated') is True),
        accounts[0] if accounts else {},
    )

    return {
        'installed': True,
        'auth_type': 'oauth-personal',
        'env_file': env_file,
        **first,
        'accounts': accounts,
        'accounts_count': len(accounts),
    }


def find_gemini_api_key(env):
    """
    Find Gemini API key.

    Priority:
      1. GEMINI_API_KEY
      2. GOOGLE_API_KEY
    """
    key = env.get('GEMINI_API_KEY') or env.get('GOOGLE_API_KEY') or ''
    return key.strip()


def validate_gemini_api_key(api_key):
    """
    Validate Gemini API key by calling Gemini models endpoint.

    This confirms that the key works, but Google does not expose
    Code Assist-style usage buckets for API key mode here.
    """
    if not api_key:
        raise RuntimeError('GEMINI_API_KEY is not set')

    url = 'https://generativelanguage.googleapis.com/v1beta/models'

    req = urllib.request.Request(
        url,
        headers={
            'Accept': 'application/json',
            'User-Agent': 'AIUsageMonitor',
            'x-goog-api-key': api_key,
        },
        method='GET',
    )

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())

    except urllib.error.HTTPError as e:
        e.retry_after_info = parse_retry_after(e.headers.get('Retry-After'))
        raise


def get_gemini_cloud_project(env):
    """
    Return Google Cloud project used for Gemini API/Vertex monitoring.
    """
    return (
        env.get('GOOGLE_CLOUD_PROJECT')
        or env.get('GCLOUD_PROJECT')
        or env.get('CLOUDSDK_CORE_PROJECT')
        or env.get('GEMINI_CLOUD_PROJECT')
        or ''
    ).strip()


def env_int(env, key, default=None):
    """
    Parse integer env value safely.
    """
    raw = env.get(key)

    if raw is None or str(raw).strip() == '':
        return default

    try:
        return int(str(raw).strip())
    except Exception:
        return default


def get_monitoring_service_for_gemini_auth(auth_type):
    """
    Return Google API service name for Cloud Monitoring.

    api-key:
      generativelanguage.googleapis.com

    vertex-ai:
      aiplatform.googleapis.com
    """
    if auth_type == 'vertex-ai':
        return 'aiplatform.googleapis.com'

    return 'generativelanguage.googleapis.com'


def monitoring_time_rfc3339(dt):
    """
    Convert datetime to RFC3339 UTC string accepted by Cloud Monitoring.
    """
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')


def read_monitoring_point_value(point):
    """
    Extract numeric value from Cloud Monitoring point.
    """
    if not isinstance(point, dict):
        return 0

    value = point.get('value') or {}

    for key in (
        'int64Value',
        'doubleValue',
        'distributionValue',
    ):
        if key not in value:
            continue

        raw = value.get(key)

        if key == 'distributionValue':
            if isinstance(raw, dict):
                count = raw.get('count')
                try:
                    return int(count or 0)
                except Exception:
                    return 0

        try:
            return float(raw)
        except Exception:
            return 0

    return 0


def get_gcloud_adc_access_token():
    """
    Get ADC access token through gcloud.

    Requires:
      gcloud auth application-default login

    This avoids adding google-auth dependency.
    """
    try:
        proc = subprocess.run(
            [
                'gcloud',
                'auth',
                'application-default',
                'print-access-token',
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=10,
            check=False,
        )

        if proc.returncode != 0:
            err = sanitize_error_text(proc.stderr.strip() or proc.stdout.strip())
            raise RuntimeError(err or 'gcloud failed to print ADC access token')

        token = proc.stdout.strip()

        if not token:
            raise RuntimeError('gcloud returned empty ADC access token')

        return token

    except FileNotFoundError:
        raise RuntimeError('gcloud not found')

    except subprocess.TimeoutExpired:
        raise RuntimeError('gcloud ADC token request timed out')


def fetch_cloud_monitoring_timeseries(
    project_id,
    access_token,
    service_name,
    hours=24,
    group_by_method=False,
):
    """
    Fetch Google API request_count from Cloud Monitoring.

    Uses:
      metric.type = "serviceruntime.googleapis.com/api/request_count"
      resource.type = "consumed_api"
      resource.labels.service = service_name

    Requires:
      - Cloud Monitoring API enabled
      - ADC token with monitoring.timeSeries.list permission
    """
    now = datetime.now(timezone.utc)
    start = now - timedelta(hours=hours)

    metric_filter = (
        'metric.type = "serviceruntime.googleapis.com/api/request_count" '
        'AND resource.type = "consumed_api" '
        f'AND resource.labels.service = "{service_name}"'
    )

    alignment_seconds = max(60, int(hours * 3600))

    params = {
        'filter': metric_filter,
        'interval.startTime': monitoring_time_rfc3339(start),
        'interval.endTime': monitoring_time_rfc3339(now),
        'aggregation.alignmentPeriod': f'{alignment_seconds}s',
        'aggregation.perSeriesAligner': 'ALIGN_DELTA',
        'aggregation.crossSeriesReducer': 'REDUCE_SUM',
        'view': 'FULL',
    }

    if group_by_method:
        params['aggregation.groupByFields'] = 'metric.label.method'

    url = (
        'https://monitoring.googleapis.com/v3/'
        + urllib.parse.quote(f'projects/{project_id}', safe='/')
        + '/timeSeries?'
        + urllib.parse.urlencode(params)
    )

    req = urllib.request.Request(
        url,
        headers={
            'Authorization': f'Bearer {access_token}',
            'Accept': 'application/json',
            'User-Agent': 'AIUsageMonitor',
        },
        method='GET',
    )

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())

    except urllib.error.HTTPError as e:
        e.retry_after_info = parse_retry_after(e.headers.get('Retry-After'))
        raise


def summarize_monitoring_request_count(time_series_response):
    """
    Sum Cloud Monitoring time series points.

    Returns:
      {
        total_requests,
        series_count,
        by_method
      }
    """
    series = time_series_response.get('timeSeries') or []
    total = 0
    by_method = {}

    for item in series:
        if not isinstance(item, dict):
            continue

        metric = item.get('metric') or {}
        labels = metric.get('labels') or {}
        method = labels.get('method') or labels.get('api_method') or 'unknown'

        points = item.get('points') or []
        series_total = 0

        for point in points:
            series_total += read_monitoring_point_value(point)

        total += series_total
        by_method[method] = by_method.get(method, 0) + series_total

    by_method_sorted = [
        {
            'method': method,
            'requests': int(count),
        }
        for method, count in sorted(by_method.items(), key=lambda x: x[1], reverse=True)
    ]

    return {
        'total_requests': int(total),
        'series_count': len(series),
        'by_method': by_method_sorted,
    }


def fetch_gemini_cloud_monitoring_usage(env, auth_type='api-key'):
    """
    Fetch Gemini usage from Google Cloud Monitoring.

    For API key:
      service = generativelanguage.googleapis.com

    For Vertex AI:
      service = aiplatform.googleapis.com

    Optional env limits:
      GEMINI_API_RPD_LIMIT=250
      GEMINI_API_RPH_LIMIT=60

    Without configured limits this returns request counts, but not percent usage.
    """
    project_id = get_gemini_cloud_project(env)
    service_name = get_monitoring_service_for_gemini_auth(auth_type)

    if not project_id:
        return {
            'enabled': False,
            'authenticated': False,
            'fail_reason': 'missing_project',
            'error': 'GOOGLE_CLOUD_PROJECT is not set, Cloud Monitoring usage is unavailable',
        }

    try:
        access_token = get_gcloud_adc_access_token()
    except Exception as e:
        return {
            'enabled': True,
            'authenticated': False,
            'project_id': project_id,
            'service': service_name,
            'credential_source': 'adc_gcloud',
            'fail_reason': 'auth_required',
            'error': (
                'Google Cloud Monitoring requires ADC. Run '
                '`gcloud auth application-default login`. '
                f'Details: {sanitize_error_text(str(e))}'
            ),
        }

    try:
        res_24h = fetch_cloud_monitoring_timeseries(
            project_id=project_id,
            access_token=access_token,
            service_name=service_name,
            hours=24,
            group_by_method=False,
        )

        res_1h = fetch_cloud_monitoring_timeseries(
            project_id=project_id,
            access_token=access_token,
            service_name=service_name,
            hours=1,
            group_by_method=False,
        )

        by_method = []

        try:
            res_methods = fetch_cloud_monitoring_timeseries(
                project_id=project_id,
                access_token=access_token,
                service_name=service_name,
                hours=24,
                group_by_method=True,
            )
            by_method = summarize_monitoring_request_count(res_methods).get('by_method') or []
        except Exception:
            by_method = []

        day_summary = summarize_monitoring_request_count(res_24h)
        hour_summary = summarize_monitoring_request_count(res_1h)

        rpd_limit = env_int(env, 'GEMINI_API_RPD_LIMIT')
        rph_limit = env_int(env, 'GEMINI_API_RPH_LIMIT')

        requests_24h = day_summary.get('total_requests', 0)
        requests_1h = hour_summary.get('total_requests', 0)

        used_pct_24h = None
        used_pct_1h = None

        if rpd_limit and rpd_limit > 0:
            used_pct_24h = round(min(100, (requests_24h / rpd_limit) * 100), 2)

        if rph_limit and rph_limit > 0:
            used_pct_1h = round(min(100, (requests_1h / rph_limit) * 100), 2)

        return {
            'enabled': True,
            'authenticated': True,
            'project_id': project_id,
            'service': service_name,
            'credential_source': 'adc_gcloud',
            'requests_24h': requests_24h,
            'requests_1h': requests_1h,
            'series_count_24h': day_summary.get('series_count', 0),
            'series_count_1h': hour_summary.get('series_count', 0),
            'by_method_24h': by_method[:20],
            'rpd_limit': rpd_limit,
            'rph_limit': rph_limit,
            'used_pct_24h': used_pct_24h,
            'used_pct_1h': used_pct_1h,
            'usage_note': 'Cloud Monitoring shows project-level API request_count, not per-key remaining quota.',
        }

    except urllib.error.HTTPError as e:
        body = read_http_error_body(e)
        err = classify_http_failure('gemini_monitoring', e.code, body)

        retry_info = getattr(e, 'retry_after_info', None)
        if retry_info:
            err.update(retry_info)

        return {
            'enabled': True,
            'authenticated': False,
            'project_id': project_id,
            'service': service_name,
            'credential_source': 'adc_gcloud',
            **err,
        }

    except Exception as e:
        return {
            'enabled': True,
            'authenticated': False,
            'project_id': project_id,
            'service': service_name,
            'credential_source': 'adc_gcloud',
            **classify_exception_failure(e),
        }


def fetch_gemini_api_key_provider(env, env_file=''):
    """
    Support Gemini CLI API key mode.

    API key mode:
      - validates key through Gemini models endpoint
      - optionally fetches project-level usage from Google Cloud Monitoring

    Cloud Monitoring requires:
      GOOGLE_CLOUD_PROJECT
      gcloud auth application-default login

    Optional limits:
      GEMINI_API_RPD_LIMIT=250
      GEMINI_API_RPH_LIMIT=60
    """
    api_key = find_gemini_api_key(env)

    if not api_key:
        return {
            'installed': True,
            'authenticated': False,
            'auth_type': 'api-key',
            'env_file': env_file,
            'fail_reason': 'auth_required',
            'error': 'GEMINI_API_KEY or GOOGLE_API_KEY is not set',
        }

    try:
        models_res = validate_gemini_api_key(api_key)
        models = models_res.get('models') or []

        monitoring = fetch_gemini_cloud_monitoring_usage(env, auth_type='api-key')

        has_monitoring_usage = (
            monitoring.get('enabled') is True
            and monitoring.get('authenticated') is True
        )
        has_percentage = monitoring.get('used_pct_24h') is not None

        response = {
            'installed': True,
            'authenticated': True,
            'auth_type': 'api-key',
            'env_file': env_file,
            'has_usage': has_percentage,
            'usage_supported': 'cloud_monitoring' if has_percentage else False,
            'usage_note': (
                'Gemini API key is authenticated. Direct per-key remaining quota is not exposed; '
                'Cloud Monitoring provides project-level request_count when configured.'
            ),
            'available_models_count': len(models),
            'sample_models': [
                m.get('name', '')
                for m in models[:8]
                if isinstance(m, dict)
            ],
            'cloud_monitoring': monitoring,
        }

        if has_monitoring_usage:
            response['requests_24h'] = monitoring.get('requests_24h', 0)
            response['requests_1h'] = monitoring.get('requests_1h', 0)

            if monitoring.get('used_pct_24h') is not None:
                response['used_pct'] = monitoring.get('used_pct_24h')
                response['reset_time'] = 'rolling 24h'
                response['model'] = 'Gemini API'

            if monitoring.get('used_pct_1h') is not None:
                response['hour_used_pct'] = monitoring.get('used_pct_1h')

        return response

    except urllib.error.HTTPError as e:
        body = read_http_error_body(e)
        err = classify_http_failure('gemini', e.code, body)

        retry_info = getattr(e, 'retry_after_info', None)
        if retry_info:
            err.update(retry_info)

        return {
            'installed': True,
            'authenticated': False,
            'auth_type': 'api-key',
            'env_file': env_file,
            **err,
        }

    except Exception as e:
        return {
            'installed': True,
            'authenticated': False,
            'auth_type': 'api-key',
            'env_file': env_file,
            **classify_exception_failure(e),
        }


def fetch_vertex_service_usage_quotas(project_id, access_token):
    """
    Fetch Vertex AI quota metrics through Service Usage API.

    This returns quota limits/configuration visible to the caller.
    It may not return current real-time consumption for all metrics.
    """
    parent = f'projects/{project_id}/services/aiplatform.googleapis.com'

    url = (
        'https://serviceusage.googleapis.com/v1beta1/'
        + urllib.parse.quote(parent, safe='/')
        + '/consumerQuotaMetrics?'
        + urllib.parse.urlencode({'view': 'FULL'})
    )

    req = urllib.request.Request(
        url,
        headers={
            'Authorization': f'Bearer {access_token}',
            'Accept': 'application/json',
            'User-Agent': 'AIUsageMonitor',
        },
        method='GET',
    )

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())

    except urllib.error.HTTPError as e:
        e.retry_after_info = parse_retry_after(e.headers.get('Retry-After'))
        raise


def simplify_vertex_quota_metrics(quota_res):
    """
    Extract compact quota info from Service Usage consumerQuotaMetrics response.
    """
    metrics = quota_res.get('metrics') or quota_res.get('consumerQuotaMetrics') or []

    simplified = []

    for metric in metrics:
        if not isinstance(metric, dict):
            continue

        metric_name = metric.get('metric') or metric.get('name') or ''
        display_name = metric.get('displayName') or ''
        unit = metric.get('unit') or ''
        limits = metric.get('consumerQuotaLimits') or metric.get('limits') or []

        compact_limits = []

        for limit in limits:
            if not isinstance(limit, dict):
                continue

            quota_buckets = limit.get('quotaBuckets') or []
            effective_limit = None

            if quota_buckets and isinstance(quota_buckets[0], dict):
                effective_limit = quota_buckets[0].get('effectiveLimit')

            compact_limits.append({
                'name': limit.get('name') or '',
                'display_name': limit.get('displayName') or '',
                'unit': limit.get('unit') or unit,
                'metric': limit.get('metric') or metric_name,
                'effective_limit': effective_limit,
                'is_precise': limit.get('isPrecise'),
            })

        simplified.append({
            'metric': metric_name,
            'display_name': display_name,
            'unit': unit,
            'limits': compact_limits,
        })

    return simplified


def validate_vertex_api_key(project_id, location, api_key):
    """Validate a Vertex AI API key without issuing a model request."""
    host = 'aiplatform.googleapis.com'
    if location != 'global':
        host = f'{location}-aiplatform.googleapis.com'

    resource = (
        f'projects/{project_id}/locations/{location}/publishers/google/models'
    )
    url = f'https://{host}/v1/{urllib.parse.quote(resource, safe="/")}'
    req = urllib.request.Request(
        url,
        headers={
            'Accept': 'application/json',
            'User-Agent': 'AIUsageMonitor',
            'x-goog-api-key': api_key,
        },
        method='GET',
    )

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        e.retry_after_info = parse_retry_after(e.headers.get('Retry-After'))
        raise


def fetch_vertex_ai_provider(env, env_file=''):
    """
    Support Gemini CLI Vertex AI mode.

    Supported validation paths:
      1. GOOGLE_API_KEY present:
         - return authenticated mode info
         - Cloud Monitoring if ADC/project is configured

      2. ADC/gcloud:
         - call Service Usage API for Vertex AI quota metrics
         - call Cloud Monitoring for request_count
         - requires serviceusage.quotas.get and monitoring.timeSeries.list
    """
    project_id = (
        env.get('GOOGLE_CLOUD_PROJECT')
        or env.get('GCLOUD_PROJECT')
        or env.get('CLOUDSDK_CORE_PROJECT')
        or ''
    ).strip()

    location = (
        env.get('GOOGLE_CLOUD_LOCATION')
        or env.get('GOOGLE_CLOUD_REGION')
        or env.get('GOOGLE_REGION')
        or ''
    ).strip()

    google_api_key = (env.get('GOOGLE_API_KEY') or '').strip()

    base = {
        'installed': True,
        'auth_type': 'vertex-ai',
        'env_file': env_file,
        'project_id': project_id,
        'location': location,
        'use_vertex_ai': parse_bool_env(env.get('GOOGLE_GENAI_USE_VERTEXAI')),
    }

    if not project_id or not location:
        return {
            **base,
            'authenticated': False,
            'fail_reason': 'invalid_credentials',
            'error': 'GOOGLE_CLOUD_PROJECT and GOOGLE_CLOUD_LOCATION are required for Vertex AI mode',
        }

    if google_api_key:
        try:
            validate_vertex_api_key(project_id, location, google_api_key)
        except urllib.error.HTTPError as e:
            err = classify_http_failure(
                'gemini_vertex',
                e.code,
                read_http_error_body(e),
            )
            retry_info = getattr(e, 'retry_after_info', None)
            if retry_info:
                err.update(retry_info)
            return {
                **base,
                'authenticated': False,
                'credential_source': 'google_api_key',
                **err,
            }
        except Exception as e:
            return {
                **base,
                'authenticated': False,
                'credential_source': 'google_api_key',
                **classify_exception_failure(e),
            }

        monitoring = fetch_gemini_cloud_monitoring_usage(env, auth_type='vertex-ai')

        has_monitoring_usage = (
            monitoring.get('enabled') is True
            and monitoring.get('authenticated') is True
        )
        has_percentage = monitoring.get('used_pct_24h') is not None

        response = {
            **base,
            'authenticated': True,
            'credential_source': 'google_api_key',
            'has_usage': has_percentage,
            'usage_supported': 'cloud_monitoring' if has_percentage else False,
            'usage_note': (
                'Vertex AI API key mode detected. Direct per-key remaining quota is not exposed; '
                'Cloud Monitoring can provide project-level request_count when configured.'
            ),
            'cloud_monitoring': monitoring,
        }

        if has_monitoring_usage:
            response['requests_24h'] = monitoring.get('requests_24h', 0)
            response['requests_1h'] = monitoring.get('requests_1h', 0)

            if monitoring.get('used_pct_24h') is not None:
                response['used_pct'] = monitoring.get('used_pct_24h')
                response['reset_time'] = 'rolling 24h'
                response['model'] = 'Vertex AI'

            if monitoring.get('used_pct_1h') is not None:
                response['hour_used_pct'] = monitoring.get('used_pct_1h')

        return response

    try:
        token = get_gcloud_adc_access_token()

    except Exception as e:
        return {
            **base,
            'authenticated': False,
            'credential_source': 'adc_gcloud',
            'fail_reason': 'auth_required',
            'error': (
                'Vertex AI ADC token not available. Run '
                '`gcloud auth application-default login` or set GOOGLE_API_KEY. '
                f'Details: {sanitize_error_text(str(e))}'
            ),
        }

    try:
        quota_res = fetch_vertex_service_usage_quotas(project_id, token)
        quota_metrics = simplify_vertex_quota_metrics(quota_res)

        monitoring = fetch_gemini_cloud_monitoring_usage(env, auth_type='vertex-ai')

        has_monitoring_usage = (
            monitoring.get('enabled') is True
            and monitoring.get('authenticated') is True
        )
        has_percentage = monitoring.get('used_pct_24h') is not None

        response = {
            **base,
            'authenticated': True,
            'credential_source': 'adc_gcloud',
            'has_usage': has_percentage,
            'usage_supported': 'cloud_monitoring_and_quota_metrics' if has_percentage else False,
            'usage_note': (
                'Service Usage API returns quota metrics/limits. '
                'Cloud Monitoring returns project-level request_count when available.'
            ),
            'quota_metrics_count': len(quota_metrics),
            'quota_metrics': quota_metrics,
            'cloud_monitoring': monitoring,
        }

        if has_monitoring_usage:
            response['requests_24h'] = monitoring.get('requests_24h', 0)
            response['requests_1h'] = monitoring.get('requests_1h', 0)

            if monitoring.get('used_pct_24h') is not None:
                response['used_pct'] = monitoring.get('used_pct_24h')
                response['reset_time'] = 'rolling 24h'
                response['model'] = 'Vertex AI'

            if monitoring.get('used_pct_1h') is not None:
                response['hour_used_pct'] = monitoring.get('used_pct_1h')

        return response

    except urllib.error.HTTPError as e:
        body = read_http_error_body(e)
        err = classify_http_failure('gemini_vertex', e.code, body)

        retry_info = getattr(e, 'retry_after_info', None)
        if retry_info:
            err.update(retry_info)

        return {
            **base,
            'authenticated': False,
            'credential_source': 'adc_gcloud',
            **err,
        }

    except Exception as e:
        return {
            **base,
            'authenticated': False,
            'credential_source': 'adc_gcloud',
            **classify_exception_failure(e),
        }


def fetch_gemini_provider():
    """
    Fetch Gemini CLI usage/auth state.

    Supports:
      - oauth-personal
      - api-key
      - vertex-ai

    Notes:
      - oauth-personal can return Code Assist quota buckets.
      - api-key can be validated and can optionally return Cloud Monitoring project-level request_count.
      - vertex-ai can return quota metrics and Cloud Monitoring project-level request_count.
      - oauth-personal supports multi-account:
          ~/.gemini/oauth_creds.json
          ~/.gemini/accounts/*.json
    """
    detected = detect_gemini_auth_mode()
    auth_type = detected['auth_type']
    env = detected['env']
    env_file = detected.get('env_file', '')

    if auth_type == 'api-key':
        return fetch_gemini_api_key_provider(env, env_file=env_file)

    if auth_type == 'vertex-ai':
        return fetch_vertex_ai_provider(env, env_file=env_file)

    return fetch_gemini_oauth_provider(env_file=env_file)


# ─────────────────────────────────────────────────────────────────────────────
# Additional providers
# ─────────────────────────────────────────────────────────────────────────────

def provider_environment():
    """Load provider variables from the process and safe declarative files."""
    environment = dict(os.environ)
    paths = [Path.home() / '.config' / 'ai-usage-monitor' / '.env']
    environment_dir = Path.home() / '.config' / 'environment.d'
    if environment_dir.exists():
        paths.extend(sorted(environment_dir.glob('*.conf')))
    for path in paths:
        for key, value in parse_simple_dotenv(path).items():
            if not environment.get(key, '').strip():
                environment[key] = value
    return environment


def first_env(*names):
    """Return the first non-empty environment value."""
    environment = provider_environment()
    for name in names:
        value = environment.get(name, '').strip()
        if value:
            return value
    return ''


def clamp_percentage(value):
    """Convert a numeric value to a percentage in the 0..100 range."""
    return max(0, min(100, round(float(value), 2)))


def flexible_number(value):
    """Parse API numbers that may be encoded as strings."""
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def reset_from_value(value, now=None):
    """Normalize ISO, Unix seconds, and Unix milliseconds reset values."""
    now = now or datetime.now(timezone.utc)
    numeric = flexible_number(value)
    if numeric is not None:
        if numeric > 10_000_000_000:
            numeric /= 1000
        return datetime.fromtimestamp(numeric, tz=timezone.utc).isoformat()
    parsed = parse_iso8601(value)
    return parsed.isoformat() if parsed else None


def make_window(used_pct, reset_time=None, window_seconds=None, label=''):
    """Build the common usage-window JSON shape."""
    return {
        'used_pct': clamp_percentage(used_pct),
        'reset_time': reset_time,
        'window_seconds': window_seconds,
        'label': label,
    }


def windows_result(primary=None, secondary=None, **extra):
    """Expose generic windows and compatibility fields used by current UIs."""
    result_data = {
        'installed': True,
        'authenticated': True,
        'has_data': bool(primary or secondary),
        'has_usage': bool(primary or secondary),
        'usage_supported': bool(primary or secondary),
        'primary': primary,
        'secondary': secondary,
    }
    if primary:
        result_data.update({
            'five_hour_pct': primary['used_pct'],
            'five_hour_reset': primary.get('reset_time'),
            'primary_label': primary.get('label') or 'Usage',
        })
    if secondary:
        result_data.update({
            'seven_day_pct': secondary['used_pct'],
            'seven_day_reset': secondary.get('reset_time'),
            'secondary_label': secondary.get('label') or 'Secondary',
        })
    result_data.update(extra)
    return result_data


def fetch_json_request(url, headers=None, data=None, method=None, timeout=10):
    """Perform a JSON request and attach retry metadata to HTTP failures."""
    request = urllib.request.Request(
        url,
        data=data,
        headers=headers or {},
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as error:
        error.retry_after_info = parse_retry_after(error.headers.get('Retry-After'))
        raise


def fetch_zai_provider():
    """Fetch Z.AI token and time quota windows."""
    api_key = first_env('ZAI_API_KEY', 'ZAI_KEY', 'ZHIPU_API_KEY', 'ZHIPUAI_API_KEY')
    if not api_key:
        return {'installed': False, 'authenticated': False}

    payload = fetch_json_request(
        'https://api.z.ai/api/monitor/usage/quota/limit',
        headers={
            'Authorization': api_key,
            'Accept': 'application/json',
            'User-Agent': 'AIUsageMonitor',
        },
    )
    if payload.get('success') is not True:
        raise RuntimeError('Invalid Z.AI quota response')

    token_window = None
    time_window = None
    for item in (payload.get('data') or {}).get('limits') or []:
        percentage = flexible_number(item.get('percentage'))
        if percentage is None:
            used = flexible_number(item.get('currentValue'))
            total = flexible_number(item.get('usage'))
            if used is None or not total:
                continue
            percentage = used / total * 100
        window = make_window(
            percentage,
            reset_from_value(item.get('nextResetTime')),
            label='Tokens' if item.get('type') == 'TOKENS_LIMIT' else 'Requests',
        )
        if item.get('type') == 'TOKENS_LIMIT':
            token_window = window
        elif item.get('type') == 'TIME_LIMIT':
            time_window = window

    return windows_result(
        token_window or time_window,
        time_window if token_window else None,
        credential_source='environment',
    )


def kimi_window(detail, window_seconds=None, now=None, label='Usage'):
    """Convert one Kimi limit object to a common usage window."""
    if not isinstance(detail, dict):
        return None
    now = now or datetime.now(timezone.utc)
    limit = flexible_number(detail.get('limit'))
    used = flexible_number(detail.get('used'))
    remaining = flexible_number(detail.get('remaining'))
    if not limit or (used is None and remaining is None):
        return None
    if used is None:
        used = limit - remaining
    reset_time = reset_from_value(detail.get('resetAt') or detail.get('resetTime'), now)
    reset_in = flexible_number(detail.get('resetIn') or detail.get('reset_in'))
    if not reset_time and reset_in and reset_in > 0:
        reset_time = (now + timedelta(seconds=reset_in)).isoformat()
    return make_window(used / limit * 100, reset_time, window_seconds, label)


def duration_seconds(duration, unit):
    """Convert provider duration/unit pairs to seconds."""
    duration = flexible_number(duration)
    if not duration or duration <= 0:
        return None
    unit = str(unit or '').upper()
    multiplier = 1
    if 'MINUTE' in unit:
        multiplier = 60
    elif 'HOUR' in unit:
        multiplier = 3600
    elif 'DAY' in unit:
        multiplier = 86400
    elif 'WEEK' in unit:
        multiplier = 604800
    return int(duration * multiplier)


def fetch_kimi_provider():
    """Fetch Kimi Code short and weekly request windows."""
    api_key = first_env('KIMI_API_KEY', 'KIMI_CODE_API_KEY', 'KIMI_KEY')
    if not api_key and 'api.kimi.com/coding' in provider_environment().get('ANTHROPIC_BASE_URL', '').lower():
        api_key = first_env('ANTHROPIC_API_KEY')
    if not api_key:
        return {'installed': False, 'authenticated': False}

    payload = fetch_json_request(
        'https://api.kimi.com/coding/v1/usages',
        headers={
            'Authorization': f'Bearer {api_key}',
            'Accept': 'application/json',
            'User-Agent': 'AIUsageMonitor',
        },
    )
    now = datetime.now(timezone.utc)
    windows = []
    for item in payload.get('limits') or []:
        window_spec = item.get('window') or {}
        seconds = duration_seconds(
            window_spec.get('duration', item.get('duration')),
            window_spec.get('timeUnit', item.get('timeUnit')),
        )
        detail = item.get('detail') or {}
        label = (
            detail.get('name') or detail.get('title') or item.get('name')
            or item.get('title') or item.get('scope') or 'Limit'
        )
        window = kimi_window(detail, seconds, now, label)
        if window:
            windows.append(window)

    weekly_summary = kimi_window(payload.get('usage'), 7 * 86400, now, 'Weekly')
    weekly = next(
        (
            window for window in windows
            if (window.get('window_seconds') or 0) >= 6 * 86400
            or 'week' in window.get('label', '').lower()
            or '7d' in window.get('label', '').lower()
        ),
        weekly_summary,
    )
    shorter = sorted(
        (window for window in windows if window is not weekly),
        key=lambda window: window.get('window_seconds') or sys.maxsize,
    )
    primary = shorter[0] if shorter else weekly
    secondary = weekly if weekly and weekly is not primary else None
    return windows_result(primary, secondary, credential_source='environment')


def fetch_minimax_provider():
    """Fetch MiniMax Coding Plan prompt quota."""
    api_key = first_env('MINIMAX_KEY', 'MINIMAX_API_KEY')
    if not api_key:
        return {'installed': False, 'authenticated': False}

    payload = fetch_json_request(
        'https://api.minimax.io/v1/api/openplatform/coding_plan/remains',
        headers={
            'Authorization': f'Bearer {api_key}',
            'Accept': 'application/json, text/plain, */*',
            'Referer': 'https://platform.minimax.com/user-center/payment/coding-plan',
            'User-Agent': 'AIUsageMonitor',
        },
    )
    base_response = payload.get('base_resp')
    if not isinstance(base_response, dict) or base_response.get('status_code') != 0:
        message = base_response.get('status_msg') if isinstance(base_response, dict) else ''
        raise RuntimeError(message or 'MiniMax quota request failed')
    models = payload.get('model_remains') or []
    if not models:
        raise RuntimeError('No active MiniMax Coding Plan')
    item = models[0]
    remaining = flexible_number(item.get('current_interval_usage_count')) or 0
    total = flexible_number(item.get('current_interval_total_count')) or 0
    if total <= 0:
        raise RuntimeError('Invalid MiniMax quota response')
    used = max(0, total - remaining)
    primary = make_window(
        used / total * 100,
        reset_from_value(item.get('end_time')),
        5 * 3600,
        '5h prompts',
    )
    model = item.get('model_name') or ''
    return windows_result(
        primary,
        account_label=f'{model}: {int(used)}/{int(total)} prompts'.strip(': '),
        credential_source='environment',
    )


def qwen_api_key():
    """Load a QwenCloud Individual Token Plan key."""
    for name in ('QWEN_API_KEY', 'BAILIAN_TOKEN_PLAN_API_KEY'):
        key = first_env(name)
        if key.startswith('sk-sp-'):
            return key
    settings_path = Path.home() / '.qwen' / 'settings.json'
    try:
        settings = json.loads(settings_path.read_text(errors='replace'))
        key = str((settings.get('env') or {}).get('BAILIAN_TOKEN_PLAN_API_KEY') or '').strip()
        return key if key.startswith('sk-sp-') else ''
    except Exception:
        return ''


def fetch_qwen_provider():
    """Validate QwenCloud Individual auth without consuming inference credits."""
    api_key = qwen_api_key()
    installed = (Path.home() / '.qwen').exists() or bool(api_key)
    if not api_key:
        return {
            'installed': installed,
            'authenticated': False,
            'has_usage': False,
            'fail_reason': 'auth_required',
            'error': 'QwenCloud Individual key not found',
        }
    fetch_json_request(
        'https://token-plan.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1/models',
        headers={
            'Authorization': f'Bearer {api_key}',
            'Accept': 'application/json',
            'User-Agent': 'AIUsageMonitor',
        },
    )
    return {
        'installed': True,
        'authenticated': True,
        'has_data': False,
        'has_usage': False,
        'usage_supported': False,
        'account_label': 'Individual Token Plan',
        'usage_note': 'API key verified. QwenCloud does not expose subscription quota through API-key authentication.',
    }


def cursor_candidate_paths():
    """Return Linux Cursor session database and auth-file candidates."""
    home = Path.home()
    databases = [
        home / '.config' / app / 'User' / 'globalStorage' / 'state.vscdb'
        for app in ('Cursor', 'Cursor Nightly')
    ]
    databases.extend([
        home / '.var' / 'app' / 'com.cursor.Cursor' / 'config' / 'Cursor' / 'User' / 'globalStorage' / 'state.vscdb',
    ])
    auth_files = [
        home / '.config' / 'cursor' / 'auth.json',
        home / '.cursor' / 'auth.json',
    ]
    return databases, auth_files


def normalize_cursor_token(value):
    """Decode tokens stored as JSON strings in Cursor's SQLite state."""
    value = str(value or '').strip()
    if not value:
        return ''
    try:
        decoded = json.loads(value)
        return decoded.strip() if isinstance(decoded, str) else value
    except Exception:
        return value


def cursor_tokens():
    """Load Cursor session tokens without modifying Cursor state."""
    override = first_env('CURSOR_SESSION_TOKEN')
    if override:
        return [override]
    databases, auth_files = cursor_candidate_paths()
    tokens = []
    for path in databases:
        if not path.exists():
            continue
        try:
            uri = f'file:{urllib.parse.quote(str(path))}?mode=ro'
            with sqlite3.connect(uri, uri=True) as database:
                row = database.execute(
                    'SELECT value FROM ItemTable WHERE key = ? LIMIT 1',
                    ('cursorAuth/accessToken',),
                ).fetchone()
            if row:
                token = normalize_cursor_token(row[0])
                if token:
                    tokens.append(token)
        except sqlite3.Error:
            continue
    for path in auth_files:
        try:
            token = normalize_cursor_token(json.loads(path.read_text(errors='replace')).get('accessToken'))
            if token:
                tokens.append(token)
        except Exception:
            continue
    return tokens


def cursor_cookie(raw_token):
    """Build Cursor's dashboard cookie from a local session JWT."""
    value = raw_token.strip()
    if value.startswith('WorkosCursorSessionToken='):
        value = value[len('WorkosCursorSessionToken='):]
    value = value.replace('%3A%3A', '::')
    supplied_user, separator, token = value.partition('::')
    if not separator:
        token = supplied_user
        supplied_user = ''
    parts = token.split('.')
    if len(parts) < 2:
        return None
    payload = base64url_decode(parts[1])
    try:
        claims = json.loads(payload.decode('utf-8', errors='replace'))
    except Exception:
        return None
    if claims.get('type') == 'api_key_token':
        return None
    expires = flexible_number(claims.get('exp'))
    if expires and expires <= datetime.now(timezone.utc).timestamp():
        return None
    subject = supplied_user or str(claims.get('sub') or '').split('|')[-1]
    return f'{subject}%3A%3A{token}' if subject else None


def cursor_request(url, cookie, method='GET', data=None):
    """Call a read-only Cursor dashboard endpoint."""
    return fetch_json_request(
        url,
        data=data,
        method=method,
        headers={
            'Cookie': f'WorkosCursorSessionToken={cookie}',
            'Accept': 'application/json',
            'Content-Type': 'application/json',
            'Origin': 'https://cursor.com',
            'User-Agent': 'AIUsageMonitor',
        },
    )


def cursor_percentage(plan, explicit_key):
    """Read explicit Cursor percentages or derive them from used/limit."""
    explicit = flexible_number((plan or {}).get(explicit_key))
    if explicit is not None:
        return clamp_percentage(explicit)
    used = flexible_number((plan or {}).get('totalSpend') or (plan or {}).get('used'))
    limit = flexible_number((plan or {}).get('limit'))
    return clamp_percentage(used / limit * 100) if used is not None and limit else None


def parse_cursor_usage(payload):
    """Convert Cursor dashboard payloads to common usage windows."""
    plan = payload.get('planUsage') or (payload.get('individualUsage') or {}).get('plan') or {}
    reset_time = reset_from_value(payload.get('billingCycleEnd'))
    start = reset_from_value(payload.get('billingCycleStart'))
    window_seconds = None
    if start and reset_time:
        window_seconds = int((parse_iso8601(reset_time) - parse_iso8601(start)).total_seconds())
    api_pct = cursor_percentage(plan, 'apiPercentUsed')
    auto_pct = cursor_percentage(plan, 'autoPercentUsed')
    unlimited_value = payload.get('isUnlimited')
    if isinstance(unlimited_value, str):
        unlimited = unlimited_value.strip().lower() in ('true', '1')
    else:
        unlimited = bool(unlimited_value)
    if not unlimited and api_pct is None and auto_pct is None:
        raise RuntimeError('Cursor returned no individual quota data')
    primary = make_window(api_pct, reset_time, window_seconds, 'API') if api_pct is not None else None
    secondary = make_window(auto_pct, reset_time, window_seconds, 'Auto/Composer') if auto_pct is not None else None
    membership = str(payload.get('membershipType') or '').strip()
    label = ' · '.join(filter(None, (membership, 'Unlimited' if unlimited else '')))
    return windows_result(primary, secondary, account_label=label, credential_source='cursor_session')


def fetch_cursor_provider():
    """Fetch Cursor Individual usage from the signed-in local session."""
    tokens = cursor_tokens()
    databases, auth_files = cursor_candidate_paths()
    installed = any(path.exists() for path in databases + auth_files) or bool(tokens)
    last_auth_error = None
    for token in tokens:
        cookie = cursor_cookie(token)
        if not cookie:
            continue
        try:
            payload = cursor_request(
                'https://cursor.com/api/dashboard/get-current-period-usage',
                cookie,
                method='POST',
                data=b'{}',
            )
            if not any(key in payload for key in ('planUsage', 'billingCycleEnd', 'isUnlimited')):
                raise RuntimeError('Cursor current-period payload unavailable')
        except urllib.error.HTTPError as error:
            if error.code in (401, 403):
                last_auth_error = error
                continue
            if error.code == 429:
                raise
            payload = cursor_request('https://cursor.com/api/usage-summary', cookie)
        except RuntimeError:
            payload = cursor_request('https://cursor.com/api/usage-summary', cookie)
        return parse_cursor_usage(payload)
    if last_auth_error:
        raise last_auth_error
    return {
        'installed': installed,
        'authenticated': False,
        'has_usage': False,
        'fail_reason': 'auth_required',
        'error': 'Sign into Cursor to read Individual usage',
    }


ADDITIONAL_PROVIDERS = {
    'zai': fetch_zai_provider,
    'kimi': fetch_kimi_provider,
    'minimax': fetch_minimax_provider,
    'qwen': fetch_qwen_provider,
    'cursor': fetch_cursor_provider,
}


# ─────────────────────────────────────────────────────────────────────────────
# Claude Code
# ─────────────────────────────────────────────────────────────────────────────

def claude_credentials():
    """Load Claude OAuth tokens from environment and common CLI paths."""
    candidates = []
    expired_found = False
    seen = set()
    for variable in ('CLAUDE_CODE_OAUTH_TOKEN', 'CLAUDE_ACCESS_TOKEN'):
        token = first_env(variable)
        if token and token not in seen:
            seen.add(token)
            candidates.append((token, 'Setup token' if variable == 'CLAUDE_CODE_OAUTH_TOKEN' else 'Environment'))

    paths = [
        Path.home() / '.claude' / '.credentials.json',
        Path.home() / '.claude' / 'credentials.json',
        Path.home() / '.config' / 'claude' / 'credentials.json',
    ]
    for path in paths:
        try:
            credentials = json.loads(path.read_text(errors='replace'))
        except Exception:
            continue
        oauth = credentials.get('claudeAiOauth') or {}
        token = str(oauth.get('accessToken') or credentials.get('accessToken') or '').strip()
        expires = flexible_number(oauth.get('expiresAt'))
        if expires and expires / 1000 <= datetime.now(timezone.utc).timestamp():
            expired_found = True
            continue
        if token and token not in seen:
            seen.add(token)
            candidates.append((token, oauth.get('rateLimitTier') or 'Claude CLI'))
    return candidates, expired_found


def fetch_claude_provider():
    """Try Claude credential sources in priority order."""
    candidates, expired_found = claude_credentials()
    if not candidates:
        if expired_found:
            return {
                'installed': True,
                'authenticated': False,
                'fail_reason': 'token_expired',
                'error': 'Claude token expired; run claude auth login',
            }
        return {'installed': False, 'authenticated': False}
    last_error = None
    for token, source in candidates:
        request = urllib.request.Request(
            'https://api.anthropic.com/api/oauth/usage',
            headers={
                'Authorization': f'Bearer {token}',
                'Accept': 'application/json',
                'anthropic-beta': 'oauth-2025-04-20',
                'User-Agent': 'claude-cli/2.1.80',
            },
        )
        for attempt in range(2):
            try:
                with urllib.request.urlopen(request, timeout=10) as response:
                    data = json.loads(response.read())
                five_hour = data.get('five_hour') or {}
                seven_day = data.get('seven_day') or {}
                return {
                    'installed': True,
                    'authenticated': True,
                    'five_hour_pct': round(five_hour.get('utilization') or 0),
                    'five_hour_reset': five_hour.get('resets_at'),
                    'seven_day_pct': round(seven_day.get('utilization') or 0) if seven_day else None,
                    'seven_day_reset': seven_day.get('resets_at') if seven_day else None,
                    'credential_source': source,
                }
            except urllib.error.HTTPError as error:
                if error.code in (401, 403):
                    last_error = error
                    break
                retry = parse_retry_after(error.headers.get('Retry-After'))
                if error.code == 429 and attempt == 0 and retry and retry.get('retry_after_seconds', 0) <= 30:
                    time.sleep(retry['retry_after_seconds'])
                    continue
                raise
    if last_error:
        raise last_error
    return {'installed': True, 'authenticated': False}

if not _only or _only == 'claude':
    try:
        result['claude'] = fetch_claude_provider()
    except urllib.error.HTTPError as e:
        result['claude'] = {
            'installed': True,
            'authenticated': False,
            **classify_http_failure('claude', e.code, read_http_error_body(e)),
        }
    except Exception as e:
        result['claude'] = {
            'installed': True,
            'authenticated': False,
            **classify_exception_failure(e),
        }


# ─────────────────────────────────────────────────────────────────────────────
# OpenAI Codex
# ─────────────────────────────────────────────────────────────────────────────

if not _only or _only == 'codex':
    try:
        result['codex'] = fetch_codex_provider()

    except Exception as e:
        result['codex'] = {
            'installed': True,
            'authenticated': False,
            **classify_exception_failure(e),
        }


# ─────────────────────────────────────────────────────────────────────────────
# Gemini CLI
# ─────────────────────────────────────────────────────────────────────────────

if not _only or _only == 'gemini':
    try:
        result['gemini'] = fetch_gemini_provider()

    except Exception as e:
        result['gemini'] = {
            'installed': True,
            'authenticated': False,
            **classify_exception_failure(e),
        }


for provider_name, fetch_provider in ADDITIONAL_PROVIDERS.items():
    if _only and _only != provider_name:
        continue
    try:
        result[provider_name] = fetch_provider()
    except urllib.error.HTTPError as e:
        error = classify_http_failure(
            provider_name,
            e.code,
            read_http_error_body(e),
        )
        retry_info = getattr(e, 'retry_after_info', None)
        if retry_info:
            error.update(retry_info)
        result[provider_name] = {
            'installed': True,
            'authenticated': False,
            **error,
        }
    except Exception as e:
        result[provider_name] = {
            'installed': True,
            'authenticated': False,
            **classify_exception_failure(e),
        }


print(json.dumps(result, ensure_ascii=False))
