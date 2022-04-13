import json
import os
from pathlib import Path
from typing import List, Tuple, Union

import typer
from caseconverter import kebabcase, snakecase
from google.cloud import aiplatform
from google.cloud.aiplatform import (
    CustomContainerTrainingJob,
    CustomJob,
    CustomPythonPackageTrainingJob,
    CustomTrainingJob,
)
from google.cloud.aiplatform.gapic import WorkerPoolSpec
from google.cloud.aiplatform_v1.types import ContainerSpec, DiskSpec, MachineSpec, PythonPackageSpec
from google.cloud.aiplatform_v1.types.pipeline_state import PipelineState
from google.protobuf.json_format import MessageToDict
from smart_open import open

from wanna.cli.docker.service import DockerService
from wanna.cli.models.training_custom_job import (
    CustomContainerTrainingJobManifest,
    CustomJobManifest,
    CustomJobModel,
    CustomJobType,
    CustomPythonPackageTrainingJobManifest,
    JobManifest,
    TrainingCustomJobModel,
    WorkerPoolModel,
)
from wanna.cli.models.wanna_config import WannaConfigModel
from wanna.cli.plugins.base.service import BaseService
from wanna.cli.plugins.tensorboard.service import TensorboardService
from wanna.cli.utils.spinners import Spinner


def _make_gcs_manifest_path(bucket: str, job_name: str) -> str:
    return f"gs://{bucket}/jobs/{kebabcase(job_name).lower()}"


def _make_local_manifest_path(build_dir: Path, job_name: str) -> Path:
    return build_dir / f"jobs/{kebabcase(job_name).lower()}"


def _read_job_manifest(manifest_path: Path) -> JobManifest:
    with open(manifest_path, "r") as fin:
        json_dict = json.loads(fin.read())
        try:
            job_type = CustomJobType[json_dict["job_type"]]
            if job_type is CustomJobType.CustomJob:
                return CustomJobManifest.parse_obj(json_dict)
            elif job_type is CustomJobType.CustomPythonPackageTrainingJob:
                return CustomPythonPackageTrainingJobManifest.parse_obj(json_dict)
            elif job_type is CustomJobType.CustomContainerTrainingJob:
                return CustomContainerTrainingJobManifest.parse_obj(json_dict)
            else:
                raise ValueError(
                    "Issue in code, this branch should have not been reached. "
                    f"job_type {json_dict['job_type']} is unknown"
                )
        except Exception as e:
            typer.echo(f"{e}", err=True)


def _remove_nones(d):
    """
    Delete keys with the value ``None`` or `null` in a dictionary, recursively.
    """
    for key, value in list(d.items()):
        if value is None:
            del d[key]
        elif isinstance(value, dict):
            _remove_nones(value)
        elif isinstance(value, list):
            for v in value:
                if isinstance(v, dict):
                    _remove_nones(v)
        else:
            del d[key]
            d[snakecase(key)] = value
    return d


def _write_job_manifest(build_dir: Path, manifest: JobManifest) -> Path:
    local_manifest_dir = _make_local_manifest_path(build_dir, manifest.job_config.name)
    local_manifest_path = local_manifest_dir / "job-manifest.json"
    os.makedirs(local_manifest_dir, exist_ok=True)

    with open(local_manifest_path, "w") as out:
        json_dict = {
            "job_type": manifest.job_type.name,
            "job_config": manifest.job_config.dict(),
            "image_refs": manifest.image_refs,
            "job_payload": manifest.job_payload,
            "tensorboard": manifest.tensorboard,
        }
        json_dump = json.dumps(
            _remove_nones(json_dict),
            allow_nan=False,
            default=lambda o: dict((key, value) for key, value in o.__dict__.items() if value),
        )
        out.write(json_dump)

    return local_manifest_path


def _run_custom_job(manifest: CustomJobManifest, sync: bool):
    custom_job = CustomJob(**manifest.job_payload)
    custom_job.run(
        timeout=manifest.job_config.timeout_seconds,
        enable_web_access=manifest.job_config.enable_web_access,
        tensorboard=manifest.tensorboard if manifest.tensorboard else None,
        sync=False,
    )

    custom_job.wait_for_resource_creation()
    job_id = custom_job.resource_name.split("/")[-1]

    if sync:
        with Spinner(text=f"Running custom job {manifest.job_config.name} in sync mode") as s:
            s.info(
                f"Job Dashboard in "
                f"https://console.cloud.google.com/vertex-ai/locations/{manifest.job_config.region}/training/{job_id}?project={manifest.job_config.project_id}"  # noqa
            )
            custom_job.wait()
    else:
        with Spinner(text=f"Running custom job {manifest.job_config.name} in async mode") as s:
            s.info(
                f"Job Dashboard in "
                f"https://console.cloud.google.com/vertex-ai/locations/{manifest.job_config.region}/training/{job_id}?project={manifest.job_config.project_id}"  # noqa
            )


