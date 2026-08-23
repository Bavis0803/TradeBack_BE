import ast
import re
from datetime import datetime, timezone as datetime_timezone
from decimal import Decimal


class StrategyCompileError(ValueError):
    pass


PRICE_SERIES = {"open", "high", "low", "close", "volume"}
INDICATORS = {"sma", "ema", "rsi", "atr"}
CONDITION_FUNCTIONS = {"crossover", "crossunder"}
MAX_INDICATORS = 30


def _split_args(value):
    return [item.strip() for item in value.split(",") if item.strip()]


def _validate_condition(expression, available_names):
    normalized = expression.replace("ta.crossover", "crossover").replace(
        "ta.crossunder", "crossunder"
    ).replace("&&", " and ").replace("||", " or ")
    try:
        tree = ast.parse(normalized, mode="eval")
    except SyntaxError as exc:
        raise StrategyCompileError(f"Invalid condition syntax: {exc.msg}.") from exc
    allowed_nodes = (
        ast.Expression, ast.BoolOp, ast.And, ast.Or, ast.UnaryOp, ast.Not,
        ast.Compare, ast.Gt, ast.GtE, ast.Lt, ast.LtE, ast.Eq, ast.NotEq,
        ast.Name, ast.Load, ast.Constant, ast.Call, ast.BinOp, ast.Add, ast.Sub,
        ast.Mult, ast.Div, ast.Mod, ast.USub, ast.UAdd,
    )
    for node in ast.walk(tree):
        if not isinstance(node, allowed_nodes):
            raise StrategyCompileError(f"Unsupported condition element: {type(node).__name__}.")
        if isinstance(node, ast.Name) and node.id not in available_names | CONDITION_FUNCTIONS:
            raise StrategyCompileError(f"Unknown indicator or price series: {node.id}.")
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in CONDITION_FUNCTIONS:
                raise StrategyCompileError("Only ta.crossover() and ta.crossunder() calls are allowed.")
            if len(node.args) != 2 or node.keywords:
                raise StrategyCompileError(f"{node.func.id}() requires exactly two values.")
    return normalized


def compile_strategy(indicator_code, long_condition, short_condition=""):
    if len(indicator_code or "") > 20000:
        raise StrategyCompileError("Indicator code is too long.")
    constants = {}
    indicators = []
    available = set(PRICE_SERIES)
    for raw_line in (indicator_code or "").splitlines():
        line = raw_line.split("//", 1)[0].strip()
        if not line or line.startswith(("indicator(", "strategy(", "plot(", "plotshape(")):
            continue
        constant_match = re.fullmatch(
            r"(?:int|float)?\s*([A-Za-z_]\w*)\s*=\s*(?:input\.(?:int|float)\()?\s*"
            r"(-?\d+(?:\.\d+)?)(?:\s*,[^)]*)?\)?",
            line,
        )
        if constant_match:
            constants[constant_match.group(1)] = float(constant_match.group(2))
            available.add(constant_match.group(1))
            continue
        match = re.fullmatch(
            r"(?:float\s+)?([A-Za-z_]\w*)\s*=\s*ta\.(sma|ema|rsi|atr)\(([^)]+)\)",
            line,
            re.IGNORECASE,
        )
        if not match:
            raise StrategyCompileError(
                f"Unsupported Pine line: {raw_line.strip()}. Use ta.sma/ema/rsi/atr assignments only."
            )
        name, function, raw_args = match.group(1), match.group(2).lower(), match.group(3)
        if name in available:
            raise StrategyCompileError(f"Duplicate or reserved variable name: {name}.")
        args = _split_args(raw_args)
        expected = 1 if function == "atr" else 2
        if len(args) != expected:
            raise StrategyCompileError(f"ta.{function}() requires {expected} argument(s).")
        source = "close" if function == "atr" else args[0]
        length_value = args[0] if function == "atr" else args[1]
        if source not in available:
            raise StrategyCompileError(f"Unknown source {source} for {name}.")
        try:
            length = int(constants.get(length_value, length_value))
        except (TypeError, ValueError) as exc:
            raise StrategyCompileError(f"Length for {name} must be a fixed integer.") from exc
        if not 2 <= length <= 500:
            raise StrategyCompileError(f"Length for {name} must be between 2 and 500.")
        indicators.append({"name": name, "function": function, "source": source, "length": length})
        available.add(name)
        if len(indicators) > MAX_INDICATORS:
            raise StrategyCompileError(f"A strategy can define at most {MAX_INDICATORS} indicators.")
    if not indicators:
        raise StrategyCompileError("Define at least one supported ta.* indicator.")
    if not (long_condition or "").strip():
        raise StrategyCompileError("A LONG activation condition is required.")
    return {
        "version": "safe-pine-v1",
        "constants": constants,
        "indicators": indicators,
        "long_condition": _validate_condition(long_condition.strip(), available),
        "short_condition": _validate_condition(short_condition.strip(), available)
        if (short_condition or "").strip() else "",
    }


