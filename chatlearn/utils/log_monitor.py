# Copyright 2024 Alibaba Group Holding Limited. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================
"""This file is modified based on ray/_private/log_monitor.py."""

import argparse
import errno
import logging
import logging.handlers
import os
import platform
import re
import shutil
import time
import traceback
from typing import Callable, List, Set

import ray
import ray.util.state
from ray._private import ray_constants
from ray._private import services
import ray._private.utils
from ray._private.ray_logging import setup_component_logger
from ray._private.worker import print_to_stdstream
from ray.util.scheduling_strategies import NodeAffinitySchedulingStrategy
from chatlearn.utils.utils import get_ray_status

# pylint: disable=unexpected-keyword-arg
# Logger for this module. It should be configured at the entry point
# into the program using Ray. Ray provides a default configuration at
# entry/init points.
logger = logging.getLogger(__name__)

# The groups are job id, and pid.
WORKER_LOG_PATTERN = re.compile(r".*worker.*-([0-9a-f]+)-(\d+)")
# The groups are job id.
RUNTIME_ENV_SETUP_PATTERN = re.compile(r".*runtime_env_setup-(\d+).log")
# Log name update interval under pressure.
# We need it because log name update is CPU intensive and uses 100%
# of cpu when there are many log files.
MIN_LOG_NAME_UPDATE_INTERVAL_S = 0.5
MAX_LOG_NAME_UPDATE_INTERVAL_S = 5
LOG_NAME_UPDATE_INTERVAL_S = float(os.getenv("LOG_NAME_UPDATE_INTERVAL_S", "0.5"))
# Once there are more files than this threshold,
# log monitor start giving backpressure to lower cpu usages.
RAY_LOG_MONITOR_MANY_FILES_THRESHOLD = int(
    os.getenv("RAY_LOG_MONITOR_MANY_FILES_THRESHOLD", "1000")
)
RAY_RUNTIME_ENV_LOG_TO_DRIVER_ENABLED = int(
    os.getenv("RAY_RUNTIME_ENV_LOG_TO_DRIVER_ENABLED", "0")
)

@ray.remote
class LogActor:
    """LogActor"""

    def list_logs(self, node_id, retry=3):
        logs = {}
        msg = None
        for _ in range(retry):
            try:
                return ray.util.state.list_logs(node_id=node_id)
            except Exception as e: # pylint: disable=redefined-outer-name
                msg = e
                time.sleep(1)
        print(f"fail to list_logs, error msg: {msg}, retry ...", flush=True)
        return logs


class LogFileInfo:
    """LogFileInfo"""
    def __init__(
        self,
        filename=None,
        size_when_last_opened=None,
        file_position=None,
        file_handle=None,
        is_err_file=False,
        job_id=None,
        worker_pid=None,
    ):
        assert (
            filename is not None
            and size_when_last_opened is not None
            and file_position is not None
        )
        self.filename = filename
        self.size_when_last_opened = size_when_last_opened
        self.file_position = file_position
        self.file_handle = file_handle
        self.is_err_file = is_err_file
        self.job_id = job_id
        self.worker_pid = worker_pid
        self.actor_name = None
        self.task_name = None

    def reopen_if_necessary(self):
        """Check if the file's inode has changed and reopen it if necessary.
        There are a variety of reasons what we would logically consider a file
        would have different inodes, such as log rotation or file syncing
        semantics.
        """
        try:
            open_inode = None
            if self.file_handle and not self.file_handle.closed:
                open_inode = os.fstat(self.file_handle.fileno()).st_ino

            new_inode = os.stat(self.filename).st_ino
            if open_inode != new_inode:
                self.file_handle = open(self.filename, "rb") # pylint: disable=consider-using-with
                self.file_handle.seek(self.file_position)
        except Exception:
            logger.debug(f"file no longer exists, skip re-opening of {self.filename}")

    def __repr__(self):
        return (
            "FileInfo(\n"
            f"\tfilename: {self.filename}\n"
            f"\tsize_when_last_opened: {self.size_when_last_opened}\n"
            f"\tfile_position: {self.file_position}\n"
            f"\tfile_handle: {self.file_handle}\n"
            f"\tis_err_file: {self.is_err_file}\n"
            f"\tjob_id: {self.job_id}\n"
            f"\tworker_pid: {self.worker_pid}\n"
            f"\tactor_name: {self.actor_name}\n"
            f"\ttask_name: {self.task_name}\n"
            ")"
        )


