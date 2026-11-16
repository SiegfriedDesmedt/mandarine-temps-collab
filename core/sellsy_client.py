from __future__ import annotations

import json
import http.client
import os
import socket
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPSHandler, Request, build_opener

SELLSY_API_URL = "https://api.sellsy.com/v2"
SELLSY_API_V1_URL = "https://apifeed.sellsy.com/0/"
SELLSY_TOKEN_URL = "https://login.sellsy.com/oauth2/access-tokens"
SELLSY_READ_PREFIXES = ("/orders", "/invoices", "/credit-notes", "/deliveries")
SELLSY_SEARCH_PATHS = frozenset({"/orders/search", "/invoices/search", "/credit-notes/search", "/deliveries/search"})


class SellsyReadError(RuntimeError):
    pass


def _ipv4_connection(address, timeout=socket._GLOBAL_DEFAULT_TIMEOUT, source_address=None):
    """Open an IPv4 socket when Windows exposes an unusable IPv6 route."""
    host, port = address
    last_error = None
    for family, socktype, proto, _canonical, sockaddr in socket.getaddrinfo(
        host, port, socket.AF_INET, socket.SOCK_STREAM
    ):
        connection = None
        try:
            connection = socket.socket(family, socktype, proto)
            if timeout is not socket._GLOBAL_DEFAULT_TIMEOUT:
                connection.settimeout(timeout)
            if source_address:
                connection.bind(source_address)
            connection.connect(sockaddr)
            return connection
        except OSError as error:
            last_error = error
            if connection is not None:
                connection.close()
    if last_error:
        raise last_error
    raise OSError(f"Aucune adresse IPv4 disponible pour {host}")


class _IPv4HTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._create_connection = _ipv4_connection


class _IPv4HTTPSHandler(HTTPSHandler):
    def https_open(self, request):
        return self.do_open(_IPv4HTTPSConnection, request)


class SellsyReadOnlyClient:
    """Allow-listed, read-only Sellsy V2 client shared by both applications."""

    def __init__(self, client_id: str, client_secret: str, *, timeout: float = 30.0):
        if not client_id or not client_secret:
            raise SellsyReadError("SELLSY_CLIENT_ID et SELLSY_CLIENT_SECRET sont requis.")
        self.client_id, self.client_secret, self.timeout = client_id, client_secret, timeout
        self._token, self._expires_at = "", 0.0
        self._opener = build_opener(_IPv4HTTPSHandler())

    @classmethod
    def from_env(cls):
        try:
            timeout = max(1, int(os.environ.get("SELLSY_API_TIMEOUT_SECONDS", "30")))
        except ValueError:
            timeout = 30
        return cls(os.environ.get("SELLSY_CLIENT_ID", "").strip(), os.environ.get("SELLSY_CLIENT_SECRET", "").strip(), timeout=float(timeout))

    def _access_token(self) -> str:
        if self._token and time.monotonic() < self._expires_at:
            return self._token
        request = Request(SELLSY_TOKEN_URL, data=urlencode({"grant_type": "client_credentials", "client_id": self.client_id, "client_secret": self.client_secret}).encode(), method="POST", headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"})
        payload = self._send(request, authentication=True)
        self._token = str(payload.get("access_token") or "")
        if not self._token:
            raise SellsyReadError("Sellsy n'a pas retourné de jeton d'accès.")
        self._expires_at = time.monotonic() + max(30, int(payload.get("expires_in") or 3600) - 30)
        return self._token

    def _send(self, request: Request, *, authentication=False) -> dict:
        for attempt in range(10):
            try:
                with self._opener.open(request, timeout=self.timeout) as response:
                    return json.loads(response.read().decode("utf-8"))
            except HTTPError as error:
                retry_limit = 9 if error.code == 429 else 2
                if error.code in {429, 500, 502, 503, 504} and attempt < retry_limit:
                    try:
                        delay = float(error.headers.get("Retry-After") or 0)
                    except (TypeError, ValueError):
                        delay = 0
                    time.sleep(min(max(delay or 2 ** attempt, 1), 120))
                    continue
                label = "Authentification Sellsy refusée" if authentication else "Lecture Sellsy impossible"
                raise SellsyReadError(f"{label} (HTTP {error.code}).") from error
            except (URLError, OSError, TimeoutError, json.JSONDecodeError) as error:
                if attempt < 2:
                    time.sleep(0.5 * (attempt + 1)); continue
                reason = getattr(error, "reason", error)
                raise SellsyReadError(
                    f"Lecture Sellsy impossible : {type(error).__name__} ({reason})"
                ) from error
        raise SellsyReadError("Lecture Sellsy impossible après plusieurs tentatives.")

    def _read_json(self, path: str, *, query=None) -> dict:
        if not any(path == prefix or path.startswith(prefix + "/") for prefix in SELLSY_READ_PREFIXES):
            raise SellsyReadError(f"Endpoint Sellsy non autorisé : {path}")
        suffix = "?" + urlencode(query, doseq=True) if query else ""
        return self._send(Request(SELLSY_API_URL + path + suffix, method="GET", headers={"Accept": "application/json", "Authorization": f"Bearer {self._access_token()}"}))

    def read_endpoint(self, path: str, *, query=None) -> dict:
        return self._read_json(path, query=query)

    def search_endpoint(self, path: str, *, filters: dict, query=None) -> dict:
        if path not in SELLSY_SEARCH_PATHS:
            raise SellsyReadError(f"Endpoint de recherche Sellsy non autorisé : {path}")
        request = Request(SELLSY_API_URL + path + "?" + urlencode(query or {}, doseq=True), data=json.dumps({"filters": filters}).encode(), method="POST", headers={"Accept": "application/json", "Content-Type": "application/json", "Authorization": f"Bearer {self._access_token()}"})
        return self._send(request)

    def v1_read(self, method: str, params: dict) -> dict:
        if method not in {"Document.getList", "Document.getLinkedDocuments", "Document.getOne"}:
            raise SellsyReadError(f"Méthode Sellsy V1 non autorisée : {method}")
        payload = {"method": method, "params": params}
        request = Request(
            SELLSY_API_V1_URL,
            data=urlencode({"io_mode": "json", "do_in": json.dumps(payload)}).encode(),
            method="POST",
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {self._access_token()}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        response = self._send(request)
        if response.get("status") != "success":
            error = response.get("error") or {}
            message = error.get("message") if isinstance(error, dict) else str(error)
            raise SellsyReadError(f"Lecture Sellsy V1 impossible : {message or 'erreur inconnue'}")
        return response.get("response") or {}

    def get_order(self, order_id) -> dict:
        if not str(order_id).isdigit():
            raise SellsyReadError("Identifiant de commande Sellsy invalide.")
        return self._read_json(f"/orders/{int(order_id)}", query={"embed[]": ["company", "invoicing_address", "delivery_address"]})

    def get_invoice(self, invoice_id) -> dict:
        if not str(invoice_id).isdigit():
            raise SellsyReadError("Identifiant de facture Sellsy invalide.")
        return self._read_json(
            f"/invoices/{int(invoice_id)}",
            query={"embed[]": ["company"]},
        )
