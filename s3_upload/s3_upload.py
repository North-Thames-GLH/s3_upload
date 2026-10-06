import argparse
from os import cpu_count, makedirs, path
from pathlib import Path
import sys
import time
from timeit import default_timer as timer

from utils.io import (
    acquire_lock,
    read_config,
    read_live_upload_state_log,
    write_live_upload_state_to_log,
    write_sync_state_to_log,
    write_upload_state_to_log,
)
from utils.upload import (
    check_aws_access,
    check_buckets_exist,
    ExpiredCredentialsError,
    list_remote_objects,
    multi_core_upload,
)
from utils.utils import (
    check_is_sequencing_run_dir,
    check_termination_file_exists,
    compute_missing_files,
    get_live_cbcl_files,
    get_runs_to_live_upload,
    get_runs_to_upload,
    get_sequencing_file_list,
    filter_uploaded_files,
    split_file_list_by_cores,
    verify_config,
)
from utils.log import get_logger, set_file_handler
from utils import slack


log = get_logger("s3_upload")


def parse_args() -> argparse.Namespace:
    """
    Parse cmd line arguments

    Returns
    -------
    argparse.Namespace
        parsed arguments
    """
    parser = argparse.ArgumentParser()

    subparsers = parser.add_subparsers(
        help="Upload mode to run", dest="mode"
    )

    monitor_parser = subparsers.add_parser(
        "monitor",
        help=(
            "Mode to be run on a schedule to monitor directories for newly"
            " completed sequencing runs"
        ),
    )
    monitor_parser.add_argument(
        "--config",
        required=True,
        help="Config file for monitoring directories to upload",
    )
    monitor_parser.add_argument(
        "--dry_run",
        default=False,
        action="store_true",
        help=(
            "Calls everything except the actual upload to check what runs"
            " would be uploaded"
        ),
    )

    live_parser = subparsers.add_parser(
        "live_cbcl",
        help=(
            "Mode to be run on a schedule to upload cbcl files from"
            " in-progress sequencing runs"
        ),
    )
    live_parser.add_argument(
        "--config",
        required=True,
        help="Config file for monitoring directories to upload",
    )
    live_parser.add_argument(
        "--dry_run",
        default=False,
        action="store_true",
        help=(
            "Calls everything except the actual upload to check what cbcl"
            " files would be uploaded"
        ),
    )
    live_parser.add_argument(
        "--grace_seconds",
        type=int,
        default=None,
        help=(
            "Optional override for minimum file age in seconds to consider"
            " cbcl files from closed cycles stable for upload"
        ),
    )

    upload_parser = subparsers.add_parser(
        "upload",
        help="Mode to upload a single directory to given location in S3",
    )
    upload_parser.add_argument(
        "--local_path", help="path to directory to upload"
    )
    upload_parser.add_argument(
        "--bucket",
        type=str,
        help="S3 bucket to upload to",
    )
    upload_parser.add_argument(
        "--remote_path",
        default="/",
        help="Remote path in bucket to upload sequencing dir to",
    )
    upload_parser.add_argument(
        "--skip_check",
        default=False,
        action="store_true",
        help=(
            "Controls if to skip checks for the provided directory being a"
            " completed sequencing run. This allows for uploading any"
            " arbitrary provided directory to AWS S3."
        ),
    )
    upload_parser.add_argument(
        "--cores",
        required=False,
        type=int,
        default=cpu_count(),
        help=(
            "Number of CPU cores to split total files to upload across, will "
            "default to using all available"
        ),
    )
    upload_parser.add_argument(
        "--threads",
        type=int,
        default=8,
        help=(
            "Number of threads to open per core to split uploading across "
            "(default: 8)"
        ),
    )

    sync_parser = subparsers.add_parser(
        "sync",
        help=(
            "Reconcile a local run directory against S3 and upload only"
            " missing / size-mismatched files"
        ),
    )
    sync_parser.add_argument(
        "--local_path",
        required=True,
        help="path to run directory to sync",
    )
    sync_parser.add_argument(
        "--bucket",
        required=True,
        type=str,
        help="S3 bucket to sync to",
    )
    sync_parser.add_argument(
        "--remote_path",
        default="/",
        help="Remote parent path in bucket to sync sequencing dir to",
    )
    sync_parser.add_argument(
        "--aws_profile",
        default=None,
        help="AWS profile to use for authentication",
    )
    sync_parser.add_argument(
        "--cores",
        required=False,
        type=int,
        default=cpu_count(),
        help=(
            "Number of CPU cores to split total files to upload across, will "
            "default to using all available"
        ),
    )
    sync_parser.add_argument(
        "--threads",
        type=int,
        default=8,
        help=(
            "Number of threads to open per core to split uploading across "
            "(default: 8)"
        ),
    )
    sync_parser.add_argument(
        "--max_passes",
        type=int,
        default=10,
        help=(
            "Maximum number of reconciliation passes to attempt before"
            " giving up (default: 10)"
        ),
    )
    sync_parser.add_argument(
        "--backoff_seconds",
        type=int,
        default=60,
        help=(
            "Seconds to wait between reconciliation passes that make no"
            " progress (default: 60)"
        ),
    )
    sync_parser.add_argument(
        "--log_dir",
        default="/var/log/s3_upload",
        help="Directory to write the sync state log to",
    )
    sync_parser.add_argument(
        "--dry_run",
        default=False,
        action="store_true",
        help="Report missing files that would be uploaded without uploading",
    )

    args = parser.parse_args()

    if args.mode is None:
        parser.error("the following arguments are required: mode")

    return args


