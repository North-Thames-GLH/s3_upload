"""Functions for handling uploading into S3"""

from concurrent.futures import (
    ProcessPoolExecutor,
    ThreadPoolExecutor,
    as_completed,
)
from os import environ, path
import re
import sys
from typing import Dict, List, Tuple

import boto3
from boto3.s3.transfer import TransferConfig
from botocore.config import Config
from botocore import exceptions as s3_exceptions

from .log import get_logger
from .slack import post_message as post_slack_message

AWS_DEFAULT_PROFILE = environ.get("AWS_DEFAULT_PROFILE")
AWS_SECRET_KEY = environ.get("AWS_SECRET_KEY")
AWS_ACCESS_KEY = environ.get("AWS_ACCESS_KEY")
AWS_S3_ENDPOINT_URL = environ.get("AWS_S3_ENDPOINT_URL")

log = get_logger("s3_upload")

# AWS error codes that indicate the credentials / session token are no
# longer valid. These are not resolved by retrying the same request; the
# only remedy is to re-resolve credentials (which, for an auto-refreshing
# source such as IAM Roles Anywhere, mints a fresh token) and try again.
EXPIRED_CREDENTIAL_ERROR_CODES = {
    "ExpiredToken",
    "ExpiredTokenException",
    "InvalidToken",
    "RequestExpired",
    "TokenRefreshRequired",
}


class ExpiredCredentialsError(Exception):
    """
    Raised when an upload fails because the AWS credentials / session
    token have expired or become invalid.

    This is used to fail-fast out of an in-progress upload batch rather
    than attempting to upload every remaining file with credentials that
    are known to be dead. Callers are expected to catch this, re-resolve
    credentials, and retry.
    """

    pass


def is_expired_credentials_error(exc) -> bool:
    """
    Determine whether the given exception indicates expired / invalid AWS
    credentials, for which retrying with the same credentials is futile.

    Parameters
    ----------
    exc : Exception
        exception raised during an upload

    Returns
    -------
    bool
        True if the exception represents an expired / invalid credential
        error, else False
    """
    if isinstance(exc, ExpiredCredentialsError):
        return True

    # credentials could not be resolved at all / token retrieval failed
    if isinstance(
        exc,
        (
            s3_exceptions.NoCredentialsError,
            s3_exceptions.CredentialRetrievalError,
            s3_exceptions.TokenRetrievalError,
        ),
    ):
        return True

    # botocore ClientError carrying an expired-token style error code
    if isinstance(exc, s3_exceptions.ClientError):
        code = exc.response.get("Error", {}).get("Code")
        if code in EXPIRED_CREDENTIAL_ERROR_CODES:
            return True

    return False


def _build_boto3_session(aws_profile=None, aws_credentials=None):
    """
    Build a boto3 session using either an explicit profile, pre-resolved
    credentials, or env vars.

    Parameters
    ----------
    aws_profile : str | None
        Optional AWS profile name to use instead of AWS_DEFAULT_PROFILE.
    aws_credentials : dict | None
        Optional dict with pre-resolved credentials containing
        access_key, secret_key, and optionally token. Used to pass
        credentials to child processes that cannot invoke credential
        helpers.

    Returns
    -------
    boto3.session.Session
        Configured boto3 session
    """
    if aws_credentials:
        return boto3.Session(
            aws_access_key_id=aws_credentials["access_key"],
            aws_secret_access_key=aws_credentials["secret_key"],
            aws_session_token=aws_credentials.get("token"),
        )

    profile_name = aws_profile or AWS_DEFAULT_PROFILE

    if profile_name:
        return boto3.Session(profile_name=profile_name)

    return boto3.Session(
        aws_access_key_id=AWS_ACCESS_KEY,
        aws_secret_access_key=AWS_SECRET_KEY,
    )


