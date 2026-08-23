import hashlib
import hmac
import json
import time
from decimal import Decimal
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from django.core.cache import cache


class BinanceServiceError(Exception):
    """A safe, user-facing Binance API error."""

    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


class BinanceAuthenticationError(BinanceServiceError):
    """Binance rejected the API key, signature, or required permission."""


class BinanceService:
    MAINNET_URL = "https://fapi.binance.com"
    TESTNET_URL = "https://testnet.binancefuture.com"
    SPOT_MAINNET_URL = "https://api.binance.com"
    SPOT_TESTNET_URL = "https://testnet.binance.vision"
    DEMO_MIN_LEVERAGE = 1
    # Exact leverage brackets require a signed Binance endpoint.
    DEMO_MAX_LEVERAGE = 125

    def __init__(self, api_key="", api_secret="", testnet=False):
        self.api_key = api_key
        self.api_secret = api_secret
        self.base_url = self.TESTNET_URL if testnet else self.MAINNET_URL
        self.spot_base_url = self.SPOT_TESTNET_URL if testnet else self.SPOT_MAINNET_URL

    def _request_json(
        self,
        path,
        params=None,
        signed=False,
        retry_timestamp=True,
        base_url=None,
        time_path="/fapi/v1/time",
        method="GET",
    ):
        params = dict(params or {})
        headers = {}
        request_base_url = base_url or self.base_url
        if signed:
            if not self.api_key or not self.api_secret:
                raise BinanceServiceError("Binance credentials are required.")
            offset = cache.get(f"binance-time-offset:{request_base_url}", 0)
            params.update({"timestamp": int(time.time() * 1000) + offset, "recvWindow": 5000})
            query = urlencode(params)
            params["signature"] = hmac.new(
                self.api_secret.encode("utf-8"), query.encode("utf-8"), hashlib.sha256
            ).hexdigest()
            headers["X-MBX-APIKEY"] = self.api_key

        query = urlencode(params)
        url = f"{request_base_url}{path}{'?' + query if query else ''}"
        request = Request(url, headers=headers, method=method)
        try:
            with urlopen(request, timeout=10) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as error:
            try:
                payload = json.loads(error.read().decode("utf-8"))
                message = payload.get("msg", str(error))
                error_code = payload.get("code")
            except (json.JSONDecodeError, UnicodeDecodeError):
                message = str(error)
                error_code = None
            if signed and error_code == -1021 and retry_timestamp:
                self._sync_server_time(request_base_url, time_path)
                clean_params = {
                    key: value
                    for key, value in params.items()
                    if key not in ("timestamp", "recvWindow", "signature")
                }
                return self._request_json(
                    path,
                    clean_params,
                    signed=True,
                    retry_timestamp=False,
                    base_url=request_base_url,
                    time_path=time_path,
                    method=method,
                )
            if error_code in (-2014, -2015, -1022):
                raise BinanceAuthenticationError(
                    "Binance rejected the API credentials, Reading permission, or IP restriction.",
                    code=error_code,
                ) from error
            raise BinanceServiceError(f"Binance API error: {message}", code=error_code) from error
        except (URLError, TimeoutError) as error:
            raise BinanceServiceError("Unable to reach Binance. Please try again.") from error

    def _sync_server_time(self, base_url=None, time_path="/fapi/v1/time"):
        request_base_url = base_url or self.base_url
        payload = self._request_json(time_path, base_url=request_base_url)
        offset = int(payload["serverTime"]) - int(time.time() * 1000)
        cache.set(f"binance-time-offset:{request_base_url}", offset, timeout=1800)

    def verify_credentials(self):
        try:
            self.get_usdt_balance()
            return {
                "success": True,
                "message": "Binance Futures account connected successfully.",
            }
        except BinanceServiceError as error:
            return {"success": False, "message": str(error)}

    def get_usdt_balance(self):
        balances = self._request_json("/fapi/v3/balance", signed=True)
        usdt = next((item for item in balances if item.get("asset") == "USDT"), None)
        if usdt is None:
            return Decimal("0")
        return Decimal(usdt.get("availableBalance", usdt.get("balance", "0")))

    def get_futures_account(self):
        return self._request_json("/fapi/v3/account", signed=True)

    def get_futures_income(self, start_time=None, end_time=None, limit=1000):
        params = {"limit": min(int(limit), 1000)}
        if start_time is not None:
            params["startTime"] = int(start_time)
        if end_time is not None:
            params["endTime"] = int(end_time)
        return self._request_json("/fapi/v1/income", params, signed=True)

    def get_futures_user_trades(self, symbol, limit=100, from_id=None):
        params = {"symbol": symbol, "limit": min(int(limit), 1000)}
        if from_id is not None:
            params["fromId"] = int(from_id)
        return self._request_json(
            "/fapi/v1/userTrades",
            params,
            signed=True,
        )

    def get_futures_tickers(self):
        cache_key = f"binance-futures-24hr-tickers:{self.base_url}"
        payload = cache.get(cache_key)
        if payload is None:
            payload = self._request_json("/fapi/v1/ticker/24hr")
            cache.set(cache_key, payload, timeout=15)
        return payload

    def get_futures_mark_prices(self, symbols=None):
        cache_key = f"binance-futures-mark-prices:{self.base_url}"
        payload = cache.get(cache_key)
        if payload is None:
            payload = self._request_json("/fapi/v1/premiumIndex")
            cache.set(cache_key, payload, timeout=2)
        rows = payload if isinstance(payload, list) else [payload]
        wanted = {item.upper() for item in symbols} if symbols else None
        return {
            item["symbol"]: Decimal(str(item["markPrice"]))
            for item in rows
            if item.get("symbol") and item.get("markPrice")
            and (wanted is None or item["symbol"] in wanted)
        }

    def get_futures_positions(self):
        return self._request_json("/fapi/v3/positionRisk", signed=True)

    def get_spot_account(self):
        return self._request_json(
            "/api/v3/account",
            {"omitZeroBalances": "true"},
            signed=True,
            base_url=self.spot_base_url,
            time_path="/api/v3/time",
        )

    def get_spot_user_trades(self, symbol, limit=100, from_id=None):
        params = {"symbol": symbol, "limit": min(int(limit), 1000)}
        if from_id is not None:
            params["fromId"] = int(from_id)
        return self._request_json(
            "/api/v3/myTrades",
            params,
            signed=True,
            base_url=self.spot_base_url,
            time_path="/api/v3/time",
        )

    def get_spot_exchange_info(self):
        cache_key = f"binance-spot-exchange-info:{self.spot_base_url}"
        payload = cache.get(cache_key)
        if payload is None:
            payload = self._request_json(
                "/api/v3/exchangeInfo",
                base_url=self.spot_base_url,
                time_path="/api/v3/time",
            )
            cache.set(cache_key, payload, timeout=300)
        return payload

    def get_spot_tickers(self):
        cache_key = f"binance-spot-24hr-tickers:{self.spot_base_url}"
        payload = cache.get(cache_key)
        if payload is None:
            payload = self._request_json(
                "/api/v3/ticker/24hr",
                {"type": "MINI"},
                base_url=self.spot_base_url,
                time_path="/api/v3/time",
            )
            cache.set(cache_key, payload, timeout=15)
        return payload

    def get_exchange_info(self):
        cache_key = f"binance-futures-exchange-info:{self.base_url}"
        exchange_info = cache.get(cache_key)
        if exchange_info is None:
            exchange_info = self._request_json("/fapi/v1/exchangeInfo")
            cache.set(cache_key, exchange_info, timeout=300)
        return exchange_info

    def get_symbol_context(self, symbol, include_symbols=True):
        symbol = symbol.upper().strip()
        exchange_info = self.get_exchange_info()
        symbol_info = next(
            (
                item
                for item in exchange_info.get("symbols", [])
                if item.get("symbol") == symbol
                and item.get("contractType") == "PERPETUAL"
                and item.get("quoteAsset") == "USDT"
                and item.get("status") == "TRADING"
            ),
            None,
        )
        if symbol_info is None:
            raise BinanceServiceError(f"{symbol} is not a tradable USDT perpetual symbol.")

        ticker = self._request_json("/fapi/v2/ticker/price", {"symbol": symbol})
        filters = {item["filterType"]: item for item in symbol_info.get("filters", [])}
        lot_size = filters.get("LOT_SIZE", {})
        price_filter = filters.get("PRICE_FILTER", {})
        min_notional_filter = filters.get("MIN_NOTIONAL", {})
        context = {
            "symbol": symbol,
            "base_asset": symbol_info["baseAsset"],
            "quote_asset": symbol_info["quoteAsset"],
            "current_price": Decimal(ticker["price"]),
            "price_precision": symbol_info.get("pricePrecision", 8),
            "quantity_precision": symbol_info.get("quantityPrecision", 8),
            "min_volume": Decimal(lot_size.get("minQty", "0")),
            "max_volume": Decimal(lot_size.get("maxQty", "0")),
            "volume_step": Decimal(lot_size.get("stepSize", "0")),
            "price_step": Decimal(price_filter.get("tickSize", "0")),
            "min_notional": Decimal(
                min_notional_filter.get("notional", min_notional_filter.get("minNotional", "0"))
            ),
        }
        if include_symbols:
            context["symbols"] = [
                {
                    "symbol": item["symbol"],
                    "base_asset": item["baseAsset"],
                    "quote_asset": item["quoteAsset"],
                }
                for item in exchange_info.get("symbols", [])
                if item.get("contractType") == "PERPETUAL"
                and item.get("quoteAsset") == "USDT"
                and item.get("status") == "TRADING"
            ]
        return context

    def get_leverage_brackets(self, symbol):
        payload = self._request_json(
            "/fapi/v1/leverageBracket", {"symbol": symbol}, signed=True
        )
        data = payload[0] if isinstance(payload, list) else payload
        brackets = data.get("brackets", [])
        if not brackets:
            raise BinanceServiceError(f"No leverage brackets are available for {symbol}.")
        return [
            {
                "initial_leverage": int(item["initialLeverage"]),
                "notional_floor": Decimal(str(item["notionalFloor"])),
                "notional_cap": Decimal(str(item["notionalCap"])),
                "maint_margin_ratio": Decimal(str(item["maintMarginRatio"])),
            }
            for item in brackets
        ]

    def change_initial_leverage(self, symbol, leverage):
        return self._request_json(
            "/fapi/v1/leverage",
            {"symbol": symbol, "leverage": int(leverage)},
            signed=True,
            method="POST",
        )

    def place_futures_order(self, **params):
        return self._request_json(
            "/fapi/v1/order", params, signed=True, method="POST"
        )

    def place_futures_algo_order(self, **params):
        return self._request_json(
            "/fapi/v1/algoOrder", params, signed=True, method="POST"
        )

    def get_futures_order(self, symbol, order_id):
        return self._request_json(
            "/fapi/v1/order", {"symbol": symbol, "orderId": order_id}, signed=True
        )

    def cancel_futures_order(self, symbol, order_id):
        return self._request_json(
            "/fapi/v1/order", {"symbol": symbol, "orderId": order_id},
            signed=True, method="DELETE",
        )


