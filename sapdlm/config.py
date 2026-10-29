"""Application settings (CAppSettings parity) persisted as config.json.

Secrets live in the credential store (credentials.py), never in this file.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, fields, is_dataclass
from pathlib import Path

CONFIG_FILE = "config.json"

# Original: CAppSettings.SAP_SUPPORT_URL - the default Support Portal address
# used as prefill in WelcomeFrame/MainFrame_Configuration.
DEFAULT_SAP_URL = "https://tech.support.sap.com"

# Keys of the original dlgmanager.config (kept for documentation/parity):
# SAP_URL, USE_CERTIFICATE_AUTH, SAP_USERNAME, SAP_CERTIFICATE_PATH,
# SAP_PRVT_KEY_PASSWORD, PROXY_HOST/PORT/USERNAME/PASSWORD, USE_PROXY,
# DOWNLOAD_STORE, CREATE_SUBDIRECTORY, SAVE_DESCRIPTION, SCHEDULE_*, APP_TRACE.


@dataclass
class Settings:
    sap_url: str = DEFAULT_SAP_URL
    username: str = ""
    use_cert_auth: bool = False
    cert_path: str = ""
    use_proxy: bool = False
    proxy_host: str = ""
    proxy_port: int = 0
    proxy_username: str = ""
    download_store: str = ""
    create_subdir: bool = False
    save_description: bool = True
    remember_credentials: bool = True
    trace: bool = False
    schedule_at: str = ""  # ISO datetime for the one-shot scheduled download
    schedule_mode: str = "all"  # "all" | "selected"
    autostart_download: bool = False  # /a, /x parity: drain basket at startup
    component_retries: int = 3  # auto-retry attempts per component download

    # ------------------------------------------------------------------ helpers

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Settings":
        valid = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in valid})

    def is_complete(self) -> bool:
        """CAppSettings.isConfigurationComplete parity (secrets checked separately)."""
        ok = bool(self.sap_url.strip()) and bool(self.download_store.strip())
        if self.use_cert_auth:
            ok = ok and bool(self.cert_path.strip())
        else:
            ok = ok and bool(self.username.strip())
        if self.use_proxy:
            ok = ok and bool(self.proxy_host.strip()) and self.proxy_port > 0
        return ok


def config_path(data_dir: Path) -> Path:
    return Path(data_dir) / CONFIG_FILE


def load_settings(data_dir: Path) -> Settings:
    path = config_path(data_dir)
    if not path.exists():
        return Settings()
    try:
        return Settings.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, TypeError, ValueError):
        return Settings()


def save_settings(data_dir: Path, settings: Settings) -> None:
    path = config_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_to_jsonable(settings), indent=2), encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _to_jsonable(settings: Settings) -> dict:
    if is_dataclass(settings):
        return asdict(settings)
    raise TypeError("unsupported settings object")
