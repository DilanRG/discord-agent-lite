from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from scripts.verify_manifest import ManifestError, verify_manifest


class ManifestTests(unittest.TestCase):
    def test_verifier_accepts_matching_files_and_rejects_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            payload = root / "payload.txt"
            payload.write_bytes(b"known release payload\n")
            digest = hashlib.sha256(payload.read_bytes()).hexdigest()
            (root / "MANIFEST.sha256").write_text(
                f"{digest}  ./payload.txt\n",
                encoding="ascii",
            )

            self.assertEqual(verify_manifest(root), 1)
            with self.assertRaisesRegex(ManifestError, "unlisted tracked files"):
                verify_manifest(root, expected_paths={"payload.txt", "missing.txt"})
            payload.write_bytes(b"tampered\n")
            with self.assertRaisesRegex(ManifestError, "hash mismatch"):
                verify_manifest(root)

    def test_verifier_rejects_unsafe_and_duplicate_paths(self) -> None:
        digest = "0" * 64
        for body, expected in (
            (f"{digest}  ./../outside.txt\n", "unsafe manifest path"),
            (
                f"{digest}  ./payload.txt\n{digest}  ./payload.txt\n",
                "duplicate manifest path",
            ),
        ):
            with self.subTest(expected=expected), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                (root / "payload.txt").write_bytes(b"payload")
                (root / "MANIFEST.sha256").write_text(body, encoding="ascii")
                with self.assertRaisesRegex(ManifestError, expected):
                    verify_manifest(root)


if __name__ == "__main__":
    unittest.main()
