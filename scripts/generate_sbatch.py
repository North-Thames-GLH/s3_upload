#!/usr/bin/env python3
"""
Generate an sbatch submission script (and matching live_cbcl config) for an
S3 live_cbcl upload run, based on a pool samplesheet.

Given a samplesheet CSV, this:
  - reads the CSV and extracts the single `run_id` (asserting all rows agree),
  - extracts the single `pool_id` (asserting all rows agree),
  - creates `upload_logs/<pool_id>/` in the repo,
  - writes a live_cbcl config there (a fixed template with the run folder
    inserted at the end of the monitored directory path),
  - writes an sbatch script there whose job name and output/error logs are
    named after the pool.

Usage:
    python3 scripts/generate_sbatch.py Pool_8888.hpc.csv
"""

import argparse
import csv
import json
from pathlib import Path


# Repo root (this file lives in <repo>/scripts/)
REPO_DIR = Path(__file__).resolve().parent.parent

# --- fixed knobs ------------------------------------------------------------
UPLOAD_LOGS_DIRNAME = "upload_logs"
JOB_NAME_PREFIX = "s3_live_cbcl"
RUNS_BASE_DIR = "/mnt/smb/RDS2/transgen-mdx/1.runs"
REMOTE_REPO_DIR = "/mnt/software/5.git_repos/s3_upload"
VENV_ACTIVATE = ".venv/bin/activate"
PARTITION = "low-level"
TIME_LIMIT = "48:00:00"

# Fixed config template. Only monitored_directories is filled in per run.
CONFIG_TEMPLATE = {
    "max_cores": 1,
    "max_threads": 4,
    "max_age": 2400,
    "live_cycle_grace_seconds": 30,
    "aws_profile": "genomics-s3-write",
    "log_level": "INFO",
    "log_dir": "/tmp/s3_upload_logs",
    "monitor": [
        {
            "monitored_directories": [],
            "bucket": "development-bucket-input",
            "remote_path": "1.runs/",
            "live_cbcl": True,
        }
    ],
}


def parse_args():
    parser = argparse.ArgumentParser(
        description=(
            "Generate an sbatch script and live_cbcl config for a pool"
            " samplesheet."
        )
    )
    parser.add_argument(
        "samplesheet",
        help="Path to the pool samplesheet CSV (e.g. Pool_8888.hpc.csv).",
    )
    return parser.parse_args()


def read_single_value(rows, column, samplesheet_path):
    """
    Return the single unique value for `column` across all rows, raising if
    the column is missing or the rows disagree.
    """
    if not rows:
        raise ValueError(f"No data rows found in {samplesheet_path}")

    if column not in rows[0]:
        raise ValueError(
            f"Column '{column}' not found in {samplesheet_path}."
            f" Available columns: {', '.join(rows[0].keys())}"
        )

    values = {(row.get(column) or "").strip() for row in rows}

    if "" in values:
        raise ValueError(
            f"Column '{column}' has empty value(s) in {samplesheet_path}"
        )

    if len(values) != 1:
        raise ValueError(
            f"Column '{column}' is not consistent across rows in"
            f" {samplesheet_path}: found {sorted(values)}"
        )

    return values.pop()


def parse_samplesheet(samplesheet_path):
    """Read the samplesheet and return (run_id, pool_id)."""
    with open(samplesheet_path, newline="") as fh:
        rows = list(csv.DictReader(fh))

    run_id = read_single_value(rows, "run_id", samplesheet_path)
    pool_id = read_single_value(rows, "pool_id", samplesheet_path)

    return run_id, pool_id


def build_config(run_id):
    """Build the live_cbcl config dict for the given run."""
    config = json.loads(json.dumps(CONFIG_TEMPLATE))  # deep copy
    config["monitor"][0]["monitored_directories"] = [
        f"{RUNS_BASE_DIR}/{run_id}"
    ]
    return config


def build_sbatch(pool_id, config_rel_path, out_rel_path, err_rel_path):
    """Build the sbatch script text."""
    lines = [
        "#!/bin/bash",
        f"#SBATCH --time={TIME_LIMIT}",
        f"#SBATCH --job-name={JOB_NAME_PREFIX}_{pool_id}",
        f"#SBATCH -p {PARTITION}",
        f"#SBATCH --output={out_rel_path}",
        f"#SBATCH --error={err_rel_path}",
        "",
        f"cd {REMOTE_REPO_DIR}",
        f"source {VENV_ACTIVATE}",
        (
            "python3 s3_upload/s3_upload.py live_cbcl"
            f" --config {config_rel_path}"
        ),
        "",
    ]
    return "\n".join(lines)


def main():
    args = parse_args()

    run_id, pool_id = parse_samplesheet(args.samplesheet)

    pool_dir = REPO_DIR / UPLOAD_LOGS_DIRNAME / pool_id
    pool_dir.mkdir(parents=True, exist_ok=True)

    # relative-to-repo-root paths (script cds into the repo root before running)
    config_rel = f"{UPLOAD_LOGS_DIRNAME}/{pool_id}/{pool_id}_live_cbcl_config.json"
    out_rel = f"{UPLOAD_LOGS_DIRNAME}/{pool_id}/{pool_id}.out"
    err_rel = f"{UPLOAD_LOGS_DIRNAME}/{pool_id}/{pool_id}.err"
    sbatch_rel = f"{UPLOAD_LOGS_DIRNAME}/{pool_id}/{pool_id}.sbatch"

    config = build_config(run_id)
    (REPO_DIR / config_rel).write_text(json.dumps(config, indent=4) + "\n")

    sbatch = build_sbatch(pool_id, config_rel, out_rel, err_rel)
    (REPO_DIR / sbatch_rel).write_text(sbatch)

    print(f"run_id:  {run_id}")
    print(f"pool_id: {pool_id}")
    print(f"Wrote config:  {config_rel}")
    print(f"Wrote sbatch:  {sbatch_rel}")


if __name__ == "__main__":
    main()
