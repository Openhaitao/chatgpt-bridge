"""File-backed Codex auth. Never logs credentials or server error bodies.

Protocol reference: openai/codex commit 89461ea6833832b0551ce24ee32205824d5c108d,
codex-rs/login/src/{auth/manager.rs,oauth/client.rs}. Only file auth is supported.
The sidecar flock coordinates bridge processes, not independent Codex writers.
"""
import base64
import contextlib
import copy
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import stat
import tempfile
import threading
import time

import requests

TOKEN_URL = 'https://auth.openai.com/oauth/token'
CLIENT_ID = 'app_EMoamEEZ73f0CkXaXp7hrann'
REFRESH_WINDOW = 300
LOCK_TIMEOUT = 45
TRANSPORT_TIMEOUT = (5, 15)
TRANSPORT_DEADLINE = 25
MAX_FILE = 1024 * 1024
MAX_RESPONSE = 65536
_FAILURES = {}
_FAILURE_LOCK = threading.Lock()


class AuthError(Exception):
    def __init__(self, message, status=401):
        super().__init__(message)
        self.status = status


def claims(token):
    """Unverified JWT metadata is a scheduling hint, never authentication proof."""
    try:
        parts = token.split('.')
        if len(parts) != 3:
            return {}
        result = json.loads(base64.urlsafe_b64decode(parts[1] + '=' * (-len(parts[1]) % 4)))
        return result if isinstance(result, dict) else {}
    except (ValueError, TypeError, AttributeError):
        return {}


def refresh_reason(document, now=None):
    now = time.time() if now is None else now
    exp = claims(document['tokens']['access_token']).get('exp')
    if type(exp) in (int, float) and math.isfinite(exp) and exp > 0:
        return 'proactive_expiry' if exp <= now + REFRESH_WINDOW else None
    # Codex's known last_refresh timestamp is an 8-day scheduling fallback,
    # not evidence that an opaque access token has any particular expiry.
    try:
        stamp = datetime.fromisoformat(document['last_refresh'].replace('Z', '+00:00'))
        if stamp.tzinfo is not None and stamp.timestamp() < now - 8 * 86400:
            return 'scheduled'
    except (KeyError, ValueError, TypeError, AttributeError, OverflowError):
        pass
    return None


