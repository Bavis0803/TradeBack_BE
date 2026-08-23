import ast
import re
from datetime import datetime, timezone as datetime_timezone
from decimal import Decimal


class StrategyCompileError(ValueError):
    pass


PRICE_SERIES = {"open", "high", "low", "close", "volume"}
DERIVED_SERIES = {"hl2", "hlc3", "ohlc4", "bar_index", "time"}
INDICATORS = {
    "sma", "ema", "rma", "wma", "hma", "vwma", "rsi", "atr", "stdev",
    "highest", "lowest", "change", "roc", "mom",
}
CONDITION_FUNCTIONS = {"crossover", "crossunder"}
VALUE_FUNCTIONS = {"abs", "min", "max", "nz"}
MAX_INDICATORS = 30


def _pine_input(code, name, kind, default):
    patterns = {
        "number": rf"\b{name}\s*=\s*input\.(?:int|float)\(\s*(-?\d+(?:\.\d+)?)",
        "bool": rf"\b{name}\s*=\s*input\.bool\(\s*(true|false)",
        "string": rf'\b{name}\s*=\s*input\.string\(\s*"([^"]+)"',
    }
    match = re.search(patterns[kind], code, re.IGNORECASE)
    if not match:
        return default
    if kind == "bool":
        return match.group(1).lower() == "true"
    if kind == "number":
        return float(match.group(1))
    return match.group(1)


def _compile_known_strategy(code, long_condition, short_condition):
    markers = (
        "ta.pivothigh", "ta.pivotlow", "longArmed", "shortArmed",
        "bullBreak", "bearBreak", "strategy.entry", "strategy.exit",
    )
    if not all(marker in code for marker in markers):
        return None
    compact = re.sub(r"\s+", " ", code)
    required_logic = (
        "upBand := close[1] > previousUp ? math.max(upRaw, previousUp) : upRaw",
        "downBand := close[1] < previousDown ? math.min(downRaw, previousDown) : downRaw",
        "bullBreak = not na(lastSwingHigh) and not swingHighBroken",
        "bearBreak = not na(lastSwingLow) and not swingLowBroken",
        "longEntry = enableLongs and inDateRange and strategy.position_size == 0",
        "shortEntry = enableShorts and inDateRange and strategy.position_size == 0",
    )
    if not all(fragment in compact for fragment in required_logic):
        raise StrategyCompileError(
            "Supertrend/BOS code was recognized, but its core state machine differs from the supported profile."
        )
    if long_condition.strip() not in {"longEntry", "long_entry"} or short_condition.strip() not in {
        "shortEntry", "short_entry",
    }:
        raise StrategyCompileError(
            "This stateful Supertrend/BOS profile requires LONG trigger longEntry and SHORT trigger shortEntry."
        )
    config = {
        "atr_period": int(_pine_input(code, "atrPeriod", "number", 10)),
        "atr_multiplier": _pine_input(code, "atrMultiplier", "number", 3.0),
        "use_wilder_atr": _pine_input(code, "useWilderATR", "bool", True),
        "pivot_left": int(_pine_input(code, "pivotLeft", "number", 5)),
        "pivot_right": int(_pine_input(code, "pivotRight", "number", 5)),
        "break_confirmation": _pine_input(code, "breakConfirmation", "string", "Close"),
        "accept_choch": _pine_input(code, "acceptChoch", "bool", False),
        "trail_with_supertrend": _pine_input(code, "trailWithSupertrend", "bool", True),
        "close_on_opposite": _pine_input(code, "closeOnOppositeST", "bool", False),
        "enable_longs": _pine_input(code, "enableLongs", "bool", True),
        "enable_shorts": _pine_input(code, "enableShorts", "bool", True),
        "risk_percent": _pine_input(code, "riskPct", "number", 1.0),
        "risk_reward": _pine_input(code, "riskReward", "number", 2.0),
        "max_position_percent": _pine_input(code, "maxPositionPct", "number", 100.0),
        "commission_percent": 0.10,
    }
    for field, fallback in (("start_time", "2020-01-01T00:00:00"), ("end_time", "2099-12-31T23:59:59")):
        pine_name = "startTime" if field == "start_time" else "endTime"
        match = re.search(rf'\b{pine_name}\s*=\s*input\.time\(timestamp\("([^"]+)"\)', code)
        value = match.group(1) if match else fallback
        config[field] = int(datetime.fromisoformat(value.replace("Z", "+00:00")).replace(
            tzinfo=datetime_timezone.utc
        ).timestamp() * 1000)
    if not 1 <= config["atr_period"] <= 500 or not 1 <= config["pivot_left"] <= 100 \
            or not 1 <= config["pivot_right"] <= 100:
        raise StrategyCompileError("Supertrend/BOS periods are outside the supported safe range.")
    return {
        "version": "safe-pine-v2",
        "engine": "supertrend_bos_v1",
        "profile_name": "Supertrend + Market Structure BOS",
        "config": config,
        "constants": {}, "aliases": {}, "indicators": [], "expressions": [], "sequence": [],
        "long_condition": "longEntry", "short_condition": "shortEntry",
    }


