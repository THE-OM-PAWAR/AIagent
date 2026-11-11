"""Production-grade safe calculator and financial computation engine."""

import ast
from decimal import Decimal, ROUND_HALF_UP
import math
import statistics
from typing import Any

from src.tools.base import BaseTool
from src.tools.permissions import ToolPermission

# Supported safe functions and constants in AST math evaluation
_SAFE_MATH_FUNCS = {
    "abs": abs,
    "round": round,
    "min": min,
    "max": max,
    "sum": sum,
    "pow": pow,
    "sqrt": math.sqrt,
    "log": math.log,
    "log10": math.log10,
    "log2": math.log2,
    "exp": math.exp,
    "ceil": math.ceil,
    "floor": math.floor,
    "sin": math.sin,
    "cos": math.cos,
    "tan": math.tan,
    "factorial": math.factorial,
}

_SAFE_MATH_CONSTANTS = {
    "pi": math.pi,
    "e": math.e,
    "tau": math.tau,
}


class SafeMathEvaluator:
    """Evaluates mathematical expressions strictly using Python AST without arbitrary code execution."""

    ALLOWED_NODES = (
        ast.Expression,
        ast.BinOp,
        ast.UnaryOp,
        ast.Constant,
        ast.Call,
        ast.Name,
        ast.Load,
        ast.Add,
        ast.Sub,
        ast.Mult,
        ast.Div,
        ast.FloorDiv,
        ast.Mod,
        ast.Pow,
        ast.UAdd,
        ast.USub,
    )

    def evaluate(self, expression: str) -> float | int:
        """Parse and evaluate an arithmetic expression safely."""
        expr_clean = expression.strip()
        if not expr_clean:
            raise ValueError("Expression cannot be empty.")
        if len(expr_clean) > 500:
            raise ValueError("Expression exceeds maximum allowed length of 500 characters.")

        try:
            tree = ast.parse(expr_clean, mode="eval")
        except SyntaxError as exc:
            raise ValueError(f"Invalid mathematical syntax: {exc}") from exc

        return self._eval_node(tree.body)

    def _eval_node(self, node: ast.AST) -> Any:
        if not isinstance(node, self.ALLOWED_NODES):
            raise ValueError(f"Unsafe or unsupported operation in expression: '{type(node).__name__}'.")

        if isinstance(node, ast.Constant):
            if isinstance(node.value, (int, float)):
                return node.value
            raise ValueError(f"Unsupported constant type: '{type(node.value).__name__}'.")

        elif isinstance(node, ast.Name):
            if node.id in _SAFE_MATH_CONSTANTS:
                return _SAFE_MATH_CONSTANTS[node.id]
            raise ValueError(f"Unknown variable or constant '{node.id}'.")

        elif isinstance(node, ast.UnaryOp):
            operand = self._eval_node(node.operand)
            if isinstance(node.op, ast.UAdd):
                return +operand
            elif isinstance(node.op, ast.USub):
                return -operand
            raise ValueError(f"Unsupported unary operator: '{type(node.op).__name__}'.")

        elif isinstance(node, ast.BinOp):
            left = self._eval_node(node.left)
            right = self._eval_node(node.right)

            if isinstance(node.op, ast.Add):
                return left + right
            elif isinstance(node.op, ast.Sub):
                return left - right
            elif isinstance(node.op, ast.Mult):
                return left * right
            elif isinstance(node.op, ast.Div):
                if right == 0:
                    raise ZeroDivisionError("Division by zero.")
                return left / right
            elif isinstance(node.op, ast.FloorDiv):
                if right == 0:
                    raise ZeroDivisionError("Floor division by zero.")
                return left // right
            elif isinstance(node.op, ast.Mod):
                if right == 0:
                    raise ZeroDivisionError("Modulo by zero.")
                return left % right
            elif isinstance(node.op, ast.Pow):
                if abs(left) > 10000 or right > 500:
                    raise ValueError("Exponentiation values too large to evaluate safely.")
                return left ** right
            raise ValueError(f"Unsupported binary operator: '{type(node.op).__name__}'.")

        elif isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name):
                raise ValueError("Nested or dynamic function calls are not permitted.")
            func_name = node.func.id
            if func_name not in _SAFE_MATH_FUNCS:
                raise ValueError(f"Function '{func_name}' is not in the safe mathematical whitelist.")

            args = [self._eval_node(arg) for arg in node.args]
            return _SAFE_MATH_FUNCS[func_name](*args)

        raise ValueError(f"Unsupported expression construct: '{type(node).__name__}'.")