def decimal_to_string(value):
    return format(value.normalize(), "f") if value != 0 else "0"


def get_bracket_for_notional(brackets, notional):
    return next(
        (
            bracket
            for bracket in brackets
            if bracket["notional_floor"] <= notional < bracket["notional_cap"]
        ),
        brackets[-1],
    )


def calculate_risk_reward(data, balance, context, brackets):
    entry = data["entry_price"]
    stop = data["stop_loss"]
    take_profit = data["take_profit"]
    volume = data["volume"]
    leverage = data["leverage"]
    direction = data["direction"]

    if direction == "LONG" and not (stop < entry < take_profit):
        raise ValueError("For LONG trades, stop loss must be below entry and take profit above entry.")
    if direction == "SHORT" and not (take_profit < entry < stop):
        raise ValueError("For SHORT trades, take profit must be below entry and stop loss above entry.")

    if context["min_volume"] and volume < context["min_volume"]:
        raise ValueError(f"Volume must be at least {decimal_to_string(context['min_volume'])}.")
    if context["max_volume"] and volume > context["max_volume"]:
        raise ValueError(f"Volume must not exceed {decimal_to_string(context['max_volume'])}.")
    step = context["volume_step"]
    if step and volume % step != 0:
        raise ValueError(f"Volume must be a multiple of {decimal_to_string(step)}.")

    notional = entry * volume
    if context["min_notional"] and notional < context["min_notional"]:
        raise ValueError(
            f"Notional value must be at least {decimal_to_string(context['min_notional'])} USDT."
        )

    bracket = get_bracket_for_notional(brackets, notional)
    if leverage > bracket["initial_leverage"]:
        raise ValueError(
            f"Maximum leverage for this {decimal_to_string(notional)} USDT position is "
            f"{bracket['initial_leverage']}x."
        )

    margin_required = notional / Decimal(leverage)
    if margin_required > balance:
        raise ValueError(
            f"Required margin ({decimal_to_string(margin_required)} USDT) exceeds account "
            f"balance ({decimal_to_string(balance)} USDT)."
        )

    risk_amount = abs(entry - stop) * volume
    potential_profit = abs(take_profit - entry) * volume
    ratio = potential_profit / risk_amount
    margin_ratio = bracket["maint_margin_ratio"]
    if direction == "LONG":
        liquidation = entry * (Decimal("1") - Decimal("1") / leverage + margin_ratio)
    else:
        liquidation = entry * (Decimal("1") + Decimal("1") / leverage - margin_ratio)

    return {
        "risk_amount": decimal_to_string(risk_amount),
        "potential_profit": decimal_to_string(potential_profit),
        "risk_reward_ratio": decimal_to_string(ratio),
        "position_size": decimal_to_string(volume),
        "notional_value": decimal_to_string(notional),
        "margin_required": decimal_to_string(margin_required),
        "estimated_liquidation_price": decimal_to_string(max(liquidation, Decimal("0"))),
        "max_leverage_for_notional": bracket["initial_leverage"],
    }