def _split_args(value):
    return [item.strip() for item in value.split(",") if item.strip()]


def _convert_ternary(expression):
    depth = 0
    question = None
    for index, character in enumerate(expression):
        if character == "(": depth += 1
        elif character == ")": depth -= 1
        elif character == "?" and depth == 0:
            question = index
            break
    if question is None:
        return expression
    depth = nested = 0
    for index in range(question + 1, len(expression)):
        character = expression[index]
        if character == "(": depth += 1
        elif character == ")": depth -= 1
        elif character == "?" and depth == 0: nested += 1
        elif character == ":" and depth == 0:
            if nested: nested -= 1
            else:
                condition = expression[:question].strip()
                truthy = expression[question + 1:index].strip()
                falsy = expression[index + 1:].strip()
                return f"({_convert_ternary(truthy)} if {_convert_ternary(condition)} else {_convert_ternary(falsy)})"
    raise StrategyCompileError("Ternary expression is missing ':'.")


def _validate_condition(expression, available_names):
    normalized = expression.replace("ta.crossover", "crossover").replace(
        "ta.crossunder", "crossunder"
    ).replace("math.abs", "abs").replace("math.min", "min").replace(
        "math.max", "max"
    ).replace("&&", " and ").replace("||", " or ")
    normalized = re.sub(r"\btrue\b", "True", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"\bfalse\b", "False", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"\bna\b", "None", normalized)
    normalized = re.sub(r"(?<![=!<>])!(?!=)", " not ", normalized).strip()
    normalized = _convert_ternary(normalized)
    try:
        tree = ast.parse(normalized, mode="eval")
    except SyntaxError as exc:
        raise StrategyCompileError(f"Invalid condition syntax: {exc.msg}.") from exc
    allowed_nodes = (
        ast.Expression, ast.BoolOp, ast.And, ast.Or, ast.UnaryOp, ast.Not,
        ast.Compare, ast.Gt, ast.GtE, ast.Lt, ast.LtE, ast.Eq, ast.NotEq,
        ast.Name, ast.Load, ast.Constant, ast.Call, ast.BinOp, ast.Add, ast.Sub,
        ast.Mult, ast.Div, ast.Mod, ast.Pow, ast.USub, ast.UAdd, ast.IfExp,
        ast.Subscript,
    )
    for node in ast.walk(tree):
        if not isinstance(node, allowed_nodes):
            raise StrategyCompileError(f"Unsupported condition element: {type(node).__name__}.")
        if isinstance(node, ast.Name) and node.id not in available_names | CONDITION_FUNCTIONS | VALUE_FUNCTIONS:
            raise StrategyCompileError(f"Unknown indicator or price series: {node.id}.")
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in CONDITION_FUNCTIONS | VALUE_FUNCTIONS:
                raise StrategyCompileError("Unsupported expression function.")
            if node.func.id in CONDITION_FUNCTIONS and (len(node.args) != 2 or node.keywords):
                raise StrategyCompileError(f"{node.func.id}() requires exactly two values.")
            if node.func.id == "abs" and len(node.args) != 1:
                raise StrategyCompileError("math.abs() requires one value.")
            if node.func.id in {"min", "max"} and len(node.args) < 2:
                raise StrategyCompileError(f"math.{node.func.id}() requires at least two values.")
            if node.func.id == "nz" and not 1 <= len(node.args) <= 2:
                raise StrategyCompileError("nz() requires one value and an optional replacement.")
        if isinstance(node, ast.Subscript):
            if not isinstance(node.value, ast.Name) or not isinstance(node.slice, ast.Constant):
                raise StrategyCompileError("History references must look like close[1].")
            if not isinstance(node.slice.value, int) or not 0 <= node.slice.value <= 500:
                raise StrategyCompileError("History offset must be an integer between 0 and 500.")
    return normalized


