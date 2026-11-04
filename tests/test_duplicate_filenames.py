"""Offline album-download tests using the real CLI and real file writes."""

from contextlib import ExitStack
import importlib.util
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch


CLI_PATH = Path(__file__).resolve().parents[1] / "smugmug_downloader" / "cli.py"


class DuplicateFilenameTests(unittest.TestCase):
    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        self.root = Path(stack.enter_context(TemporaryDirectory()))
        old_cwd = Path.cwd()
        stack.callback(os.chdir, old_cwd)
        os.chdir(self.root)
        stack.enter_context(patch.dict(os.environ, {
            "SMUGMUG_API_KEY": "test-key",
            "SMUGMUG_API_SECRET": "test-secret",
        }))
        stack.enter_context(patch.object(Path, "home", return_value=self.root))
        stack.enter_context(patch(
            "requests.sessions.Session.send",
            side_effect=AssertionError("Network transport forbidden in unit tests"),
        ))
        stack.enter_context(patch(
            "requests_oauthlib.OAuth1Session.__init__",
            side_effect=AssertionError("OAuth construction forbidden in unit tests"),
        ))
        spec = importlib.util.spec_from_file_location("_smugmug_filename_test", CLI_PATH)
        cli = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cli)
        self.cli = cli
        stack.enter_context(patch.object(
            cli, "authenticate", side_effect=AssertionError("Authentication forbidden"),
        ))
        stack.enter_context(patch.object(
            cli, "load_tokens", side_effect=AssertionError("Token reads forbidden"),
        ))
        self.images = []
        self.payloads = {}
        self.session = Mock(spec=["get"])
        self.session.get.side_effect = self.response
        self.downloader = cli.SmugMugDownloader(self.session, "downloads")
        self.downloader.get_album_images = Mock(side_effect=lambda _: self.images)
        self.downloader.get_image_download_url = Mock(
            side_effect=lambda image: self.url(image["ImageKey"])
        )
        self.progress = Mock(spec=["add_task", "update", "remove_task"])
        self.progress.add_task.return_value = 1
        self.album = self.root / "downloads" / "album"
        self.cache = self.root / "cache.txt"

    @staticmethod
    def url(key):
        return f"https://example.invalid/{key}"

    def add_image(self, key, filename, payload):
        self.images.append({"ImageKey": key, "FileName": filename})
        self.payloads[self.url(key)] = payload

    def response(self, url, stream):
        self.assertTrue(stream)
        response = Mock(spec=["raise_for_status", "iter_content"])
        response.iter_content.return_value = [self.payloads[url]]
        return response

    def download(self):
        self.downloader.download_album(
            "/api/v2/album/test", self.album, "Test album", self.progress,
        )

    def files(self):
        return {path.name: path.read_bytes() for path in self.album.iterdir()
                if path.name != ".smugmug-filenames.json"}

    def test_distinct_images_with_same_filename_are_both_saved(self):
        self.add_image("A", "photo.jpg", b"first photo")
        self.add_image("B", "photo.jpg", b"second photo")
        self.download()
        self.assertEqual(self.files(), {
            "photo-A.jpg": b"first photo", "photo-B.jpg": b"second photo",
        })

    def test_reordered_repeat_keeps_destinations_and_skips_downloads(self):
        self.add_image("A", "photo.jpg", b"first photo")
        self.add_image("B", "photo.jpg", b"second photo")
        self.download()
        first_files = self.files()
        self.images.reverse()
        self.session.get.reset_mock()
        self.download()
        self.assertEqual(self.files(), first_files)
        self.session.get.assert_not_called()

    def test_legacy_cache_does_not_hide_missing_copies(self):
        self.add_image("A", "photo.jpg", b"first photo")
        self.add_image("B", "photo.jpg", b"second photo")
        self.album.mkdir()
        (self.album / "photo.jpg").write_bytes(b"legacy file")
        self.cache.write_text("A\nB\n")
        self.download()
        self.assertEqual(self.files(), {
            "photo.jpg": b"legacy file",
            "photo-A.jpg": b"first photo", "photo-B.jpg": b"second photo",
        })

    def test_generated_name_does_not_replace_another_original_name(self):
        self.add_image("B", "photo.jpg", b"second photo")
        self.add_image("C", "photo-A.jpg", b"third photo")
        self.add_image("A", "photo.jpg", b"first photo")
        self.download()
        self.assertEqual(self.files(), {
            "photo-A-2.jpg": b"first photo", "photo-B.jpg": b"second photo",
            "photo-A.jpg": b"third photo",
        })

    def test_case_only_collisions_get_distinct_names(self):
        self.add_image("A", "Photo.jpg", b"first photo")
        self.add_image("B", "photo.JPG", b"second photo")
        self.download()
        self.assertEqual(set(self.files().values()), {b"first photo", b"second photo"})
        self.assertEqual(len({name.casefold() for name in self.files()}), 2)

    def test_unique_names_and_existing_files_keep_their_behavior(self):
        self.add_image("A", "existing.jpg", b"remote contents")
        self.add_image("B", "new.jpg", b"new contents")
        self.album.mkdir()
        (self.album / "existing.jpg").write_bytes(b"local contents")
        self.download()
        self.assertEqual(self.files(), {
            "existing.jpg": b"local contents", "new.jpg": b"new contents",
        })
        self.session.get.assert_called_once_with(self.url("B"), stream=True)

    def test_duplicate_metadata_for_one_image_keeps_original_name(self):
        self.add_image("A", "photo.jpg", b"one photo")
        self.images.append(dict(self.images[0]))
        self.download()
        self.assertEqual(self.files(), {"photo.jpg": b"one photo"})
        self.session.get.assert_called_once_with(self.url("A"), stream=True)

    def test_key_characters_cannot_create_subdirectories(self):
        self.add_image("A/B", "photo.jpg", b"first photo")
        self.add_image("AB", "photo.jpg", b"second photo")
        self.download()
        self.assertEqual(set(self.files().values()), {b"first photo", b"second photo"})
        self.assertEqual(len(self.files()), 2)
        first_files = self.files()
        self.images.reverse()
        self.cache.write_text("")
        self.album = self.root / "downloads" / "reversed"
        self.download()
        self.assertEqual(self.files(), first_files)

    def test_missing_disambiguated_copy_is_downloaded_again(self):
        self.add_image("A", "photo.jpg", b"first photo")
        self.add_image("B", "photo.jpg", b"second photo")
        self.download()
        (self.album / "photo-A.jpg").unlink()
        self.session.get.reset_mock()
        self.download()
        self.assertEqual(self.files(), {
            "photo-A.jpg": b"first photo", "photo-B.jpg": b"second photo",
        })
        self.session.get.assert_called_once_with(self.url("A"), stream=True)

    def test_new_original_name_cannot_take_a_previous_generated_destination(self):
        self.add_image("A", "photo.jpg", b"first photo")
        self.add_image("B", "photo.jpg", b"second photo")
        self.download()
        first_files = self.files()
        self.add_image("C", "photo-A.jpg", b"third photo")
        self.images.reverse()
        self.download()
        self.assertEqual(self.files(), {
            **first_files, "photo-A-C.jpg": b"third photo",
        })
        self.session.get.reset_mock()
        self.download()
        self.session.get.assert_not_called()

    def test_removed_image_keeps_ownership_when_another_image_reuses_its_name(self):
        self.add_image("A", "photo.jpg", b"first photo")
        self.add_image("B", "photo.jpg", b"second photo")
        self.download()
        removed = self.images.pop(0)
        self.add_image("C", "photo-A.jpg", b"third photo")
        self.download()
        self.assertEqual(self.files(), {
            "photo-A.jpg": b"first photo", "photo-B.jpg": b"second photo",
            "photo-A-C.jpg": b"third photo",
        })
        self.images.append(removed)
        self.images.reverse()
        self.session.get.reset_mock()
        self.download()
        self.session.get.assert_not_called()

    def test_existing_generated_path_is_preserved_and_not_assumed_to_be_ours(self):
        self.add_image("A", "photo.jpg", b"first photo")
        self.add_image("B", "photo.jpg", b"second photo")
        self.album.mkdir()
        (self.album / "photo-A.jpg").write_bytes(b"unrelated local file")
        self.download()
        self.assertEqual(self.files(), {
            "photo-A.jpg": b"unrelated local file",
            "photo-A-2.jpg": b"first photo", "photo-B.jpg": b"second photo",
        })

    def test_missing_map_preserves_old_files_and_recovers_both_images(self):
        self.add_image("A", "photo.jpg", b"first photo")
        self.add_image("B", "photo.jpg", b"second photo")
        self.download()
        first_files = self.files()
        (self.album / ".smugmug-filenames.json").unlink()
        self.download()
        self.assertEqual(self.files(), {
            **first_files, "photo-A-2.jpg": b"first photo",
            "photo-B-2.jpg": b"second photo",
        })

    def test_map_name_is_reserved_even_for_an_original_filename(self):
        self.add_image("A", ".smugmug-filenames.json", b"photo contents")
        self.download()
        self.assertEqual(self.files(), {".smugmug-filenames-A.json": b"photo contents"})
        self.session.get.reset_mock()
        self.download()
        self.session.get.assert_not_called()

    def test_invalid_map_is_not_overwritten_and_downloads_do_not_start(self):
        self.add_image("A", "photo.jpg", b"first photo")
        self.add_image("B", "photo.jpg", b"second photo")
        self.album.mkdir()
        metadata = self.album / ".smugmug-filenames.json"
        invalid = ["unrelated local file", "{}"]
        for filenames in (
            {"A": "../outside.jpg"}, {"A": "/outside.jpg"},
            {"A": "sub\\outside.jpg"}, {"A": ".smugmug-filenames.json"},
            {"A": "photo.jpg", "B": "PHOTO.jpg"}, {"A": 17},
        ):
            invalid.append(json.dumps({"version": 1, "filenames": filenames}))
        for contents in invalid:
            with self.subTest(contents=contents):
                metadata.write_text(contents)
                with self.assertRaisesRegex(ValueError, "Invalid filename map"):
                    self.download()
                self.assertEqual(metadata.read_text(), contents)
                self.assertEqual(self.cache.read_text(), "")
                self.session.get.assert_not_called()

    def test_map_replace_failure_preserves_prior_mapping_and_cache(self):
        self.add_image("A", "photo.jpg", b"first photo")
        self.add_image("B", "photo.jpg", b"second photo")
        self.download()
        metadata = self.album / ".smugmug-filenames.json"
        old_map, old_cache, old_files = metadata.read_bytes(), self.cache.read_bytes(), self.files()
        self.add_image("C", "photo-A.jpg", b"third photo")
        self.session.get.reset_mock()
        with patch.object(self.cli, "replace", side_effect=OSError("map write failed")):
            with self.assertRaisesRegex(OSError, "map write failed"):
                self.download()
        self.assertEqual(metadata.read_bytes(), old_map)
        self.assertEqual(self.cache.read_bytes(), old_cache)
        self.assertEqual(self.files(), old_files)
        self.session.get.assert_not_called()
        self.download()
        self.assertIn(b"third photo", self.files().values())

    def test_map_symlink_does_not_write_to_its_target(self):
        self.add_image("A", "photo.jpg", b"first photo")
        self.add_image("B", "photo.jpg", b"second photo")
        self.album.mkdir()
        outside = self.root / "outside.json"
        contents = '{"version": 1, "filenames": {}}'
        outside.write_text(contents)
        (self.album / ".smugmug-filenames.json").symlink_to(outside)
        with self.assertRaisesRegex(ValueError, "Invalid filename map"):
            self.download()
        self.assertEqual(outside.read_text(), contents)
        self.assertEqual(self.cache.read_text(), "")
        self.session.get.assert_not_called()

    def test_renamed_source_still_recovers_a_missing_owned_copy(self):
        self.add_image("A", "photo.jpg", b"first photo")
        self.add_image("B", "photo.jpg", b"second photo")
        self.download()
        self.images[0]["FileName"] = "photo-A.jpg"
        (self.album / "photo-A.jpg").unlink()
        self.session.get.reset_mock()
        self.download()
        self.assertEqual(self.files(), {
            "photo-A.jpg": b"first photo", "photo-B.jpg": b"second photo",
        })
        self.session.get.assert_called_once_with(self.url("A"), stream=True)


if __name__ == "__main__":
    unittest.main()
