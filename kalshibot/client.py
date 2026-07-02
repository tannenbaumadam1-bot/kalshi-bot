"""Authenticated Kalshi REST API client (RSA-PSS request signing)."""

from __future__ import annotations

import base64
import datetime
import time
import uuid
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import requests
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding


class KalshiError(Exception):
    """Raised when the Kalshi API returns an error or auth fails."""


class KalshiClient:
    def __init__(self, key_id: str, private_key_path: str, base_url: str):
        self.key_id = key_id
        self.base_url = base_url.rstrip("/")
        self.private_key = self._load_private_key(private_key_path)
        self.session = requests.Session()
        self._min_gap_s = 0.10
        self._last_call = 0.0

    # ----- auth -----------------------------------------------------
    @staticmethod
    def _load_private_key(path: str):
        try:
            with open(path, "rb") as f:
                return serialization.load_pem_private_key(
                    f.read(), password=None, backend=default_backend()
                )
        except FileNotFoundError:
            raise KalshiError(
                f"Private key file not found at '{path}'. Check "
                f"api.private_key_path in config.yaml."
            )
        except Exception as e:
            raise KalshiError(f"Could not read private key '{path}': {e}")

    def _sign(self, timestamp: str, method: str, path: str) -> str:
        path_no_query = path.split("?")[0]
        message = f"{timestamp}{method}{path_no_query}".encode("utf-8")
        signature = self.private_key.sign(
            message,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )
        return base64.b64encode(signature).decode("utf-8")

    def _headers(self, method: str, full_path: str) -> Dict[str, str]:
        timestamp = str(int(datetime.datetime.now().timestamp() * 1000))
        sign_path = urlparse(self.base_url + full_path).path
        return {
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-SIGNATURE": self._sign(timestamp, method, sign_path),
            "KALSHI-ACCESS-TIMESTAMP": timestamp,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    # ----- low-level request ---------------------------------------
    def _request(self, method: str, path: str,
                 params: Optional[Dict[str, Any]] = None,
                 body: Optional[Dict[str, Any]] = None,
                 retries: int = 2) -> Dict[str, Any]:
        gap = time.time() - self._last_call
        if gap < self._min_gap_s:
            time.sleep(self._min_gap_s - gap)

        url = self.base_url + path
        last_err: Optional[str] = None
        for attempt in range(retries + 1):
            headers = self._headers(method, path)
            try:
                resp = self.session.request(
                    method, url, headers=headers, params=params,
                    json=body, timeout=15,
                )
            except requests.RequestException as e:
                last_err = f"network error: {e}"
                time.sleep(0.5 * (attempt + 1))
                continue
            finally:
                self._last_call = time.time()

            if resp.status_code == 429:
                time.sleep(1.0 * (attempt + 1))
                last_err = "rate limited (429)"
                continue
            if 200 <= resp.status_code < 300:
                if not resp.content:
                    return {}
                return resp.json()

            detail = resp.text[:400]
            if resp.status_code in (401, 403):
                raise KalshiError(
                    f"Auth failed ({resp.status_code}). Check your key_id, "
                    f"private key file, and that environment matches the key. "
                    f"Server said: {detail}"
                )
            if 400 <= resp.status_code < 500:
                raise KalshiError(f"Request rejected ({resp.status_code}): {detail}")
            last_err = f"server error {resp.status_code}: {detail}"
            time.sleep(0.5 * (attempt + 1))

        raise KalshiError(f"Request to {path} failed after retries: {last_err}")

    # ----- portfolio -----------------------------------------------
    def get_balance_cents(self) -> int:
        data = self._request("GET", "/portfolio/balance")
        return int(data.get("balance", 0))

    def get_positions(self) -> List[Dict[str, Any]]:
        data = self._request("GET", "/portfolio/positions")
        return data.get("market_positions", []) or []

    def get_resting_orders(self) -> List[Dict[str, Any]]:
        data = self._request("GET", "/portfolio/orders", params={"status": "resting"})
        return data.get("orders", []) or []

    def get_fills(self, limit: int = 100) -> List[Dict[str, Any]]:
        data = self._request("GET", "/portfolio/fills", params={"limit": limit})
        return data.get("fills", []) or []

    # ----- market data ---------------------------------------------
    def get_markets(self, limit: int = 100, status: str = "open",
                    cursor: Optional[str] = None) -> Dict[str, Any]:
        params: Dict[str, Any] = {"limit": limit, "status": status}
        if cursor:
            params["cursor"] = cursor
        return self._request("GET", "/markets", params=params)

    def get_event(self, event_ticker: str) -> Dict[str, Any]:
        return self._request("GET", f"/events/{event_ticker}")

    def get_event_markets(self, event_ticker: str) -> List[Dict[str, Any]]:
        d = self._request("GET", "/markets",
                           params={"event_ticker": event_ticker, "limit": 200,
                                   "status": "open"})
        return d.get("markets", []) or []

    def get_orderbook(self, ticker: str, depth: int = 10) -> Dict[str, Any]:
        data = self._request(
            "GET", f"/markets/{ticker}/orderbook", params={"depth": depth}
        )
        return data.get("orderbook_fp") or data.get("orderbook") or data

    # ----- trading (V2 create-order endpoint) ----------------------
    @staticmethod
    def build_order_v2_body(ticker: str, action: str, side: str, count: int,
                            order_type: str = "limit",
                            price_cents: Optional[int] = None,
                            client_order_id: Optional[str] = None) -> Dict[str, Any]:
        """Build the V2 create-order body.

        V2 quotes everything from the YES leg as bid/ask:
          buy YES  -> 'bid' at the yes price
          sell YES -> 'ask' at the yes price
        NO orders are converted to their YES-equivalent at (1 - price).
        Prices are fixed-point dollar strings (e.g. "0.85").
        """
        if price_cents is None:
            raise KalshiError("orders require price_cents")
        pc = int(price_cents)
        if side == "yes":
            book_side = "bid" if action == "buy" else "ask"
            yes_price = pc
        else:
            book_side = "ask" if action == "buy" else "bid"
            yes_price = 100 - pc
        yes_price = max(1, min(99, yes_price))
        tif = "immediate_or_cancel" if order_type == "market" else "good_till_canceled"
        return {
            "ticker": ticker,
            "client_order_id": client_order_id or str(uuid.uuid4()),
            "side": book_side,
            "count": str(int(count)),
            "price": f"{yes_price / 100:.2f}",
            "time_in_force": tif,
            "self_trade_prevention_type": "maker",
        }

    def create_order(self, ticker: str, action: str, side: str, count: int,
                     order_type: str = "limit", price_cents: Optional[int] = None,
                     client_order_id: Optional[str] = None) -> Dict[str, Any]:
        body = self.build_order_v2_body(
            ticker, action, side, count, order_type, price_cents, client_order_id
        )
        return self._request("POST", "/portfolio/events/orders", body=body)

    def cancel_order(self, order_id: str) -> Dict[str, Any]:
        return self._request("DELETE", f"/portfolio/orders/{order_id}")