def compile_strategy(indicator_code, long_condition, short_condition=""):
    if len(indicator_code or "") > 20000:
        raise StrategyCompileError("Indicator code is too long.")
    known = _compile_known_strategy(indicator_code or "", long_condition or "", short_condition or "")
    if known:
        return known
    constants = {}
    indicators = []
    expressions = []
    aliases = {}
    sequence = []
    available = set(PRICE_SERIES | DERIVED_SERIES)
    for raw_line in (indicator_code or "").splitlines():
        line = raw_line.split("//", 1)[0].strip()
        line = re.sub(r"^(?:var\s+)?(?:float|int|bool)\s+", "", line)
        if not line or line.startswith((
            "indicator(", "strategy(", "plot(", "plotshape(", "plotchar(",
            "hline(", "fill(", "bgcolor(", "barcolor(", "alertcondition(",
        )):
            continue
        constant_match = re.fullmatch(
            r"([A-Za-z_]\w*)\s*=\s*(?:(?:input(?:\.(?:int|float))?)\()?\s*"
            r"(-?\d+(?:\.\d+)?)(?:\s*,[^)]*)?\)?",
            line,
        )
        if constant_match:
            constants[constant_match.group(1)] = float(constant_match.group(2))
            available.add(constant_match.group(1))
            continue
        bool_match = re.fullmatch(
            r"([A-Za-z_]\w*)\s*=\s*(?:input\.bool\()?\s*(true|false)(?:\s*,[^)]*)?\)?",
            line, re.IGNORECASE,
        )
        if bool_match:
            constants[bool_match.group(1)] = bool_match.group(2).lower() == "true"
            available.add(bool_match.group(1))
            continue
        source_match = re.fullmatch(
            r"([A-Za-z_]\w*)\s*=\s*input\.source\(\s*([A-Za-z_]\w*)(?:\s*,[^)]*)?\)",
            line, re.IGNORECASE,
        )
        if source_match:
            name, source = source_match.group(1), source_match.group(2)
            if name in available or source not in available:
                raise StrategyCompileError(f"Invalid input source assignment: {line}.")
            aliases[name] = source
            available.add(name)
            continue
        multi = re.fullmatch(
            r"\[\s*([A-Za-z_]\w*)\s*,\s*([A-Za-z_]\w*)\s*,\s*([A-Za-z_]\w*)\s*\]"
            r"\s*=\s*ta\.(macd|bb)\(([^)]+)\)", line, re.IGNORECASE,
        )
        if multi:
            names = [multi.group(1), multi.group(2), multi.group(3)]
            function, args = multi.group(4).lower(), _split_args(multi.group(5))
            if any(name in available for name in names):
                raise StrategyCompileError("Duplicate or reserved multi-output variable name.")
            expected = 4 if function == "macd" else 3
            if len(args) != expected or args[0] not in available:
                raise StrategyCompileError(f"ta.{function}() requires a known source and {expected - 1} fixed parameters.")
            try:
                values = [float(constants.get(value, value)) for value in args[1:]]
            except (TypeError, ValueError) as exc:
                raise StrategyCompileError(f"Parameters for ta.{function}() must be fixed numbers.") from exc
            if function == "macd":
                lengths = [int(value) for value in values]
                if any(not 2 <= value <= 500 for value in lengths):
                    raise StrategyCompileError("MACD lengths must be between 2 and 500.")
                indicators.append({"names": names, "function": function, "source": args[0], "lengths": lengths})
            else:
                length, multiplier = int(values[0]), values[1]
                if not 2 <= length <= 500 or not 0.1 <= multiplier <= 20:
                    raise StrategyCompileError("Bollinger length/multiplier is outside the safe range.")
                indicators.append({"names": names, "function": function, "source": args[0], "length": length, "multiplier": multiplier})
            sequence.append({"kind": "indicator", "index": len(indicators) - 1})
            available.update(names)
            if len(indicators) + len(expressions) > MAX_INDICATORS:
                raise StrategyCompileError(f"A strategy can define at most {MAX_INDICATORS} computed series.")
            continue
        match = re.fullmatch(
            r"([A-Za-z_]\w*)\s*=\s*ta\.([a-z]+)\(([^)]+)\)",
            line,
            re.IGNORECASE,
        )
        if match:
            name, function, raw_args = match.group(1), match.group(2).lower(), match.group(3)
            if function not in INDICATORS:
                raise StrategyCompileError(f"Unsupported indicator ta.{function}().")
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
            if not 1 <= length <= 500:
                raise StrategyCompileError(f"Length for {name} must be between 1 and 500.")
            indicators.append({"name": name, "function": function, "source": source, "length": length})
            sequence.append({"kind": "indicator", "index": len(indicators) - 1})
            available.add(name)
        else:
            expression_match = re.fullmatch(r"([A-Za-z_]\w*)\s*=\s*(.+)", line)
            if not expression_match:
                raise StrategyCompileError(f"Unsupported Pine line: {raw_line.strip()}.")
            name, expression = expression_match.group(1), expression_match.group(2)
            if name in available:
                raise StrategyCompileError(f"Duplicate or reserved variable name: {name}.")
            normalized = _validate_condition(expression, available)
            expressions.append({"name": name, "expression": normalized})
            sequence.append({"kind": "expression", "index": len(expressions) - 1})
            available.add(name)
        if len(indicators) + len(expressions) > MAX_INDICATORS:
            raise StrategyCompileError(f"A strategy can define at most {MAX_INDICATORS} computed series.")
    if not indicators:
        raise StrategyCompileError("Define at least one supported ta.* indicator.")
    if not (long_condition or "").strip():
        raise StrategyCompileError("A LONG activation condition is required.")
    return {
        "version": "safe-pine-v1",
        "constants": constants,
        "aliases": aliases,
        "indicators": indicators,
        "expressions": expressions,
        "sequence": sequence,
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


def _rma(values, length):
    result = [None] * len(values)
    seed = None
    valid = []
    for index, value in enumerate(values):
        if value is None:
            continue
        if seed is None:
            valid.append(value)
            if len(valid) == length:
                seed = sum(valid) / length
                result[index] = seed
        else:
            seed = (seed * (length - 1) + value) / length
            result[index] = seed
    return result


def _wma(values, length):
    result = [None] * len(values)
    denominator = length * (length + 1) / 2
    for index in range(length - 1, len(values)):
        window = values[index - length + 1:index + 1]
        if all(value is not None for value in window):
            result[index] = sum(value * weight for weight, value in enumerate(window, 1)) / denominator
    return result


def _binary_series(left, right, operation):
    return [operation(a, b) if a is not None and b is not None else None for a, b in zip(left, right)]


def _hma(values, length):
    half = _wma(values, max(1, length // 2))
    full = _wma(values, length)
    difference = _binary_series(half, full, lambda a, b: 2 * a - b)
    return _wma(difference, max(1, int(length ** 0.5)))


def _rolling(values, length, reducer):
    result = [None] * len(values)
    for index in range(length - 1, len(values)):
        window = values[index - length + 1:index + 1]
        if all(value is not None for value in window):
            result[index] = reducer(window)
    return result


def _stdev(values, length):
    return _rolling(values, length, lambda window: (
        sum((value - sum(window) / len(window)) ** 2 for value in window) / len(window)
    ) ** 0.5)


def _lag_operation(values, length, operation):
    result = [None] * len(values)
    for index in range(length, len(values)):
        if values[index] is not None and values[index - length] is not None:
            result[index] = operation(values[index], values[index - length])
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


def _supertrend_bos_simulation(candles, config, risk_reward=None, risk_percent=None):
    count = len(candles)
    prices = {key: [float(item[key]) for item in candles] for key in PRICE_SERIES}
    highs, lows, closes = prices["high"], prices["low"], prices["close"]
    source = [(high + low) / 2 for high, low in zip(highs, lows)]
    true_range = []
    for index in range(count):
        previous = closes[index - 1] if index else closes[index]
        true_range.append(max(highs[index] - lows[index], abs(highs[index] - previous), abs(lows[index] - previous)))
    atr = (_rma if config["use_wilder_atr"] else _sma)(true_range, config["atr_period"])
    up_bands, down_bands, trends = [None] * count, [None] * count, [1] * count
    long_entries, short_entries = [False] * count, [False] * count
    bull_breaks, bear_breaks = [False] * count, [False] * count
    last_high = last_low = None
    high_broken = low_broken = False
    structure_trend = 0
    long_armed = short_armed = False
    long_armed_bar = short_armed_bar = None
    position = None
    trades, curve = [], []
    equity, peak, max_drawdown = 100.0, 100.0, 0.0
    rr = float(risk_reward if risk_reward is not None else config["risk_reward"])
    risk_pct = float(risk_percent if risk_percent is not None else config["risk_percent"])

    def close_position(index, price, reason):
        nonlocal position, equity, peak, max_drawdown
        direction = position["direction"]
        gross = (price - position["entry"]) * position["quantity"] * (1 if direction == "LONG" else -1)
        fee = (position["entry"] + price) * position["quantity"] * config["commission_percent"] / 100
        pnl = gross - fee
        equity += pnl
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, (peak - equity) / peak * 100 if peak else 0)
        unit_risk = position["risk_distance"] * position["quantity"]
        trades.append({
            "direction": direction, "entry_time": position["time"],
            "exit_time": candles[index]["open_time"], "entry": round(position["entry"], 10),
            "exit": round(price, 10), "outcome": "WIN" if pnl > 0 else "LOSS",
            "pnl_r": round(pnl / unit_risk, 3) if unit_risk else 0,
            "return_percent": round(pnl / position["equity_at_entry"] * 100, 4),
            "reason": reason,
        })
        position = None

    left, right = config["pivot_left"], config["pivot_right"]
    for index in range(count):
        if atr[index] is not None:
            up_raw = source[index] - config["atr_multiplier"] * atr[index]
            down_raw = source[index] + config["atr_multiplier"] * atr[index]
            previous_up = up_bands[index - 1] if index and up_bands[index - 1] is not None else up_raw
            previous_down = down_bands[index - 1] if index and down_bands[index - 1] is not None else down_raw
            previous_close = closes[index - 1] if index else closes[index]
            up_bands[index] = max(up_raw, previous_up) if previous_close > previous_up else up_raw
            down_bands[index] = min(down_raw, previous_down) if previous_close < previous_down else down_raw
            previous_trend = trends[index - 1] if index else 1
            trends[index] = (
                1 if previous_trend == -1 and closes[index] > previous_down
                else -1 if previous_trend == 1 and closes[index] < previous_up
                else previous_trend
            )
        elif index:
            trends[index] = trends[index - 1]
        previous_trend = trends[index - 1] if index else trends[index]
        st_buy = trends[index] == 1 and previous_trend == -1
        st_sell = trends[index] == -1 and previous_trend == 1

        if position and index > position["entry_index"]:
            stop_hit = lows[index] <= position["stop"] if position["direction"] == "LONG" else highs[index] >= position["stop"]
            target_hit = highs[index] >= position["target"] if position["direction"] == "LONG" else lows[index] <= position["target"]
            opposite = config["close_on_opposite"] and (st_sell if position["direction"] == "LONG" else st_buy)
            if stop_hit:
                close_position(index, position["stop"], "STOP_LOSS")
            elif target_hit:
                close_position(index, position["target"], "TAKE_PROFIT")
            elif opposite:
                close_position(index, closes[index], "OPPOSITE_SUPERTREND")
            elif config["trail_with_supertrend"]:
                if position["direction"] == "LONG" and trends[index] == 1 and up_bands[index] is not None and up_bands[index] < closes[index]:
                    position["stop"] = max(position["stop"], up_bands[index])
                elif position["direction"] == "SHORT" and trends[index] == -1 and down_bands[index] is not None and down_bands[index] > closes[index]:
                    position["stop"] = min(position["stop"], down_bands[index])

        center = index - right
        if center >= left:
            candidate_high, candidate_low = highs[center], lows[center]
            if all(candidate_high > value for value in highs[center - left:center]) and all(
                candidate_high >= value for value in highs[center + 1:index + 1]
            ):
                last_high, high_broken = candidate_high, False
            if all(candidate_low < value for value in lows[center - left:center]) and all(
                candidate_low <= value for value in lows[center + 1:index + 1]
            ):
                last_low, low_broken = candidate_low, False
        break_up = closes[index] if config["break_confirmation"].lower() == "close" else highs[index]
        break_down = closes[index] if config["break_confirmation"].lower() == "close" else lows[index]
        previous_up_source = (closes[index - 1] if config["break_confirmation"].lower() == "close" else highs[index - 1]) if index else break_up
        previous_down_source = (closes[index - 1] if config["break_confirmation"].lower() == "close" else lows[index - 1]) if index else break_down
        bull_break = last_high is not None and not high_broken and break_up > last_high and previous_up_source <= last_high
        bear_break = last_low is not None and not low_broken and break_down < last_low and previous_down_source >= last_low
        bull_breaks[index], bear_breaks[index] = bull_break, bear_break
        bull_choch, bear_choch = bull_break and structure_trend == -1, bear_break and structure_trend == 1
        bull_bos, bear_bos = bull_break and not bull_choch, bear_break and not bear_choch
        if bull_break:
            high_broken, structure_trend = True, 1
        if bear_break:
            low_broken, structure_trend = True, -1

        if position is None and st_buy:
            long_armed, short_armed, long_armed_bar, short_armed_bar = True, False, index, None
        if position is None and st_sell:
            short_armed, long_armed, short_armed_bar, long_armed_bar = True, False, index, None
        long_confirm = bull_break and (config["accept_choch"] or bull_bos) and long_armed_bar is not None and index > long_armed_bar
        short_confirm = bear_break and (config["accept_choch"] or bear_bos) and short_armed_bar is not None and index > short_armed_bar
        valid_long_stop = up_bands[index] is not None and up_bands[index] < closes[index]
        valid_short_stop = down_bands[index] is not None and down_bands[index] > closes[index]
        in_date_range = config["start_time"] <= candles[index]["open_time"] <= config["end_time"]
        long_signal = config["enable_longs"] and in_date_range and position is None and long_armed and long_confirm and trends[index] == 1 and valid_long_stop
        short_signal = config["enable_shorts"] and in_date_range and position is None and short_armed and short_confirm and trends[index] == -1 and valid_short_stop
        long_entries[index], short_entries[index] = long_signal, short_signal
        if long_signal or short_signal:
            direction = "LONG" if long_signal else "SHORT"
            stop = up_bands[index] if long_signal else down_bands[index]
            distance = abs(closes[index] - stop)
            risk_cash = equity * risk_pct / 100
            risk_quantity = risk_cash / distance if distance > 0 else 0
            max_quantity = equity * config["max_position_percent"] / 100 / closes[index] if closes[index] > 0 else 0
            quantity = min(risk_quantity, max_quantity)
            if quantity > 0:
                position = {
                    "direction": direction, "entry": closes[index], "stop": stop,
                    "target": closes[index] + distance * rr * (1 if long_signal else -1),
                    "quantity": quantity, "risk_distance": distance, "time": candles[index]["close_time"],
                    "entry_index": index, "equity_at_entry": equity,
                }
                if long_signal:
                    long_armed, long_armed_bar = False, None
                else:
                    short_armed, short_armed_bar = False, None
        if index % max(count // 100, 1) == 0:
            curve.append({"time": candles[index]["close_time"], "equity": round(equity, 4)})
    prices.update({
        "hl2": source, "upBand": up_bands, "downBand": down_bands, "stTrend": trends,
        "bullBreak": bull_breaks, "bearBreak": bear_breaks,
        "longEntry": long_entries, "shortEntry": short_entries,
    })
    wins = sum(item["outcome"] == "WIN" for item in trades)
    gross_win = sum(max(item["return_percent"], 0) for item in trades)
    gross_loss = abs(sum(min(item["return_percent"], 0) for item in trades))
    return prices, {
        "bars_tested": count, "total_trades": len(trades), "winning_trades": wins,
        "losing_trades": len(trades) - wins, "win_rate": wins * 100 / len(trades) if trades else 0,
        "net_return_percent": equity - 100, "profit_factor": gross_win / gross_loss if gross_loss else gross_win,
        "max_drawdown_percent": max_drawdown, "trades": trades[-500:], "equity_curve": curve,
    }


def _apply_indicator(series, item):
        source = series[item["source"]]
        function = item["function"]
        if function == "sma":
            values = _sma(source, item["length"])
        elif function == "ema":
            values = _ema(source, item["length"])
        elif function == "rma":
            values = _rma(source, item["length"])
        elif function == "wma":
            values = _wma(source, item["length"])
        elif function == "hma":
            values = _hma(source, item["length"])
        elif function == "vwma":
            weighted = _binary_series(source, series["volume"], lambda a, b: a * b)
            numerator = _rolling(weighted, item["length"], sum)
            denominator = _rolling(series["volume"], item["length"], sum)
            values = _binary_series(numerator, denominator, lambda a, b: a / b if b else None)
        elif function == "rsi":
            values = _rsi(source, item["length"])
        elif function == "stdev":
            values = _stdev(source, item["length"])
        elif function == "highest":
            values = _rolling(source, item["length"], max)
        elif function == "lowest":
            values = _rolling(source, item["length"], min)
        elif function == "change":
            values = _lag_operation(source, item["length"], lambda now, before: now - before)
        elif function == "mom":
            values = _lag_operation(source, item["length"], lambda now, before: now - before)
        elif function == "roc":
            values = _lag_operation(
                source, item["length"], lambda now, before: (now - before) / before * 100 if before else None
            )
        elif function == "macd":
            fast = _ema(source, item["lengths"][0])
            slow = _ema(source, item["lengths"][1])
            line = _binary_series(fast, slow, lambda a, b: a - b)
            signal = _ema(line, item["lengths"][2])
            histogram = _binary_series(line, signal, lambda a, b: a - b)
            for name, output in zip(item["names"], (line, signal, histogram)):
                series[name] = output
            return
        elif function == "bb":
            basis = _sma(source, item["length"])
            deviation = _stdev(source, item["length"])
            upper = _binary_series(basis, deviation, lambda a, b: a + b * item["multiplier"])
            lower = _binary_series(basis, deviation, lambda a, b: a - b * item["multiplier"])
            for name, output in zip(item["names"], (basis, upper, lower)):
                series[name] = output
            return
        else:
            values = _atr(series, item["length"])
        series[item["name"]] = values


def build_series(candles, spec):
    if spec.get("engine") == "supertrend_bos_v1":
        return _supertrend_bos_simulation(candles, spec["config"])[0]
    series = {key: [float(item[key]) for item in candles] for key in PRICE_SERIES}
    series["hl2"] = [(high + low) / 2 for high, low in zip(series["high"], series["low"])]
    series["hlc3"] = [(high + low + close) / 3 for high, low, close in zip(series["high"], series["low"], series["close"])]
    series["ohlc4"] = [
        (open_ + high + low + close) / 4
        for open_, high, low, close in zip(series["open"], series["high"], series["low"], series["close"])
    ]
    series["bar_index"] = list(range(len(candles)))
    series["time"] = [item["open_time"] for item in candles]
    series.update(spec.get("constants", {}))
    for name, source in spec.get("aliases", {}).items():
        series[name] = series[source]
    if spec.get("sequence"):
        for step in spec["sequence"]:
            if step["kind"] == "indicator":
                _apply_indicator(series, spec["indicators"][step["index"]])
            else:
                item = spec["expressions"][step["index"]]
                tree = ast.parse(item["expression"], mode="eval").body
                series[item["name"]] = [_value(tree, series, index) for index in range(len(candles))]
        return series
    for item in spec["indicators"]:
        _apply_indicator(series, item)
    for item in spec.get("expressions", []):
        tree = ast.parse(item["expression"], mode="eval").body
        series[item["name"]] = [_value(tree, series, index) for index in range(len(candles))]
    return series


def _value(node, series, index):
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        value = series[node.id]
        return value[index] if isinstance(value, list) else value
    if isinstance(node, ast.Subscript):
        offset = int(node.slice.value)
        target = index - offset
        value = series[node.value.id]
        return value[target] if isinstance(value, list) and target >= 0 else None
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
                      ast.Mod: lambda: left % right, ast.Pow: lambda: left ** right}
        try:
            return operations[type(node.op)]()
        except (ZeroDivisionError, OverflowError):
            return None
    if isinstance(node, ast.IfExp):
        return _value(node.body if _value(node.test, series, index) else node.orelse, series, index)
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
        if node.func.id in VALUE_FUNCTIONS:
            values = [_value(argument, series, index) for argument in node.args]
            if node.func.id == "nz":
                return values[0] if values[0] is not None else (values[1] if len(values) > 1 else 0)
            if any(value is None for value in values):
                return None
            if node.func.id == "abs":
                return abs(values[0])
            return min(values) if node.func.id == "min" else max(values)
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
    if spec.get("engine") == "supertrend_bos_v1":
        return _supertrend_bos_simulation(
            candles, spec["config"], risk_reward_ratio, stop_loss_percent
        )[1]
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
