#!/usr/bin/env python3
"""
Usage: python3 calc.py "<expression>"

Evaluates an arithmetic expression safely: the expression is parsed, and only numbers,
arithmetic operators and a few math functions and constants are allowed (no eval).
"""
import ast
import math
import operator
import sys

OPERATORS = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
    ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod,
    ast.Pow: operator.pow, ast.UAdd: operator.pos, ast.USub: operator.neg,
}
FUNCTIONS = {name: getattr(math, name) for name in (
    "sqrt", "exp", "log", "log2", "log10", "sin", "cos", "tan", "asin", "acos", "atan",
    "floor", "ceil", "factorial", "radians", "degrees")}
FUNCTIONS.update(abs=abs, round=round, min=min, max=max)
CONSTANTS = {"pi": math.pi, "e": math.e, "tau": math.tau}
MAX_EXPONENT = 10_000  # Keeps 9**9**9 from running forever


def evaluate(node):
    if isinstance(node, ast.Expression):
        return evaluate(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
        return node.value
    if isinstance(node, ast.Name) and node.id in CONSTANTS:
        return CONSTANTS[node.id]
    if isinstance(node, ast.UnaryOp) and type(node.op) in OPERATORS:
        return OPERATORS[type(node.op)](evaluate(node.operand))
    if isinstance(node, ast.BinOp) and type(node.op) in OPERATORS:
        left, right = evaluate(node.left), evaluate(node.right)
        if isinstance(node.op, ast.Pow) and abs(right) > MAX_EXPONENT:
            raise ValueError(f"exponent {right} is too large")
        return OPERATORS[type(node.op)](left, right)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in FUNCTIONS and not node.keywords:
        return FUNCTIONS[node.func.id](*(evaluate(arg) for arg in node.args))
    raise ValueError("only numbers, + - * / // % **, parentheses, pi, e, tau and the listed math "
                     "functions are allowed")


def main() -> int:
    if len(sys.argv) != 2 or not sys.argv[1].strip():
        print("Error: pass one arithmetic expression, e.g. \"(2 + 3) * 4\"")
        return 1
    expression = sys.argv[1]
    try:
        result = evaluate(ast.parse(expression, mode="eval"))
    except (SyntaxError, ValueError, TypeError, ZeroDivisionError, OverflowError) as error:
        print(f"Error: cannot evaluate '{expression}': {error}")
        return 1
    if isinstance(result, float) and result.is_integer() and abs(result) < 1e15:
        result = int(result)
    print(f"{expression} = {result}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
