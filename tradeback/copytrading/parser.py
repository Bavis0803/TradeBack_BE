import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation


@dataclass(frozen=True)
class ParsedSignal:
    symbol: str
    direction: str
    entry_low: Decimal
    entry_high: Decimal
    stop_loss: Decimal
    take_profits: list[Decimal]
    leverage: int | None


class SignalParseError(ValueError):
    pass


NUMBER = r"(\d+(?:\.\d+)?)"
SIGNAL_SYMBOL = re.compile(
    r"#(?:FUTURE\s+#)?([A-Z0-9]{2,15})(?:/USDT)?\b",
    re.IGNORECASE,
)


def signal_symbol_hint(text):
    """Return a normalized symbol from complete or partial signal posts."""
    normalized = (text or "").replace("–", "-").replace("—", "-").strip()
    match = SIGNAL_SYMBOL.search(normalized)
    if not match:
        return None
    asset = match.group(1).upper()
    return asset if asset.endswith("USDT") else f"{asset}USDT"


def _numbers(value):
    try:
        return [Decimal(item) for item in re.findall(NUMBER, value)]
    except InvalidOperation as exc:
        raise SignalParseError("Signal contains an invalid price.") from exc


def parse_signal(text):
    """Parse explicit CHN-style Futures signals; commentary/results fail closed."""
    normalized = (text or "").replace("–", "-").replace("—", "-").strip()
    upper = normalized.upper()
    if any(marker in upper for marker in ("TARGET DONE", "ALL TARGET", "BOOK 50%", "MOVE SL")):
        return None

    header = re.search(r"#(?:FUTURE\s+#)?([A-Z0-9]{2,15})(?:/USDT)?\s*[-:]?\s*(LONG|SHORT)\b", upper)
    if not header:
        return None

    asset = header.group(1)
    symbol = asset if asset.endswith("USDT") else f"{asset}USDT"
    direction = header.group(2)
    entry_match = re.search(r"\bENTR(?:Y|IES)\s*:\s*([^\n]+)", upper)
    target_match = re.search(r"\bTARGETS?\s*:\s*([^\n]+)", upper)
    stop_match = re.search(r"\b(?:SL|STOP\s*LOSS|STOPLOSS)\s*:\s*([^\n]+)", upper)
    if not (entry_match and target_match and stop_match):
        raise SignalParseError("Signal header found but Entry, Target, or SL is missing.")

    entries = _numbers(entry_match.group(1))
    targets = _numbers(target_match.group(1))
    stops = _numbers(stop_match.group(1))
    if not entries or not targets or not stops:
        raise SignalParseError("Signal prices are incomplete.")

    low, high = min(entries), max(entries)
    stop = stops[0]
    if low <= 0 or stop <= 0 or any(target <= 0 for target in targets):
        raise SignalParseError("Signal prices must be positive.")

    reference = (low + high) / 2
    if direction == "LONG" and not (stop < reference < min(targets)):
        raise SignalParseError("LONG signal must have SL below entry and targets above entry.")
    if direction == "SHORT" and not (max(targets) < reference < stop):
        raise SignalParseError("SHORT signal must have targets below entry and SL above entry.")

    leverage_match = re.search(r"(?:LEVERAGE\s*)?X\s*(\d{1,3})", upper)
    leverage = int(leverage_match.group(1)) if leverage_match else None
    if leverage is not None and not 1 <= leverage <= 125:
        raise SignalParseError("Leverage must be between 1x and 125x.")

    return ParsedSignal(symbol, direction, low, high, stop, targets, leverage)
