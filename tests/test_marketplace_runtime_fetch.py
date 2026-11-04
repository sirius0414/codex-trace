"""运行时来源、许可完整性与安全提取检查；合成归档从不执行其中的解释器。"""
from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest import mock
from urllib.error import URLError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
try:
    import fetch_marketplace_runtime as fetch
finally:
    sys.path.pop(0)


class RuntimeFetchTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="runtime-fetch-中文 空格-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.cache = self.root / "cache"
        self.output = self.root / "prepared"
        self.metadata = {"python_version": "3.14.2", "target_triple": "aarch64-apple-darwin",
                         "license_path": "licenses/LICENSE.cpython.txt",
                         "build_info": {"libraries": [{"license_paths": ["licenses/LICENSE.library.txt"]}]}}
        self.files = {"python/install/bin/python3": b"synthetic interpreter, do not execute",
                      "python/licenses/LICENSE.cpython.txt": b"Synthetic CPython license\n",
                      "python/licenses/LICENSE.library.txt": b"Synthetic library license\n",
                      "python/licenses/UNREFERENCED-NOTICE": b"Also retain upstream notices\n",
                      "python/build/unwanted.o": b"Do not extract build products",
                      "unrelated.txt": b"Not part of the runtime"}

    def archive(self, *, extra=(), omit=(), name="runtime.tar"):
        path = self.root / name
        files = {**self.files, "python/PYTHON.json": json.dumps(self.metadata).encode()}
        with tarfile.open(path, "w") as archive:
            for filename, contents in files.items():
                if filename in omit:
                    continue
                member = tarfile.TarInfo(filename)
                member.size = len(contents)
                member.mode = 0o755 if filename.endswith("/python3") else 0o644
                archive.addfile(member, io.BytesIO(contents))
            for member, contents in extra:
                archive.addfile(member, io.BytesIO(contents) if contents is not None else None)
        data = path.read_bytes()
        entry = {"target": "darwin-arm64", "pythonVersion": "3.14.2", "targetTriple": "aarch64-apple-darwin",
                 "url": fetch.UPSTREAM_PREFIX + "synthetic-full.tar.zst", "size": len(data),
                 "sha256": hashlib.sha256(data).hexdigest(), "supplementalLicenses": {}}
        return path, entry

    def extract(self, archive):
        destination = self.root / "extract"
        destination.mkdir()
        return fetch.extract_runtime(archive, destination)

    def test_native_target_mapping_rejects_unlocked_platforms(self):
        self.assertEqual(fetch.native_target("darwin", "aarch64"), "darwin-arm64")
        self.assertEqual(fetch.native_target("darwin", "x86_64"), "darwin-x64")
        self.assertEqual(fetch.native_target("win32", "AMD64"), "win32-x64")
        for platform, machine in (("linux", "aarch64"), ("win32", "arm64"), ("darwin", "sparc")):
            with self.subTest(platform=platform, machine=machine), self.assertRaises(ValueError):
                fetch.native_target(platform, machine)

    def test_checked_in_lock_uses_fixed_full_archives_and_versioned_supplements(self):
        for target in fetch.TARGETS:
            entry = fetch.locked_runtime(target)
            self.assertEqual(entry["pythonVersion"], "3.14.2")
            self.assertIn("20260114", entry["url"])
            self.assertIn("cpython-3.14.2%2B20260114-", entry["url"])
            self.assertEqual(len(entry["sha256"]), 64)
            self.assertEqual(set(entry["supplementalLicenses"]),
                             {"licenses/LICENSE.zstd.txt", "licenses/LICENSE.zlib-ng.txt"})
            self.assertEqual(entry["supplementalLicenses"]["licenses/LICENSE.zstd.txt"]["version"], "1.5.7")

    def test_supplied_archive_is_verified_without_network_and_tamper_is_rejected(self):
        archive, entry = self.archive()
        with mock.patch.object(fetch, "urlopen") as network:
            self.assertEqual(fetch.acquire_archive(entry, self.cache, archive), archive)
            contents = archive.read_bytes()
            archive.write_bytes(b"x" + contents[1:])
            with self.assertRaisesRegex(fetch.RuntimeFetchError, "SHA-256"):
                fetch.acquire_archive(entry, self.cache, archive)
            network.assert_not_called()
        self.assertFalse(self.cache.exists())

    def test_download_cache_is_published_only_after_size_and_hash_match(self):
        archive, entry = self.archive()
        content = archive.read_bytes()
        with mock.patch.object(fetch, "urlopen", return_value=io.BytesIO(content)) as network:
            cached = fetch.acquire_archive(entry, self.cache)
            self.assertEqual(cached.read_bytes(), content)
            self.assertEqual(fetch.acquire_archive(entry, self.cache), cached)
            self.assertEqual(network.call_count, 1)
        for name, content in (("oversized", content + b"x"), ("wrong", b"x" * len(content))):
            with self.subTest(name=name), mock.patch.object(fetch, "urlopen", return_value=io.BytesIO(content)):
                cache = self.root / name
                with self.assertRaises(fetch.RuntimeFetchError):
                    fetch.acquire_archive(entry, cache)
                self.assertEqual(list(cache.iterdir()), [])

    def test_download_error_reports_archive_and_cache_recovery_without_partial_file(self):
        _, entry = self.archive()
        with mock.patch.object(fetch, "urlopen", side_effect=URLError("synthetic unavailable")):
            with self.assertRaisesRegex(fetch.RuntimeFetchError, "download failed.*supplemental license cache"):
                fetch.acquire_archive(entry, self.cache)
        self.assertEqual(list(self.cache.iterdir()), [])

    def test_extraction_allowlist_retains_all_licenses_and_excludes_build_files(self):
        archive, entry = self.archive()
        root = self.extract(archive)
        self.assertFalse((root / "build").exists())
        self.assertFalse((root.parent / "unrelated.txt").exists())
        licenses = fetch.runtime_licenses(root, entry)
        self.assertEqual({path.name for path in licenses},
                         {"LICENSE.cpython.txt", "LICENSE.library.txt", "UNREFERENCED-NOTICE", "PYTHON.json"})

    def test_path_traversal_absolute_paths_and_duplicates_are_rejected(self):
        for index, name in enumerate(("../outside", "/absolute", "C:/escape", "python/install/../escape",
                                     "python\\install\\escape", "python/install/bin/python3")):
            member = tarfile.TarInfo(name)
            member.size = 1
            archive, _ = self.archive(extra=[(member, b"x")], name=f"bad-{index}.tar")
            with self.subTest(name=name), self.assertRaisesRegex(fetch.RuntimeFetchError, "unsafe member|duplicate"):
                fetch.extract_runtime(archive, self.root / f"extract-{index}")
        self.assertFalse((self.root.parent / "outside").exists())

    def test_symlink_and_hardlink_escape_are_rejected_before_extraction(self):
        for index, (kind, link) in enumerate(((tarfile.SYMTYPE, "../../../outside"),
                                            (tarfile.SYMTYPE, "/outside"),
                                            (tarfile.LNKTYPE, "python/licenses/LICENSE.cpython.txt"))):
            member = tarfile.TarInfo("python/install/bin/link")
            member.type, member.linkname = kind, link
            archive, _ = self.archive(extra=[(member, None)], name=f"links-{index}.tar")
            with self.subTest(link=link), self.assertRaisesRegex(fetch.RuntimeFetchError, "unsafe link|escapes"):
                fetch.extract_runtime(archive, self.root / f"links-{index}")

    def test_special_files_and_extraction_budget_are_rejected(self):
        member = tarfile.TarInfo("python/install/device")
        member.type = tarfile.CHRTYPE
        archive, _ = self.archive(extra=[(member, None)])
        with self.assertRaisesRegex(fetch.RuntimeFetchError, "special"):
            fetch.extract_runtime(archive, self.root / "device")
        archive, _ = self.archive(name="budget.tar")
        with mock.patch.object(fetch, "MAX_EXTRACTED_BYTES", 1):
            with self.assertRaisesRegex(fetch.RuntimeFetchError, "512 MiB"):
                fetch.extract_runtime(archive, self.root / "budget")

    def test_missing_referenced_license_fails_before_preparing_or_executing_runtime(self):
        archive, entry = self.archive(omit=["python/licenses/LICENSE.library.txt"])
        with mock.patch.object(fetch, "native_target", return_value="darwin-arm64"), \
             mock.patch.object(fetch, "locked_runtime", return_value=entry), \
             mock.patch.object(fetch.prepare, "prepare_runtime") as prepare:
            with self.assertRaisesRegex(fetch.RuntimeFetchError, "missing licenses.*LICENSE.library.txt"):
                fetch.fetch_runtime(self.output, cache_dir=self.cache, archive=archive)
            prepare.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_version_target_and_unsafe_license_metadata_are_rejected(self):
        for index, change in enumerate(({"python_version": "3.14.1"}, {"target_triple": "other"},
                                        {"license_path": "../outside"})):
            with self.subTest(change=change):
                original = dict(self.metadata)
                self.metadata.update(change)
                archive, entry = self.archive(name=f"metadata-{index}.tar")
                root = fetch.extract_runtime(archive, self.root / f"metadata-{index}")
                with self.assertRaises(fetch.RuntimeFetchError):
                    fetch.runtime_licenses(root, entry)
                self.metadata = original

    def test_locked_supplement_cache_fills_missing_license_and_records_provenance(self):
        archive, entry = self.archive(omit=["python/licenses/LICENSE.library.txt"])
        content = b"Locked matching upstream license\n"
        declaration = {"url": "https://raw.githubusercontent.com/python/cpython-source-deps/example-1.0/LICENSE",
                       "sha256": hashlib.sha256(content).hexdigest(), "size": len(content),
                       "component": "example", "version": "1.0"}
        entry["supplementalLicenses"] = {"licenses/LICENSE.library.txt": declaration}
        self.cache.mkdir()
        (self.cache / "LICENSE.library.txt").write_bytes(content)
        captured = {}

        def prepare(source, output, **kwargs):
            captured.update(source=source.name, **kwargs)
            captured["licenseNames"] = [path.name for path in kwargs["license_files"]]
            self.assertEqual((source.parent / "licenses/LICENSE.library.txt").read_bytes(), content)
            output.mkdir()
            (output / fetch.prepare.PROVENANCE).write_text('{"source": {"url": "synthetic"}}')
            return {"runtime": str(output), "provenanceSha256": "old"}

        with mock.patch.object(fetch, "native_target", return_value="darwin-arm64"), \
             mock.patch.object(fetch, "locked_runtime", return_value=entry), \
             mock.patch.object(fetch.prepare, "prepare_runtime", side_effect=prepare), \
             mock.patch.object(fetch, "urlopen") as network:
            result = fetch.fetch_runtime(self.output, cache_dir=self.cache, archive=archive)
            network.assert_not_called()
        provenance = self.output / fetch.prepare.PROVENANCE
        self.assertEqual(json.loads(provenance.read_bytes())["source"]["supplementalLicenses"], entry["supplementalLicenses"])
        self.assertEqual(result["provenanceSha256"], hashlib.sha256(provenance.read_bytes()).hexdigest())
        self.assertEqual(result["runtime"], str(self.output))
        self.assertTrue(result["upstreamLicenseReferencesVerified"])
        self.assertEqual(captured["source"], "install")
        self.assertIn("PYTHON.json", captured["licenseNames"])
        self.assertEqual(captured["source_archive"], archive)
        self.assertFalse(captured["license_files"][0].exists())

    def test_bad_supplement_cache_and_existing_output_are_preserved(self):
        archive, entry = self.archive()
        self.output.mkdir()
        sentinel = self.output / "keep"
        sentinel.write_text("existing runtime")
        with mock.patch.object(fetch, "acquire_archive") as acquire:
            with self.assertRaisesRegex(fetch.RuntimeFetchError, "already exists"):
                fetch.fetch_runtime(self.output, cache_dir=self.cache, archive=archive)
            acquire.assert_not_called()
        self.assertEqual(sentinel.read_text(), "existing runtime")
        self.cache.mkdir()
        cached = self.cache / "LICENSE.library.txt"
        cached.write_bytes(b"x")
        with mock.patch.object(fetch, "urlopen") as network:
            with self.assertRaises(fetch.RuntimeFetchError):
                fetch.download_verified({"size": 1, "sha256": "0" * 64}, self.cache, cached.name)
            network.assert_not_called()
        self.assertEqual(cached.read_bytes(), b"x")


if __name__ == "__main__":
    unittest.main()
