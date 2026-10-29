"""Pluggable credential store.

v1 ships the ``file`` backend: ``credentials.json`` next to config.json, mode
0600 - same trust model as the original's Java-serialized config (a protected
local file on a trusted server). ``encrypted``/``keyring``/``db`` backends are
planned; requesting them raises NotImplementedError.

Secrets are never returned over the API: reads for the API use ``masked()``,
and PUT round-trips keep existing values when the client sends the mask.
Environment variables (DLM_SAP_PASSWORD, DLM_PROXY_PASSWORD, DLM_PRVT_KEY_PASSWORD,
DLM_WEBHOOK_SECRET) override the store for one run without persisting (Application /p- /r- parity).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

MASK = "******"

SECRET_KEYS = ("sap_password", "proxy_password", "cert_key_password", "webhook_secret")

ENV_OVERRIDES = {
    "sap_password": "DLM_SAP_PASSWORD",
    "proxy_password": "DLM_PROXY_PASSWORD",
    "cert_key_password": "DLM_PRVT_KEY_PASSWORD",
    "webhook_secret": "DLM_WEBHOOK_SECRET",
}


class CredentialStore:
    def __init__(self, data_dir: Path, backend: str = "file"):
        if backend != "file":
            raise NotImplementedError(
                f"Credential backend '{backend}' is not implemented yet "
                "(encrypted, keyring and db backends are not implemented)"
            )
        self.path = Path(data_dir) / "credentials.json"
        self._data = self._load()

    # ----------------------------------------------------------------- internals

    def _load(self) -> dict[str, str]:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return {k: v for k, v in data.items() if k in SECRET_KEYS}
        except (json.JSONDecodeError, OSError):
            return {}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self._data, indent=2), encoding="utf-8")
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    # --------------------------------------------------------------- effective API

    def get(self, key: str) -> str:
        """Effective secret: env override wins, then the stored value."""
        env = ENV_OVERRIDES.get(key)
        if env and os.environ.get(env):
            return os.environ[env]
        return self._data.get(key, "")

    def set(self, key: str, value: str) -> None:
        if key not in SECRET_KEYS:
            raise KeyError(f"Unknown secret '{key}'")
        if value:
            self._data[key] = value
        else:
            self._data.pop(key, None)
        self._save()

    def delete(self, key: str) -> None:
        self._data.pop(key, None)
        self._save()

    def stored(self, key: str) -> bool:
        env = ENV_OVERRIDES.get(key)
        return bool((env and os.environ.get(env)) or self._data.get(key))

    # ---------------------------------------------------------------- API views

    def masked(self) -> dict[str, dict]:
        return {
            key: {"stored": self.stored(key), "value": MASK if self.stored(key) else ""}
            for key in SECRET_KEYS
        }

    def update_masked(self, payload: dict[str, str]) -> None:
        """Apply an API PUT. Semantics (defensive against empty/stale forms):

        - ``******`` (mask) -> keep the stored value
        - empty string -> keep the stored value (an empty form field must
          never wipe a stored secret - e.g. after a failed/stale page load)
        - any other string -> replace the stored value

        Explicit clearing is only possible via :meth:`delete`.
        """
        for key, value in (payload or {}).items():
            if key not in SECRET_KEYS:
                raise KeyError(f"Unknown secret '{key}'")
            if not value or value == MASK:
                continue
            self.set(key, value)

    def forget_all(self) -> None:
        self._data.clear()
        self._save()