def _resolve_credentials(aws_profile=None):
    """
    Resolve AWS credentials from the given profile or environment,
    returning a dict that can be passed to child processes.

    Parameters
    ----------
    aws_profile : str | None
        Optional AWS profile name

    Returns
    -------
    dict
        dict with access_key, secret_key, and optionally token
    """
    session = _build_boto3_session(aws_profile=aws_profile)
    credentials = session.get_credentials()

    if credentials is None:
        raise RuntimeError("Failed to resolve AWS credentials")

    frozen = credentials.get_frozen_credentials()

    resolved = {
        "access_key": frozen.access_key,
        "secret_key": frozen.secret_key,
    }

    if frozen.token:
        resolved["token"] = frozen.token

    return resolved


def check_aws_access(slack_alert_webhook=None, aws_profile=None) -> None:
    """
    Check that valid AWS credentials / configuration are available.

    This validates that authentication configuration is present (either
    an AWS profile or access key / secret key environment variables)
    without requiring s3:ListAllMyBuckets permission. Actual bucket
    accessibility is verified separately by check_buckets_exist().

    slack_alert_webhook : str
        webhook URL for sending alerts to

    Raises
    ------
    SystemExit
        Raised when mutually exclusive AWS environment variables provided
    SystemExit
        Raised when required environment variables not defined
    """
    log.info("Checking access to AWS")

    if not slack_alert_webhook:
        # try get from env if not set, mostly for testing
        slack_alert_webhook = environ.get("SLACK_ALERT_WEBHOOK")

    error_message = None
    slack_base_error = (
        ":warning:  *S3 Upload*: Error in connecting to AWS!\n\n"
    )

    if aws_profile is None and AWS_DEFAULT_PROFILE and (
        AWS_ACCESS_KEY or AWS_SECRET_KEY
    ):
        error_message = (
            "Both `AWS_DEFAULT_PROFILE` provided as well as `AWS_ACCESS_KEY`"
            " and / or `AWS_SECRET_KEY`. Only one authentication method may be"
            " used."
        )

    if aws_profile or AWS_DEFAULT_PROFILE:
        log.info(
            "AWS profile defined, will be used for authentication"
        )
    elif AWS_ACCESS_KEY and AWS_SECRET_KEY:
        log.info(
            "Environment variables AWS_ACCESS_KEY and AWS_SECRET_KEY defined,"
            " will be used for authentication"
        )
    else:
        error_message = (
            "Required environment variables for AWS authentication not"
            " defined. Requires either `AWS_DEFAULT_PROFILE` or"
            " `AWS_ACCESS_KEY` and `AWS_SECRET_KEY`."
        )

    if error_message:
        if slack_alert_webhook:
            post_slack_message(
                url=slack_alert_webhook,
                message=slack_base_error + error_message,
            )
        log.error(error_message)
        sys.exit(error_message)


def check_buckets_exist(
    buckets, slack_alert_webhook=None, aws_profile=None
) -> List[dict]:
    """
    Check that the provided bucket(s) exist and are accessible

    Parameters
    ----------
    buckets : list
        S3 bucket(s) to check access for
    slack_alert_webhook : str
        webhook URL for sending alerts to

    Returns
    -------
    list
        lists of dicts with bucket metadata

    Raises
    ------
    RuntimeError
        Raised when one or more buckets do not exist / not accessible
    """
    log.info("Checking bucket(s) exist and accessible: %s", ", ".join(buckets))

    if not slack_alert_webhook:
        # try get from env if not set, mostly for testing
        slack_alert_webhook = environ.get("SLACK_ALERT_WEBHOOK")

    valid = []
    invalid = []

    for bucket in buckets:
        try:
            log.debug("Checking %s exists and accessible", bucket)
            valid.append(
                _build_boto3_session(aws_profile=aws_profile)
                .client("s3", endpoint_url=AWS_S3_ENDPOINT_URL)
                .head_bucket(Bucket=bucket)
            )
        except s3_exceptions.ClientError:
            invalid.append(bucket)

    if invalid:
        error_message = (
            f"{len(invalid) } bucket(s) not accessible or do not exist: "
            f"{', '.join(invalid)}"
        )

        if slack_alert_webhook:
            slack_alert_message = (
                ":warning:  *S3 Upload*: Error in accessing specified S3"
                f" buckets!\n\n\t\t{error_message}"
            )

            post_slack_message(
                url=slack_alert_webhook, message=slack_alert_message
            )

        log.error(error_message)

        raise RuntimeError(error_message)

    log.debug("All buckets exist and accessible")
    return valid


