from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import math


@dataclass(frozen=True)
class Settings:
    project_root: Path
    installation_path: Path
    auth_path: Path
    metadata_path: Path
    heartbeat_path: Path
    cors_origins: tuple[str, ...] = ("http://localhost:5173", "http://127.0.0.1:5173", "http://localhost:3000")
    max_body_bytes: int = 1024 * 1024
    runtime_timeout_seconds: int = 60
    runtime_capabilities_timeout_seconds: int = 60
    runtime_bootstrap_timeout_seconds: int = 60
    runtime_accept_timeout_seconds: int = 60
    compute_budget_seconds: float = 180.0
    runtime_replay_timeout_seconds: int = 120
    replay_step_seconds: int = 60
    runtime_persistent_bridge: bool = False
    deferred_session_recovery: bool = False
    ollama_enabled: bool = True
    ollama_url: str = 'http://127.0.0.1:11434'
    ollama_model: str = 'qwen3:4b'
    ollama_timeout_seconds: int = 18

    def __post_init__(self):
        from urllib.parse import urlsplit
        url = urlsplit(self.ollama_url)
        if url.scheme != 'http' or url.hostname not in ('127.0.0.1', 'localhost', '::1') or url.username or url.password or url.path or url.query or url.fragment:
            raise ValueError('Ollama must use a local HTTP origin')
        if not 1 <= self.ollama_timeout_seconds <= 180:
            raise ValueError('Ollama timeout must be within [1,180]')
        if type(self.compute_budget_seconds) not in (int, float) or not math.isfinite(self.compute_budget_seconds) or not 0 < self.compute_budget_seconds <= 600:
            raise ValueError("Server compute budget must be finite and within (0,600]")
        if type(self.replay_step_seconds) is not int or not 0 < self.replay_step_seconds <= 3600:
            raise ValueError("Replay step must be an integer within [1,3600]")

    @property
    def worker_lock_path(self):
        return self.metadata_path.with_name("compute_worker.lock")

    def installation_identity(self):
        import hashlib
        value = self.installation()
        fields = ("runtime_root", "runtime_python", "snapshot_root", "authority_store_parent", "expected_build_sha256")
        binding = {field: value[field] for field in fields}
        return hashlib.sha256(json.dumps(binding, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    @classmethod
    def from_environment(cls):
        root = Path(__file__).resolve().parents[2]
        local = root.parents[1] / ".local/m3-step7"
        private = local / "backend"
        origins = tuple(value.strip() for value in os.getenv("SAFEROUTE_CORS_ORIGINS", ",".join(cls.__dataclass_fields__["cors_origins"].default)).split(",") if value.strip())
        if "*" in origins:
            raise ValueError("Explicit CORS origins required")
        return cls(root, Path(os.getenv("SAFEROUTE_INSTALLATION_CONFIG", str(local / "installation.json"))),
                   Path(os.getenv("SAFEROUTE_AUTH_FILE", str(private / "auth_tokens.json"))),
                   Path(os.getenv("SAFEROUTE_METADATA_DB", str(private / "metadata.sqlite"))),
                   Path(os.getenv("SAFEROUTE_WORKER_HEARTBEAT", str(private / "worker_heartbeat.json"))), origins,
                   compute_budget_seconds=float(os.getenv("SAFEROUTE_COMPUTE_BUDGET_SECONDS", "180")),
                   replay_step_seconds=int(os.getenv("SAFEROUTE_REPLAY_STEP_SECONDS", "60")),
                   runtime_persistent_bridge=os.getenv('SAFEROUTE_PERSISTENT_BRIDGE','1')=='1',
                   deferred_session_recovery=os.getenv('SAFEROUTE_DEFERRED_SESSION_RECOVERY','1')=='1',
                   ollama_enabled=os.getenv('SAFEROUTE_OLLAMA_ENABLED','1')=='1',
                   ollama_url=os.getenv('SAFEROUTE_OLLAMA_URL','http://127.0.0.1:11434').rstrip('/'),
                   ollama_model=os.getenv('SAFEROUTE_OLLAMA_MODEL','qwen3:4b'),
                   ollama_timeout_seconds=int(os.getenv('SAFEROUTE_OLLAMA_TIMEOUT_SECONDS','18')))

    def installation(self):
        value = json.loads(self.installation_path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or value.get("schema_version") != "saferoute-m3-server-installation/1":
            raise ValueError("Unsupported installation configuration")
        if not re.fullmatch(r"[a-f0-9]{64}", value["expected_build_sha256"]):
            raise ValueError("Pinned build digest required")
        for field in ("runtime_root", "runtime_python", "snapshot_root", "latest_preflight_receipt", "authority_store_parent"):
            if not Path(value[field]).is_absolute():
                raise ValueError("Absolute server installation paths required")
        return value
