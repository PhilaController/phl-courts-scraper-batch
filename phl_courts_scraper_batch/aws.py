import os
from pathlib import Path

import boto3
import pandas as pd
import simplejson as json
from dotenv import find_dotenv, load_dotenv
from fsspec.implementations.local import LocalFileSystem
from loguru import logger
from s3fs import S3FileSystem

from . import APP_NAME, DATA_DIR, HOME_DIR, io


def parse_aws_path(path):
    """Split a path on AWS into bucket and key."""

    path = str(path)
    bucket = path.split("/")[2]
    key = "/".join(path.split("/")[3:])
    return bucket, key


def is_ec2_instance():
    """Check if an instance is running on ECS Fargate on AWS."""
    return os.getenv("AWS_EXECUTION_ENV") == "AWS_ECS_FARGATE"


class AWS:
    """Connection to Amazon Web Services."""

    def __init__(self, debug=False):
        """Initialize the connection to AWS."""

        self.debug = debug

        # Load any environment variables from .env files
        load_dotenv(find_dotenv())

        # Set up the AWS session
        # This searches for AWS credentials in the environment
        self.session = boto3.Session(
            region_name=os.getenv("AWS_REGION", "us-east-1"),
        )

        # Set up the file systems
        self.remote = S3FileSystem()
        self.local = LocalFileSystem(str(HOME_DIR))

        # Set up clients
        self.ecs = self.session.client("ecs")
        self.ec2 = self.session.client("ec2")
        self.s3 = self.session.client("s3")

        # Set up the output s3 bucket (and create it if we need to)
        self.bucket_name = APP_NAME
        if not self.remote.exists(self.bucket_name):
            self.s3.create_bucket(Bucket=self.bucket_name)

        # Are we running on AWS
        self.on_aws = is_ec2_instance()

        # Set up cluster if we're not on AWS
        self.cluster_name = f"{APP_NAME}-cluster"
        if not self.on_aws:
            self._init_cluster()

    def _init_cluster(self):
        """Initialize the ECS cluster."""

        # Verify that the cluster exists
        clusters = self.ecs.list_clusters()
        cluster_names = [c.split("/")[-1] for c in clusters["clusterArns"]]
        if self.cluster_name not in cluster_names:
            raise ValueError(f"Missing ECS cluster: {self.cluster_name}")

        # Get the subnets
        self.subnets = [d["SubnetId"] for d in self.ec2.describe_subnets()["Subnets"]]
        if self.debug:
            logger.info(f"Subnets: {self.subnets}")

        # Get the latest task definition
        tasks = self.ecs.list_task_definitions(familyPrefix=APP_NAME, sort="ASC")
        self.task_definition = tasks["taskDefinitionArns"][-1]

        if self.debug:
            logger.info(f"Task definition: {self.task_definition}")

    def exists(self, path):
        """See if a file exists."""

        # File is on AWS
        if path.startswith("s3://"):
            fs = self.remote
        else:
            path = str(Path(path).resolve().relative_to(HOME_DIR))
            fs = self.local

        return fs.exists(path)

    def submit_jobs(
        self,
        flavor,
        dataset,
        search_by=None,
        tag=None,
        pid=None,
        dry_run=False,
        sample=None,
        log_freq=50,
        seed=42,
        errors="ignore",
        sleep=2,
        interval=1,
        time_limit=20,
        output_folder=None,
        debug=False,
        ntasks=1,
        wait=False,
    ):
        """Submit jobs to the ECS cluster."""

        # Init if we need to
        if not hasattr(self, "subnets"):
            self._init_cluster()

        # Set the network config
        NETWORK_CONFIG = {
            "awsvpcConfiguration": {
                "assignPublicIp": "ENABLED",
                "subnets": self.subnets,
            }
        }

        # Build the base command
        base_command = [
            "run",
            APP_NAME,
            "scrape",
            flavor,
            dataset,
            f"--nprocs={ntasks}",
            f"--sleep={sleep}",
            f"--errors={errors}",
            f"--log-freq={log_freq}",
            f"--seed={seed}",
            f"--interval={interval}",
            f"--time-limit={time_limit}",
        ]

        # Add the optional arguments
        if search_by is not None:
            base_command += [f"--search-by={search_by}"]
        if sample is not None:
            base_command += [f"--sample={sample}"]
        if tag is not None:
            base_command += [f"--tag={tag}"]
        if dry_run:
            base_command += [f"--dry-run"]
        if debug:
            base_command += ["--debug"]

        # Add output folder
        output_folder, _ = io.get_output_paths(
            flavor, dataset, chunk=None, output_folder=output_folder
        )
        if debug:
            logger.debug(f"Output folder: {output_folder}")
        base_command += [f"--output-folder={output_folder}"]

        # Run in parallel
        tasks = []
        for pid in range(0, ntasks):

            # Log
            logger.info(f"Submitting job #{pid}")

            # Build the final command
            command = base_command + [f"--pid={pid}"]

            # Submit job
            task = self.ecs.run_task(
                taskDefinition=self.task_definition,
                cluster=self.cluster_name,
                networkConfiguration=NETWORK_CONFIG,
                launchType="FARGATE",
                overrides={
                    "containerOverrides": [{"name": APP_NAME, "command": command}]
                },
            )

            tasks.append(task)

        # Do not wait for tasks to finish
        if not wait:
            return

        # Check if provisioning failed:
        failed = False
        for task in tasks:
            if not len(task["tasks"]) and len(task["tasks"]["failures"]):
                failed = True
                reason = task["tasks"]["failures"][0]["reason"]
                logger.warning(f"Task provisioning failed: {reason}")

        # Trim to successful tasks
        tasks = [task for task in tasks if len(task["tasks"])]

        # Stop successful
        if failed:
            for task in tasks:
                self.ecs.stop_task(
                    cluster=self.cluster_name, task=task["tasks"][0]["taskArn"]
                )
            raise ValueError("Error provisioning some tasks; all tasks stopped.")

        # Get the task ids
        task_ids = [task["tasks"][0]["taskArn"] for task in tasks]

        # Wait for all jobs to complete
        logger.info(f"Waiting for tasks to complete")
        waiter = self.ecs.get_waiter("tasks_stopped")
        waiter.wait(
            cluster=self.cluster_name,
            tasks=task_ids,
            WaiterConfig={"Delay": 60, "MaxAttempts": 500},
        )
        logger.info(f"...all tasks completed")

        # Check for errors
        task_results = self.ecs.describe_tasks(
            cluster=self.cluster_name, tasks=task_ids
        )

        # Check the exit codes
        exit_codes = [
            task["containers"][0]["exitCode"] for task in task_results["tasks"]
        ]
        if any([code != 0 for code in exit_codes]):
            logger.warning("One or more tasks failed!")

        # And combine
        logger.info("Combining parallel results on AWS")
        outfile = self.combine_parallel_results(
            flavor, dataset, f"s3://{APP_NAME}/{output_folder}/chunks"
        )

        return outfile

    def combine_parallel_results(self, flavor, dataset, output_folder):
        """Iterate through parallel, chunked scraping results from AWS."""

        # Invalidate the cache
        self.remote.invalidate_cache()

        # Make sure it exists
        if not self.exists(output_folder):
            raise FileNotFoundError(
                f"Output folder does not exist for parallel results: '{output_folder}'"
            )

        # The file system
        fs = self.remote

        # Get the files
        tags = [f"{flavor}_results", f"{flavor}_input"]
        extensions = [".json", ".csv"]

        data_file = None
        for i, (tag, extension) in enumerate(zip(tags, extensions)):

            # Get the files
            pattern = f"{output_folder}/{tag}*{extension}"
            files = sorted(fs.glob(pattern))
            N = len(files)
            if N == 0:
                raise ValueError(
                    f"No files found for dataset '{dataset}' and kind '{flavor}' in output folder '{output_folder}'"
                )

            # Combine
            if i == 0:
                logger.info(
                    f"Combining {N} files for dataset '{dataset}' and kind '{flavor}'"
                )
            results = None
            for f in files:

                with fs.open(f, "rb") as ff:

                    # load this result
                    if extension == ".json":
                        r = json.loads(ff.read())

                        # Convert to a list if we need to
                        if isinstance(r, dict):
                            r = [v for _, v in r.items() if v]

                        # Add the results
                        if results is None:
                            results = r
                        else:
                            results += r
                    else:

                        r = pd.read_csv(ff, header=None)
                        if results is None:
                            results = r
                        else:
                            results = pd.concat([results, r])

            # Normalize the output file
            filename = f"{output_folder}/../{tag}{extension}"
            if filename.startswith("s3://"):
                filename = filename[5:]
            filename = f"s3://{os.path.normpath(filename)}"

            if i == 0:
                data_file = filename

            if i == 0:
                logger.info(f"Total number of results from AWS: {len(results)}")
                logger.info(f"Saving combined results to {filename}")

            with fs.open(filename, "w") as ff:
                if extension == ".json":
                    ff.write(json.dumps(results))
                else:
                    results.to_csv(ff, header=False, index=False)

        return data_file

    def sync(
        self,
        source: str,
        dest: str,
        dry_run: bool = False,
    ) -> None:
        """
        Sync source to dest.

        All elements existing in source that don't exist in dest will be
        copied to dest. No element will be deleted.


        Parameters
        ----------
        source: str
            Source folder on AWS
        dest: str
            Destination folder locally

        Returns
        -------
        None
        """
        # Determine the source
        SOURCE = None
        if source.startswith("s3://"):
            SOURCE = "aws"
        elif dest.startswith("s3://"):
            SOURCE = "local"
        else:
            raise ValueError("One of source or dest should start with s3://")

        # AWS to local
        if SOURCE == "aws":
            source_fs = self.remote
            dest_fs = self.local

            # Make sure dest is relative to HOME_DIR
            dest = str(Path(dest).resolve().relative_to(HOME_DIR))

        # Local to AWS
        else:
            source_fs = self.local
            dest_fs = self.remote

            # Make sure source is relative to HOME_DIR
            source = str(Path(source).resolve().relative_to(HOME_DIR))

        # Get the source files
        source_files = source_fs.find(source)
        for source_file in source_files:

            # Make it relative to HOME_DIR if we need to
            if SOURCE == "local":
                source_file = str(Path(source_file).resolve().relative_to(HOME_DIR))

            # Remove the root from the source file
            source_file_key = "/".join(source_file.split("/")[1:])

            # Get source file path on dest system
            dest_file = f"{dest}/{source_file_key}"

            # Update if file doesn't exist or is out of date
            if not Path(dest_file).exists() or source_fs.modified(
                source_file
            ) > dest_fs.modified(dest_file):

                logger.info(f"Syncing {source_file} to {dest_file}")
                if not dry_run:

                    if SOURCE == "aws":
                        self.remote.get(source_file, str(HOME_DIR / dest_file))
                    else:
                        self.remote.put(str(HOME_DIR / source_file), dest_file)