def upload_single_run(args) -> None:
    """
    Upload provided single run directory into AWS S3

    Parameters
    ----------
    args : argparse.NameSpace
        parsed command line arguments
    """
    check_aws_access()
    check_buckets_exist(buckets=[args.bucket])

    if not args.skip_check:
        log.info(
            "Checking if the provided directory is a complete sequencing run"
        )
        if not check_is_sequencing_run_dir(
            args.local_path
        ) or not check_termination_file_exists(args.local_path):
            log.error(
                "Provided directory: %s does not appear to be a complete "
                "sequencing run. Please check the provided path and try"
                " again.",
                args.local_path,
            )
            exit()

    files = get_sequencing_file_list(args.local_path)
    files = split_file_list_by_cores(files=files, n=args.cores)

    # pass through the parent of the specified directory to upload
    # to ensure we upload into the actual run directory
    parent_path = Path(args.local_path).parent

    # simple timer of upload
    start = timer()

    try:
        multi_core_upload(
            files=files,
            bucket=args.bucket,
            remote_path=args.remote_path,
            cores=args.cores,
            threads=args.threads,
            parent_path=parent_path,
        )
    except ExpiredCredentialsError:
        log.error(
            "AWS credentials expired during upload of %s. Re-authenticate"
            " and rerun to complete the upload.",
            args.local_path,
        )
        sys.exit(1)

    end = timer()
    total = end - start

    log.info(
        "Uploaded %s in %s",
        args.local_path,
        f"{int(total // 60)}m {int(total % 60)}s",
    )


