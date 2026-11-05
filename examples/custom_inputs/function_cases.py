# Run: gbm experiment --use examples/custom_inputs/function_cases.py --for examples/custom_inputs/function_app.py
CASES = [
    {"name": "positive", "function": "add", "inputs": {"a": 2, "b": 3}, "expected": 5},
    {"name": "negative", "function": "add", "args": [-1, 1], "expected": 0},
]
