check-submit strips the failure text of eb-stack with Python's whitespace rules. A stderr of Unicode whitespace alone no longer hides the stdout. The Python guard reports the stdout in that case.
