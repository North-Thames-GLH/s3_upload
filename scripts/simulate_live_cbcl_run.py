"""
Generate a fake Illumina run folder and incrementally create cbcl files.

This allows testing `s3_upload.py live_cbcl` behavior against a controlled
run directory where cycles appear over time.
"""

import argparse
from datetime import datetime
import os
from pathlib import Path
import time


def parse_args() -> argparse.Namespace:
    """
    Parse command line arguments.

    Returns
    -------
    argparse.Namespace
        Parsed command line arguments.
    """
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--run_dir",
        required=True,
        help="Path to fake run directory to create/update",
    )
    parser.add_argument(
        "--lanes",
        type=int,
        default=2,
        help="Number of lanes to simulate (default: 2)",
    )
    parser.add_argument(
        "--cycles",
        type=int,
        default=20,
        help="Total cycles to generate (default: 20)",
    )
    parser.add_argument(
        "--interval_seconds",
        type=float,
        default=2.0,
        help="Delay between cycle generation events (default: 2.0)",
    )
    parser.add_argument(
        "--cbcl_size_kb",
        type=int,
        default=4,
        help="Size of each fake cbcl file in KB (default: 64)",
    )
    parser.add_argument(
        "--cbcl_index",
        type=int,
        default=2,
        help="Index in cbcl filename LXXX_<index>.cbcl (default: 2)",
    )
    parser.add_argument(
        "--write_termination_file",
        action="store_true",
        default=False,
        help="Write CopyComplete.txt at the end of simulation",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        default=False,
        help="Allow running if run_dir already exists",
    )

    return parser.parse_args()


def write_file(file_path: Path, contents: str) -> None:
    """
    Write text contents to file.

    Parameters
    ----------
    file_path : pathlib.Path
        Path to file to write.
    contents : str
        Text contents to write.
    """
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(contents, encoding="utf-8")


def create_run_skeleton(run_dir: Path) -> None:
    """
    Create minimum run skeleton for uploader checks.

    Parameters
    ----------
    run_dir : pathlib.Path
        Run directory root.
    """
    write_file(
        run_dir / "RunInfo.xml",
        (
            "<?xml version='1.0'?>\n"
            "<RunInfo>\n"
            f"  <Run Id='{run_dir.name}' Number='1'>\n"
            "    <Flowcell>SIMULATED</Flowcell>\n"
            "  </Run>\n"
            "</RunInfo>\n"
        ),
    )

    write_file(
        run_dir / "SampleSheet.csv",
        (
            "[Header]\n"
            "IEMFileVersion,4\n"
            "Investigator Name,Sim\n"
            "Experiment Name,LiveCbclSimulation\n"
            "\n"
            "[Data]\n"
            "Sample_ID,Sample_Name,index,index2\n"
            "SIM_SAMPLE_01,SIM_SAMPLE_01,ACGTACGT,TGCATGCA\n"
        ),
    )


def make_cbcl_payload(size_kb: int, cycle: int, lane: int) -> bytes:
    """
    Create deterministic payload bytes for a fake cbcl file.

    Parameters
    ----------
    size_kb : int
        File size in KB.
    cycle : int
        Cycle number.
    lane : int
        Lane number.

    Returns
    -------
    bytes
        Byte payload of requested size.
    """
    size_bytes = int(size_kb) * 1024
    byte_value = (cycle + lane) % 256
    return bytes([byte_value]) * size_bytes


def generate_cycles(
    run_dir: Path,
    lanes: int,
    cycles: int,
    interval_seconds: float,
    cbcl_size_kb: int,
    cbcl_index: int,
) -> None:
    """
    Create cycle directories and cbcl files incrementally.

    Parameters
    ----------
    run_dir : pathlib.Path
        Run directory root.
    lanes : int
        Number of lanes to generate.
    cycles : int
        Number of cycles to generate.
    interval_seconds : float
        Delay between cycle generations.
    cbcl_size_kb : int
        Size per cbcl file in KB.
    cbcl_index : int
        cbcl filename index component.
    """
    basecalls_dir = run_dir / "Data" / "Intensities" / "BaseCalls"

    for cycle in range(1, cycles + 1):
        created_files = []
        cycle_dir_name = f"C{cycle}.1"

        for lane in range(1, lanes + 1):
            lane_id = f"L{lane:03d}"
            lane_dir = basecalls_dir / lane_id
            cycle_dir = lane_dir / cycle_dir_name
            cycle_dir.mkdir(parents=True, exist_ok=True)

            cbcl_file = cycle_dir / f"{lane_id}_{cbcl_index}.cbcl"
            cbcl_file.write_bytes(
                make_cbcl_payload(size_kb=cbcl_size_kb, cycle=cycle, lane=lane)
            )
            created_files.append(str(cbcl_file))

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(
            f"[{now}] Created cycle {cycle}/{cycles} with {len(created_files)}"
            f" cbcl files ({', '.join([Path(x).name for x in created_files])})"
        )

        if cycle < cycles:
            time.sleep(interval_seconds)


def main() -> None:
    args = parse_args()

    run_dir = Path(args.run_dir).absolute()
    if run_dir.exists() and not args.overwrite:
        raise FileExistsError(
            f"Run directory already exists: {run_dir}. "
            "Use --overwrite to continue."
        )

    run_dir.mkdir(parents=True, exist_ok=True)

    print(f"Creating/updating fake run at: {run_dir}")
    create_run_skeleton(run_dir)

    generate_cycles(
        run_dir=run_dir,
        lanes=args.lanes,
        cycles=args.cycles,
        interval_seconds=args.interval_seconds,
        cbcl_size_kb=args.cbcl_size_kb,
        cbcl_index=args.cbcl_index,
    )

    if args.write_termination_file:
        termination_file = run_dir / "CopyComplete.txt"
        write_file(termination_file, "Run complete\n")
        print(f"Wrote termination file: {termination_file}")

    print("Simulation complete.")


if __name__ == "__main__":
    main()
