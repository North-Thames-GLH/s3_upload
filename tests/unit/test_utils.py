from datetime import datetime, timedelta
import os
import re
from shutil import copyfile, rmtree
from uuid import uuid4
import unittest
from unittest.mock import patch

import pytest

from unit import TEST_DATA_DIR
from s3_upload.utils import io, utils


class TestCheckTerminationFileExists(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.test_run_dir = os.path.join(TEST_DATA_DIR, "test_run")
        os.makedirs(
            cls.test_run_dir,
            exist_ok=True,
        )

    @classmethod
    def tearDownClass(cls):
        rmtree(cls.test_run_dir)

    def test_complete_novaseq_run_returns_true(self):
        """
        Check complete NovaSeq runs correctly identified from
        CopyComplete.txt file in the run directory
        """
        termination_file = os.path.join(self.test_run_dir, "CopyComplete.txt")
        open(termination_file, "w").close()

        with self.subTest("Complete NovaSeq run identified"):
            self.assertTrue(
                utils.check_termination_file_exists(self.test_run_dir)
            )

        os.remove(termination_file)

    def test_complete_non_novaseq_run_returns_true(self):
        """
        Check other completed non-NovaSeq runs correctly identified from
        RTAComplete.txt or RTAComplete.xml files
        """
        for suffix in ["txt", "xml"]:
            termination_file = os.path.join(
                self.test_run_dir, f"RTAComplete.{suffix}"
            )

            open(termination_file, "w").close()

            with self.subTest(f"Checking RTAComplete.{suffix}"):
                self.assertTrue(
                    utils.check_termination_file_exists(self.test_run_dir)
                )

            os.remove(termination_file)

    def test_incomplete_sequencing_run_returns_false(self):
        """
        Check incomplete runs correctly identified
        """
        self.assertFalse(
            utils.check_termination_file_exists(self.test_run_dir)
        )


class TestCheckIsSequencingRunDir(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.test_run_dir = os.path.join(TEST_DATA_DIR, "test_run")
        os.makedirs(
            cls.test_run_dir,
            exist_ok=True,
        )

    @classmethod
    def tearDownClass(cls):
        rmtree(cls.test_run_dir)

    def test_non_sequencing_run_dir_returns_false(self):
        # no RunInfo.xml file present in test_data dir => not a run
        utils.check_is_sequencing_run_dir(self.test_run_dir)

    def test_check_sequencing_run_dir_returns_true(self):
        run_info_xml = os.path.join(self.test_run_dir, "RunInfo.xml")
        open(run_info_xml, "w").close()

        with self.subTest("RunInfo.xml exists"):
            utils.check_is_sequencing_run_dir(self.test_run_dir)

        os.remove(run_info_xml)


class TestCheckRunAgeWithinLimit(unittest.TestCase):
    def setUp(self):
        self.test_run_dir = os.path.join(TEST_DATA_DIR, "test_run")
        os.makedirs(
            self.test_run_dir,
            exist_ok=True,
        )

        open(os.path.join(self.test_run_dir, "RunInfo.xml"), mode="a").close()

    def tearDown(self):
        rmtree(self.test_run_dir)

    def test_run_newer_than_specified_max_age_returns_true(self):
        self.assertTrue(
            utils.check_run_age_within_limit(self.test_run_dir, max_age=24)
        )

    def test_run_older_than_max_age_returns_false(self):
        # update the modified time of RunInfo.xml to be older than the
        # given max age (1621091308 => 16:08:28 - 15/5/2021)
        os.utime(
            path=os.path.join(self.test_run_dir, "RunInfo.xml"),
            times=(1621091308, 1621091308),
        )

        self.assertFalse(
            utils.check_run_age_within_limit(self.test_run_dir, max_age=24)
        )


@patch("s3_upload.utils.utils.path.exists")
@patch("s3_upload.utils.utils.read_upload_state_log")
class TestCheckUploadState(unittest.TestCase):
    def test_new_returned_when_upload_log_does_not_exist(
        self, mock_log, mock_exists
    ):
        mock_exists.return_value = False

        upload_state, uploaded_files = utils.check_upload_state(
            run_dir="/some/path/to/test_run"
        )

        with self.subTest("correct path provided to check exists"):
            expected_log_path = (
                "/var/log/s3_upload/uploads/test_run.upload.log.json"
            )

            self.assertEqual(expected_log_path, mock_exists.call_args[0][0])

        with self.subTest("correct string returned"):
            self.assertEqual(upload_state, "new")

        with self.subTest("empty file list returned"):
            self.assertEqual(uploaded_files, [])

    def test_uploaded_returned_for_run_that_has_uploaded(
        self, mock_log, mock_exists
    ):
        mock_exists.return_value = True

        mock_log.return_value = {
            "run_id": "test_run",
            "run_path": "/some/path/to/test_run",
            "completed": True,
            "uploaded_files": {"file1.txt": "abc123", "file2.txt": "def456"},
        }

        upload_state, uploaded_files = utils.check_upload_state(
            run_dir="/some/path/to/test_run"
        )

        with self.subTest("uploaded run correctly returned"):
            self.assertEqual(upload_state, "uploaded")

        with self.subTest("correct file list returned"):
            self.assertEqual(uploaded_files, ["file1.txt", "file2.txt"])

    def test_partial_returned_for_run_that_has_not_fully_uploaded(
        self, mock_log, mock_exists
    ):
        mock_exists.return_value = True

        mock_log.return_value = {
            "run_id": "test_run",
            "run_path": "/some/path/to/test_run",
            "completed": False,
            "uploaded_files": {"file1.txt": "abc123", "file2.txt": "def456"},
        }

        upload_state, uploaded_files = utils.check_upload_state(
            run_dir="/some/path/to/test_run"
        )

        with self.subTest("partial correctly returned"):
            self.assertEqual(upload_state, "partial")

        with self.subTest("correct file list returned"):
            self.assertEqual(uploaded_files, ["file1.txt", "file2.txt"])


@patch("s3_upload.utils.utils.get_samplenames_from_samplesheet")
class TestCheckAllUploadableSamples(unittest.TestCase):
    def test_all_matching_samples_returns_true(self, mock_get_names):
        mock_get_names.return_value = [
            "sample_a_assay_1",
            "sample_b_assay_1",
            "sample_c_assay_1",
        ]

        self.assertTrue(utils.check_all_uploadable_samples([], "assay_1"))

    def test_all_matching_samples_returns_true_with_multiple_patterns(
        self, mock_get_names
    ):
        """
        Test if we have mixed runs and always want to upload against multiple
        patterns in a single regex
        """
        mock_get_names.return_value = [
            "sample_a_assay_1",
            "sample_b_assay_2",
            "sample_c_assay_3",
        ]

        self.assertTrue(
            utils.check_all_uploadable_samples([], "assay_1|assay_2|assay_3")
        )

    def test_no_matching_samples_returns_false(self, mock_get_names):
        mock_get_names.return_value = [
            "sample_a_assay_1",
            "sample_b_assay_1",
            "sample_c_assay_1",
        ]

        self.assertFalse(utils.check_all_uploadable_samples([], "assay_2"))

    def test_partial_matching_samples_returns_false(self, mock_get_names):
        mock_get_names.return_value = [
            "sample_a_assay_1",
            "sample_b_assay_2",
            "sample_c_assay_3",
        ]

        self.assertFalse(utils.check_all_uploadable_samples([], "assay_2"))

    def test_no_sample_names_parsed_logs_warning_and_returns_none(
        self,
        mock_get_names,
    ):
        """
        If no samplenames are parsed from the samplesheet with
        utils.get_samplenames_from_samplesheet we should log a warning
        and return None
        """
        mock_get_names.return_value = None

        with self.subTest("testing log warning"):
            with self.assertLogs("s3_upload", level="DEBUG") as log:
                utils.check_all_uploadable_samples([], "assay_2")

                self.assertIn(
                    "Failed parsing samplenames from samplesheet",
                    "".join(log.output),
                )

        with self.subTest("testing None returned"):
            uploadable = utils.check_all_uploadable_samples([], "assay_2")
            self.assertEqual(uploadable, None)


class TestGetRunsToUpload(unittest.TestCase):
    def test_no_sub_directories_in_provided_dir_does_not_raise_error(self):
        # make empty example sequencer output dir to monitor
        random_named_empty_dir = os.path.join(TEST_DATA_DIR, uuid4().hex)
        os.makedirs(
            random_named_empty_dir,
            exist_ok=True,
        )

        with self.subTest():
            to_upload, partial_upload = utils.get_runs_to_upload(
                [random_named_empty_dir]
            )
            self.assertTrue(to_upload == [] and partial_upload == {})

        rmtree(random_named_empty_dir)

    def test_non_sequencing_directories_are_skipped(self):
        sequencer_output_dir = os.path.join(TEST_DATA_DIR, uuid4().hex)
        not_a_sequencing_dir = os.path.join(sequencer_output_dir, "myRun")
        os.makedirs(
            not_a_sequencing_dir,
            exist_ok=True,
        )

        to_upload, partial_upload = utils.get_runs_to_upload(
            [sequencer_output_dir]
        )

        with self.subTest("testing outputs are empty"):
            self.assertTrue(to_upload == [] and partial_upload == {})

        with self.subTest("testing log message"):
            with self.assertLogs("s3_upload", level="DEBUG") as log:
                utils.get_runs_to_upload([sequencer_output_dir])

                expected_log_message = (
                    f"{not_a_sequencing_dir} is not a sequencing run and will"
                    " not be uploaded"
                )

                self.assertTrue(expected_log_message in "".join(log.output))

        rmtree(sequencer_output_dir)

    def test_incomplete_sequencing_runs_are_skipped(self):
        """
        Incomplete run determined from presence of just having RunInfo.xml
        file and no termination files
        """
        sequencer_output_dir = os.path.join(TEST_DATA_DIR, uuid4().hex)
        ongoing_run = os.path.join(
            sequencer_output_dir, "16102023_A01295_001_ABC123"
        )
        os.makedirs(
            ongoing_run,
            exist_ok=True,
        )
        open(os.path.join(ongoing_run, "RunInfo.xml"), "w").close()

        to_upload, partial_upload = utils.get_runs_to_upload(
            [sequencer_output_dir]
        )

        with self.subTest("testing outputs are empty"):
            self.assertTrue(to_upload == [] and partial_upload == {})

        with self.subTest("testing log message"):
            with self.assertLogs("s3_upload", level="DEBUG") as log:
                utils.get_runs_to_upload([sequencer_output_dir])

                expected_log_message = (
                    f"{ongoing_run} has not completed sequencing and will not"
                    " be uploaded"
                )

                self.assertTrue(expected_log_message in "".join(log.output))

        rmtree(sequencer_output_dir)

    def test_checking_run_age_against_max_age_is_correct(self):
        """
        Runs to not monitor are determined from the max_age value stored
        in the config, and mtime of the RunInfo.xml file.

        Test if they are older than the given age that they are correctly
        skipped, and if within the given max age they are correctly
        included.
        """
        sequencer_output_dir = os.path.join(TEST_DATA_DIR, uuid4().hex)
        run_dir = os.path.join(
            sequencer_output_dir, "16102023_A01295_001_ABC123"
        )
        os.makedirs(run_dir, exist_ok=True)
        open(os.path.join(run_dir, "RunInfo.xml"), "w").close()
        open(os.path.join(run_dir, "CopyComplete.txt"), "w").close()
        copyfile(
            os.path.join(TEST_DATA_DIR, "example_samplesheet.csv"),
            os.path.join(run_dir, "samplesheet.csv"),
        )

        # update the modified time of RunInfo.xml to be 100 hours old
        old_utime = int((datetime.now() - timedelta(hours=100)).timestamp())
        os.utime(
            path=os.path.join(run_dir, "RunInfo.xml"),
            times=(old_utime, old_utime),
        )

        # test that when the run is over max_age it is now selected for upload
        with self.subTest("testing outputs are empty for over max_age"):
            to_upload, partial_upload = utils.get_runs_to_upload(
                monitor_dirs=[sequencer_output_dir], max_age=96
            )
            self.assertTrue(to_upload == [] and partial_upload == {})

        with self.subTest("testing log message for over max_age"):
            with self.assertLogs("s3_upload", level="DEBUG") as log:
                utils.get_runs_to_upload(
                    monitor_dirs=[sequencer_output_dir], max_age=96
                )

                expected_log_message = (
                    f"{run_dir} older than maximum age (96 h) to monitor "
                    "for upload and will not be uploaded"
                )

                self.assertIn(expected_log_message, "".join(log.output))

        # test that when the run age is within the max_age it is
        # selected for upload
        with self.subTest("testing outputs are present when within max_age"):
            to_upload, partial_upload = utils.get_runs_to_upload(
                monitor_dirs=[sequencer_output_dir], max_age=128
            )
            self.assertTrue(to_upload == [run_dir] and partial_upload == {})

        rmtree(sequencer_output_dir)

    @patch("s3_upload.utils.utils.check_upload_state")
    @patch("s3_upload.utils.utils.check_all_uploadable_samples")
    @patch("s3_upload.utils.utils.read_samplesheet_from_run_directory")
    def test_complete_sequencing_runs_are_identified(
        self, mock_read, mock_uploadable, mock_state
    ):
        """
        Complete runs are determined from presence of CopyComplete.txt
        (for NovaSeqs) or RTAComplete.txt / RTAComplete.xml for other
        sequencers.

        Test that if these are present the run is picked up for upload
        (without checking for samplesheet or upload state)
        """
        mock_read.return_value = ["some_samplesheet_contents"]
        mock_uploadable.return_value = True
        mock_state.return_value = ("uploaded", [])

        sequencer_output_dir = os.path.join(TEST_DATA_DIR, uuid4().hex)
        complete_run = os.path.join(
            sequencer_output_dir, "16102023_A01295_001_ABC123"
        )
        os.makedirs(
            complete_run,
            exist_ok=True,
        )
        open(os.path.join(complete_run, "RunInfo.xml"), "w").close()

        termination_files = [
            "CopyComplete.txt",
            "RTAComplete.txt",
            "RTAComplete.xml",
        ]

        for termination_file in termination_files:
            open(os.path.join(complete_run, termination_file), "w").close()

            with self.subTest("testing log message"):
                with self.assertLogs("s3_upload", level="DEBUG") as log:
                    utils.get_runs_to_upload([sequencer_output_dir])

                    expected_log_message = (
                        "Termination file exists => sequencing complete"
                    )

                    self.assertIn(expected_log_message, "".join(log.output))

            os.remove(os.path.join(complete_run, termination_file))

        rmtree(sequencer_output_dir)

    @patch("s3_upload.utils.utils.read_samplesheet_from_run_directory")
    def test_invalid_samplesheet_logged_and_continues(self, mock_read):
        """
        Test when None is returned when reading samplesheet contents
        that the error is logged and continues
        """
        mock_read.return_value = None

        sequencer_output_dir = os.path.join(TEST_DATA_DIR, uuid4().hex)
        complete_run = os.path.join(
            sequencer_output_dir, "16102023_A01295_001_ABC123"
        )
        os.makedirs(
            complete_run,
            exist_ok=True,
        )
        open(os.path.join(complete_run, "RunInfo.xml"), "w").close()
        open(os.path.join(complete_run, "CopyComplete.txt"), "w").close()

        with self.subTest("testing log message"):
            with self.assertLogs("s3_upload", level="DEBUG") as log:
                utils.get_runs_to_upload([sequencer_output_dir])

                expected_log_message = (
                    f"Failed parsing samplesheet from {complete_run}, run will"
                    " not be uploaded"
                )

                self.assertIn(expected_log_message, "".join(log.output))

        rmtree(sequencer_output_dir)

    def test_runs_with_not_all_samples_matching_sample_pattern_skipped(self):
        """
        Test when the samples in the samplesheet don't match the given
        regex pattern that will come from the config that the run is
        skipped
        """
        sequencer_output_dir = os.path.join(TEST_DATA_DIR, uuid4().hex)
        complete_run = os.path.join(
            sequencer_output_dir, "16102023_A01295_001_ABC123"
        )
        os.makedirs(
            complete_run,
            exist_ok=True,
        )
        open(os.path.join(complete_run, "RunInfo.xml"), "w").close()
        open(os.path.join(complete_run, "CopyComplete.txt"), "w").close()
        copyfile(
            os.path.join(TEST_DATA_DIR, "example_samplesheet.csv"),
            os.path.join(complete_run, "samplesheet.csv"),
        )

        with self.subTest("testing log message"):
            with self.assertLogs("s3_upload", level="DEBUG") as log:
                to_upload, part_uploaded = utils.get_runs_to_upload(
                    monitor_dirs=[sequencer_output_dir],
                    sample_pattern="foo_bar",
                )

                expected_log_message = (
                    "One or more samples in samplesheet do not match the"
                    " sample pattern from the config [foo_bar], run will not"
                    " be uploaded"
                )

                self.assertIn(expected_log_message, "".join(log.output))

        with self.subTest("run not returned to upload"):
            self.assertEqual((to_upload, part_uploaded), ([], {}))

        rmtree(sequencer_output_dir)

    @patch("s3_upload.utils.utils.check_upload_state")
    @patch("s3_upload.utils.utils.check_all_uploadable_samples")
    @patch("s3_upload.utils.utils.read_samplesheet_from_run_directory")
    def test_runs_in_completed_upload_state_skipped(
        self, mock_read, mock_uploadable, mock_state
    ):
        """
        Test checking the upload state of completed upload runs skips
        adding the runs to be uploaded
        """
        mock_read.return_value = ["some_samplesheet_contents"]
        mock_uploadable.return_value = True
        mock_state.return_value = ("uploaded", [])

        sequencer_output_dir = os.path.join(TEST_DATA_DIR, uuid4().hex)
        complete_run = os.path.join(
            sequencer_output_dir, "16102023_A01295_001_ABC123"
        )
        os.makedirs(
            complete_run,
            exist_ok=True,
        )
        open(os.path.join(complete_run, "RunInfo.xml"), "w").close()
        open(os.path.join(complete_run, "CopyComplete.txt"), "w").close()

        with self.subTest("testing upload state: uploaded"):
            with self.assertLogs("s3_upload", level="DEBUG") as log:
                utils.get_runs_to_upload([sequencer_output_dir])

                expected_log_message = (
                    f"{complete_run} has completed uploading and will be"
                    " skipped"
                )

                self.assertIn(expected_log_message, "".join(log.output))

        with self.subTest("testing if uploaded runs are skipped"):
            upload_state = utils.get_runs_to_upload([sequencer_output_dir])

            self.assertEqual(upload_state, ([], {}))

        rmtree(sequencer_output_dir)

    @patch("s3_upload.utils.utils.check_upload_state")
    @patch("s3_upload.utils.utils.check_all_uploadable_samples")
    @patch("s3_upload.utils.utils.read_samplesheet_from_run_directory")
    def test_runs_in_partial_upload_state_are_picked_up_to_continue(
        self, mock_read, mock_uploadable, mock_state
    ):
        """
        Test checking the upload state of partially uploaded runs that
        these are returned
        """
        mock_read.return_value = ["some_samplesheet_contents"]
        mock_uploadable.return_value = True
        mock_state.return_value = ("partial", ["RunInfo.xml"])

        sequencer_output_dir = os.path.join(TEST_DATA_DIR, uuid4().hex)
        complete_run = os.path.join(
            sequencer_output_dir, "16102023_A01295_001_ABC123"
        )
        os.makedirs(
            complete_run,
            exist_ok=True,
        )
        open(os.path.join(complete_run, "RunInfo.xml"), "w").close()
        open(os.path.join(complete_run, "CopyComplete.txt"), "w").close()

        with self.subTest("testing upload state: partial"):
            with self.assertLogs("s3_upload", level="DEBUG") as log:
                utils.get_runs_to_upload([sequencer_output_dir])

                expected_log_message = (
                    f"{complete_run} has partially uploaded (1 files), will"
                    " continue uploading"
                )

                self.assertIn(expected_log_message, "".join(log.output))

        with self.subTest("testing returned upload state"):
            _, partial_upload = utils.get_runs_to_upload(
                [sequencer_output_dir]
            )

            expected_state = {complete_run: ["RunInfo.xml"]}

            self.assertDictEqual(partial_upload, expected_state)

        rmtree(sequencer_output_dir)

    @patch("s3_upload.utils.utils.check_upload_state")
    @patch("s3_upload.utils.utils.check_all_uploadable_samples")
    @patch("s3_upload.utils.utils.read_samplesheet_from_run_directory")
    def test_runs_in_not_uploaded_upload_state_are_picked_up_to_upload(
        self, mock_read, mock_uploadable, mock_state
    ):
        """
        Test checking the upload state of new runs that they are picked
        up to upload
        """
        mock_read.return_value = ["some_samplesheet_contents"]
        mock_uploadable.return_value = True
        mock_state.return_value = ("new", [])

        sequencer_output_dir = os.path.join(TEST_DATA_DIR, uuid4().hex)
        complete_run = os.path.join(
            sequencer_output_dir, "16102023_A01295_001_ABC123"
        )
        os.makedirs(
            complete_run,
            exist_ok=True,
        )
        open(os.path.join(complete_run, "RunInfo.xml"), "w").close()
        open(os.path.join(complete_run, "CopyComplete.txt"), "w").close()

        with self.subTest("testing upload state: new"):
            with self.assertLogs("s3_upload", level="DEBUG") as log:
                utils.get_runs_to_upload([sequencer_output_dir])

                expected_log_message = (
                    f"{complete_run} has not started uploading, to be uploaded"
                )

                self.assertIn(expected_log_message, "".join(log.output))

        with self.subTest("testing returned upload state"):
            uploadable, _ = utils.get_runs_to_upload([sequencer_output_dir])

            self.assertEqual(uploadable, [complete_run])

        rmtree(sequencer_output_dir)


class TestGetRunDirectoriesProvidedDirectly(unittest.TestCase):
    def _create_run_dir(self, root_dir):
        run_dir = os.path.join(root_dir, "16102023_A01295_001_ABC123")
        os.makedirs(run_dir, exist_ok=True)
        open(os.path.join(run_dir, "RunInfo.xml"), "w").close()
        copyfile(
            os.path.join(TEST_DATA_DIR, "example_samplesheet.csv"),
            os.path.join(run_dir, "SampleSheet.csv"),
        )
        return run_dir

    def test_monitor_mode_accepts_run_directory_itself(self):
        monitored_root = os.path.join(TEST_DATA_DIR, uuid4().hex)
        os.makedirs(monitored_root, exist_ok=True)
        run_dir = self._create_run_dir(monitored_root)
        open(os.path.join(run_dir, "CopyComplete.txt"), "w").close()

        to_upload, partial_upload = utils.get_runs_to_upload(
            [run_dir], max_age=96
        )

        self.assertEqual(to_upload, [run_dir])
        self.assertEqual(partial_upload, {})

        rmtree(monitored_root)

    def test_live_cbcl_mode_accepts_run_directory_itself(self):
        monitored_root = os.path.join(TEST_DATA_DIR, uuid4().hex)
        os.makedirs(monitored_root, exist_ok=True)
        run_dir = self._create_run_dir(monitored_root)

        live_runs = utils.get_runs_to_live_upload([run_dir])

        self.assertEqual(live_runs, [run_dir])

        rmtree(monitored_root)


class TestGetSequencingFileList(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        """
        Set up a reasonable approximation of a sequencing dir structure with
        files of differing sizes
        """
        cls.sequencing_dir_paths = [
            "Data/Intensities/BaseCalls/L001/C1.1",
            "Data/Intensities/BaseCalls/L002/C1.1",
            "Thumbnail_Images/L001/C1.1",
            "Thumbnail_Images/L002/C1.1",
            "InterOp/C1.1",
            "Logs",
        ]

        cls.sequencing_files = [
            ("Data/Intensities/BaseCalls/L001/C1.1/L001_2.cbcl", 232012345),
            ("Data/Intensities/BaseCalls/L002/C1.1/L002_2.cbcl", 232016170),
            ("Thumbnail_Images/L001/C1.1/s_1_2103_green.png", 69551),
            ("Thumbnail_Images/L002/C1.1/s_1_2103_red.png", 54132),
            ("InterOp/C1.1/BasecallingMetricsOut.bin", 13731),
            ("Logs/240927_A01295_0425_AHJWGFDRX5_Cycle0_Log.00.log", 5243517),
        ]

        cls.test_run_dir = os.path.join(TEST_DATA_DIR, "test_run")

        for sub_dir in cls.sequencing_dir_paths:
            os.makedirs(
                os.path.join(cls.test_run_dir, sub_dir),
                exist_ok=True,
            )

        for seq_file, size in cls.sequencing_files:
            with open(os.path.join(cls.test_run_dir, seq_file), "wb") as f:
                # create test file of given size without actually
                # writing any data to disk
                f.truncate(size)

    @classmethod
    def tearDownClass(cls):
        rmtree(cls.test_run_dir)

    def test_files_returned_in_sorted_order_by_file_size(self):
        expected_file_list = [
            "Data/Intensities/BaseCalls/L002/C1.1/L002_2.cbcl",
            "Data/Intensities/BaseCalls/L001/C1.1/L001_2.cbcl",
            "Logs/240927_A01295_0425_AHJWGFDRX5_Cycle0_Log.00.log",
            "Thumbnail_Images/L001/C1.1/s_1_2103_green.png",
            "Thumbnail_Images/L002/C1.1/s_1_2103_red.png",
            "InterOp/C1.1/BasecallingMetricsOut.bin",
        ]

        expected_file_list = [
            os.path.join(self.test_run_dir, x) for x in expected_file_list
        ]

        returned_file_list = utils.get_sequencing_file_list(
            seq_dir=self.test_run_dir
        )

        self.assertEqual(returned_file_list, expected_file_list)

    def test_empty_directories_ignored_and_only_files_returned(self):
        empty_dir = os.path.join(self.test_run_dir, "empty_dir")
        os.makedirs(
            empty_dir,
            exist_ok=True,
        )

        with self.subTest("empty directory ignored"):
            returned_file_list = utils.get_sequencing_file_list(
                seq_dir=self.test_run_dir
            )
            # just test we get back the same files and ignore their ordering
            self.assertEqual(
                sorted(returned_file_list),
                sorted(
                    os.path.join(self.test_run_dir, x[0])
                    for x in self.sequencing_files
                ),
            )

        rmtree(empty_dir)

    def test_exclude_patterns_removes_matching_files(self):
        """
        Test that both patterns of filenames and / or directory names
        correctly excludes expected files from the returned file list
        """
        exclude_patterns_matched_files = [
            (
                [".*png$"],
                [
                    "Data/Intensities/BaseCalls/L002/C1.1/L002_2.cbcl",
                    "Data/Intensities/BaseCalls/L001/C1.1/L001_2.cbcl",
                    "Logs/240927_A01295_0425_AHJWGFDRX5_Cycle0_Log.00.log",
                    "InterOp/C1.1/BasecallingMetricsOut.bin",
                ],
            ),
            (
                [".*log$"],
                [
                    "Data/Intensities/BaseCalls/L002/C1.1/L002_2.cbcl",
                    "Data/Intensities/BaseCalls/L001/C1.1/L001_2.cbcl",
                    "Thumbnail_Images/L001/C1.1/s_1_2103_green.png",
                    "Thumbnail_Images/L002/C1.1/s_1_2103_red.png",
                    "InterOp/C1.1/BasecallingMetricsOut.bin",
                ],
            ),
            (
                [".*png$", ".*log$"],
                [
                    "Data/Intensities/BaseCalls/L002/C1.1/L002_2.cbcl",
                    "Data/Intensities/BaseCalls/L001/C1.1/L001_2.cbcl",
                    "InterOp/C1.1/BasecallingMetricsOut.bin",
                ],
            ),
            (
                ["Logs/"],
                [
                    "Data/Intensities/BaseCalls/L002/C1.1/L002_2.cbcl",
                    "Data/Intensities/BaseCalls/L001/C1.1/L001_2.cbcl",
                    "Thumbnail_Images/L001/C1.1/s_1_2103_green.png",
                    "Thumbnail_Images/L002/C1.1/s_1_2103_red.png",
                    "InterOp/C1.1/BasecallingMetricsOut.bin",
                ],
            ),
            (
                ["Thumbnail_Images/"],
                [
                    "Data/Intensities/BaseCalls/L002/C1.1/L002_2.cbcl",
                    "Data/Intensities/BaseCalls/L001/C1.1/L001_2.cbcl",
                    "Logs/240927_A01295_0425_AHJWGFDRX5_Cycle0_Log.00.log",
                    "InterOp/C1.1/BasecallingMetricsOut.bin",
                ],
            ),
        ]

        for patterns, expected_files in exclude_patterns_matched_files:
            with self.subTest("files correctly excluded by pattern(s)"):
                returned_file_list = utils.get_sequencing_file_list(
                    seq_dir=self.test_run_dir, exclude_patterns=patterns
                )

                expected_files = [
                    os.path.join(self.test_run_dir, x) for x in expected_files
                ]

                self.assertEqual(
                    sorted(returned_file_list), sorted(expected_files)
                )

    def test_file_not_found_error_raised_on_invalid_directory_provided(self):
        invalid_dir = os.path.join(TEST_DATA_DIR, uuid4().hex)
        expected_error = f"Provided directory does not exist: {invalid_dir}"

        with pytest.raises(FileNotFoundError, match=re.escape(expected_error)):
            utils.get_sequencing_file_list(seq_dir=invalid_dir)


class TestGetSamplenamesFromSamplesheet(unittest.TestCase):
    samplesheet_contents = [
        "[Header],,,,,,",
        "IEMFileVersion,5,,,,,",
        "Investigator Name,,,,,,",
        "Experiment Name,,,,,,",
        "Date,28/02/2024,,,,,",
        "Workflow,GenerateFASTQ,,,,,",
        "Application,NovaSeq FASTQ Only,,,,,",
        "Instrument Type,NovaSeq,,,,,",
        "Assay,TruSeq,,,,,",
        "Index Adapters,96_UDI_PN101308,,,,,",
        ",,,,,,",
        "[Reads],,,,,,",
        "151,,,,,,",
        "151,,,,,,",
        ",,,,,,",
        "[Settings],,,,,,",
        "Adapter,AGATCGGAAGAGCACACGTCTGAACTCCAGTCA,,,,,",
        "AdapterRead2,AGATCGGAAGAGCGTCGTGTAGGGAAAGAGTGT,,,,,",
        ",,,,,,",
        "[Data],,,,,,",
        "Sample_ID,Sample_Name,Sample_Plate,Sample_Well,Index_Plate_Well,index,index2",
        "sample_a,sample_a,820,A7,A07,ACCAATCTCG,AGTGCCGGAA",
        "sample_b,sample_b,820,B7,B07,GTCGTGACAC,AGCCATACAA",
        "sample_c,sample_c,820,C7,C07,TCTCTAGTCG,AATCGATCCA",
        "sample_d,sample_d,820,D7,D07,ATTACGGTTG,GGTGATTCCG",
        "sample_e,sample_e,820,E7,E07,CGGTAAGTAA,TAGATAGCTC",
    ]

    def test_sample_names_correctly_returned(self):
        parsed_names = utils.get_samplenames_from_samplesheet(
            contents=self.samplesheet_contents
        )

        expected_names = [
            "sample_a",
            "sample_b",
            "sample_c",
            "sample_d",
            "sample_e",
        ]

        self.assertEqual(parsed_names, expected_names)

    def test_none_returned_if_sample_id_line_missing_from_samplesheet(self):
        contents = self.samplesheet_contents.copy()
        contents = [x for x in contents if not x.startswith("Sample_ID")]

        parsed_names = utils.get_samplenames_from_samplesheet(
            contents=contents
        )

        self.assertEqual(parsed_names, None)

    def test_none_returned_if_multiple_sample_id_lines_present(self):
        contents = self.samplesheet_contents.copy()
        contents.append(
            "Sample_ID,Sample_Name,Sample_Plate,Sample_Well,"
            "Index_Plate_Well,index,index2"
        )

        parsed_names = utils.get_samplenames_from_samplesheet(
            contents=contents
        )

        self.assertEqual(parsed_names, None)


class TestFilterUploadedFiles(unittest.TestCase):
    def test_correct_file_list_returned(self):
        local_files = ["file1.txt", "file2.txt", "file3.txt"]
        uploaded_files = ["file1.txt", "file2.txt"]

        to_upload = utils.filter_uploaded_files(
            local_files=local_files, uploaded_files=uploaded_files
        )

        self.assertEqual(to_upload, ["file3.txt"])


class TestSplitFileListByCores(unittest.TestCase):
    items = [1, 2, 3, 4, 5, 6, 7, 8, 100, 110, 120, 130, 140, 150, 160, 170]

    def test_list_split_as_expected(self):

        returned_split_list = utils.split_file_list_by_cores(
            files=self.items, n=4
        )

        expected_list = [
            [1, 5, 100, 140],
            [2, 6, 110, 150],
            [3, 7, 120, 160],
            [4, 8, 130, 170],
        ]

        self.assertEqual(returned_split_list, expected_list)

    def test_correct_return_when_file_length_not_exact_multiple_of_n(self):
        returned_split_list = utils.split_file_list_by_cores(
            files=self.items, n=3
        )

        expected_list = [
            [1, 4, 7, 110, 140, 170],
            [2, 5, 8, 120, 150],
            [3, 6, 100, 130, 160],
        ]

        self.assertEqual(returned_split_list, expected_list)

    def test_error_not_raised_when_n_greater_than_total_files(self):
        returned_split_list = utils.split_file_list_by_cores(files=[1, 2], n=3)

        self.assertEqual([[1], [2]], returned_split_list)

    def test_empty_file_list_returns_empty_list(self):
        returned_split_list = utils.split_file_list_by_cores(files=[], n=2)

        self.assertEqual([], returned_split_list)


class TestParseConfig(unittest.TestCase):
    def test_contents_of_config_returned_as_dict(self):
        config_contents = io.read_config(
            os.path.join(TEST_DATA_DIR, "test_config.json")
        )

        expected_contents = {
            "max_cores": 4,
            "max_threads": 8,
            "log_level": "INFO",
            "log_dir": "/var/log/s3_upload",
            "monitor": [
                {
                    "monitored_directories": ["/absolute/path/to/sequencer_1"],
                    "bucket": "bucket_A",
                    "remote_path": "/",
                },
            ],
        }

        self.assertEqual(expected_contents, config_contents)


class TestVerifyArgs(unittest.TestCase):
    # TODO - add tests
    pass


class TestVerifyConfig(unittest.TestCase):
    def test_valid_config_passes(self):
        valid_config = {
            "max_cores": 4,
            "max_threads": 8,
            "max_age": 96,
            "log_level": "DEBUG",
            "log_dir": "/var/log/s3_upload",
            "monitor": [
                {
                    "monitored_directories": [
                        "/absolute/path/to/sequencer_1",
                        "/absolute/path/to/sequencer_2",
                    ],
                    "bucket": "bucket_A",
                    "remote_path": "/",
                },
                {
                    "monitored_directories": [
                        "/absolute/path/to/sequencer_3",
                    ],
                    "bucket": "bucket_B",
                    "remote_path": "/sequencer_3_runs",
                },
            ],
        }

        utils.verify_config(valid_config)

    def test_invalid_config_raises_runtime_error_with_expected_errors(self):
        invalid_config = {
            "max_cores": "4",
            "max_threads": "8",
            "max_age": "96",
            "log_level": "BLARG",
            "monitor": [
                {
                    "bucket": "bucket_A",
                },
                {
                    "monitored_directories": [
                        "/absolute/path/to/sequencer_3",
                    ],
                    "bucket": 1,
                    "remote_path": "/sequencer_3_runs",
                    "sample_regex": "[assay_1",
                },
            ],
        }

        expected_errors = (
            "8 errors found in config:\n\tmax_cores must be an"
            " integer\n\tmax_threads must be an integer\n\tmax_age must be a"
            " positive integer\n\tGiven log level is not valid:"
            " BLARG\n\trequired parameter monitored_directories missing from"
            " monitor section 0\n\trequired parameter remote_path missing from"
            " monitor section 0\n\tbucket not of expected type from monitor"
            " section 1. Expected: <class 'str'> | Found <class"
            " 'int'>\n\tInvalid regex pattern provided in monitor section 1:"
            " [assay_1"
        )

        with pytest.raises(RuntimeError, match=re.escape(expected_errors)):
            utils.verify_config(invalid_config)

    def test_missing_monitor_section_raises_runtime_error(self):
        invalid_config = {
            "max_cores": 4,
            "max_threads": 8,
            "log_dir": "/var/log/s3_upload",
        }

        expected_error = (
            "1 errors found in config:\n\trequired parameter monitor not"
            " defined"
        )

        with pytest.raises(RuntimeError, match=re.escape(expected_error)):
            utils.verify_config(invalid_config)


class TestSizeofFmt(unittest.TestCase):
    def test_expected_value_and_suffix_returned(self):
        bytes_to_formatted = [
            (0, "0.00B"),
            (1, "1.00B"),
            (1050, "1.03KB"),
            (1234567, "1.18MB"),
            (123456789, "117.74MB"),
            (1234567890, "1.15GB"),
            (112233445566, "104.53GB"),
            (11223344556677, "10.21TB"),
            (112233445566778899, "99.68PB"),
            (11223344556677889900, "9.73EB"),
            (111222333444555666777888999, "92.00YiB"),
        ]

        for byte in bytes_to_formatted:
            with self.subTest():
                self.assertEqual(utils.sizeof_fmt(byte[0]), byte[1])


class TestComputeMissingFiles(unittest.TestCase):
    """
    Tests for the missing-file diff used by sync mode. S3 is treated as
    the source of truth: a local file counts as already uploaded only
    when its derived key is present AND its local byte size matches the
    remote object size. No local state log is consulted.
    """

    @classmethod
    def setUpClass(cls):
        """
        Create a small run directory with files of known byte sizes so
        that compute_missing_files' os.stat size check runs against real
        files, following the f.truncate(size) convention used elsewhere.
        """
        cls.parent_path = os.path.join(TEST_DATA_DIR, uuid4().hex)
        cls.run_dir = os.path.join(cls.parent_path, "run1")

        # relative path -> byte size, covering a root-level file and a
        # nested cbcl file
        cls.local_files_and_sizes = {
            "SampleSheet.csv": 100,
            "Data/Intensities/BaseCalls/L001/C1.1/L001_1.cbcl": 5000,
        }

        cls.local_files = []
        for rel_path, size in cls.local_files_and_sizes.items():
            full_path = os.path.join(cls.run_dir, rel_path)
            os.makedirs(os.path.dirname(full_path), exist_ok=True)
            with open(full_path, "wb") as f:
                f.truncate(size)
            cls.local_files.append(full_path)

        cls.remote_path = "/bucket_dir1/"

        # the S3 keys these local files would occupy, derived via the
        # single source of truth helper so the diff keys line up
        cls.keys = {
            f: utils.derive_remote_key(
                local_file=f,
                parent_path=cls.parent_path,
                remote_path=cls.remote_path,
            )
            for f in cls.local_files
        }

    @classmethod
    def tearDownClass(cls):
        rmtree(cls.parent_path)

    def _remote_all_present(self):
        """Remote listing where every local file is present with matching size"""
        return {
            self.keys[f]: self.local_files_and_sizes[
                os.path.relpath(f, self.run_dir)
            ]
            for f in self.local_files
        }

    def test_file_included_when_key_absent_from_remote(self):
        """key absent => file is missing (Requirement 4.1)"""
        # start from a fully-present listing then drop one key
        remote = self._remote_all_present()
        absent_file = self.local_files[0]
        del remote[self.keys[absent_file]]

        missing = utils.compute_missing_files(
            local_files=self.local_files,
            remote_objects=remote,
            parent_path=self.parent_path,
            remote_path=self.remote_path,
        )

        self.assertEqual(missing, [absent_file])

    def test_file_included_when_key_present_but_size_differs(self):
        """key present + size mismatch => file is missing (Requirement 4.2)"""
        remote = self._remote_all_present()
        mismatched_file = self.local_files[1]
        # set the remote size to something other than the local size
        remote[self.keys[mismatched_file]] = (
            self.local_files_and_sizes[
                os.path.relpath(mismatched_file, self.run_dir)
            ]
            + 1
        )

        missing = utils.compute_missing_files(
            local_files=self.local_files,
            remote_objects=remote,
            parent_path=self.parent_path,
            remote_path=self.remote_path,
        )

        self.assertEqual(missing, [mismatched_file])

    def test_file_excluded_when_key_present_and_size_equal(self):
        """key present + size equal => file already uploaded (Requirement 4.3)"""
        remote = self._remote_all_present()

        missing = utils.compute_missing_files(
            local_files=self.local_files,
            remote_objects=remote,
            parent_path=self.parent_path,
            remote_path=self.remote_path,
        )

        self.assertEqual(missing, [])

    def test_all_files_included_when_remote_is_empty(self):
        """empty remote + non-empty local => all missing (Requirement 3.5)"""
        missing = utils.compute_missing_files(
            local_files=self.local_files,
            remote_objects={},
            parent_path=self.parent_path,
            remote_path=self.remote_path,
        )

        self.assertEqual(sorted(missing), sorted(self.local_files))

    def test_diff_is_independent_of_state_log_entries_in_remote_map(self):
        """
        The missing set is a pure function of the local files and the
        live remote listing; it must be identical whether or not entries
        resembling state-log files (.upload.log.json / .sync.log.json /
        .live_cbcl.upload.log.json) exist in the remote object map
        (Property 3 / Requirement 3.3).
        """
        # remote listing where one file is present and one is absent
        remote_without_logs = self._remote_all_present()
        absent_file = self.local_files[0]
        del remote_without_logs[self.keys[absent_file]]

        # same listing but polluted with state-log-like keys that must
        # not influence the diff (none of them are derived local keys)
        run_id = os.path.basename(self.run_dir)
        remote_with_logs = dict(remote_without_logs)
        remote_with_logs.update(
            {
                "bucket_dir1/{}.upload.log.json".format(run_id): 12,
                "bucket_dir1/{}.sync.log.json".format(run_id): 34,
                "bucket_dir1/{}.live_cbcl.upload.log.json".format(run_id): 56,
            }
        )

        missing_without_logs = utils.compute_missing_files(
            local_files=self.local_files,
            remote_objects=remote_without_logs,
            parent_path=self.parent_path,
            remote_path=self.remote_path,
        )

        missing_with_logs = utils.compute_missing_files(
            local_files=self.local_files,
            remote_objects=remote_with_logs,
            parent_path=self.parent_path,
            remote_path=self.remote_path,
        )

        with self.subTest("diff unaffected by state-log entries"):
            self.assertEqual(missing_without_logs, missing_with_logs)

        with self.subTest("diff still reflects the genuinely missing file"):
            self.assertEqual(missing_with_logs, [absent_file])