def _sma(values, length):
    result = [None] * len(values)
    for index in range(length - 1, len(values)):
        window = values[index - length + 1:index + 1]
        if all(value is not None for value in window):
            result[index] = sum(window) / length
    return result


def _ema(values, length):
    result = [None] * len(values)
    multiplier = 2.0 / (length + 1)
    seed = None
    for index, value in enumerate(values):
        if value is None:
            continue
        seed = value if seed is None else (value - seed) * multiplier + seed
        if index >= length - 1:
            result[index] = seed
    return result


def _rsi(values, length):
    result = [None] * len(values)
    gains = [0.0] * len(values)
    losses = [0.0] * len(values)
    for index in range(1, len(values)):
        change = values[index] - values[index - 1]
        gains[index] = max(change, 0.0)
        losses[index] = max(-change, 0.0)
    if len(values) <= length:
        return result
    avg_gain = sum(gains[1:length + 1]) / length
    avg_loss = sum(losses[1:length + 1]) / length
    for index in range(length, len(values)):
        if index > length:
            avg_gain = (avg_gain * (length - 1) + gains[index]) / length
            avg_loss = (avg_loss * (length - 1) + losses[index]) / length
        result[index] = 100.0 if avg_loss == 0 else 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)
    return result


def _atr(series, length):
    highs, lows, closes = series["high"], series["low"], series["close"]
    tr = []
    for index in range(len(closes)):
        previous = closes[index - 1] if index else closes[index]
        tr.append(max(highs[index] - lows[index], abs(highs[index] - previous), abs(lows[index] - previous)))
    return _ema(tr, length)


def build_series(candles, spec):
    series = {
        key: [float(item[key]) for item in candles]
        for key in PRICE_SERIES
    }
    series.update(spec.get("constants", {}))
    for item in spec["indicators"]:
        source = series[item["source"]]
        function = item["function"]
        if function == "sma":
            values = _sma(source, item["length"])
        elif function == "ema":
            values = _ema(source, item["length"])
        elif function == "rsi":
            values = _rsi(source, item["length"])
        else:
            values = _atr(series, item["length"])
        series[item["name"]] = values
    return series


def _value(node, series, index):
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        value = series[node.id]
        return value[index] if isinstance(value, list) else value
    if isinstance(node, ast.UnaryOp):
        value = _value(node.operand, series, index)
        if isinstance(node.op, ast.Not): return not bool(value)
        if isinstance(node.op, ast.USub): return -value
        return +value
    if isinstance(node, ast.BoolOp):
        values = [_value(item, series, index) for item in node.values]
        return all(values) if isinstance(node.op, ast.And) else any(values)
    if isinstance(node, ast.BinOp):
        left, right = _value(node.left, series, index), _value(node.right, series, index)
        if left is None or right is None: return None
        operations = {ast.Add: lambda: left + right, ast.Sub: lambda: left - right,
                      ast.Mult: lambda: left * right, ast.Div: lambda: left / right,
                      ast.Mod: lambda: left % right}
        return operations[type(node.op)]()
    if isinstance(node, ast.Compare):
        left = _value(node.left, series, index)
        if left is None: return False
        for operator, comparator in zip(node.ops, node.comparators):
            right = _value(comparator, series, index)
            if right is None: return False
            checks = {ast.Gt: left > right, ast.GtE: left >= right, ast.Lt: left < right,
                      ast.LtE: left <= right, ast.Eq: left == right, ast.NotEq: left != right}
            if not checks[type(operator)]: return False
            left = right
        return True
    if isinstance(node, ast.Call):
        if index <= 0: return False
        left_now = _value(node.args[0], series, index)
        right_now = _value(node.args[1], series, index)
        left_prev = _value(node.args[0], series, index - 1)
        right_prev = _value(node.args[1], series, index - 1)
        if None in (left_now, right_now, left_prev, right_prev): return False
        if node.func.id == "crossover": return left_prev <= right_prev and left_now > right_now
        return left_prev >= right_prev and left_now < right_now
    raise StrategyCompileError("Unsupported compiled condition.")