def _run_training_job(
    manifest: Union[CustomContainerTrainingJobManifest, CustomPythonPackageTrainingJobManifest],
    training_job: Union[CustomContainerTrainingJob, CustomPythonPackageTrainingJob],
    sync: bool,
):
    with Spinner(text=f"Initiating {manifest.job_config.name} custom job") as s:
        s.info(f"Outputs will be saved to {manifest.job_config.base_output_directory}")
        training_job.run(
            machine_type=manifest.job_config.worker.machine_type,
            accelerator_type=manifest.job_config.worker.gpu.accelerator_type
            if manifest.job_config.worker.gpu and manifest.job_config.worker.gpu.accelerator_type
            else "ACCELERATOR_TYPE_UNSPECIFIED",
            accelerator_count=manifest.job_config.worker.gpu.count
            if manifest.job_config.worker.gpu and manifest.job_config.worker.gpu.count
            else 0,
            args=manifest.job_config.worker.args,
            base_output_dir=manifest.job_config.base_output_directory,
            service_account=manifest.job_config.service_account,
            network=manifest.job_config.network,
            environment_variables=manifest.job_config.worker.env,
            replica_count=manifest.job_config.worker.replica_count,
            boot_disk_type=manifest.job_config.worker.boot_disk_type,
            boot_disk_size_gb=manifest.job_config.worker.boot_disk_size_gb,
            reduction_server_replica_count=manifest.job_config.reduction_server.replica_count
            if manifest.job_config.reduction_server
            else 0,
            reduction_server_machine_type=manifest.job_config.reduction_server.machine_type
            if manifest.job_config.reduction_server
            else None,
            reduction_server_container_uri=manifest.job_config.reduction_server.container_uri
            if manifest.job_config.reduction_server
            else None,
            timeout=manifest.job_config.timeout_seconds,
            enable_web_access=manifest.job_config.enable_web_access,
            tensorboard=manifest.tensorboard if manifest.tensorboard else None,
            sync=False,
        )

    if sync:
        training_job.wait_for_resource_creation()
        job_id = training_job.resource_name.split("/")[-1]
        with Spinner(text=f"Running custom training job {manifest.job_config.name} in sync mode") as s:
            s.info(
                "Job Dashboard in "
                f"https://console.cloud.google.com/vertex-ai/locations/{manifest.job_config.region}/training/{job_id}?project={manifest.job_config.project_id}"  # noqa
            )
            training_job.wait()
    else:
        with Spinner(text=f"Running custom training job {manifest.job_config.name} in async mode") as s:
            training_job.wait_for_resource_creation()
            job_id = training_job.resource_name.split("/")[-1]
            s.info(
                f"Job Dashboard in "
                f"https://console.cloud.google.com/vertex-ai/locations/{manifest.job_config.region}/training/{job_id}?project={manifest.job_config.project_id}"  # noqa
            )

            # TODO:
            # Currently training_job does not release the future even with sync=False
            # and wait_for_resource_creation succeeds
            # the job is running at this stage
            # we need a "hack" to terminate main with exit 0.


