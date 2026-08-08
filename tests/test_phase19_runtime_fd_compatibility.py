from __future__ import annotations

import datetime as dt
import fcntl
import hashlib
import os
from pathlib import Path
import tempfile
import unittest

from l2i.experiment_contract import (
    ExperimentContractError,
    ExperimentIdentity,
    ExperimentRunDirectory,
    RepositoryProvenance,
    atomic_write_text,
    build_run_manifest,
    normalize_open_path,
)
from scenarios.multidomain_s1 import _load_specification


class RuntimeFdCompatibilityTests(unittest.TestCase):
    def _identity(self) -> ExperimentIdentity:
        return ExperimentIdentity(
            scenario_id="S1",
            execution_id="S1-fd-compatibility-test",
            profile_id="nominal",
            mode="baseline",
            backend_mode="mock",
            repetition=1,
        )

    def _provenance(self) -> RepositoryProvenance:
        return RepositoryProvenance(
            commit="1" * 40,
            branch="develop",
            worktree_clean=True,
            origin_commit="1" * 40,
            describe="fd-compatibility-test",
        )

    def test_ordinary_paths_keep_existing_resolution_semantics(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "target"
            target.mkdir()
            link = root / "link"
            link.symlink_to(target, target_is_directory=True)
            self.assertEqual(normalize_open_path(link), target.resolve())

    @unittest.skipUnless(hasattr(os, "memfd_create"), "Linux memfd_create required")
    def test_sealed_specification_fd_is_loaded_and_manifested(self) -> None:
        repository = Path(__file__).resolve().parents[1]
        content = repository.joinpath("specs/valid/s1_unicast_qos.json").read_bytes()
        descriptor = os.memfd_create(
            "phase19-scenario-spec-test", getattr(os, "MFD_ALLOW_SEALING", 0)
        )
        try:
            os.write(descriptor, content)
            os.lseek(descriptor, 0, os.SEEK_SET)
            seals = (
                fcntl.F_SEAL_SEAL
                | fcntl.F_SEAL_SHRINK
                | fcntl.F_SEAL_GROW
                | fcntl.F_SEAL_WRITE
            )
            fcntl.fcntl(descriptor, fcntl.F_ADD_SEALS, seals)
            capability = Path(f"/proc/self/fd/{descriptor}")

            document, _intent = _load_specification(capability)
            self.assertEqual(document["l2i_version"], "0.1")

            manifest = build_run_manifest(
                identity=self._identity(),
                provenance=self._provenance(),
                specification_path=capability,
                configuration={"probe": "fd-compatibility"},
                started_at=dt.datetime(2026, 1, 2, 3, 4, 5, tzinfo=dt.UTC),
            )
            self.assertEqual(manifest["specification"]["path"], str(capability))
            self.assertEqual(
                manifest["specification"]["sha256"], hashlib.sha256(content).hexdigest()
            )
        finally:
            os.close(descriptor)

    def test_results_directory_fd_remains_authoritative_for_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            backing_root = Path(temporary, "native")
            backing_root.mkdir()
            descriptor = os.open(backing_root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                capability = Path(f"/proc/self/fd/{descriptor}")
                run_directory = ExperimentRunDirectory.create(
                    capability, self._identity()
                )
                artifact = run_directory.artifact("summary.json")
                self.assertEqual(run_directory.root, capability)
                self.assertTrue(str(run_directory.path).startswith(f"{capability}/"))
                self.assertTrue(str(artifact).startswith(f"{capability}/"))
                atomic_write_text(artifact, "fd-capability-preserved\n")
                self.assertEqual(
                    backing_root
                    .joinpath("S1", "S1-fd-compatibility-test", "summary.json")
                    .read_text(encoding="utf-8"),
                    "fd-capability-preserved\n",
                )
            finally:
                os.close(descriptor)

    def test_artifact_traversal_and_absolute_paths_remain_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_directory = ExperimentRunDirectory.create(
                Path(temporary), self._identity()
            )
            for unsafe in ("../escape.json", "nested/../../escape.json", "/tmp/escape"):
                with self.subTest(unsafe=unsafe):
                    with self.assertRaises(ExperimentContractError):
                        run_directory.artifact(unsafe)

            external = Path(temporary, "external")
            external.mkdir()
            run_directory.path.joinpath("redirect").symlink_to(
                external, target_is_directory=True
            )
            with self.assertRaises(ExperimentContractError):
                run_directory.artifact("redirect/escape.json")


if __name__ == "__main__":
    unittest.main()
