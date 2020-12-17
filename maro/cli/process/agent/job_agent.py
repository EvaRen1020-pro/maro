# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.

import json
import multiprocessing as mp
import os
import signal
import subprocess
import time
from collections import defaultdict

import psutil
import redis

from maro.cli.process.utils.details import close_by_pid, get_child_pid, load_redis_info
from maro.cli.utils.params import LocalPaths, ProcessRedisName


class PendingJobAgent(mp.Process):
    def __init__(self, redis_connection, check_interval: int = 120):
        super().__init__()
        self.redis_connection = redis_connection
        self.check_interval = check_interval

    def run(self):
        while True:
            self._check_pending_ticket()
            time.sleep(self.check_interval)

    def _check_pending_ticket(self):
        # check pending job ticket
        pending_jobs = self.redis_connection.lrange(ProcessRedisName.PENDING_JOB_TICKETS, 0, -1)

        for pending_job in pending_jobs:
            job_detail = json.loads(self.redis_connection.hget(ProcessRedisName.JOB_DETAILS, pending_job))

            # control process number by parallel
            running_jobs_length = self.redis_connection.hlen(ProcessRedisName.RUNNING_JOB)
            parallel_level = self.redis_connection.hget(ProcessRedisName.SETTING, "parallel_level")
            if int(parallel_level) > running_jobs_length:
                self._start_job(job_detail)
                # remove using ticket
                self.redis_connection.lrem(ProcessRedisName.PENDING_JOB_TICKETS, 0, pending_job)

    def _start_job(self, job_details: dict):
        pid_dict = defaultdict(list)
        for component_type, command_info in job_details["components"].items():
            number = command_info["num"]
            command = command_info["command"]
            for num in range(number):
                job_local_path = os.path.expanduser(f"{LocalPaths.MARO_PROCESS}/{job_details['name']}")
                if not os.path.exists(job_local_path):
                    os.makedirs(job_local_path)

                with open(f"{job_local_path}/{component_type}_{num}.log", "w") as log_file:
                    proc = subprocess.Popen(command, shell=True, stdout=log_file)   # , preexec_fn=os.setsid)
                    command_pid = get_child_pid(proc.pid)
                    pid_dict["shell_pids"].append(proc.pid)
                    pid_dict["command_pids"].append(command_pid)

        self.redis_connection.hset(ProcessRedisName.RUNNING_JOB, job_details["name"], json.dumps(pid_dict))


class JobTrackingAgent(mp.Process):
    def __init__(self, redis_connection, check_interval: int = 120):
        super().__init__()
        self.redis_connection = redis_connection
        self.check_interval = check_interval
        self._shutdown_count = 0

    def run(self):
        while True:
            self._check_job_status()
            time.sleep(self.check_interval)
            keep_alive = int(self.redis_connection.hget(ProcessRedisName.SETTING, "keep_agent_alive"))
            if not keep_alive:
                self._close_process_cli()

    def _check_job_status(self):
        running_jobs = self.redis_connection.hgetall(ProcessRedisName.RUNNING_JOB)

        for running_job, pid_dict in running_jobs.items():
            # Check pid status
            pid_dict = json.loads(pid_dict)
            still_alive = False
            for pid in pid_dict["command_pids"]:
                if psutil.pid_exists(pid):
                    still_alive = True

            # Update if any finished or error
            if not still_alive:
                self.redis_connection.hdel(ProcessRedisName.RUNNING_JOB, running_job)
                close_by_pid(pid_dict["shell_pids"])

    def _close_process_cli(self):
        if (
            not self.redis_connection.hlen(ProcessRedisName.RUNNING_JOB) and
            not self.redis_connection.llen(ProcessRedisName.PENDING_JOB_TICKETS)
        ):
            self._shutdown_count += 1
        else:
            self._shutdown_count = 0

        if self._shutdown_count >= 5:
            agent_pid = int(self.redis_connection.hget(ProcessRedisName.SETTING, "agent_pid"))

            # close agent
            close_by_pid(pid=agent_pid, recursive=True)

            # Set agent status to 0
            self.redis_connection.hset(ProcessRedisName.SETTING, "agent_status", 0)


class KilledJobAgent(mp.Process):
    def __init__(self, redis_connection, check_interval: int = 120):
        super().__init__()
        self.redis_connection = redis_connection
        self.check_interval = check_interval

    def run(self):
        while True:
            self._check_kill_ticket()
            time.sleep(self.check_interval)

    def _check_kill_ticket(self):
        # Check pending job ticket
        killed_job_names = self.redis_connection.lrange(ProcessRedisName.KILLED_JOB_TICKETS, 0, -1)

        for job_name in killed_job_names:
            if self.redis_connection.hexists(ProcessRedisName.RUNNING_JOB, job_name):
                pid_list = json.loads(self.redis_connection.hget(ProcessRedisName.RUNNING_JOB, job_name))
                self._stop_job(job_name, pid_list)
            else:
                self.redis_connection.lrem(ProcessRedisName.PENDING_JOB_TICKETS, 0, job_name)

            self.redis_connection.lrem(ProcessRedisName.KILLED_JOB_TICKETS, 0, job_name)

    def _stop_job(self, job_name, pid_list):
        # kill all process by pid_list
        for pid in pid_list:
            os.killpg(os.getpgid(pid), signal.SIGTERM)

        self.redis_connection.hdel(ProcessRedisName.RUNNING_JOB, job_name)


class MasterAgent:
    def __init__(self, check_interval: int = 60):
        redis_info = load_redis_info()

        self.redis_connection = redis.Redis(
            host=redis_info["redis_info"]["host"],
            port=redis_info["redis_info"]["port"]
        )
        self.redis_connection.hset(ProcessRedisName.SETTING, "agent_pid", os.getpid())
        self.check_interval = check_interval

    def start(self) -> None:
        """Start agents."""
        self.pending_job_agent = PendingJobAgent(
            redis_connection=self.redis_connection,
            check_interval=self.check_interval
        )
        self.pending_job_agent.start()

        self.killed_job_agent = KilledJobAgent(
            redis_connection=self.redis_connection,
            check_interval=self.check_interval
        )
        self.killed_job_agent.start()

        self.job_tracking_agent = JobTrackingAgent(
            redis_connection=self.redis_connection,
            check_interval=self.check_interval
        )
        self.job_tracking_agent.start()


if __name__ == "__main__":
    master_agent = MasterAgent()
    master_agent.start()