"""
Unit tests for the top level `s3_upload.s3_upload` module.

Currently covers the `sync_single_run` retry-loop orchestration. The
individual building blocks it calls (`list_remote_objects`,
`compute_missing_files`, `multi_core_upload`, the access checks and the
state-log writer) are unit tested in their own modules, so here they are
patched at the module level where `sync_single_run` looks them up. This
keeps these tests focused on the orchestration logic: pass looping, the
already-synced / dry-run / persistent-failure / no-progress branches, and
credential re-resolution per pass.
"""

from argparse import Namespace
import os
import sys
import unittest
from unittest.mock import patch

# `s3_upload/s3_upload.py` imports its dependencies as `from utils.io import`
# etc., so the inner `s3_upload/` directory has to be importable as well as
# the repo root. Append (not insert) the inner directory so the `s3_upload`
# package itself still resolves from the repo root first (mirrors the path
# setup used by the e2e tests).
REPO_ROOT = os.path.abspath(
    os.path.join(os.path.realpath(__file__), "../../../")
)
sys.path.append(REPO_ROOT)
sys.path.append(os.path.join(REPO_ROOT, "s3_upload"))

from s3_upload.s3_upload import parse_args, sync_single_run


def _build_sync_args(**overrides):
    """
    Build a lightweight args object mirroring the parsed `sync` subcommand
    namespace, with sensible defaults that individual tests can override.
    """
    defaults = dict(
        local_path="/parent/run1",
        bucket="test_bucket",
        remote_path="/",
        aws_profile="my-profile",
        cores=2,
        threads=8,
        max_passes=3,
        backoff_seconds=60,
        log_dir="/var/log/s3_upload",
        dry_run=False,
    )
    defaults.update(overrides)
    return Namespace(**defaults)