def sync_single_run(args) -> None:
    """
    Reconcile a single local run directory against its S3 prefix and
    upload only the files that are missing or size-mismatched.

    S3 is treated as the source of truth: on each pass the remote prefix
    is re-listed, the missing set is recomputed against the (fixed) local
    file list, and only the missing files are uploaded. The pass is
    repeated until the run is fully synced or `max_passes` is reached.
    Credentials are re-resolved each pass (via `multi_core_upload` /
    `list_remote_objects`) so a sync exceeding the credential lifetime can
    complete across passes.

    Parameters
    ----------
    args : argparse.Namespace
        parsed command line arguments
    """
    aws_profile = args.aws_profile
    run_dir = args.local_path

    check_aws_access(aws_profile=aws_profile)
    check_buckets_exist(buckets=[args.bucket], aws_profile=aws_profile)

    if not Path(run_dir).exists():
        log.error("Run directory does not exist: %s", run_dir)
        sys.exit(1)

    # pass through the parent of the specified directory so the run
    # directory name forms the leaf of the remote key, matching how the
    # other modes upload
    parent_path = Path(run_dir).parent

    # local run directory is stable during a sync, so enumerate once
    local_files = get_sequencing_file_list(run_dir)
    run_id = Path(run_dir).name
    run_prefix = path.join(args.remote_path, run_id).lstrip("/")

    log_file = path.join(
        args.log_dir, "uploads", "{}.sync.log.json".format(run_id)
    )

    # cumulative counts across passes for the sync state log
    cumulative_uploaded = {}
    cumulative_failed = []
    missing = []

    for attempt in range(1, args.max_passes + 1):
        # re-list remote objects each pass since previous passes change
        # what exists in S3; raises RuntimeError on failure (no fallback)
        remote = list_remote_objects(args.bucket, run_prefix, aws_profile)
        missing = compute_missing_files(
            local_files=local_files,
            remote_objects=remote,
            parent_path=parent_path,
            remote_path=args.remote_path,
        )

        log.info(
            "Pass %s: %s local, %s remote, %s missing",
            attempt,
            len(local_files),
            len(remote),
            len(missing),
        )

        if not missing:
            log.info("Run %s fully synced", run_id)
            return

        if args.dry_run:
            for f in missing:
                log.info("Would upload %s", f)
            log.info(
                "--dry_run specified, %s files would be uploaded for %s,"
                " exiting without uploading",
                len(missing),
                run_id,
            )
            return

        split = split_file_list_by_cores(files=missing, n=args.cores)

        try:
            uploaded, failed = multi_core_upload(
                files=split,
                bucket=args.bucket,
                remote_path=args.remote_path,
                cores=args.cores,
                threads=args.threads,
                parent_path=parent_path,
                aws_profile=aws_profile,
            )
        except ExpiredCredentialsError:
            # credentials expired mid-pass; the upload aborted fast rather
            # than failing every remaining file. Back off, then loop: the
            # next pass re-lists remote objects (so anything uploaded
            # before the abort is skipped) and re-resolves credentials.
            log.warning(
                "Pass %s aborted due to expired AWS credentials, backing"
                " off %ss before retrying with fresh credentials",
                attempt,
                args.backoff_seconds,
            )
            time.sleep(args.backoff_seconds)
            continue

        cumulative_uploaded = {**cumulative_uploaded, **uploaded}
        cumulative_failed = failed

        log.info(
            "Pass %s: uploaded %s files, %s failed",
            attempt,
            len(uploaded),
            len(failed),
        )

        write_sync_state_to_log(
            run_id=run_id,
            run_path=run_dir,
            log_file=log_file,
            total_local=len(local_files),
            total_remote=len(remote),
            uploaded_files=cumulative_uploaded,
            failed_files=cumulative_failed,
            passes=attempt,
        )

        if not uploaded:
            # no progress this pass (e.g. all uploads failed / expired
            # credentials) => back off before re-resolving and retrying
            log.warning(
                "No progress on pass %s, backing off %ss",
                attempt,
                args.backoff_seconds,
            )
            time.sleep(args.backoff_seconds)

    # exhausted all passes with work still remaining
    log.error(
        "Reached max passes (%s) with %s files still missing",
        args.max_passes,
        len(missing),
    )
    sys.exit(1)


