# Run: gbm experiment --use examples/custom_inputs/input_cases.py --for examples/custom_inputs/input_app.py
CASES = [
    {"name": "positive", "stdin": "2\n3\n", "expected_stdout": "5\n"},
    {"name": "negative", "stdin": "-1\n1\n", "expected_stdout": "0\n"},
]
