from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import pandas as pd

from app.services.cleaner import clean_csv


class CleanerTests(unittest.TestCase):
    def test_clean_csv_removes_duplicates_and_fills_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            input_path = tmp_path / "sample.csv"
            output_path = tmp_path / "sample.cleaned.csv"

            frame = pd.DataFrame(
                {
                    "name": ["Alpha", "Alpha", "Beta", "Gamma"],
                    "amount": ["10", "10", "20", "1000"],
                    "note": ["  hello ", "  hello ", None, "world"],
                }
            )
            frame.to_csv(input_path, index=False)

            summary = clean_csv(input_path, output_path)

            self.assertTrue(output_path.exists())
            self.assertTrue(summary["cleaning_applied"])
            self.assertGreaterEqual(summary["duplicates_removed"], 1)

            cleaned = pd.read_csv(output_path)
            self.assertEqual(len(cleaned), summary["rows_after"])
            self.assertEqual(int(cleaned["note"].isna().sum()), 0)
            self.assertEqual(summary["missing_values_after"], int(cleaned.isna().sum().sum()))


if __name__ == "__main__":
    unittest.main()