class CalculatorTool(BaseTool):
    """Production-grade tool for deterministic arithmetic, financial, and statistical calculations."""

    def __init__(self) -> None:
        self._evaluator = SafeMathEvaluator()

    @property
    def name(self) -> str:
        return "calculator"

    @property
    def description(self) -> str:
        return (
            "Perform deterministic arithmetic, percentage, ratio, average, profit/loss, margin, "
            "interest (simple/compound), and loan EMI calculations with decimal precision."
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "description": "Calculation type to perform.",
                    "enum": [
                        "evaluate",
                        "percentage",
                        "ratio",
                        "average",
                        "profit_loss",
                        "margin_markup",
                        "interest",
                        "loan_emi",
                    ],
                },
                "expression": {
                    "type": "string",
                    "description": "Arithmetic formula for 'evaluate' action (e.g. '(1500 * 0.18) + (250 * 4)').",
                },
                "value": {"type": "number", "description": "Base value for percentage calculations."},
                "percentage": {"type": "number", "description": "Percentage value (e.g. 18 for 18%)."},
                "comparison_value": {"type": "number", "description": "Comparison/original value for change calculations."},
                "numbers": {
                    "type": "array",
                    "items": {"type": "number"},
                    "description": "List of numbers for average, median, or ratio calculations.",
                },
                "weights": {
                    "type": "array",
                    "items": {"type": "number"},
                    "description": "Optional weights corresponding to numbers for weighted averages.",
                },
                "cost_price": {"type": "number", "description": "Cost price for profit/loss or margin calculations."},
                "selling_price": {"type": "number", "description": "Selling price for profit/loss or margin calculations."},
                "principal": {"type": "number", "description": "Principal amount for interest or loan calculations."},
                "annual_rate": {"type": "number", "description": "Annual interest rate percentage (e.g. 8.5 for 8.5%)."},
                "time_years": {"type": "number", "description": "Time in years for interest calculations."},
                "tenure_months": {"type": "integer", "description": "Loan tenure in months for EMI calculations."},
                "compound_frequency_per_year": {
                    "type": "integer",
                    "description": "Compounding frequency per year (1=annual, 2=semi-annual, 4=quarterly, 12=monthly). Default is 1.",
                },
            },
            "required": ["action"],
        }

    @property
    def permission(self) -> ToolPermission:
        return ToolPermission.READ_ONLY

    async def execute(self, **kwargs: Any) -> dict[str, Any]:
        action = kwargs.get("action", "evaluate")

        if action == "evaluate":
            expr = kwargs.get("expression")
            if not expr:
                raise ValueError("The 'expression' parameter is required for action='evaluate'.")
            result = self._evaluator.evaluate(expr)
            return {
                "action": "evaluate",
                "expression": expr,
                "result": result,
                "formatted": f"{result:,}" if isinstance(result, (int, float)) else str(result),
            }

        elif action == "percentage":
            val = kwargs.get("value")
            pct = kwargs.get("percentage")
            comp = kwargs.get("comparison_value")

            if val is not None and pct is not None:
                calc_val = (val * pct) / 100.0
                added = val + calc_val
                discounted = val - calc_val
                return {
                    "action": "percentage",
                    "base_value": val,
                    "percentage": pct,
                    "percentage_amount": round(calc_val, 4),
                    "value_with_percentage_added": round(added, 4),
                    "value_with_percentage_subtracted": round(discounted, 4),
                }
            elif val is not None and comp is not None:
                if comp == 0:
                    raise ZeroDivisionError("Comparison value cannot be zero when calculating percentage change.")
                diff = val - comp
                pct_change = (diff / abs(comp)) * 100.0
                return {
                    "action": "percentage_change",
                    "original_value": comp,
                    "new_value": val,
                    "absolute_difference": round(diff, 4),
                    "percentage_change": round(pct_change, 2),
                    "is_increase": diff > 0,
                }
            raise ValueError("Provide 'value' and 'percentage', or 'value' and 'comparison_value'.")

        elif action == "ratio":
            numbers = kwargs.get("numbers") or []
            if len(numbers) < 2:
                raise ValueError("At least two numbers are required to compute a ratio.")
            ints = [int(n) for n in numbers if n == int(n)]
            if len(ints) == len(numbers):
                common_gcd = math.gcd(*ints)
                simplified = [n // common_gcd for n in ints]
                ratio_str = ":".join(str(s) for s in simplified)
            else:
                ratio_str = ":".join(str(n) for n in numbers)
            return {
                "action": "ratio",
                "original_numbers": numbers,
                "simplified_ratio": ratio_str,
            }

        elif action == "average":
            numbers = kwargs.get("numbers") or []
            if not numbers:
                raise ValueError("The 'numbers' array cannot be empty for action='average'.")
            weights = kwargs.get("weights")
            mean_val = statistics.mean(numbers)
            median_val = statistics.median(numbers)

            res = {
                "action": "average",
                "count": len(numbers),
                "mean": round(mean_val, 4),
                "median": round(median_val, 4),
                "min": min(numbers),
                "max": max(numbers),
                "sum": round(sum(numbers), 4),
            }
            if weights:
                if len(weights) != len(numbers):
                    raise ValueError("Length of 'weights' must equal length of 'numbers'.")
                total_w = sum(weights)
                if total_w == 0:
                    raise ValueError("Sum of weights cannot be zero.")
                w_mean = sum(n * w for n, w in zip(numbers, weights)) / total_w
                res["weighted_mean"] = round(w_mean, 4)
            return res

        elif action == "profit_loss":
            cp = kwargs.get("cost_price")
            sp = kwargs.get("selling_price")
            if cp is None or sp is None:
                raise ValueError("Both 'cost_price' and 'selling_price' are required.")
            if cp <= 0:
                raise ValueError("Cost price must be greater than zero.")
            diff = sp - cp
            is_profit = diff >= 0
            pct = (abs(diff) / cp) * 100.0
            return {
                "action": "profit_loss",
                "cost_price": cp,
                "selling_price": sp,
                "amount": round(abs(diff), 2),
                "percentage": round(pct, 2),
                "status": "Profit" if is_profit else "Loss",
            }

        elif action == "margin_markup":
            cp = kwargs.get("cost_price")
            sp = kwargs.get("selling_price")
            if cp is None or sp is None:
                raise ValueError("Both 'cost_price' and 'selling_price' are required.")
            if sp <= 0:
                raise ValueError("Selling price must be greater than zero for margin calculation.")
            profit = sp - cp
            margin_pct = (profit / sp) * 100.0
            markup_pct = (profit / cp) * 100.0 if cp > 0 else 0.0
            return {
                "action": "margin_markup",
                "cost": cp,
                "revenue": sp,
                "gross_profit": round(profit, 2),
                "profit_margin_percentage": round(margin_pct, 2),
                "markup_percentage": round(markup_pct, 2),
            }

        elif action == "interest":
            p = kwargs.get("principal")
            r = kwargs.get("annual_rate")
            t = kwargs.get("time_years")
            n = kwargs.get("compound_frequency_per_year", 1)
            if p is None or r is None or t is None:
                raise ValueError("'principal', 'annual_rate', and 'time_years' are required.")
            if p <= 0 or r < 0 or t <= 0:
                raise ValueError("Principal and time must be positive numbers; rate cannot be negative.")

            # Simple Interest: I = P * (r/100) * t
            simple_interest = p * (r / 100.0) * t
            simple_total = p + simple_interest

            # Compound Interest: A = P * (1 + (r / 100) / n) ** (n * t)
            freq = max(1, int(n))
            compound_total = p * ((1 + (r / (100.0 * freq))) ** (freq * t))
            compound_interest = compound_total - p

            return {
                "action": "interest",
                "principal": p,
                "annual_rate": r,
                "time_years": t,
                "compounding_frequency_per_year": freq,
                "simple_interest": {
                    "interest_amount": round(simple_interest, 2),
                    "total_amount": round(simple_total, 2),
                },
                "compound_interest": {
                    "interest_amount": round(compound_interest, 2),
                    "total_amount": round(compound_total, 2),
                },
            }

        elif action == "loan_emi":
            p = kwargs.get("principal")
            r = kwargs.get("annual_rate")
            n_months = kwargs.get("tenure_months")
            if n_months is None and kwargs.get("time_years"):
                n_months = int(kwargs["time_years"] * 12)
            if p is None or r is None or n_months is None:
                raise ValueError("'principal', 'annual_rate', and 'tenure_months' (or 'time_years') are required.")
            if p <= 0 or n_months <= 0:
                raise ValueError("Principal and tenure must be positive numbers.")

            if r == 0:
                emi = p / n_months
                total_payment = p
                total_interest = 0.0
            else:
                monthly_rate = (r / 100.0) / 12.0
                emi = (p * monthly_rate * ((1 + monthly_rate) ** n_months)) / (((1 + monthly_rate) ** n_months) - 1)
                total_payment = emi * n_months
                total_interest = total_payment - p

            return {
                "action": "loan_emi",
                "principal": p,
                "annual_rate_percentage": r,
                "tenure_months": n_months,
                "monthly_emi": round(emi, 2),
                "total_interest_payable": round(total_interest, 2),
                "total_payment_payable": round(total_payment, 2),
            }

        raise ValueError(f"Unknown calculation action '{action}'.")