def derive_remote_key(local_file, parent_path, remote_path) -> str:
    """
    Derive the S3 key for a local file by stripping the parent path and
    joining it under the remote path.

    This is the single source of truth for how local file paths are
    transformed into S3 keys, shared by upload_single_file and the sync
    mode's missing-file computation so that keys line up byte-for-byte
    across modes.

    Parameters
    ----------
    local_file : str
        file and path to derive the key for
    parent_path : str
        path to parent of sequencing directory, removed from the file
        path so it does not form part of the remote key
    remote_path : str
        parent directory in bucket to upload to

    Returns
    -------
    str
        the S3 key the local file would occupy under the remote path
    """
    key = re.sub(rf"^{parent_path}", "", local_file).lstrip("/")
    key = path.join(remote_path, key).lstrip("/")

    return key


def list_remote_objects(bucket, prefix, aws_profile=None) -> Dict[str, int]:
    """
    List all S3 objects under the given prefix, returning a mapping of
    object key to object size in bytes.

    All paginated responses are aggregated so that the returned mapping
    is complete. Object sizes are read directly from the listing, so no
    per-object metadata request is required for size verification.

    This requires only s3:ListBucket permission and does not fall back
    to any local state log; any failure is treated as fatal.

    Parameters
    ----------
    bucket : str
        S3 bucket to list objects from
    prefix : str
        S3 key prefix to list objects under
    aws_profile : str | None
        Optional AWS profile name to use for authentication

    Returns
    -------
    dict
        mapping of S3 object key to object size in bytes

    Raises
    ------
    RuntimeError
        Raised when listing objects under the prefix fails
    """
    log.info("Listing remote objects under %s:%s", bucket, prefix)

    try:
        client = _build_boto3_session(aws_profile=aws_profile).client(
            "s3", endpoint_url=AWS_S3_ENDPOINT_URL
        )

        remote_objects = {}

        paginator = client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            for remote_object in page.get("Contents", []):
                remote_objects[remote_object["Key"]] = remote_object["Size"]
    except Exception as exc:
        error_message = (
            "Failed to list remote objects under prefix {}: {}".format(
                prefix, exc
            )
        )
        log.error(error_message)
        raise RuntimeError(error_message)

    log.info(
        "Found %s remote objects under %s:%s",
        len(remote_objects),
        bucket,
        prefix,
    )

    return remote_objects


def upload_single_file(
    s3_client, bucket, remote_path, local_file, parent_path
) -> Tuple[str, str]:
    """
    Uploads single file into S3 storage bucket

    Parameters
    ----------
    client : bootcore.client.S3
        boto3 S3 client
    bucket : str
        S3 bucket to upload to
    remote_path : str
        parent directory in bucket to upload to
    local_file : str
        file and path to upload
    parent_path : str
        path to parent of sequencing directory, will be removed from
        the file path for uploading to not form part of the remote path

    Returns
    -------
    str
        path and filename of uploaded file
    str
        ETag attribute of the uploaded file
    """
    # remove base directory and join to specified S3 location
    upload_file = derive_remote_key(
        local_file=local_file,
        parent_path=parent_path,
        remote_path=remote_path,
    )

    log.debug("Uploading %s to %s:%s", local_file, bucket, upload_file)

    # set threshold for splitting across cores to 1GB and to not use
    # multiple threads for single file upload to allow us to control
    # this better from the config
    config = TransferConfig(multipart_threshold=1024**3, use_threads=False)
    s3_client.upload_file(
        Filename=local_file,
        Bucket=bucket,
        Key=upload_file,
        Config=config,
    )

    # ensure we can access the remote file to log the object ID
    remote_object = s3_client.get_object(Bucket=bucket, Key=upload_file)

    log.debug("%s uploaded as %s", local_file, remote_object.get("ETag"))

    return local_file, remote_object.get("ETag", "").strip('"')


