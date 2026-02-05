#!/usr/bin/env python3
"""
Test file filtering logic to ensure test data files are excluded.
"""

import sys

# Test the new filtering logic
REVIEWABLE_SUFFIXES = {
    '.c', '.h', '.cc', '.cpp', '.cxx', '.hpp', '.hxx',
    '.s', '.S',
    '.sh', '.bash', '.ksh', '.zsh',
    '.py',
    '.awk', '.sed', '.perl', '.pl',
    '.mk', '.cmake',
    '.m4',
    '.rs',
    '.go',
    '.m', '.mm',
    '.1', '.2', '.3', '.4', '.5', '.6', '.7', '.8', '.9', '.mdoc',
    '.l', '.y', '.ll', '.yy',
    '.md',
}

EXCLUDED_SUFFIXES = {
    '.in', '.ok', '.out', '.err', '.txt', '.log', '.dat', '.data',
    '.expected', '.actual', '.diff', '.orig', '.rej', '.bak',
    '.golden', '.baseline', '.result', '.output', '.input',
}


def should_review(filename: str) -> bool:
    """Check if a file should be reviewed."""
    # Extract suffix
    if '.' not in filename:
        return False

    suffix = '.' + filename.rsplit('.', 1)[1].lower()

    # Check if excluded
    if suffix in EXCLUDED_SUFFIXES:
        return False

    # Check if reviewable
    return suffix in REVIEWABLE_SUFFIXES


# Test cases
test_cases = [
    # Should be reviewed
    ("test.c", True),
    ("foo.h", True),
    ("bar.cpp", True),
    ("script.sh", True),
    ("main.py", True),
    ("config.mk", True),
    ("README.md", True),

    # Should NOT be reviewed (test data)
    ("pf0058.in", False),
    ("pf0057.ok", False),
    ("test.out", False),
    ("error.err", False),
    ("data.txt", False),
    ("test.log", False),
    ("expected.dat", False),
    ("baseline.golden", False),
]

print("Testing file filtering logic...")
print("=" * 60)

all_passed = True
for filename, expected in test_cases:
    result = should_review(filename)
    status = "✓" if result == expected else "✗"
    if result != expected:
        all_passed = False
        print(f"{status} FAIL: {filename} - expected {expected}, got {result}")
    else:
        print(f"{status} {filename} -> {'review' if result else 'skip'}")

print("=" * 60)
if all_passed:
    print("All tests passed!")
    sys.exit(0)
else:
    print("Some tests failed!")
    sys.exit(1)
