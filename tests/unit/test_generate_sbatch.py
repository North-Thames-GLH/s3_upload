"""
Unit tests for `scripts/generate_sbatch.py`.

These cover the pure, filesystem-independent helpers:

  * `parse_samplesheet` extracting the single run_id / pool_id from the
    repo's `Pool_8888.hpc.csv` fixture,
  * `build_config` placing `live_cbcl_exit_on_complete` at the TOP LEVEL of
    the live config (not inside the per-directory `monitor[0]` block), and
  * `build_live_sbatch` / `build_finished_sbatch` emitting the right job
    body for each `--mode`, including the exact finished-route sync line
    that carries the locked bucket / prefix / marker contract.

`scripts/` is not an importable package, so we load the module by file path
with importlib rather than a normal import.
"""

import importlib.util
import os
import unittest


REPO_ROOT = os.path.abspath(
    os.path.join(os.path.realpath(__file__), "../../../")
)
GENERATE_SBATCH_PATH = os.path.join(
    REPO_ROOT, "scripts", "generate_sbatch.py"
)
SAMPLESHEET_FIXTURE = os.path.join(REPO_ROOT, "Pool_8888.hpc.csv")


def _load_generate_sbatch():
    """Import scripts/generate_sbatch.py by file path as a module."""
    spec = importlib.util.spec_from_file_location(
        "generate_sbatch", GENERATE_SBATCH_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gs = _load_generate_sbatch()


class TestParseSamplesheet(unittest.TestCase):
    """`parse_samplesheet` against the committed Pool_8888 fixture."""

    def test_extracts_single_run_id_and_pool_id(self):
        run_id, pool_id = gs.parse_samplesheet(SAMPLESHEET_FIXTURE)

        with self.subTest("run_id"):
            self.assertEqual(
                run_id, "20260827_LH00575_0330_A23KWJJLT3"
            )
        with self.subTest("pool_id"):
            self.assertEqual(pool_id, "Pool_8888")


class TestBuildConfig(unittest.TestCase):
    """live-mode config places the exit flag at the top level."""

    def test_exit_on_complete_at_top_level(self):
        run_id, _ = gs.parse_samplesheet(SAMPLESHEET_FIXTURE)
        config = gs.build_config(run_id)

        with self.subTest("flag present at top level and True"):
            self.assertTrue(config.get("live_cbcl_exit_on_complete"))
        with self.subTest("flag NOT inside monitor[0]"):
            self.assertNotIn(
                "live_cbcl_exit_on_complete", config["monitor"][0]
            )
        with self.subTest("monitored directory filled from run_id"):
            self.assertEqual(
                config["monitor"][0]["monitored_directories"],
                [f"{gs.RUNS_BASE_DIR}/{run_id}"],
            )


class TestBuildLiveSbatch(unittest.TestCase):
    """live-mode sbatch body keeps the existing live_cbcl invocation."""

    def test_live_sbatch_runs_live_cbcl_with_config(self):
        _, pool_id = gs.parse_samplesheet(SAMPLESHEET_FIXTURE)
        config_rel = "upload_logs/Pool_8888/Pool_8888_live_cbcl_config.json"
        out_rel = "upload_logs/Pool_8888/Pool_8888.out"
        err_rel = "upload_logs/Pool_8888/Pool_8888.err"

        sbatch = gs.build_live_sbatch(
            pool_id, config_rel, out_rel, err_rel
        )

        with self.subTest("job name reflects the pool"):
            self.assertIn(
                f"#SBATCH --job-name={gs.JOB_NAME_PREFIX}_{pool_id}", sbatch
            )
        with self.subTest("partition / time preserved"):
            self.assertIn(f"#SBATCH -p {gs.PARTITION}", sbatch)
            self.assertIn(f"#SBATCH --time={gs.TIME_LIMIT}", sbatch)
        with self.subTest("cd + venv activate preserved"):
            self.assertIn(f"cd {gs.REMOTE_REPO_DIR}", sbatch)
            self.assertIn(f"source {gs.VENV_ACTIVATE}", sbatch)
        with self.subTest("runs live_cbcl with the config"):
            self.assertIn(
                "python3 s3_upload/s3_upload.py live_cbcl"
                f" --config {config_rel}",
                sbatch,
            )
        with self.subTest("does not run sync / publish marker"):
            self.assertNotIn("--publish-complete-marker", sbatch)


class TestBuildFinishedSbatch(unittest.TestCase):
    """finished-mode sbatch body runs the one-shot sync + marker publish."""

    def test_finished_sbatch_runs_sync_with_marker(self):
        run_id, pool_id = gs.parse_samplesheet(SAMPLESHEET_FIXTURE)
        out_rel = "upload_logs/Pool_8888/Pool_8888.out"
        err_rel = "upload_logs/Pool_8888/Pool_8888.err"

        sbatch = gs.build_finished_sbatch(
            pool_id, run_id, out_rel, err_rel
        )

        with self.subTest("job name / partition / time preserved"):
            self.assertIn(
                f"#SBATCH --job-name={gs.JOB_NAME_PREFIX}_{pool_id}", sbatch
            )
            self.assertIn(f"#SBATCH -p {gs.PARTITION}", sbatch)
            self.assertIn(f"#SBATCH --time={gs.TIME_LIMIT}", sbatch)
        with self.subTest("cd + venv activate preserved"):
            self.assertIn(f"cd {gs.REMOTE_REPO_DIR}", sbatch)
            self.assertIn(f"source {gs.VENV_ACTIVATE}", sbatch)
        with self.subTest("exact sync line with locked contract values"):
            expected = (
                "python3 s3_upload/s3_upload.py sync"
                f" --local_path {gs.RUNS_BASE_DIR}/{run_id}"
                " --bucket development-bucket-input"
                " --remote_path 1.runs/"
                " --aws_profile genomics-s3-write"
                " --max_passes 20"
                " --publish-complete-marker"
            )
            self.assertIn(expected, sbatch)


class TestParseArgsMode(unittest.TestCase):
    """the new --mode flag defaults to live and accepts finished."""

    def test_mode_defaults_to_live(self):
        import sys
        from unittest.mock import patch

        with patch.object(sys, "argv", ["generate_sbatch", "sheet.csv"]):
            args = gs.parse_args()
        with self.subTest("samplesheet positional"):
            self.assertEqual(args.samplesheet, "sheet.csv")
        with self.subTest("mode default"):
            self.assertEqual(args.mode, "live")

    def test_mode_finished_accepted(self):
        import sys
        from unittest.mock import patch

        with patch.object(
            sys,
            "argv",
            ["generate_sbatch", "sheet.csv", "--mode", "finished"],
        ):
            args = gs.parse_args()
        self.assertEqual(args.mode, "finished")


if __name__ == "__main__":
    unittest.main()
