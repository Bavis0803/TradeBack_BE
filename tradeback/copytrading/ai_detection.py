import hashlib
import json
import re
from decimal import Decimal, InvalidOperation
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from django.db import IntegrityError
from django.utils import timezone

from .models import AISignalAgent, AISignalAnalysis
from .parser import ParsedSignal, SignalParseError, parse_signal


PROMPT_VERSION = "signal-v1"
SYSTEM_PROMPT = """Extract only a NEW actionable USDT-M perpetual entry signal from the message.
Reject commentary, news, results, TP/SL updates, closed/cancelled calls, and any message missing symbol, direction, entry, stop-loss, or at least one take-profit.
Never guess or calculate missing values. Normalize the symbol to BASEUSDT. Preserve explicit decimals and leverage. Set is_signal=false when uncertain. Return only the required schema."""

RESULT_MARKERS = (
    "TARGET DONE", "ALL TARGET", "MOVE SL", "BOOK PROFIT", "CLOSED", "CLOSE NOW",
    "STOPPED", "SL HIT", "TP HIT", "RESULT", "PROFIT",
)
FIELD_MARKERS = (
    ("ENTRY", "ENTRIES", "BUY ZONE", "SELL ZONE"),
    ("TARGET", "TARGETS", "TP1", "TP 1", "TAKE PROFIT"),
    ("STOP", "STOPLOSS", "STOP LOSS", "SL"),
)

SIGNAL_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "is_signal": {"type": "boolean"},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "symbol": {"type": ["string", "null"]},
        "direction": {"type": ["string", "null"], "enum": ["LONG", "SHORT", None]},
        "entry_low": {"type": ["string", "null"]},
        "entry_high": {"type": ["string", "null"]},
        "stop_loss": {"type": ["string", "null"]},
        "take_profits": {"type": "array", "items": {"type": "string"}, "maxItems": 10},
        "leverage": {"type": ["integer", "null"], "minimum": 1, "maximum": 125},
        "reason_code": {
            "type": "string",
            "enum": ["ACTIONABLE", "NOT_SIGNAL", "RESULT_UPDATE", "INCOMPLETE", "AMBIGUOUS"],
        },
    },
    "required": [
        "is_signal", "confidence", "symbol", "direction", "entry_low", "entry_high",
        "stop_loss", "take_profits", "leverage", "reason_code",
    ],
}


class AISignalError(Exception):
    pass


def is_ai_candidate(text):
    """Cheap deterministic gate; most chat messages never reach an AI provider."""
    normalized = re.sub(r"\s+", " ", text or "").strip().upper()
    if len(normalized) < 15 or len(normalized) > 6000:
        return False
    if any(marker in normalized for marker in RESULT_MARKERS):
        return False
    has_direction = bool(re.search(r"\b(LONG|SHORT|BUY|SELL)\b", normalized))
    has_symbol = bool(
        re.search(r"#[A-Z0-9]{2,15}\b|\b[A-Z0-9]{2,15}\s*/?\s*USDT\b", normalized)
    )
    field_groups = sum(any(marker in normalized for marker in group) for group in FIELD_MARKERS)
    return has_direction and has_symbol and field_groups >= 2


def _content_hash(text):
    normalized = re.sub(r"\s+", " ", text or "").strip().lower()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _extract_output_text(payload):
    if payload.get("output_text"):
        return payload["output_text"]
    for item in payload.get("output", []):
        if item.get("type") != "message":
            continue
        for content in item.get("content", []):
            if content.get("type") == "output_text" and content.get("text"):
                return content["text"]
    raise AISignalError("AI provider returned no structured output.")