class LogMonitor:
    """A monitor process for monitoring Ray log files.

    This class maintains a list of open files and a list of closed log files. We
    can't simply leave all files open because we'll run out of file
    descriptors.

    The "run" method of this class will cycle between doing several things:
    1. First, it will check if any new files have appeared in the log
       directory. If so, they will be added to the list of closed files.
    2. Then, if we are unable to open any new files, we will close all of the
       files.
    3. Then, we will open as many closed files as we can that may have new
       lines (judged by an increase in file size since the last time the file
       was opened).
    4. Then we will loop through the open files and see if there are any new
       lines in the file. If so, we will publish them to Ray pubsub.

    Attributes:
        ip: The hostname of this machine, for grouping log messages.
        logs_dir: The directory that the log files are in.
        log_filenames: This is the set of filenames of all files in
            open_file_infos and closed_file_infos.
        open_file_infos (list[LogFileInfo]): Info for all of the open files.
        closed_file_infos (list[LogFileInfo]): Info for all of the closed
            files.
        can_open_more_files: True if we can still open more files and
            false otherwise.
        max_files_open: The maximum number of files that can be open.
    """

    def __init__(
        self,
        logs_dir,
        is_proc_alive_fn: Callable[[int], bool],
        log_actor=None,
        max_files_open: int = ray_constants.LOG_MONITOR_MAX_OPEN_FILES,
    ):
        """Initialize the log monitor object."""
        ray_context = ray.get_runtime_context()
        self.node_id = ray_context.get_node_id()
        self.ip: str = services.get_node_ip_address()
        self.logs_dir: str = logs_dir
        self.log_filenames: Set[str] = set()
        self.open_file_infos: List[LogFileInfo] = []
        self.closed_file_infos: List[LogFileInfo] = []
        self.can_open_more_files: bool = True
        self.max_files_open: int = max_files_open
        self.is_proc_alive_fn: Callable[[int], bool] = is_proc_alive_fn
        self.log_actor = log_actor
        self.need_quit = False
        self.log_files = []

        logger.info(
            f"Starting log monitor with [max open files={max_files_open}]."
        )


    def _close_all_files(self):
        """Close all open files (so that we can open more)."""
        while len(self.open_file_infos) > 0:
            file_info = self.open_file_infos.pop(0)
            file_info.file_handle.close()
            file_info.file_handle = None

            proc_alive = True
            # Test if the worker process that generated the log file
            # is still alive. Only applies to worker processes.
            # For all other system components, we always assume they are alive.
            if (
                file_info.worker_pid != "raylet"
                and file_info.worker_pid != "gcs_server"
                and file_info.worker_pid != "autoscaler"
                and file_info.worker_pid != "runtime_env"
                and file_info.worker_pid is not None
            ):
                assert not isinstance(file_info.worker_pid, str), (
                    "PID should be an int type. " f"Given PID: {file_info.worker_pid}."
                )
                proc_alive = self.is_proc_alive_fn(file_info.worker_pid)
                if not proc_alive:
                    # The process is not alive any more, so move the log file
                    # out of the log directory so glob.glob will not be slowed
                    # by it.
                    target = os.path.join(
                        self.logs_dir, "old", os.path.basename(file_info.filename)
                    )
                    try:
                        shutil.move(file_info.filename, target)
                    except (IOError, OSError) as e: # pylint: disable=redefined-outer-name
                        if e.errno == errno.ENOENT:
                            logger.warning(
                                f"Warning: The file {file_info.filename} was not found."
                            )
                        else:
                            raise e

            if proc_alive:
                self.closed_file_infos.append(file_info)

        self.can_open_more_files = True


    def has_log_file_list_changed(self, logs):
        if len(self.log_files) != len(logs):
            return True
        for log in logs:
            if log not in self.log_files:
                return True
        return False

    def update_log_filenames(self):
        """Update the list of log files to monitor."""
        monitor_log_paths = []
        # avoid the error raised by ray:
        # The core worker has already been shutdown.
        cluster_state, msg = get_ray_status()
        if cluster_state:
            # unknown msg
            if msg is not None:
                return
            try:
                # TODO: try to reduce this frequency
                logs = ray.get(self.log_actor.list_logs.remote(self.node_id))
                global LOG_NAME_UPDATE_INTERVAL_S
                if not self.has_log_file_list_changed(logs):
                    LOG_NAME_UPDATE_INTERVAL_S = min(LOG_NAME_UPDATE_INTERVAL_S * 2, MAX_LOG_NAME_UPDATE_INTERVAL_S)
                else:
                    LOG_NAME_UPDATE_INTERVAL_S = max(LOG_NAME_UPDATE_INTERVAL_S / 2, MIN_LOG_NAME_UPDATE_INTERVAL_S)
                self.log_files = logs
            except (ray.exceptions.RayActorError, AttributeError):
                # AttributeError: 'NoneType' object has no attribute 'address_info'
                logger.info("log_actor exit, quit the LogMonitor")
                self.need_quit = True
                return
        else:
            self.need_quit = True
            return

        for key in ['worker_out', 'worker_err']:
            monitor_log_paths += [f"{self.logs_dir}/{fn}" for fn in logs.get(key, [])]
        for key in ['gcs_server', 'raylet']:
            # record only err
            for fn in logs.get(key, []):
                if fn.endswith('.err'):
                    monitor_log_paths.append(f"{self.logs_dir}/{fn}")

        for file_path in monitor_log_paths:
            if os.path.isfile(file_path) and file_path not in self.log_filenames:
                worker_match = WORKER_LOG_PATTERN.match(file_path)
                if worker_match:
                    worker_pid = int(worker_match.group(2))
                else:
                    worker_pid = None
                job_id = None

                # Perform existence check first because most file will not be
                # including runtime_env. This saves some cpu cycle.
                if "runtime_env" in file_path:
                    runtime_env_job_match = RUNTIME_ENV_SETUP_PATTERN.match(file_path)
                    if runtime_env_job_match:
                        job_id = runtime_env_job_match.group(1)

                is_err_file = file_path.endswith("err")

                self.log_filenames.add(file_path)
                self.closed_file_infos.append(
                    LogFileInfo(
                        filename=file_path,
                        size_when_last_opened=0,
                        file_position=0,
                        file_handle=None,
                        is_err_file=is_err_file,
                        job_id=job_id,
                        worker_pid=worker_pid,
                    )
                )

    def open_closed_files(self):
        """Open some closed files if they may have new lines.

        Opening more files may require us to close some of the already open
        files.
        """
        if not self.can_open_more_files:
            # If we can't open any more files. Close all of the files.
            self._close_all_files()

        files_with_no_updates = []
        while len(self.closed_file_infos) > 0:
            if len(self.open_file_infos) >= self.max_files_open:
                self.can_open_more_files = False
                break

            file_info = self.closed_file_infos.pop(0)
            assert file_info.file_handle is None
            # Get the file size to see if it has gotten bigger since we last
            # opened it.
            try:
                file_size = os.path.getsize(file_info.filename)
            except (IOError, OSError) as e: # pylint: disable=redefined-outer-name
                # Catch "file not found" errors.
                if e.errno == errno.ENOENT:
                    logger.warning(
                        f"Warning: The file {file_info.filename} was not found."
                    )
                    self.log_filenames.remove(file_info.filename)
                    continue
                raise e

            # If some new lines have been added to this file, try to reopen the
            # file.
            if file_size > file_info.size_when_last_opened:
                try:
                    f = open(file_info.filename, "rb") # pylint: disable=consider-using-with
                except (IOError, OSError) as e:
                    if e.errno == errno.ENOENT:
                        logger.warning(
                            f"Warning: The file {file_info.filename} was not found."
                        )
                        self.log_filenames.remove(file_info.filename)
                        continue
                    raise e

                f.seek(file_info.file_position)
                file_info.size_when_last_opened = file_size
                file_info.file_handle = f
                self.open_file_infos.append(file_info)
            else:
                files_with_no_updates.append(file_info)

        if len(self.open_file_infos) >= self.max_files_open:
            self.can_open_more_files = False
        # Add the files with no changes back to the list of closed files.
        self.closed_file_infos += files_with_no_updates

    def check_log_files_and_publish_updates(self):
        """Gets updates to the log files and publishes them.

        Returns:
            True if anything was published and false otherwise.
        """
        anything_published = False
        lines_to_publish = []
        if ray.__version__ < "2.11.0":
            raise ValueError("Just support ray version >= 2.11.0")

        def flush():
            nonlocal lines_to_publish
            nonlocal anything_published
            if len(lines_to_publish) > 0:
                data = {
                    "ip": self.ip,
                    "pid": file_info.worker_pid,
                    "job": file_info.job_id,
                    "is_err": file_info.is_err_file,
                    "lines": lines_to_publish,
                    "actor_name": file_info.actor_name,
                    "task_name": file_info.task_name,
                }
                if ray.__version__ >= "2.38.0":
                    print_to_stdstream(data, ignore_prefix=False)
                else:
                    print_to_stdstream(data)
                anything_published = True
                lines_to_publish = []

        for file_info in self.open_file_infos:
            assert not file_info.file_handle.closed
            file_info.reopen_if_necessary()

            max_num_lines_to_read = ray_constants.LOG_MONITOR_NUM_LINES_TO_READ
            for _ in range(max_num_lines_to_read):
                try:
                    next_line = file_info.file_handle.readline()
                    # Replace any characters not in UTF-8 with
                    # a replacement character, see
                    # https://stackoverflow.com/a/38565489/10891801
                    next_line = next_line.decode("utf-8", "replace")
                    if next_line == "":
                        break
                    next_line = next_line.rstrip("\r\n")
                    if next_line.startswith(ray_constants.LOG_PREFIX_ACTOR_NAME):
                        flush()  # Possible change of task/actor name.
                        file_info.actor_name = next_line.split(
                            ray_constants.LOG_PREFIX_ACTOR_NAME, 1
                        )[1]
                        file_info.task_name = None
                    elif next_line.startswith(ray_constants.LOG_PREFIX_TASK_NAME):
                        flush()  # Possible change of task/actor name.
                        file_info.task_name = next_line.split(
                            ray_constants.LOG_PREFIX_TASK_NAME, 1
                        )[1]
                    elif next_line.startswith(ray_constants.LOG_PREFIX_JOB_ID):
                        file_info.job_id = next_line.split(
                            ray_constants.LOG_PREFIX_JOB_ID, 1
                        )[1]
                    elif next_line.startswith(
                        "Windows fatal exception: access violation"
                    ):
                        # We are suppressing the
                        # 'Windows fatal exception: access violation'
                        # message on workers on Windows here.
                        # As far as we know it is harmless,
                        # but is frequently popping up if Python
                        # functions are run inside the core
                        # worker C extension. See the investigation in
                        # github.com/ray-project/ray/issues/18944
                        # Also skip the following line, which is an
                        # empty line.
                        file_info.file_handle.readline()
                    else:
                        lines_to_publish.append(next_line)
                except Exception:
                    logger.error(
                        f"Error: Reading file: {file_info.filename}, "
                        f"position: {file_info.file_handle.tell()} "
                        "failed."
                    )
                    raise

            if file_info.file_position == 0:
                # make filename windows-agnostic
                filename = file_info.filename.replace("\\", "/")
                if "/raylet" in filename:
                    file_info.worker_pid = "raylet"
                elif "/gcs_server" in filename:
                    file_info.worker_pid = "gcs_server"
                elif "/monitor" in filename or "event_AUTOSCALER" in filename:
                    file_info.worker_pid = "autoscaler"
                elif "/runtime_env" in filename:
                    file_info.worker_pid = "runtime_env"

            # Record the current position in the file.
            file_info.file_position = file_info.file_handle.tell()
            flush()

        return anything_published

    def should_update_filenames(self, last_file_updated_time: float) -> bool:
        """Return true if filenames should be updated.

        This method is used to apply the backpressure on file updates because
        that requires heavy glob operations which use lots of CPUs.

        Args:
            last_file_updated_time: The last time filenames are updated.

        Returns:
            True if filenames should be updated. False otherwise.
        """
        elapsed_seconds = float(time.time() - last_file_updated_time)
        return (
            len(self.log_filenames) < RAY_LOG_MONITOR_MANY_FILES_THRESHOLD
            or elapsed_seconds > LOG_NAME_UPDATE_INTERVAL_S
        )

    def run(self, quit_event=None):
        """Run the log monitor.

        This will scan the file system once every LOG_NAME_UPDATE_INTERVAL_S to
        check if there are new log files to monitor. It will also publish new
        log lines.
        """
        last_updated = time.time()
        while not self.need_quit:
            if self.should_update_filenames(last_updated):
                self.update_log_filenames()
                last_updated = time.time()

            self.open_closed_files()
            anything_published = self.check_log_files_and_publish_updates()
            # If nothing was published, then wait a little bit before checking
            # for logs to avoid using too much CPU.
            if not anything_published:
                time.sleep(0.1)
            if quit_event is not None and quit_event.is_set():
                logger.info("============= quit_event is set, quit the LogMonitor =============")
                break


