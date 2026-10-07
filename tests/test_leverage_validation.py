import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tests._aiohttp_stub  # noqa: F401,E402
import tests._pydantic_stub  # noqa: F401,E402
import tests._fastapi_stub  # noqa: F401,E402

from app.api.schemas import _validate_leverage  # noqa: E402


class TestLeverageMustBeWholeNumber(unittest.TestCase):
    """Guards against a real risk: Binance/OKX both require integer
    leverage - if a fractional value (e.g. 12.5) were ever accepted here,
    it would be silently truncated later (int(12.5) == 12) with no warning
    anywhere the user would see. This confirms the rejection happens up
    front, before that silent truncation could ever occur."""

    def test_fractional_leverage_is_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            _validate_leverage(12.5)
        self.assertIn("whole number", str(ctx.exception))

    def test_whole_number_leverage_passes_through_unchanged(self):
        self.assertEqual(_validate_leverage(12.0), 12.0)
        self.assertEqual(_validate_leverage(1.0), 1.0)
        self.assertEqual(_validate_leverage(125.0), 125.0)

    def test_small_fractional_difference_is_still_rejected(self):
        # Not just "obviously fractional" values - anything that isn't
        # exactly a whole number must be caught.
        with self.assertRaises(ValueError):
            _validate_leverage(20.001)


if __name__ == "__main__":
    unittest.main()