def _openai_response(agent, text):
    body = {
        "model": agent.model,
        "instructions": SYSTEM_PROMPT,
        "input": (text or "")[:6000],
        "max_output_tokens": 350,
        "store": False,
        "prompt_cache_key": PROMPT_VERSION,
        "text": {
            "format": {
                "type": "json_schema", "name": "trade_signal",
                "strict": True, "schema": SIGNAL_SCHEMA,
            }
        },
    }
    if agent.model == "gpt-5-nano":
        body["reasoning"] = {"effort": "minimal"}
    request = Request(
        "https://api.openai.com/v1/responses",
        data=json.dumps(body).encode("utf-8"),
        headers={"Authorization": f"Bearer {agent.api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode("utf-8")).get("error", {}).get("message")
        except (json.JSONDecodeError, UnicodeDecodeError):
            detail = None
        raise AISignalError(f"OpenAI rejected the request: {detail or exc.reason}") from exc
    except (URLError, TimeoutError) as exc:
        raise AISignalError("Unable to reach OpenAI.") from exc
    try:
        result = json.loads(_extract_output_text(payload))
    except (json.JSONDecodeError, TypeError) as exc:
        raise AISignalError("OpenAI returned invalid structured output.") from exc
    usage = payload.get("usage") or {}
    return result, int(usage.get("input_tokens") or 0), int(usage.get("output_tokens") or 0)


def verify_agent(agent):
    result, _, _ = _openai_response(agent, "Hello. This is not a trading signal.")
    if result.get("is_signal") is not False:
        raise AISignalError("AI model did not pass the signal detector self-test.")
    return True


def _to_parsed_signal(result):
    if not result.get("is_signal"):
        return None
    symbol = re.sub(r"[^A-Z0-9]", "", str(result.get("symbol") or "").upper())
    if symbol and not symbol.endswith("USDT"):
        symbol = f"{symbol}USDT"
    targets = result.get("take_profits") or []
    leverage = result.get("leverage")
    canonical = (
        f"#{symbol} {result.get('direction')}\n"
        f"Entry: {result.get('entry_low')} - {result.get('entry_high')}\n"
        f"Target: {' - '.join(map(str, targets))}\n"
        f"SL: {result.get('stop_loss')}"
        + (f"\nLeverage x{leverage}" if leverage else "")
    )
    parsed = parse_signal(canonical)
    if not isinstance(parsed, ParsedSignal):
        raise SignalParseError("AI output is not a complete actionable signal.")
    return parsed


def analyze_signal_candidate(strategy, text):
    if not strategy.ai_detection_enabled or not is_ai_candidate(text):
        return None
    try:
        agent = strategy.user.ai_signal_agent
    except AISignalAgent.DoesNotExist:
        return None
    if not agent.enabled or agent.status != AISignalAgent.Status.CONNECTED:
        return None

    content_hash = _content_hash(text)
    cached = AISignalAnalysis.objects.filter(
        user=strategy.user, content_hash=content_hash, provider=agent.provider,
        model=agent.model, prompt_version=PROMPT_VERSION,
    ).first()
    if cached:
        if (
            not cached.result.get("is_signal")
            or cached.confidence < agent.min_confidence
        ):
            return None
        try:
            return _to_parsed_signal(cached.result)
        except SignalParseError:
            return None

    calls_today = AISignalAnalysis.objects.filter(
        user=strategy.user, created_at__date=timezone.localdate()
    ).count()
    if calls_today >= agent.daily_call_limit:
        return None

    try:
        result, input_tokens, output_tokens = _openai_response(agent, text)
        confidence = Decimal(str(result.get("confidence") or 0))
        if not Decimal("0") <= confidence <= Decimal("1"):
            raise InvalidOperation("AI confidence is outside 0-1.")
        confidence = confidence.quantize(Decimal("0.001"))
        parsed = _to_parsed_signal(result)
        status = AISignalAnalysis.Status.NOT_SIGNAL
        if parsed and confidence >= agent.min_confidence:
            status = AISignalAnalysis.Status.SIGNAL
        elif parsed:
            status = AISignalAnalysis.Status.LOW_CONFIDENCE
            parsed = None
        analysis = AISignalAnalysis(
            user=strategy.user, content_hash=content_hash, provider=agent.provider,
            model=agent.model, prompt_version=PROMPT_VERSION, status=status,
            confidence=confidence, result=result, input_tokens=input_tokens,
            output_tokens=output_tokens,
        )
    except (AISignalError, SignalParseError, InvalidOperation) as exc:
        parsed = None
        analysis = AISignalAnalysis(
            user=strategy.user, content_hash=content_hash, provider=agent.provider,
            model=agent.model, prompt_version=PROMPT_VERSION,
            status=AISignalAnalysis.Status.ERROR, error=str(exc)[:500],
        )
    try:
        analysis.save()
    except IntegrityError:
        pass
    return parsed