def monitor_directories_for_upload(config, dry_run) -> None:
    """
    Monitor specified directories for complete sequencing runs to upload

    Parameters
    ----------
    config : dict
        contents of config file
    dry_run : bool
        calls everything except the actual upload for testing / debugging
    """
    log.info("Beginning monitoring directories for runs to upload")
    aws_profile = config.get("aws_profile")

    # preferentially use respective log and alert channel webhooks if specified
    log_url = config.get("slack_log_webhook") or config.get(
        "slack_alert_webhook"
    )
    alert_url = config.get("slack_alert_webhook") or config.get(
        "slack_log_webhook"
    )

    if not log_url and not alert_url:
        log.warning(
            "Neither `slack_log_webhook` or `slack_alert_webhook` specified =>"
            " no Slack notifications will be sent"
        )

    check_aws_access(slack_alert_webhook=alert_url, aws_profile=aws_profile)
    check_buckets_exist(
        buckets=set([x["bucket"] for x in config["monitor"]]),
        slack_alert_webhook=alert_url,
        aws_profile=aws_profile,
    )

    cores = config.get("max_cores", cpu_count())
    threads = config.get("max_threads", 4)
    log_dir = config.get("log_dir", "/var/log/s3_upload")

    to_upload = []
    partially_uploaded = []

    # find all the runs to upload in the specified monitored directories
    # and build a dict per run with config of where to upload
    for monitor_dir_config in config["monitor"]:
        completed_runs, incomplete_runs = get_runs_to_upload(
            monitor_dir_config.get("monitored_directories"),
            log_dir=log_dir,
            sample_pattern=monitor_dir_config.get("sample_regex"),
            max_age=config.get("max_age", 72),
        )

        for run_dir in completed_runs:
            to_upload.append(
                {
                    "run_dir": run_dir,
                    "run_id": Path(run_dir).name,
                    "parent_path": Path(run_dir).parent,
                    "bucket": monitor_dir_config["bucket"],
                    "remote_path": monitor_dir_config["remote_path"],
                    "exclude_patterns": monitor_dir_config.get(
                        "exclude_patterns"
                    ),
                }
            )

        for partial_run, uploaded_files in incomplete_runs.items():
            partially_uploaded.append(
                {
                    "run_dir": partial_run,
                    "run_id": Path(partial_run).name,
                    "parent_path": Path(partial_run).parent,
                    "bucket": monitor_dir_config["bucket"],
                    "remote_path": monitor_dir_config["remote_path"],
                    "uploaded_files": uploaded_files,
                }
            )

    if not to_upload and not partially_uploaded:
        log.info("No sequencing runs requiring upload found.")
        return

    log.info(
        "Found %s new sequencing runs to upload: %s",
        len(to_upload),
        ", ".join([x["run_id"] for x in to_upload]),
    )
    if partially_uploaded:
        log.info(
            "Found %s partially uploaded runs to continue uploading: %s",
            len(partially_uploaded),
            ", ".join([x["run_id"] for x in partially_uploaded]),
        )

        # preferentially upload partial runs first
        to_upload = partially_uploaded + to_upload

    if dry_run:
        for run in to_upload:
            log.info(
                "%s would be uploaded to %s:%s",
                run["run_dir"],
                run["bucket"],
                path.join(run["remote_path"], run["run_id"]),
            )
        log.info("--dry_run specified, exiting now without uploading")
        sys.exit()

    log.info("Beginning upload of %s runs", len(to_upload))

    runs_successfully_uploaded = []
    runs_failed_upload = []

    for idx, run_config in enumerate(to_upload, 1):
        # begin uploading of each sequencing run
        log.info(
            "Uploading run %s [%s/%s]",
            run_config["run_id"],
            idx,
            len(to_upload),
        )

        # simple timer to log total upload time
        start = timer()

        all_run_files = get_sequencing_file_list(
            seq_dir=run_config["run_dir"],
            exclude_patterns=run_config.get("exclude_patterns"),
        )

        files_to_upload = all_run_files.copy()

        if run_config.get("uploaded_files"):
            # files we stored from a previous partial upload to not reupload
            files_to_upload = filter_uploaded_files(
                local_files=files_to_upload,
                uploaded_files=run_config.get("uploaded_files"),
            )

        files_to_upload = split_file_list_by_cores(
            files=files_to_upload, n=cores
        )

        # call the actual upload, any errors being raised that result in
        # files not being uploaded should be returned and will result in
        # upload state log storing the run as not complete, allowing for
        # retries on uploading
        try:
            uploaded_files, failed_upload = multi_core_upload(
                files=files_to_upload,
                bucket=run_config["bucket"],
                remote_path=run_config["remote_path"],
                cores=cores,
                threads=threads,
                parent_path=run_config["parent_path"],
                aws_profile=aws_profile,
            )
        except ExpiredCredentialsError:
            # credentials expired mid-upload; stop processing further runs
            # rather than failing them all with a dead token. The next
            # scheduled invocation re-resolves credentials and resumes the
            # partially uploaded run from its state log.
            log.error(
                "AWS credentials expired while uploading %s, stopping"
                " remaining uploads. Rerun to resume with fresh"
                " credentials.",
                run_config["run_id"],
            )
            runs_failed_upload.append(run_config["run_id"])
            break

        # set output logs to go into subdirectory with stdout/stderr log
        makedirs(path.join(log_dir, "uploads"), exist_ok=True)
        run_log_file = path.join(
            log_dir, f"uploads/{run_config['run_id']}.upload.log.json"
        )

        log_data = write_upload_state_to_log(
            run_id=run_config["run_id"],
            run_path=run_config["run_dir"],
            log_file=run_log_file,
            local_files=all_run_files,
            uploaded_files=uploaded_files,
            failed_files=failed_upload,
        )

        if log_data["completed"]:
            upload_state = "Fully"
            runs_successfully_uploaded.append(log_data["run_id"])
        else:
            upload_state = "Partially"
            runs_failed_upload.append(log_data["run_id"])

        end = timer()
        total = end - start

        log.info(
            "%s uploaded %s in %s",
            upload_state,
            run_config["run_id"],
            f"{int(total // 60)}m {int(total % 60)}s",
        )

    log.info(
        "Completed uploading all runs, %s successfully uploaded, %s failed"
        " upload",
        len(runs_successfully_uploaded),
        len(runs_failed_upload),
    )

    if runs_successfully_uploaded and log_url:
        log.debug(
            "Sending success upload message to Slack channel %s", log_url
        )
        message = slack.format_message(completed=runs_successfully_uploaded)
        slack.post_message(url=log_url, message=message)

    if runs_failed_upload and alert_url:
        log.debug("Sending failed upload alert to Slack channel %s", alert_url)
        message = slack.format_message(failed=runs_failed_upload)
        slack.post_message(url=alert_url, message=message)