class AuthStore:
    def __init__(self, path, *, endpoint=TOKEN_URL, post=None):
        self.path = Path(path).absolute()
        # Endpoint injection is for offline tests; no environment-driven token URL.
        self.endpoint = endpoint
        self.post = post or requests.post

    def _read(self):
        try:
            fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, 'rb') as file:
                info = os.fstat(file.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                    raise ValueError()
                raw = file.read(MAX_FILE + 1)
            if len(raw) > MAX_FILE:
                raise ValueError()
            document = json.loads(raw)
            tokens = document['tokens']
            if not isinstance(tokens, dict) or not all(isinstance(tokens.get(k), str) and tokens[k] for k in ('access_token', 'account_id')):
                raise ValueError()
            if document.get('auth_mode') not in (None, 'chatgpt'):
                raise ValueError()
            return document, (info.st_dev, info.st_ino, info.st_mtime_ns, raw)
        except (OSError, ValueError, KeyError, TypeError):
            raise AuthError('Cannot read Codex file credentials. Run `codex login` and check BRIDGE_CODEX_AUTH; keyring/external auth is not supported.') from None

    def load(self):
        return self._read()[0]

    @contextlib.contextmanager
    def _lock(self):
        fd = None
        try:
            fd = os.open(str(self.path) + '.bridge-refresh.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
                raise OSError()
            os.fchmod(fd, 0o600)
            deadline = time.monotonic() + LOCK_TIMEOUT
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise AuthError('Codex refresh is busy. Retry shortly; another bridge process may be refreshing credentials.', 503)
                    time.sleep(0.05)
            yield
        except OSError:
            raise AuthError('Cannot lock or save Codex credentials. Check auth-directory ownership and permissions; then run `codex login` if needed.', 503) from None
        finally:
            if fd is not None:
                os.close(fd)  # releases flock; never unlink a shared lock inode

    @staticmethod
    def _same_credentials(a, b):
        return a.get('auth_mode') == b.get('auth_mode') and a['tokens'] == b['tokens']

    @staticmethod
    def _same_account(a, b):
        if a['tokens']['account_id'] != b['tokens']['account_id']:
            raise AuthError('Codex account changed during the request. Retry explicitly with the newly signed-in account.')

    def _failure_key(self, document):
        raw = json.dumps([document.get('auth_mode'), document['tokens']], sort_keys=True).encode()
        return str(self.path), hashlib.sha256(raw).digest()

    def _exchange(self, token, reason):
        response = None
        try:
            started = time.monotonic()
            response = self.post(self.endpoint, json={'grant_type': 'refresh_token', 'client_id': CLIENT_ID,
                                'refresh_token': token, 'refresh_reason': reason},
                                 timeout=TRANSPORT_TIMEOUT, allow_redirects=False, stream=True)
            raw = bytearray()
            # Small chunks enforce the wall-time check even for a trickling peer.
            for chunk in response.iter_content(chunk_size=1):
                raw.extend(chunk)
                if len(raw) > MAX_RESPONSE or time.monotonic() - started > TRANSPORT_DEADLINE:
                    raise AuthError('Codex refresh response exceeded safety limits. Retry later.', 503)
            try:
                data = json.loads(raw)
            except (ValueError, TypeError):
                data = {}
            if not isinstance(data, dict):
                data = {}
            if response.status_code != 200:
                error = data.get('error')
                code = error.get('code') or error.get('type') if isinstance(error, dict) else error
                if not isinstance(code, str):
                    code = data.get('code')
                code = code.lower() if isinstance(code, str) else ''
                labels = {'refresh_token_expired': 'expired', 'refresh_token_reused': 'already used',
                          'refresh_token_invalidated': 'revoked'}
                if response.status_code == 401 or code in labels or (response.status_code == 400 and code == 'invalid_grant'):
                    label = labels.get(code, 'no longer valid')
                    raise AuthError(f'Codex refresh token is {label}. Run `codex logout` then `codex login`, and retry. Credentials were not overwritten.')
                raise AuthError('Codex refresh service rejected the request. Retry later; if it persists, run `codex login`.', 503)
            fields = {key: data[key] for key in ('access_token', 'refresh_token', 'id_token') if data.get(key) is not None}
            if not fields or any(not isinstance(value, str) or not value for value in fields.values()):
                raise AuthError('Codex refresh returned invalid token data. Run `codex login` before retrying.', 503)
            return fields
        except requests.RequestException:
            raise AuthError('Codex refresh could not reach the auth service or timed out. Check connectivity and retry; no credentials were overwritten.', 503) from None
        finally:
            if response is not None:
                response.close()

    def _save(self, document, snapshot):
        temporary = None
        try:
            fd, temporary = tempfile.mkstemp(prefix='.bridge-auth-', dir=self.path.parent)
            with os.fdopen(fd, 'w') as file:
                os.fchmod(file.fileno(), 0o600)
                json.dump(document, file, ensure_ascii=False, indent=2)
                file.write('\n')
                file.flush()
                os.fsync(file.fileno())
            # Best-effort compare-before-replace: independent Codex writers do not
            # take our lock; no portable filesystem CAS can close the final gap.
            if self._read()[1] != snapshot:
                raise AuthError('Codex credentials changed while saving refreshed tokens. Retry; if refresh fails, run `codex login`. External changes were not overwritten.', 503)
            os.replace(temporary, self.path)
            temporary = None
            directory = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if temporary is not None:
                os.unlink(temporary)

    def refresh(self, observed, reason):
        with self._lock():
            current, _ = self._read()
            self._same_account(observed, current)
            if not self._same_credentials(observed, current):
                return current  # includes detection of externally rotated refresh tokens
            key = self._failure_key(current)
            with _FAILURE_LOCK:
                failure = _FAILURES.get(key)
            if failure:
                raise AuthError(failure)
            token = current['tokens'].get('refresh_token')
            if not isinstance(token, str) or not token:
                raise AuthError('Codex credentials have no refresh token. Run `codex login`, then retry.')
            try:
                fields = self._exchange(token, reason)
            except AuthError as error:
                latest = self.load()
                self._same_account(current, latest)
                if not self._same_credentials(current, latest):
                    return latest
                if error.status == 401:
                    with _FAILURE_LOCK:
                        # Bound memory; cache only digests and fixed safe messages.
                        if len(_FAILURES) >= 128:
                            _FAILURES.clear()
                        _FAILURES[key] = str(error)
                raise
            latest, snapshot = self._read()
            self._same_account(current, latest)
            if not self._same_credentials(current, latest):
                return latest
            for field in ('access_token', 'id_token'):
                account = claims(fields.get(field, '')).get('https://api.openai.com/auth', {})
                if isinstance(account, dict) and account.get('chatgpt_account_id') not in (None, current['tokens']['account_id']):
                    raise AuthError('Refreshed Codex token belongs to a different account. Run `codex login`; credentials were not overwritten.')
            updated = copy.deepcopy(latest)  # preserve unrelated top-level and token fields
            updated['tokens'].update(fields)
            updated['last_refresh'] = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
            self._save(updated, snapshot)
            return updated


def authorized_post(store, send):
    """At most one refresh and two upstream attempts, before streaming starts."""
    document = store.load()
    reason = refresh_reason(document)
    refreshed = reason is not None
    if reason:
        document = store.refresh(document, reason)
    response = send(document['tokens'])
    if response.status_code != 401:
        return response, document['tokens']
    response.close()
    latest = store.load()  # always reload first on 401
    store._same_account(document, latest)
    if not store._same_credentials(document, latest):
        document = latest
    elif not refreshed:
        document = store.refresh(document, 'unauthorized_recovery')
    else:
        raise AuthError('Codex access is still unauthorized after refresh. Run `codex login` and retry; no further retry was attempted.')
    response = send(document['tokens'])
    if response.status_code == 401:
        response.close()
        raise AuthError('Codex access is still unauthorized after credential recovery. Run `codex login` and retry; automatic retries stopped.')
    return response, document['tokens']
