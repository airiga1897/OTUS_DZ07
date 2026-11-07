"""Real local Git clones and release archives, synthetic HTTPS and packages."""

import hashlib
import io
import json
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import release_artifacts as release
from artifact_content import digest
from artifact_types import ReleaseSettings
from remote.fetch_artifacts import download, extract

ROOT = Path(__file__).resolve().parents[1]


class Response(io.BytesIO):
    status = 200

    def __init__(self, data: bytes, url: str) -> None:
        super().__init__(data)
        self.url = url

    def geturl(self) -> str:
        return self.url

    def read1(self, size: int | None = -1, /) -> bytes:
        return self.read(size)


class ReleaseArtifactsTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory(dir=ROOT / ".tools")
        self.addCleanup(temp.cleanup)
        self.folder = Path(temp.name)
        self.root = self.folder / "repo"
        self.root.mkdir()
        (self.root / "config").mkdir()
        (self.root / "app").mkdir()
        (self.root / ".gitignore").write_text(".tools/\n.local/\n")
        (self.root / "app/public.py").write_text("value = 42\n")
        (self.root / "config/services.json").write_text(
            json.dumps({"artifact_delivery": "release", "elastic_version": "8.19.0"})
        )
        self.lock = {}
        cache = self.root / ".tools/elastic-packages"
        cache.mkdir(parents=True)
        for component in ("elasticsearch", "filebeat", "kibana"):
            name = component + "-8.19.0-amd64.deb"
            body = (component + "-synthetic").encode()
            (cache / name).write_bytes(body)
            self.lock[name] = hashlib.sha512(body).hexdigest()
        (self.root / "config/elastic-artifacts.json").write_text(json.dumps(self.lock))
        release.git(self.root, "init", "-q")
        self.commit()

    def commit(self) -> None:
        release.git(self.root, "add", ".")
        release.git(
            self.root,
            "-c",
            "user.name=Local Test",
            "-c",
            "user.email=local@example.invalid",
            "commit",
            "-qm",
            "synthetic source",
        )

    def build(self) -> Path:
        return release.build(self.root, "v0.0.1-test", "example/repository")

    def configure(self, output: Path, root: Path | None = None) -> ReleaseSettings:
        root = root or self.root
        settings = release.checked_settings(json.loads((output / "release-settings.json").read_text()))
        (root / "config/artifacts.json").write_text(json.dumps({"github_release": settings}))
        return settings

    def test_clean_clone_downloads_source_and_all_packages_without_s3(self) -> None:
        output = self.build()
        clone = self.folder / "clean-clone"
        subprocess.run(["git", "clone", "-q", "--local", str(self.root), str(clone)], check=True, capture_output=True)
        self.configure(output, clone)
        opener = Mock()
        opener.open.side_effect = lambda url, **kw: Response((output / url.rsplit("/", 1)[1]).read_bytes(), url)

        def fetch(item: object, destination: Path) -> None:
            download(item, destination, opener=opener)

        with patch.object(release, "download", side_effect=fetch), patch("s3_artifacts.client") as s3:
            manifest = release.prepare(clone)
        s3.assert_not_called()
        self.assertEqual(set(manifest["packages"]), set(self.lock))
        work = self.folder / "vm"
        work.mkdir()
        for name, item in [("source.tar.gz", manifest["source"]), *manifest["packages"].items()]:
            download(item, work / name, opener=opener)
        extract(work / "source.tar.gz", work / "unpacked")
        self.assertEqual((work / "unpacked/app/public.py").read_text(), "value = 42\n")
        self.assertFalse((clone / ".local/artifact-credentials.json").exists())
        sums = {line.split("  ")[1]: line.split("  ")[0] for line in (output / "SHA512SUMS").read_text().splitlines()}
        self.assertEqual(set(sums), {"source.tar.gz", "manifest.json", "release-settings.json", *self.lock})
        self.assertTrue(all(digest(output / n) == sha for n, sha in sums.items()))

    def test_build_refuses_dirty_tree_bad_packages_and_existing_version(self) -> None:
        (self.root / "uncommitted.txt").write_text("draft")
        with self.assertRaisesRegex(ValueError, "clean"):
            self.build()
        (self.root / "uncommitted.txt").unlink()
        name = next(iter(self.lock))
        cached = self.root / ".tools/elastic-packages" / name
        body = cached.read_bytes()
        cached.write_bytes(b"corrupt")
        with self.assertRaisesRegex(ValueError, "official SHA512"):
            self.build()
        cached.write_bytes(body)
        self.build()
        with self.assertRaisesRegex(ValueError, "overwrite"):
            self.build()

    def test_unsafe_tracked_sources_never_become_release_assets(self) -> None:
        for name, kind in ((".env", "file"), ("secret.key", "file"), ("escape", "symlink"), ("../outside", "file")):
            stream = io.BytesIO()
            with tarfile.open(fileobj=stream, mode="w") as archive:
                info = tarfile.TarInfo(name)
                if kind == "symlink":
                    info.type = tarfile.SYMTYPE
                    info.linkname = "/outside"
                archive.addfile(info)
            with self.subTest(name=name), self.assertRaises(ValueError):
                release.checked_archive(stream.getvalue())
        release.checked_archive(release.git(self.root, "archive", "--format=tar", "HEAD"))

    def test_release_names_cannot_select_latest_or_escape_repository(self) -> None:
        for repository, tag in (
            ("example/repository", "latest"),
            ("../repo", "v1.0.0"),
            ("example/repo?token=secret", "v1.0.0"),
            ("example/repo", None),
        ):
            with self.subTest(repository=repository, tag=tag), self.assertRaises(ValueError):
                release.base(repository, tag)

    def test_manifest_identity_packages_hash_and_urls_are_pinned(self) -> None:
        output = self.build()
        settings = self.configure(output)
        original = json.loads((output / "manifest.json").read_text())
        for mutation in ("commit", "tag", "schema", "missing-package", "url", "sha512", "size", "source"):
            manifest = json.loads(json.dumps(original))
            name = next(iter(self.lock))
            if mutation == "commit":
                manifest["commit"] = "a" * 40
            elif mutation == "tag":
                manifest["tag"] = "v9.9.9"
            elif mutation == "schema":
                manifest["schema"] = 2
            elif mutation == "missing-package":
                manifest["packages"].pop(name)
            elif mutation == "source":
                manifest["source"]["url"] = "https://other.invalid/code"
            elif mutation == "url":
                manifest["packages"][name]["url"] += "?credential=secret"
            elif mutation == "sha512":
                manifest["packages"][name]["sha512"] = "a" * 128
            else:
                manifest["packages"][name]["size"] = False
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                release.validate(manifest, settings, self.lock)

    def test_prepare_rejects_missing_settings_bad_length_or_corrupt_https_manifest(self) -> None:
        (self.root / "config/artifacts.json").write_text("{}")
        with self.assertRaisesRegex(ValueError, "Configure"):
            release.prepare(self.root)
        (self.root / "config/artifacts.json").unlink()
        output = self.build()
        settings = self.configure(output)
        settings["manifest_size"] = 0
        (self.root / "config/artifacts.json").write_text(json.dumps({"github_release": settings}))
        with self.assertRaisesRegex(ValueError, "size"):
            release.prepare(self.root)
        settings = self.configure(output)
        local = self.root / ".local"
        local.mkdir(exist_ok=True)
        (local / "artifact-delivery.json").write_text("stale")
        opener = Mock()
        opener.open.return_value = Response(b"corrupt", "https://github.com/fake")
        with (
            patch.object(
                release, "download", side_effect=lambda item, destination: download(item, destination, opener=opener)
            ),
            self.assertRaises(ValueError),
        ):
            release.prepare(self.root)
        self.assertFalse((local / "artifact-delivery.json").exists())
        self.assertEqual(list(local.iterdir()), [])

    def test_package_mutation_during_copy_never_exposes_partial_build(self) -> None:
        real = shutil.copyfile

        def corrupt(source: Path, target: Path) -> None:
            real(source, target)
            Path(target).write_bytes(b"corrupt")

        with (
            patch("shutil.copyfile", side_effect=corrupt),
            self.assertRaisesRegex(ValueError, "changed"),
        ):
            self.build()
        self.assertEqual(list((self.root / ".tools/releases").iterdir()), [])

    def test_repository_change_during_assembly_never_exposes_partial_build(self) -> None:
        real = release.git
        calls = 0

        def changed(root: Path, *args: str) -> bytes:
            nonlocal calls
            if args == ("status", "--porcelain"):
                calls += 1
                if calls == 2:
                    return b" M source.py"
            return real(root, *args)

        with patch.object(release, "git", side_effect=changed), self.assertRaisesRegex(ValueError, "changed"):
            self.build()
        self.assertEqual(list((self.root / ".tools/releases").iterdir()), [])

    def test_cli_build_and_prepare_do_not_publish_or_create_cloud_resources(self) -> None:
        with (
            patch.object(release, "ROOT", self.root),
            patch.object(release, "build", return_value=self.root / "assets") as build,
            patch(
                "sys.argv", ["release_artifacts.py", "build", "--tag", "v1.0.0", "--repository", "example/repository"]
            ),
        ):
            release.main()
        build.assert_called_once_with(self.root, "v1.0.0", "example/repository")
        with (
            patch.object(release, "ROOT", self.root),
            patch.object(release, "prepare") as prepare,
            patch("sys.argv", ["release_artifacts.py", "prepare"]),
        ):
            release.main()
        prepare.assert_called_once_with(self.root)

    def test_normal_deployment_preparation_dispatches_release_without_s3(self) -> None:
        import cache_packages
        import delivery
        import staged_runtime

        api = SimpleNamespace(ROOT=self.root, prepare_identity=Mock())
        with (
            patch.object(release, "prepare", return_value={"source": "synthetic"}) as prepare,
            patch("s3_artifacts.prepare") as s3,
        ):
            self.assertTrue(delivery.internet(self.root))
            staged_runtime.prepare_files(api)
            with patch.object(cache_packages, "ROOT", self.root):
                cache_packages.main()
        self.assertEqual(prepare.call_count, 2)
        api.prepare_identity.assert_called_once()
        s3.assert_not_called()

    def test_invalid_commit_and_source_manifest_digest_are_rejected(self) -> None:
        output = self.build()
        settings = self.configure(output)
        manifest = json.loads((output / "manifest.json").read_text())
        settings["commit"] = manifest["commit"] = "not-a-commit"
        with self.assertRaisesRegex(ValueError, "identity"):
            release.validate(manifest, settings, self.lock)
        settings = self.configure(output)
        manifest["commit"] = settings["commit"]
        manifest["source"]["sha512"] = "not-a-digest"
        with self.assertRaisesRegex(ValueError, "SHA512"):
            release.validate(manifest, settings, self.lock)
