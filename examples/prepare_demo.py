"""Create a repeatable, deliberately failing bug-fix demo outside the core code."""
from __future__ import annotations

import argparse
from pathlib import Path


CART_SOURCE = '''"""Demo shopping cart: discount=0.10 means 10% off the subtotal."""


def calculate_total(prices: list[float], discount: float = 0.0) -> float:
    """Return the amount due, rounded to two decimal places."""
    subtotal = sum(prices)
    # Deliberate demo bug: this returns the discount amount instead of the amount due.
    return round(subtotal * discount, 2)
'''

TEST_SOURCE = '''import unittest

from cart import calculate_total


class CartTests(unittest.TestCase):
    def test_without_discount(self):
        self.assertEqual(calculate_total([100.0, 150.0]), 250.0)

    def test_ten_percent_discount(self):
        self.assertEqual(calculate_total([100.0, 150.0], 0.10), 225.0)

    def test_full_discount(self):
        self.assertEqual(calculate_total([100.0, 150.0], 1.0), 0.0)

    def test_empty_cart(self):
        self.assertEqual(calculate_total([], 0.10), 0.0)


if __name__ == "__main__":
    unittest.main()
'''


def main() -> None:
    parser = argparse.ArgumentParser(description="Prepare the multiCC bug-fix demo")
    parser.add_argument("--reset", action="store_true", help="Overwrite the two demo files with their original contents")
    args = parser.parse_args()
    repository = Path(__file__).resolve().parent.parent
    workspace = repository / ".demo-workspace"
    files = {"cart.py": CART_SOURCE, "test_cart.py": TEST_SOURCE}
    if workspace.is_symlink() or not workspace.resolve().is_relative_to(repository):
        parser.error("Demo workspace must stay inside this repository")
    for name in files:
        target = workspace / name
        if target.is_symlink() or not target.resolve().is_relative_to(workspace.resolve()):
            parser.error(f"Refusing redirected demo file: {name}")
        if target.exists() and not args.reset:
            parser.error("Demo files already exist; use --reset to restore the initial bug")
    workspace.mkdir(exist_ok=True)
    for name, content in files.items():
        (workspace / name).write_text(content, encoding="utf-8", newline="\n")
    print(f"Demo ready: {workspace}")
    print("Run: python -m unittest discover -s .demo-workspace -v")
    print("Expected before the fix: 4 tests, 3 failures. Expected after the fix: 4 passing tests.")
    print("Reset with: python examples/prepare_demo.py --reset")


if __name__ == "__main__":
    main()