@patch("s3_upload.s3_upload.time.sleep")
@patch("s3_upload.s3_upload.write_sync_state_to_log")
@patch("s3_upload.s3_upload.split_file_list_by_cores")
@patch("s3_upload.s3_upload.multi_core_upload")
@patch("s3_upload.s3_upload.compute_missing_files")
@patch("s3_upload.s3_upload.list_remote_objects")
@patch("s3_upload.s3_upload.get_sequencing_file_list")
@patch("s3_upload.s3_upload.Path")
@patch("s3_upload.s3_upload.check_buckets_exist")
@patch("s3_upload.s3_upload.check_aws_access")
class TestSyncSingleRun(unittest.TestCase):
    """
    Orchestration tests for `sync_single_run`. Decorators are applied
    bottom-up, so each test method receives mocks in this order:

        mock_check_access, mock_check_buckets, mock_path,
        mock_get_files, mock_list_remote, mock_compute_missing,
        mock_multi_core, mock_split, mock_write_log, mock_sleep
    """

    def _configure_common(
        self, mock_path, mock_get_files, run_dir_exists=True
    ):
        """
        Set up the filesystem-facing mocks shared by every scenario: the
        run directory exists (so the run-dir guard passes), the run id and
        parent path derive from the local path, and a fixed local file
        list is discovered once.
        """
        mock_path_instance = mock_path.return_value
        mock_path_instance.exists.return_value = run_dir_exists
        mock_path_instance.name = "run1"
        mock_path_instance.parent = "/parent"

        mock_get_files.return_value = [
            "/parent/run1/file_1.txt",
            "/parent/run1/file_2.txt",
        ]

    def test_already_synced_single_pass_no_upload_zero_exit(
        self,
        mock_check_access,
        mock_check_buckets,
        mock_path,
        mock_get_files,
        mock_list_remote,
        mock_compute_missing,
        mock_multi_core,
        mock_split,
        mock_write_log,
        mock_sleep,
    ):
        """
        Requirement 5.2: when the run is already fully synced the first
        pass finds no missing files, so sync returns normally (zero exit)
        without ever calling the upload engine.
        """
        self._configure_common(mock_path, mock_get_files)
        mock_list_remote.return_value = {"run1/file_1.txt": 1}
        mock_compute_missing.return_value = []

        # returns normally, i.e. does not raise SystemExit
        result = sync_single_run(_build_sync_args())

        with self.subTest("returns without raising"):
            self.assertIsNone(result)

        with self.subTest("single reconciliation pass only"):
            self.assertEqual(mock_list_remote.call_count, 1)

        with self.subTest("upload engine never invoked"):
            mock_multi_core.assert_not_called()

        with self.subTest("no backoff performed"):
            mock_sleep.assert_not_called()

    def test_one_gap_uploaded_then_second_pass_empty_zero_exit(
        self,
        mock_check_access,
        mock_check_buckets,
        mock_path,
        mock_get_files,
        mock_list_remote,
        mock_compute_missing,
        mock_multi_core,
        mock_split,
        mock_write_log,
        mock_sleep,
    ):
        """
        Requirements 8.2 / 8.4: a single missing file is uploaded on the
        first pass; the second pass re-lists, finds nothing missing, and
        sync returns with a zero exit.
        """
        self._configure_common(mock_path, mock_get_files)

        # remote listing differs per pass (grows after the upload), the
        # missing set is non-empty on pass 1 then empty on pass 2
        mock_list_remote.side_effect = [
            {"run1/file_1.txt": 1},
            {"run1/file_1.txt": 1, "run1/file_2.txt": 2},
        ]
        mock_compute_missing.side_effect = [
            ["/parent/run1/file_2.txt"],
            [],
        ]
        mock_split.return_value = [["/parent/run1/file_2.txt"]]
        # progress made: one file uploaded, none failed
        mock_multi_core.return_value = (
            {"/parent/run1/file_2.txt": "etag"},
            [],
        )

        result = sync_single_run(_build_sync_args())

        with self.subTest("returns without raising"):
            self.assertIsNone(result)

        with self.subTest("two reconciliation passes run"):
            self.assertEqual(mock_list_remote.call_count, 2)

        with self.subTest("upload engine invoked once (only pass 1)"):
            self.assertEqual(mock_multi_core.call_count, 1)

        with self.subTest("no backoff since progress was made"):
            mock_sleep.assert_not_called()

    def test_dry_run_reports_and_returns_without_upload_or_loop(
        self,
        mock_check_access,
        mock_check_buckets,
        mock_path,
        mock_get_files,
        mock_list_remote,
        mock_compute_missing,
        mock_multi_core,
        mock_split,
        mock_write_log,
        mock_sleep,
    ):
        """
        Requirement 1.8: with --dry_run the missing set is reported on the
        first pass and sync returns without uploading and without entering
        further passes.
        """
        self._configure_common(mock_path, mock_get_files)
        mock_list_remote.return_value = {}
        mock_compute_missing.return_value = [
            "/parent/run1/file_1.txt",
            "/parent/run1/file_2.txt",
        ]

        result = sync_single_run(_build_sync_args(dry_run=True))

        with self.subTest("returns without raising"):
            self.assertIsNone(result)

        with self.subTest("single pass only (no retry loop)"):
            self.assertEqual(mock_list_remote.call_count, 1)

        with self.subTest("no upload performed"):
            mock_multi_core.assert_not_called()

        with self.subTest("no state log written on dry run"):
            mock_write_log.assert_not_called()

    def test_persistent_failure_loops_to_max_passes_then_non_zero_exit(
        self,
        mock_check_access,
        mock_check_buckets,
        mock_path,
        mock_get_files,
        mock_list_remote,
        mock_compute_missing,
        mock_multi_core,
        mock_split,
        mock_write_log,
        mock_sleep,
    ):
        """
        Requirement 8.6: when a gap never closes, sync loops up to
        max_passes and then exits with a non-zero status reporting the
        files still missing.
        """
        args = _build_sync_args(max_passes=3)
        self._configure_common(mock_path, mock_get_files)

        # every pass sees the same gap and never uploads it
        mock_list_remote.return_value = {}
        mock_compute_missing.return_value = ["/parent/run1/file_1.txt"]
        mock_split.return_value = [["/parent/run1/file_1.txt"]]
        # no progress: nothing uploaded, the file keeps failing
        mock_multi_core.return_value = ({}, ["/parent/run1/file_1.txt"])

        with self.subTest("SystemExit raised with non-zero code"):
            with self.assertRaises(SystemExit) as ctx:
                sync_single_run(args)
            self.assertEqual(ctx.exception.code, 1)

        with self.subTest("looped exactly max_passes times"):
            self.assertEqual(mock_list_remote.call_count, args.max_passes)
            self.assertEqual(mock_multi_core.call_count, args.max_passes)

    def test_no_progress_pass_triggers_backoff_sleep(
        self,
        mock_check_access,
        mock_check_buckets,
        mock_path,
        mock_get_files,
        mock_list_remote,
        mock_compute_missing,
        mock_multi_core,
        mock_split,
        mock_write_log,
        mock_sleep,
    ):
        """
        Requirement 8.7: a pass that makes no progress (upload engine
        returns an empty uploaded mapping) waits the configured backoff
        interval before the next pass.
        """
        args = _build_sync_args(max_passes=2, backoff_seconds=42)
        self._configure_common(mock_path, mock_get_files)

        mock_list_remote.return_value = {}
        mock_compute_missing.return_value = ["/parent/run1/file_1.txt"]
        mock_split.return_value = [["/parent/run1/file_1.txt"]]
        # no progress on every pass -> backoff each pass
        mock_multi_core.return_value = ({}, ["/parent/run1/file_1.txt"])

        with self.assertRaises(SystemExit):
            sync_single_run(args)

        with self.subTest("backoff invoked on no-progress pass"):
            mock_sleep.assert_called_with(args.backoff_seconds)

        with self.subTest("backoff invoked once per no-progress pass"):
            self.assertEqual(mock_sleep.call_count, args.max_passes)

    def test_multi_core_upload_called_with_aws_profile_each_pass(
        self,
        mock_check_access,
        mock_check_buckets,
        mock_path,
        mock_get_files,
        mock_list_remote,
        mock_compute_missing,
        mock_multi_core,
        mock_split,
        mock_write_log,
        mock_sleep,
    ):
        """
        Requirement 8.3: credentials are re-resolved every pass by calling
        the upload engine once per pass with the configured aws_profile,
        so a sync exceeding the credential lifetime can complete across
        passes. Assert every multi_core_upload call includes aws_profile.
        """
        args = _build_sync_args(aws_profile="creds-profile", max_passes=2)
        self._configure_common(mock_path, mock_get_files)

        # two passes both with a gap that fails to upload -> two upload
        # calls -> two credential re-resolutions
        mock_list_remote.return_value = {}
        mock_compute_missing.return_value = ["/parent/run1/file_1.txt"]
        mock_split.return_value = [["/parent/run1/file_1.txt"]]
        mock_multi_core.return_value = ({}, ["/parent/run1/file_1.txt"])

        with self.assertRaises(SystemExit):
            sync_single_run(args)

        with self.subTest("upload engine called once per pass"):
            self.assertEqual(mock_multi_core.call_count, args.max_passes)

        with self.subTest("every call passes the configured aws_profile"):
            for upload_call in mock_multi_core.call_args_list:
                # index [1] is the kwargs dict (Python 3.6 compatible,
                # matches the call_args style used elsewhere in the repo)
                self.assertEqual(
                    upload_call[1]["aws_profile"], "creds-profile"
                )

    def test_run_directory_missing_exits_non_zero_before_listing(
        self,
        mock_check_access,
        mock_check_buckets,
        mock_path,
        mock_get_files,
        mock_list_remote,
        mock_compute_missing,
        mock_multi_core,
        mock_split,
        mock_write_log,
        mock_sleep,
    ):
        """
        Requirement 2.3: if the run directory does not exist sync exits
        non-zero without listing remote objects or uploading.
        """
        self._configure_common(
            mock_path, mock_get_files, run_dir_exists=False
        )

        with self.subTest("SystemExit raised with non-zero code"):
            with self.assertRaises(SystemExit) as ctx:
                sync_single_run(_build_sync_args())
            self.assertEqual(ctx.exception.code, 1)

        with self.subTest("no remote listing attempted"):
            mock_list_remote.assert_not_called()

        with self.subTest("no upload attempted"):
            mock_multi_core.assert_not_called()