def _submit_to_pool(pool, func, item_input, items, **kwargs) -> dict:
    """
    Submits one call to `func` in `pool` (either ThreadPoolExecutor or
    ProcessPoolExecutor) for each item in `items`. All additional
    arguments defined in `kwargs` are passed to the given function.

    This has been abstracted from both multi_thread_upload and
    multi_core_upload to allow for unit testing of the called function
    raising exceptions that are caught and handled.

    In this context we will be calling upload_single_file() once for
    each of files in the given list of files, passing through the S3
    bucket and paths for uploading.

    Parameters
    ----------
    pool : ThreadPoolExecutor | ProcessPoolExecutor
        concurrent.futures executor to submit calls to
    func : callable
        function to call on submitting
    item_input : str
        function input field to submit each items of `items` to
    items : iterable
        iterable of object to submit

    Returns
    -------
    dict
        mapping of concurrent.futures.Future objects to the original
        `item` submitted for that future
    """
    return {
        pool.submit(
            func,
            **{**{item_input: item}, **kwargs},
        ): item
        for item in items
    }


def multi_thread_upload(
    files, bucket, remote_path, threads, parent_path, aws_credentials=None,
    aws_profile=None
) -> Tuple[Dict[str, str], list]:
    """
    Uploads the given set of `files` to S3 on a single CPU core using
    maximum of n threads.

    We are defining one S3 client per core due to boto3 clients being thread
    safe but not safe to share across processes due to expected response
    ordering: https://boto3.amazonaws.com/v1/documentation/api/latest/guide/clients.html#caveat

    We will also set `disable_request_compression` since the majority of run
    data will not compress and this reduces unneeded CPU and memory load. In
    addition, `tcp_keepalive` is set to ensure the session is kept alive.
    Other available config parameters for the botocore Config object are here:
    https://botocore.amazonaws.com/v1/documentation/api/latest/reference/config.html

    Parameters
    ----------
    files : list
        list of local files to upload
    bucket : str
        S3 bucket to upload to
    remote_path : str
        parent directory in bucket to upload to
    threads : int
        maximum number of threaded process to open per core
    parent_path : str
        path to parent of sequencing directory, will be removed from
        the file path for uploading to not form part of the remote path

    Returns
    -------
    dict
        mapping of local file to ETag ID of uploaded file
    list
        list of any files that failed to upload
    """
    log.info("Uploading %s files with %s threads", len(files), threads)

    session = _build_boto3_session(
        aws_profile=aws_profile, aws_credentials=aws_credentials
    )
    s3_client = session.client(
        "s3",
        endpoint_url=AWS_S3_ENDPOINT_URL,
        config=Config(
            retries={"total_max_attempts": 10, "mode": "standard"},
            max_pool_connections=100,
        ),
    )

    uploaded_files = {}
    failed_upload = []

    with ThreadPoolExecutor(max_workers=threads) as executor:
        concurrent_jobs = _submit_to_pool(
            pool=executor,
            func=upload_single_file,
            item_input="local_file",
            items=files,
            s3_client=s3_client,
            bucket=bucket,
            remote_path=remote_path,
            parent_path=parent_path,
        )

        for future in as_completed(concurrent_jobs):
            # access returned output as each is returned in any order
            try:
                local_file, remote_id = future.result()
                uploaded_files[local_file] = remote_id
            except Exception as exc:
                if is_expired_credentials_error(exc):
                    # credentials have expired mid-batch; every remaining
                    # file would fail the same way. Abort fast instead of
                    # grinding through the rest, cancelling any not yet
                    # started so the caller can re-resolve and retry.
                    for pending in concurrent_jobs:
                        pending.cancel()
                    log.error(
                        "AWS credentials expired while uploading, aborting"
                        " this batch to retry with fresh credentials: %s",
                        exc,
                    )
                    raise ExpiredCredentialsError(str(exc)) from exc

                # catch any errors that may get raised from uploading, we
                # will return a list of failed files to try reupload later
                log.error(
                    "Error in uploading %s: %s", concurrent_jobs[future], exc
                )
                failed_upload.append(concurrent_jobs[future])

    return uploaded_files, failed_upload


