from __future__ import annotations

import unittest
from types import SimpleNamespace

from agentbot.commands import _journal_pages, _profile_pages, _requested_page


class CommandPaginationTests(unittest.TestCase):
    def test_every_record_is_paginated_without_truncation(self) -> None:
        profile_records = [
            SimpleNamespace(
                id=index,
                kind="impression" if index % 2 else "fact",
                topic=f"topic-{index}",
                status="confirmed",
                provenance="inferred" if index % 2 else "direct",
                text=(f"profile text {index} " + "*_" * 140),
            )
            for index in range(1, 101)
        ]
        journal_entries = [
            SimpleNamespace(
                id=index,
                created_at=1_700_000_000 + index,
                text=(f"journal text {index} " + "*_" * 260),
            )
            for index in range(1, 101)
        ]
        cases = (
            (
                "profile",
                profile_records,
                _profile_pages,
                "`profile:{}`",
                "profile text {}",
            ),
            (
                "journal",
                journal_entries,
                _journal_pages,
                "`journal:{}`",
                "journal text {}",
            ),
        )

        for kind, records, paginate, marker, text in cases:
            with self.subTest(kind=kind):
                pages = paginate(records)
                self.assertGreater(len(pages), 1)
                self.assertTrue(all(len(page) <= 1_850 for page in pages))
                combined = "\n".join(pages)
                for record in records:
                    self.assertEqual(combined.count(marker.format(record.id)), 1)
                    self.assertIn(text.format(record.id), combined)

    def test_requested_page_reports_bounds_and_never_hides_page_count(self) -> None:
        pages = ("first", "second", "third")

        self.assertEqual(
            _requested_page(pages, 2, label="Profile records"),
            "Profile records - page 2/3\nsecond",
        )
        self.assertEqual(
            _requested_page(pages, 99, label="Profile records"),
            "Page 99 is out of range. Choose a page from 1 to 3.",
        )


if __name__ == "__main__":
    unittest.main()
