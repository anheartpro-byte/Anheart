from __future__ import annotations

import ipaddress
import os
import ssl
from collections.abc import Mapping
from http import HTTPStatus
from pathlib import Path
from typing import Final

import httpx

IPV4_VERSION: Final[int] = 4


def healthy(env: Mapping[str, str]) -> bool:
    host = env.get("UI_HOST", "127.0.0.1")
    certificate = env.get("UI_TLS_CERT", "")
    scheme = "https" if certificate else "http"
    try:
        if host not in {"localhost", "localhost.localdomain"}:
            address = ipaddress.ip_address(host)
            if address.is_unspecified:
                host = "127.0.0.1" if address.version == IPV4_VERSION else "::1"
        if ":" in host:
            host = f"[{host}]"
        url = f"{scheme}://{host}:{env.get('UI_PORT', '8080')}/healthz"
        trust = (
            ssl.create_default_context(cafile=Path(certificate).parent / "authority.crt")
            if certificate
            else True
        )
        with httpx.Client(verify=trust, timeout=3, trust_env=False) as client:
            return client.get(url).status_code == HTTPStatus.OK
    except (OSError, ValueError, httpx.HTTPError):
        return False


if __name__ == "__main__":
    raise SystemExit(0 if healthy(os.environ) else 1)
