# Calculate

Evaluates an arithmetic expression safely. The expression is parsed and only numbers, arithmetic
operators (`+ - * / // % **`), parentheses, the constants `pi`, `e` and `tau`, and a few math
functions are allowed; nothing is passed to `eval`. Exponents are capped to keep runaway powers
from hanging.

**Usage Example:**

```bash
python3 calc/calc.py "(17 * 23) + sqrt(144)"
```