def multi_core_upload(
    files, bucket, remote_path, cores, threads, parent_path, aws_profile=None
) -> Tuple[Dict[str, str], list]:
    """
    Call the multi_thread_upload on `files` split across n
    logical CPU cores

    Parameters
    ----------
    files : list
        list of local files to upload
    bucket : str
        S3 bucket to upload to
    remote_path : str
        parent directory in bucket to upload to
    cores : int
        maximum number of logical CPU cores to split uploading across
    threads : int
        maximum number of threaded process to open per core
    parent_path : str
        path to parent of sequencing directory, will be removed from
        the file path for uploading to not form part of the remote path

    Returns
    -------
    dict
        mapping of local file to ETag ID of uploaded file
    list
        list of any files that failed to upload
    """
    log.info(
        "Beginning uploading %s files with %s cores to %s:%s",
        sum([len(x) for x in files]),
        cores,
        bucket,
        remote_path,
    )

    all_uploaded_files = {}
    all_failed_upload = []

    # Resolve credentials in the parent process so child processes
    # don't need to invoke the credential helper (which may not work
    # across process boundaries with signing helpers like Roles Anywhere)
    aws_credentials = _resolve_credentials(aws_profile=aws_profile)

    pool_executor = ProcessPoolExecutor(max_workers=cores)
    concurrent_jobs = _submit_to_pool(
        pool=pool_executor,
        func=multi_thread_upload,
        item_input="files",
        items=files,
        bucket=bucket,
        remote_path=remote_path,
        parent_path=parent_path,
        threads=threads,
        aws_credentials=aws_credentials,
    )

    expired_credentials_exc = None

    for future in as_completed(concurrent_jobs):
        # access returned output as each is returned in any order
        try:
            uploaded_files, failed_upload = future.result()

            all_uploaded_files = {**all_uploaded_files, **uploaded_files}
            all_failed_upload.extend(failed_upload)
        except Exception as exc:
            if is_expired_credentials_error(exc):
                # a child core aborted due to expired credentials; stop
                # consuming the remaining cores and surface the abort so
                # the caller can re-resolve credentials and retry. Cancel
                # any cores not yet started to avoid more doomed work.
                expired_credentials_exc = exc
                for pending in concurrent_jobs:
                    pending.cancel()
                break

            # catch any other errors that might get raised from the
            # ProcessPool but not handled in the child ThreadPool
            all_failed_upload.extend(concurrent_jobs[future])
            log.error(
                "Failed uploading %s files for a single ProcessPool due to"
                " an unhandled error: %s",
                len(concurrent_jobs[future]),
                exc,
            )

    # don't wait on in-flight cores when aborting on expired credentials,
    # they are using the same dead token and would only delay the retry
    pool_executor.shutdown(wait=expired_credentials_exc is None)

    if expired_credentials_exc is not None:
        log.error(
            "Aborting upload batch: AWS credentials expired. %s files"
            " uploaded before abort; batch will be retried with fresh"
            " credentials.",
            len(all_uploaded_files),
        )
        raise ExpiredCredentialsError(
            str(expired_credentials_exc)
        ) from expired_credentials_exc

    if all_uploaded_files:
        log.info(
            "Successfully uploaded %s files to %s:%s",
            len(all_uploaded_files.keys()),
            bucket,
            remote_path,
        )
    if all_failed_upload:
        log.error(
            "%s files failed to upload and will be logged for retrying",
            len(all_failed_upload),
        )

    return all_uploaded_files, all_failed_upload