def evaluate_condition(expression, series, index):
    if not expression:
        return False
    return bool(_value(ast.parse(expression, mode="eval").body, series, index))


def backtest(candles, spec, risk_reward_ratio, stop_loss_percent):
    series = build_series(candles, spec)
    rr = float(risk_reward_ratio)
    risk_pct = float(stop_loss_percent)
    position = None
    trades = []
    equity = 100.0
    peak = equity
    max_drawdown = 0.0
    curve = []
    for index in range(1, len(candles)):
        candle = candles[index]
        if position:
            direction = position["direction"]
            stop_hit = candle["low"] <= position["stop"] if direction == "LONG" else candle["high"] >= position["stop"]
            target_hit = candle["high"] >= position["target"] if direction == "LONG" else candle["low"] <= position["target"]
            opposite = evaluate_condition(
                spec["short_condition"] if direction == "LONG" else spec["long_condition"], series, index
            )
            if stop_hit or target_hit or opposite:
                if stop_hit:
                    exit_price, outcome, pnl_r = position["stop"], "LOSS", -1.0
                elif target_hit:
                    exit_price, outcome, pnl_r = position["target"], "WIN", rr
                else:
                    exit_price = candle["close"]
                    raw = ((exit_price - position["entry"]) / position["risk"])
                    pnl_r = raw if direction == "LONG" else -raw
                    outcome = "WIN" if pnl_r > 0 else "LOSS"
                return_pct = pnl_r * risk_pct - 0.08
                equity *= 1 + return_pct / 100
                peak = max(peak, equity)
                max_drawdown = max(max_drawdown, (peak - equity) / peak * 100)
                trades.append({
                    "direction": direction, "entry_time": position["time"],
                    "exit_time": candle["open_time"], "entry": round(position["entry"], 10),
                    "exit": round(exit_price, 10), "outcome": outcome,
                    "pnl_r": round(pnl_r, 3), "return_percent": round(return_pct, 4),
                })
                position = None
        if position is None:
            long_signal = evaluate_condition(spec["long_condition"], series, index)
            short_signal = evaluate_condition(spec["short_condition"], series, index)
            if long_signal != short_signal:
                direction = "LONG" if long_signal else "SHORT"
                entry = candle["close"]
                risk = entry * risk_pct / 100
                stop = entry - risk if direction == "LONG" else entry + risk
                target = entry + risk * rr if direction == "LONG" else entry - risk * rr
                position = {"direction": direction, "entry": entry, "risk": risk,
                            "stop": stop, "target": target, "time": candle["close_time"]}
        if index % max(len(candles) // 100, 1) == 0:
            curve.append({"time": candle["close_time"], "equity": round(equity, 4)})
    wins = sum(item["outcome"] == "WIN" for item in trades)
    losses = len(trades) - wins
    gross_win = sum(max(item["return_percent"], 0) for item in trades)
    gross_loss = abs(sum(min(item["return_percent"], 0) for item in trades))
    return {
        "bars_tested": len(candles), "total_trades": len(trades),
        "winning_trades": wins, "losing_trades": losses,
        "win_rate": wins * 100 / len(trades) if trades else 0,
        "net_return_percent": equity - 100,
        "profit_factor": gross_win / gross_loss if gross_loss else (gross_win if gross_win else 0),
        "max_drawdown_percent": max_drawdown,
        "trades": trades[-500:], "equity_curve": curve,
    }


def parse_klines(rows):
    return [{
        "open_time": int(row[0]), "open": float(row[1]), "high": float(row[2]),
        "low": float(row[3]), "close": float(row[4]), "volume": float(row[5]),
        "close_time": int(row[6]),
    } for row in rows]


def timestamp_datetime(value):
    return datetime.fromtimestamp(value / 1000, tz=datetime_timezone.utc)
