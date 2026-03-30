"""
Safe AST-based formula evaluator for PLC4X Manager calculated tags.

Evaluates mathematical expressions using a restricted subset of Python
operators and functions. No arbitrary code execution is possible.

Usage:
    result, error = evaluate_formula("(Temp1 + Temp2) / 2", {"Temp1": 100.0, "Temp2": 90.0})
    # result = 95.0, error = None
"""

from __future__ import annotations

import ast
import math
import operator
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple, Union

# =============================================
# Allowed operators
# =============================================

_SAFE_OPS: dict = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
}

# =============================================
# Allowed functions
# =============================================

_SAFE_FUNCS: dict = {
    "abs": abs,
    "round": round,
    "min": min,
    "max": max,
    "sqrt": math.sqrt,
    "ceil": math.ceil,
    "floor": math.floor,
    "log": math.log,
    "log10": math.log10,
    "pow": pow,
    "sum": sum,
    "avg": lambda *args: sum(args) / len(args) if args else 0,
    "int": int,
    "float": float,
}


# =============================================
# Value serializer
# =============================================

def _serialize_value(val: Any) -> Any:
    """Convert a value to a JSON-serializable type."""
    if val is None:
        return None
    if isinstance(val, (int, float, bool)):
        return val
    if isinstance(val, str):
        # Only keep printable ASCII; replace non-printable chars with hex codes
        return "".join(
            ch if 32 <= ord(ch) < 127 else f"[{ord(ch):02X}]"
            for ch in val
        )
    if isinstance(val, bytes):
        return val.hex()
    if isinstance(val, (list, tuple)):
        return [_serialize_value(v) for v in val]
    return str(val)


# =============================================
# AST evaluator
# =============================================

def _eval_node(node: ast.expr, variables: Dict[str, Any]) -> Any:
    """Recursively evaluate an AST node using only safe operations."""
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float)):
            return node.value
        raise ValueError(f"Unsupported constant type: {type(node.value)}")

    if isinstance(node, ast.Name):
        if node.id in _SAFE_FUNCS:
            return _SAFE_FUNCS[node.id]
        if node.id in variables:
            val = variables[node.id]
            if isinstance(val, (int, float)):
                return val
            raise ValueError(f"Tag '{node.id}' has non-numeric value")
        raise KeyError(node.id)

    if isinstance(node, ast.BinOp):
        op = _SAFE_OPS.get(type(node.op))
        if op is None:
            raise ValueError(f"Unsupported operator: {type(node.op).__name__}")
        left = _eval_node(node.left, variables)
        right = _eval_node(node.right, variables)
        return op(left, right)

    if isinstance(node, ast.UnaryOp):
        op = _SAFE_OPS.get(type(node.op))
        if op is None:
            raise ValueError(f"Unsupported operator: {type(node.op).__name__}")
        return op(_eval_node(node.operand, variables))

    if isinstance(node, ast.Call):
        func = _eval_node(node.func, variables)
        if not callable(func):
            raise ValueError("Not a function")
        args = [_eval_node(a, variables) for a in node.args]
        return func(*args)

    if isinstance(node, ast.IfExp):
        test = _eval_node(node.test, variables)
        return _eval_node(node.body, variables) if test else _eval_node(node.orelse, variables)

    if isinstance(node, ast.Compare):
        left = _eval_node(node.left, variables)
        for op_node, comparator in zip(node.ops, node.comparators):
            right = _eval_node(comparator, variables)
            if isinstance(op_node, ast.Gt):
                if not (left > right):
                    return False
            elif isinstance(op_node, ast.Lt):
                if not (left < right):
                    return False
            elif isinstance(op_node, ast.GtE):
                if not (left >= right):
                    return False
            elif isinstance(op_node, ast.LtE):
                if not (left <= right):
                    return False
            elif isinstance(op_node, ast.Eq):
                if not (left == right):
                    return False
            elif isinstance(op_node, ast.NotEq):
                if not (left != right):
                    return False
            else:
                raise ValueError("Unsupported comparison operator")
            left = right
        return True

    if isinstance(node, ast.BoolOp):
        if isinstance(node.op, ast.And):
            return all(_eval_node(v, variables) for v in node.values)
        if isinstance(node.op, ast.Or):
            return any(_eval_node(v, variables) for v in node.values)

    raise ValueError(f"Unsupported expression: {type(node).__name__}")


# =============================================
# Public API
# =============================================

def evaluate_formula(
    formula: str,
    tag_values: Dict[str, Any],
) -> Tuple[Optional[Any], Optional[str]]:
    """Safely evaluate a mathematical formula using tag values as variables.

    Args:
        formula: Expression string, e.g. "(Temp1 + Temp2) / 2"
        tag_values: Mapping of tag alias -> numeric value

    Returns:
        (result, None) on success, or (None, error_string) on failure.
    """
    try:
        tree = ast.parse(formula, mode="eval")
        result = _eval_node(tree.body, tag_values)
        return _serialize_value(result), None
    except ZeroDivisionError:
        return None, "Division by zero"
    except KeyError as e:
        return None, f"Unknown tag: {e}"
    except Exception as e:
        return None, f"Formula error: {type(e).__name__}"


def _process_calculated_tags(device_result: dict, dev_config: Optional[dict] = None) -> None:
    """Evaluate all calculated tags for a device and append results to device_result["tags"].

    Calculated tags can reference regular tags AND previously evaluated calculated tags
    (i.e., they chain in declaration order).

    Args:
        device_result: The live-read result dict for a device (mutated in place).
        dev_config: The device config dict containing calculatedTags. If None, loads from
                    config_manager.load_config() by matching device name.
    """
    if dev_config is None:
        from config_manager import load_config
        config = load_config()
        for d in config.get("devices", []):
            if d.get("name") == device_result.get("name"):
                dev_config = d
                break

    if not dev_config:
        return

    calc_tags = dev_config.get("calculatedTags", [])
    if not calc_tags:
        return

    # Build variable map from successfully read real tags
    tag_values: Dict[str, Any] = {}
    for tag in device_result.get("tags", []):
        if tag.get("status") == "ok" and isinstance(tag.get("value"), (int, float)):
            tag_values[tag["alias"]] = tag["value"]

    # Evaluate each calculated tag (chaining: later tags can use earlier results)
    for calc in calc_tags:
        alias = calc.get("alias", "")
        formula = calc.get("formula", "")
        if not alias or not formula:
            continue

        value, error = evaluate_formula(formula, tag_values)
        tag_result = {
            "alias": alias,
            "address": f"calc: {formula}",
            "value": value,
            "status": "ok" if error is None else "calc_error",
            "timestamp": datetime.now(timezone.utc).isoformat() if error is None else None,
            "calculated": True,
            "formula": formula,
            "error": error,
        }
        device_result["tags"].append(tag_result)

        # Make available to subsequent formulas
        if error is None and isinstance(value, (int, float)):
            tag_values[alias] = value
