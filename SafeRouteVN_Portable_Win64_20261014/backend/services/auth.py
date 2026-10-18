from dataclasses import dataclass
import hashlib
import hmac
import json
from pathlib import Path
import re
import time

from backend.api.errors import ApiError


@dataclass(frozen=True)
class Actor:
    actor_id: str
    role: str
    session_id: str | None = None

    def can_access_session(self, session_id, owner):
        if self.role == 'driver':
            return owner is not None and self.session_id == session_id
        return owner == self.actor_id


class AuthStore:
    def __init__(self, path: Path):
        self.path = path

    def records(self):
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            rows = value["tokens"]
            if value.get("schema_version") != "saferoute-m3-auth/1" or not isinstance(rows, list) or not rows:
                raise ValueError("Invalid token configuration")
            seen = set()
            for row in rows:
                if (not re.fullmatch(r"[a-f0-9]{64}", row["token_sha256"])
                        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,199}", row["actor_id"])
                        or row["role"] not in ("dispatcher", "viewer", "driver") or row["token_sha256"] in seen):
                    raise ValueError("Invalid token record")
                if row['role'] == 'driver' and (
                    not isinstance(row.get('session_id'), str)
                    or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:/-]{0,199}', row['session_id'])
                    or type(row.get('expires_at')) is not int
                ):
                    raise ValueError('Driver requires an assigned session and expiry')
                if 'revoked' in row and type(row['revoked']) is not bool:
                    raise ValueError('Invalid revocation flag')
                if 'expires_at' in row and type(row['expires_at']) is not int:
                    raise ValueError('Invalid token expiry')
                seen.add(row["token_sha256"])
            return rows
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise ApiError(503, "AUTH_NOT_CONFIGURED", "authentication", "Server authentication is unavailable") from error

    def authenticate(self, token: str):
        if not 20 <= len(token) <= 256:
            raise ApiError(401, "UNAUTHORIZED", "authentication", "Valid bearer token required")
        digest = hashlib.sha256(token.encode()).hexdigest()
        for row in self.records():
            if hmac.compare_digest(digest, row["token_sha256"]):
                if row.get('revoked') or row.get('expires_at', float('inf')) <= time.time():
                    break
                return Actor(row["actor_id"], row["role"], row.get('session_id'))
        raise ApiError(401, "UNAUTHORIZED", "authentication", "Valid bearer token required")