def is_proc_alive(pid):
    # Import locally to make sure the bundled version is used if needed
    import psutil # pylint: disable=import-outside-toplevel

    try:
        return psutil.Process(pid).is_running()
    except psutil.NoSuchProcess:
        # The process does not exist.
        return False


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description=("Parse GCS server address for the log monitor to connect to.")
    )
    parser.add_argument(
        "--logging-level",
        required=False,
        type=str,
        default=ray_constants.LOGGER_LEVEL,
        choices=ray_constants.LOGGER_LEVEL_CHOICES,
        help=ray_constants.LOGGER_LEVEL_HELP,
    )
    parser.add_argument(
        "--logging-format",
        required=False,
        type=str,
        default=ray_constants.LOGGER_FORMAT,
        help=ray_constants.LOGGER_FORMAT_HELP,
    )
    parser.add_argument(
        "--logging-filename",
        required=False,
        type=str,
        default=ray_constants.LOG_MONITOR_LOG_FILE_NAME,
        help="Specify the name of log file, "
        "log to stdout if set empty, default is "
        f'"{ray_constants.LOG_MONITOR_LOG_FILE_NAME}"',
    )
    parser.add_argument(
        "--logs-dir",
        required=True,
        type=str,
        help="Specify the path of the temporary directory used by Ray processes.",
    )
    parser.add_argument(
        "--logging-rotate-bytes",
        required=False,
        type=int,
        default=ray_constants.LOGGING_ROTATE_BYTES,
        help="Specify the max bytes for rotating "
        "log file, default is "
        f"{ray_constants.LOGGING_ROTATE_BYTES} bytes.",
    )
    parser.add_argument(
        "--logging-rotate-backup-count",
        required=False,
        type=int,
        default=ray_constants.LOGGING_ROTATE_BACKUP_COUNT,
        help="Specify the backup count of rotated log file, default is "
        f"{ray_constants.LOGGING_ROTATE_BACKUP_COUNT}.",
    )
    args = parser.parse_args()
    setup_component_logger(
        logging_level=args.logging_level,
        logging_format=args.logging_format,
        log_dir=args.logs_dir,
        filename=args.logging_filename,
        max_bytes=args.logging_rotate_bytes,
        backup_count=args.logging_rotate_backup_count,
    )
    ray.init()

    _node_id = [node for node in ray.nodes() if 'master' in node['NodeManagerHostname']][0]['NodeID']
    actor = LogActor.options(
        name='_LOG_ACTOR_NAME',
        scheduling_strategy=NodeAffinitySchedulingStrategy(
            node_id=_node_id,
            soft = False,
        )
    ).remote()

    log_monitor = LogMonitor(
        args.logs_dir,
        is_proc_alive,
        actor
    )

    try:
        log_monitor.run()
    except Exception as e:
        # Something went wrong, so push an error to all drivers.
        traceback_str = ray._private.utils.format_error_message(traceback.format_exc())
        message = (
            f"The log monitor on node {platform.node()} "
            f"failed with the following error:\n{traceback_str}"
        )
        logger.error(message)
        raise e