class TestParseArgsSubcommandDispatch(unittest.TestCase):
    """
    Regression tests for the argparse subcommand dispatch.

    Requirements 7.1 / 10.1: adding the new `sync` subcommand must not
    disturb how the existing `upload`, `monitor`, and `live_cbcl`
    subcommands parse. Each existing subcommand must still resolve to the
    correct `args.mode` with its expected arguments, and `sync` must parse
    to its own mode with its new retry-control arguments.

    `parse_args` reads from `sys.argv`, so each test patches it with a
    representative invocation for the subcommand under test.
    """

    def _parse(self, argv):
        """Run parse_args with sys.argv patched to the given argv list"""
        with patch.object(sys, "argv", ["s3_upload"] + argv):
            return parse_args()

    def test_upload_subcommand_still_parses_to_upload_mode(self):
        """`upload` unaffected by the new sync subcommand (7.1)"""
        args = self._parse(
            [
                "upload",
                "--local_path",
                "/data/run1",
                "--bucket",
                "my-bucket",
                "--remote_path",
                "/seq",
                "--cores",
                "4",
                "--threads",
                "6",
            ]
        )

        with self.subTest("mode"):
            self.assertEqual(args.mode, "upload")
        with self.subTest("local_path"):
            self.assertEqual(args.local_path, "/data/run1")
        with self.subTest("bucket"):
            self.assertEqual(args.bucket, "my-bucket")
        with self.subTest("remote_path"):
            self.assertEqual(args.remote_path, "/seq")
        with self.subTest("cores"):
            self.assertEqual(args.cores, 4)
        with self.subTest("threads"):
            self.assertEqual(args.threads, 6)

    def test_monitor_subcommand_still_parses_to_monitor_mode(self):
        """`monitor` unaffected by the new sync subcommand (7.1)"""
        args = self._parse(
            ["monitor", "--config", "/etc/s3_upload/config.json", "--dry_run"]
        )

        with self.subTest("mode"):
            self.assertEqual(args.mode, "monitor")
        with self.subTest("config"):
            self.assertEqual(args.config, "/etc/s3_upload/config.json")
        with self.subTest("dry_run"):
            self.assertTrue(args.dry_run)

    def test_live_cbcl_subcommand_still_parses_to_live_cbcl_mode(self):
        """`live_cbcl` unaffected by the new sync subcommand (7.1)"""
        args = self._parse(
            [
                "live_cbcl",
                "--config",
                "/etc/s3_upload/config.json",
                "--grace_seconds",
                "300",
            ]
        )

        with self.subTest("mode"):
            self.assertEqual(args.mode, "live_cbcl")
        with self.subTest("config"):
            self.assertEqual(args.config, "/etc/s3_upload/config.json")
        with self.subTest("grace_seconds"):
            self.assertEqual(args.grace_seconds, 300)

    def test_sync_subcommand_parses_to_sync_mode(self):
        """the new `sync` subcommand parses to sync mode (1.1)"""
        args = self._parse(
            [
                "sync",
                "--local_path",
                "/data/run1",
                "--bucket",
                "my-bucket",
                "--remote_path",
                "/seq",
                "--aws_profile",
                "prof",
                "--cores",
                "2",
                "--threads",
                "8",
                "--max_passes",
                "5",
                "--backoff_seconds",
                "30",
                "--dry_run",
            ]
        )

        with self.subTest("mode"):
            self.assertEqual(args.mode, "sync")
        with self.subTest("local_path"):
            self.assertEqual(args.local_path, "/data/run1")
        with self.subTest("bucket"):
            self.assertEqual(args.bucket, "my-bucket")
        with self.subTest("remote_path"):
            self.assertEqual(args.remote_path, "/seq")
        with self.subTest("aws_profile"):
            self.assertEqual(args.aws_profile, "prof")
        with self.subTest("max_passes"):
            self.assertEqual(args.max_passes, 5)
        with self.subTest("backoff_seconds"):
            self.assertEqual(args.backoff_seconds, 30)
        with self.subTest("dry_run"):
            self.assertTrue(args.dry_run)

    def test_sync_subcommand_defaults_applied(self):
        """sync retry-control args have sensible defaults (1.10)"""
        args = self._parse(
            ["sync", "--local_path", "/data/run1", "--bucket", "my-bucket"]
        )

        with self.subTest("mode"):
            self.assertEqual(args.mode, "sync")
        with self.subTest("remote_path default"):
            self.assertEqual(args.remote_path, "/")
        with self.subTest("max_passes default"):
            self.assertEqual(args.max_passes, 10)
        with self.subTest("backoff_seconds default"):
            self.assertEqual(args.backoff_seconds, 60)
        with self.subTest("dry_run default"):
            self.assertFalse(args.dry_run)

    def test_no_subcommand_errors_out(self):
        """the existing `args.mode is None` guard still applies (7.1)"""
        with self.assertRaises(SystemExit):
            self._parse([])


if __name__ == "__main__":
    unittest.main()