class JobService(BaseService):
    def __init__(self, config: WannaConfigModel, workdir: Path, version: str = "dev"):
        super().__init__(
            instance_type="job",
            instance_model=TrainingCustomJobModel,
        )

        aiplatform.init(
            project=config.gcp_profile.project_id,
            location=config.gcp_profile.region,
        )

        self.instances = config.jobs
        self.wanna_project = config.wanna_project
        self.bucket_name = config.gcp_profile.bucket
        self.config = config
        self.tensorboard_service = TensorboardService(config=config)
        self.docker_service = DockerService(
            docker_model=config.docker,
            gcp_profile=config.gcp_profile,
            version=version,
            work_dir=workdir,
            wanna_project_name=self.wanna_project.name,
        )
        self.build_dir = workdir / "build"
        self.version = version

    def build(self, instance_name: str) -> List[Tuple[Path, JobManifest]]:
        instances = self._filter_instances_by_name(instance_name)
        built_instances = []
        for instance in instances:
            job_manifest = self._build_manifest(instance)
            manifest_path = _write_job_manifest(self.build_dir, job_manifest)
            result = (
                manifest_path,
                job_manifest,
            )
            built_instances.append(result)

        return built_instances

    def push(self, manifests: List[Tuple[Path, JobManifest]], local: bool = False) -> List[str]:
        pushed_manifests = []
        for manifest_path, manifest in manifests:
            loaded_manifest = _read_job_manifest(manifest_path)

            with Spinner(text=f"Pushing job manifest {manifest.job_config.name}") as s:

                for docker_image_ref in loaded_manifest.image_refs:
                    self.docker_service.push_image_ref(docker_image_ref)

                if local:
                    deployment_dir = f"{manifest_path.parent}/deployment/release/{self.version}"
                    os.makedirs(deployment_dir, exist_ok=True)
                else:
                    gcs_manifest_path = _make_gcs_manifest_path(
                        self.config.gcp_profile.bucket, manifest.job_config.name
                    )
                    deployment_dir = f"{gcs_manifest_path}/deployment/release/{self.version}"

                target_manifest_path = f"{deployment_dir}/job-manifest.json"

                with open(target_manifest_path, "w") as f:
                    f.write(manifest.json())

                s.info(f"Pushed wanna job manifest to {target_manifest_path}")
                pushed_manifests.append(target_manifest_path)

        return pushed_manifests

    @staticmethod
    def deploy(
        manifests: List[str],
        sync: bool = True,
    ) -> None:
        raise NotImplementedError

    @staticmethod
    def run(
        manifests: List[str],
        sync: bool = True,
    ) -> None:

        for manifest_path in manifests:
            manifest = _read_job_manifest(Path(manifest_path))

            aiplatform.init(location=manifest.job_config.region, project=manifest.job_config.project_id)

            if manifest.job_type is CustomJobType.CustomContainerTrainingJob:
                _run_training_job(manifest, CustomContainerTrainingJob(**manifest.job_payload), sync)
            elif manifest.job_type is CustomJobType.CustomPythonPackageTrainingJob:
                _run_training_job(manifest, CustomPythonPackageTrainingJob(**manifest.job_payload), sync)
            else:
                _run_custom_job(manifest, sync)

    def _build_manifest(self, instance: Union[CustomJobModel, TrainingCustomJobModel]) -> JobManifest:
        """
        Create one custom job based on TrainingCustomJobModel.
        The function also waits until the job is initiated (no longer pending)

        Args:
            instance: custom job model to create
        """
        if isinstance(instance, TrainingCustomJobModel):
            return self._create_training_job_manifest(instance)
        else:
            image_refs, worker_pool_specs = list(
                zip(*[self._create_worker_pool_spec(worker) for worker in instance.workers])
            )
            return CustomJobManifest(
                job_type=CustomJobType.CustomJob,
                job_config=instance,
                job_payload={
                    "display_name": instance.name,
                    "worker_pool_specs": [
                        _remove_nones(MessageToDict(s._pb, preserving_proto_field_name=True))
                        for s in list(worker_pool_specs)
                    ],
                    "labels": instance.labels,
                    "staging_bucket": instance.bucket,
                },
                image_refs=set(image_refs),
                tensorboard=self.tensorboard_service.get_or_create_tensorboard_instance_by_name(
                    instance.tensorboard_ref
                )
                if instance.tensorboard_ref
                else None,
            )

    def _create_training_job_manifest(
        self,
        job_model: TrainingCustomJobModel,
    ) -> Union[CustomPythonPackageTrainingJobManifest, CustomContainerTrainingJobManifest]:
        """"""

        if job_model.worker.python_package:
            image_ref = job_model.worker.python_package.docker_image_ref
            _, _, tag = self.docker_service.get_image(docker_image_ref=job_model.worker.python_package.docker_image_ref)
            result = CustomPythonPackageTrainingJobManifest(
                job_type=CustomJobType.CustomPythonPackageTrainingJob,
                job_config=job_model,
                job_payload={
                    "display_name": job_model.name,
                    "python_package_gcs_uri": job_model.worker.python_package.package_gcs_uri,
                    "python_module_name": job_model.worker.python_package.module_name,
                    "container_uri": tag,
                    "labels": job_model.labels,
                    "staging_bucket": job_model.bucket,
                },
                image_refs=[image_ref],
                tensorboard=self.tensorboard_service.get_or_create_tensorboard_instance_by_name(
                    job_model.tensorboard_ref
                )
                if job_model.tensorboard_ref
                else None,
            )
            return result
        else:
            image_ref = job_model.worker.container.docker_image_ref
            _, _, tag = self.docker_service.get_image(docker_image_ref=job_model.worker.container.docker_image_ref)
            result = CustomContainerTrainingJobManifest(
                job_type=CustomJobType.CustomContainerTrainingJob,
                job_config=job_model,
                job_payload={
                    "display_name": job_model.name,
                    "container_uri": tag,
                    "command": job_model.worker.container.command,
                    "labels": job_model.labels,
                    "staging_bucket": job_model.bucket,
                },
                image_refs=[image_ref],
                tensorboard=self.tensorboard_service.get_or_create_tensorboard_instance_by_name(
                    job_model.tensorboard_ref
                )
                if job_model.tensorboard_ref
                else None,
            )
            return result

    def _create_worker_pool_spec(self, worker_pool_model: WorkerPoolModel) -> Tuple[str, WorkerPoolSpec]:
        # TODO: this can be doggy
        image_ref = (
            worker_pool_model.container.docker_image_ref
            if worker_pool_model.container
            else worker_pool_model.python_package.docker_image_ref
        )
        return image_ref, WorkerPoolSpec(
            container_spec=ContainerSpec(
                image_uri=self.docker_service.get_image(image_ref)[2],
                command=worker_pool_model.container.command,
                args=worker_pool_model.args,
                env=worker_pool_model.args,
            )
            if worker_pool_model.container
            else None,
            python_package_spec=PythonPackageSpec(
                executor_image_uri=self.docker_service.get_image(image_ref)[2],
                package_uris=[worker_pool_model.python_package.package_gcs_uri],
                python_module=worker_pool_model.python_package.module_name,
            )
            if worker_pool_model.python_package
            else None,
            machine_spec=MachineSpec(
                machine_type=worker_pool_model.machine_type,
                accelerator_type=worker_pool_model.gpu.accelerator_type if worker_pool_model.gpu else None,
                accelerator_count=worker_pool_model.gpu.count if worker_pool_model.gpu else None,
            ),
            disk_spec=DiskSpec(
                boot_disk_type=worker_pool_model.boot_disk_type, boot_disk_size_gb=worker_pool_model.boot_disk_size_gb
            ),
            replica_count=worker_pool_model.replica_count,
        )

    @staticmethod
    def _create_list_jobs_filter_expr(states: List[PipelineState], job_name: str = None) -> str:
        """
        Creates a filter expression that can be used when listing current jobs on GCP.
        Args:
            states: list of desired states
            job_name: desire job name

        Returns:
            filter expression
        """
        filter_expr = "(" + " OR ".join([f'state="{state.name}"' for state in states]) + ")"
        if job_name:
            filter_expr = filter_expr + f' AND display_name="{job_name}"'
        return filter_expr

    def _list_jobs(self, states: List[PipelineState], job_name: str = None) -> List[CustomTrainingJob]:
        """
        List all custom jobs with given project_id, region with given states.

        Args:
            states: list of custom job states, eg [JobState.JOB_STATE_RUNNING, JobState.JOB_STATE_PENDING]
            job_name:

        Returns:
            list of jobs
        """
        filter_expr = self._create_list_jobs_filter_expr(states=states, job_name=job_name)
        jobs = aiplatform.CustomTrainingJob.list(filter=filter_expr)
        return jobs  # type: ignore

    def _stop_one_instance(self, instance: TrainingCustomJobModel) -> None:
        """
        Pause one all jobs that have the same region and name as "instance".
        First we list all jobs with state running and pending and then
        user is prompted to choose which to kill.

        Args:
            instance: custom job model
        """
        active_jobs = self._list_jobs(
            states=[PipelineState.PIPELINE_STATE_RUNNING, PipelineState.PIPELINE_STATE_PENDING],  # type: ignore
            job_name=instance.name,
        )
        if active_jobs:
            for job in active_jobs:
                should_cancel = typer.prompt(
                    f"Do you want to cancel job {job.display_name} (started at {job.create_time})?"
                )
                if should_cancel:
                    with Spinner(text=f"Canceling job {job.display_name}"):
                        job.cancel()
        else:
            typer.echo(f"No running or pending job with name {instance.name}")