def monitor_directories_for_live_cbcl_upload(
    config, dry_run, grace_seconds=None
) -> None:
    """
    Monitor specified directories for active sequencing runs and upload
    cbcl files from closed cycle directories.

    Runs in a continuous polling loop, sleeping for `poll_interval`
    seconds (configurable, default 600) between passes. Designed to be
    run as a long-lived process (e.g. inside an sbatch job).

    Parameters
    ----------
    config : dict
        contents of config file
    dry_run : bool
        calls everything except the actual upload for testing / debugging
    grace_seconds : int | None
        optional override for cbcl file age threshold
    """
    import time

    log.info("Beginning monitoring directories for live cbcl upload")
    aws_profile = config.get("aws_profile")
    poll_interval = config.get("poll_interval", 600)

    log_url = config.get("slack_log_webhook") or config.get(
        "slack_alert_webhook"
    )
    alert_url = config.get("slack_alert_webhook") or config.get(
        "slack_log_webhook"
    )

    if not log_url and not alert_url:
        log.warning(
            "Neither `slack_log_webhook` or `slack_alert_webhook` specified =>"
            " no Slack notifications will be sent"
        )

    check_aws_access(slack_alert_webhook=alert_url, aws_profile=aws_profile)
    check_buckets_exist(
        buckets=set([x["bucket"] for x in config["monitor"]]),
        slack_alert_webhook=alert_url,
        aws_profile=aws_profile,
    )

    cores = config.get("max_cores", cpu_count())
    threads = config.get("max_threads", 4)
    log_dir = config.get("log_dir", "/var/log/s3_upload")

    log.info("Polling interval set to %s seconds", poll_interval)

    poll_count = 0

    while True:
        poll_count += 1
        log.info("Starting live cbcl poll cycle %s", poll_count)

        runs_to_upload = []

        for monitor_dir_config in config["monitor"]:
            if monitor_dir_config.get("live_cbcl") is False:
                continue

            live_runs = get_runs_to_live_upload(
                monitor_dirs=monitor_dir_config.get("monitored_directories"),
                sample_pattern=monitor_dir_config.get("sample_regex"),
            )

            for run_dir in live_runs:
                runs_to_upload.append(
                    {
                        "run_dir": run_dir,
                        "run_id": Path(run_dir).name,
                        "parent_path": Path(run_dir).parent,
                        "bucket": monitor_dir_config["bucket"],
                        "remote_path": monitor_dir_config["remote_path"],
                        "live_lanes": monitor_dir_config.get("live_lanes"),
                        "grace_seconds": grace_seconds
                        if grace_seconds is not None
                        else monitor_dir_config.get(
                            "live_cycle_grace_seconds",
                            config.get("live_cycle_grace_seconds", 120),
                        ),
                    }
                )

        if not runs_to_upload:
            log.info("No active sequencing runs requiring live cbcl upload found.")
        else:
            log.info(
                "Found %s active runs for live cbcl upload: %s",
                len(runs_to_upload),
                ", ".join([x["run_id"] for x in runs_to_upload]),
            )

        runs_successfully_uploaded = []
        runs_failed_upload = []
        credentials_expired = False

        for idx, run_config in enumerate(runs_to_upload, 1):
            log.info(
                "Uploading live cbcl for run %s [%s/%s]",
                run_config["run_id"],
                idx,
                len(runs_to_upload),
            )

            run_log_file = path.join(
                log_dir, f"uploads/{run_config['run_id']}.live_cbcl.upload.log.json"
            )

            all_cbcl_files = get_live_cbcl_files(
                run_dir=run_config["run_dir"],
                lanes=run_config.get("live_lanes"),
                grace_seconds=run_config["grace_seconds"],
            )

            if not all_cbcl_files:
                log.info("No closed-cycle cbcl files available for %s", run_config["run_id"])
                continue

            previously_uploaded = []
            if path.exists(run_log_file):
                state = read_live_upload_state_log(run_log_file)
                previously_uploaded = [
                    local_file
                    for lane_data in state.get("lanes", {}).values()
                    for local_file in lane_data.get("uploaded_files", {})
                ]

            files_to_upload = filter_uploaded_files(
                local_files=all_cbcl_files, uploaded_files=previously_uploaded
            )

            if not files_to_upload:
                log.info(
                    "No new closed-cycle cbcl files to upload for %s",
                    run_config["run_id"],
                )
                continue

            if dry_run:
                log.info(
                    "%s cbcl files would be uploaded for %s to %s:%s",
                    len(files_to_upload),
                    run_config["run_id"],
                    run_config["bucket"],
                    path.join(run_config["remote_path"], run_config["run_id"]),
                )
                continue

            files_to_upload = split_file_list_by_cores(files=files_to_upload, n=cores)
            try:
                uploaded_files, failed_upload = multi_core_upload(
                    files=files_to_upload,
                    bucket=run_config["bucket"],
                    remote_path=run_config["remote_path"],
                    cores=cores,
                    threads=threads,
                    parent_path=run_config["parent_path"],
                    aws_profile=aws_profile,
                )
            except ExpiredCredentialsError:
                # credentials expired mid-upload; stop processing the rest
                # of this poll cycle and let the next cycle re-resolve
                # fresh credentials (multi_core_upload resolves per call).
                log.warning(
                    "AWS credentials expired during live cbcl upload of %s,"
                    " stopping this poll cycle to retry on the next cycle",
                    run_config["run_id"],
                )
                credentials_expired = True
                break

            makedirs(path.join(log_dir, "uploads"), exist_ok=True)
            log_data = write_live_upload_state_to_log(
                run_id=run_config["run_id"],
                run_path=run_config["run_dir"],
                log_file=run_log_file,
                uploaded_files=uploaded_files,
                failed_files=failed_upload,
            )

            if failed_upload:
                runs_failed_upload.append(log_data["run_id"])
            else:
                runs_successfully_uploaded.append(log_data["run_id"])

        if dry_run:
            log.info("--dry_run specified, skipping upload and continuing to finalisation checks")

        log.info(
            "Completed live cbcl upload pass, %s runs without upload errors, %s runs"
            " with upload errors",
            len(runs_successfully_uploaded),
            len(runs_failed_upload),
        )

        if runs_successfully_uploaded and log_url:
            message = slack.format_message(completed=runs_successfully_uploaded)
            slack.post_message(url=log_url, message=message)

        if runs_failed_upload and alert_url:
            message = slack.format_message(failed=runs_failed_upload)
            slack.post_message(url=alert_url, message=message)

        # Finalise completed runs in the same pass so top-level metadata files
        # (e.g. RunInfo.xml/SampleSheet.csv/CopyComplete.txt) are uploaded after
        # run termination without requiring a separate monitor invocation.
        # Skip finalisation if credentials expired this cycle; it would only
        # fail the same way. The next poll cycle re-resolves credentials.
        if credentials_expired:
            log.info(
                "Skipping finalisation this cycle due to expired credentials"
            )
        elif config.get("live_cbcl_finalize_completed_runs", True):
            log.info(
                "Checking for completed runs requiring final upload in monitor mode"
            )
            monitor_directories_for_upload(config=config, dry_run=dry_run)

        log.info(
            "Poll cycle %s complete, sleeping for %s seconds",
            poll_count,
            poll_interval,
        )
        time.sleep(poll_interval)


def main() -> None:
    args = parse_args()

    if args.mode == "upload":
        upload_single_run(args)
    elif args.mode == "sync":
        sync_single_run(args)
    else:
        config = read_config(config=args.config)
        verify_config(config=config)

        log_dir = config.get("log_dir", "/var/log/s3_upload")
        makedirs(log_dir, exist_ok=True)
        acquire_lock(lock_file=path.join(log_dir, "s3_upload.lock"))

        if config.get("log_level"):
            log.setLevel(config.get("log_level"))

        set_file_handler(log, log_dir=log_dir)

        if args.mode == "monitor":
            monitor_directories_for_upload(config=config, dry_run=args.dry_run)
        else:
            monitor_directories_for_live_cbcl_upload(
                config=config,
                dry_run=args.dry_run,
                grace_seconds=args.grace_seconds,
            )


if __name__ == "__main__":
    main()
