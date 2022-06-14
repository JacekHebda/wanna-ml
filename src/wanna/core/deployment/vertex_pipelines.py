from pathlib import Path
from typing import Optional

import pandas as pd
from google.cloud import aiplatform
from google.cloud.aiplatform import PipelineJob

from wanna.core.deployment.artifacts_push import ArtifactsPushMixin
from wanna.core.deployment.models import CloudFunctionResource, CloudSchedulerResource, PipelineResource
from wanna.core.deployment.vertex_scheduling import VertexSchedulingMixIn
from wanna.core.services.path_utils import PipelinePaths
from wanna.core.utils.gcp import convert_project_id_to_project_number
from wanna.core.utils.loaders import load_yaml_path
from wanna.core.utils.spinners import Spinner
from wanna.core.utils.time import get_timestamp


class VertexPipelinesMixInVertex(VertexSchedulingMixIn, ArtifactsPushMixin):
    def run_pipeline(
        self,
        resource: PipelineResource,
        extra_params: Optional[Path],
        sync: bool = True,
    ) -> None:
        mode = "sync mode" if sync else "fire-forget mode"

        Spinner().info(text=f"Running pipeline {resource.pipeline_name} in {mode}")

        # fetch compiled params
        pipeline_job_id = f"pipeline-{resource.pipeline_name}-{get_timestamp()}"

        # Apply override with cli provided params file
        override_params = load_yaml_path(extra_params, Path(".")) if extra_params else {}
        pipeline_params = {**resource.parameter_values, **override_params}

        project_number = convert_project_id_to_project_number(resource.project)
        network = f"projects/{project_number}/global/networks/{resource.network}"

        # Define Vertex AI Pipeline job
        pipeline_job = PipelineJob(
            display_name=resource.pipeline_name,
            job_id=pipeline_job_id,
            template_path=str(resource.json_spec_path),
            pipeline_root=resource.pipeline_root,
            parameter_values=pipeline_params,
            enable_caching=True,
            labels=resource.labels,
            project=resource.project,
            location=resource.location,
        )

        # TODO: Cancel pipeline if wanna process exits
        # exit_callback(manifest.pipeline_name, pipeline_job, sync, s)

        # submit pipeline job for execution
        # TODO: should we remove service_account and  network from this call ?
        pipeline_job.submit(service_account=resource.service_account, network=network)

        if sync:
            Spinner().info(f"Pipeline dashboard at {pipeline_job._dashboard_uri()}.")
            pipeline_job.wait()

            df_pipeline = aiplatform.get_pipeline_df(pipeline=resource.pipeline_name.replace("_", "-"))
            with pd.option_context(
                "display.max_rows", None, "display.max_columns", None
            ):  # more options can be specified also
                Spinner().info(f"Pipeline results info: \n\t{df_pipeline}")

    def deploy_pipeline(
        self, resource: PipelineResource, pipeline_paths: PipelinePaths, version: str, env: str
    ) -> None:

        function = self.upsert_cloud_function(
            resource=CloudFunctionResource(
                name=resource.pipeline_name,
                project=resource.project,
                location=resource.location,
                service_account=resource.schedule.service_account
                if resource.schedule and resource.schedule.service_account
                else resource.service_account,
                build_dir=pipeline_paths.get_local_pipeline_deployment_path(version),
                resource_root=pipeline_paths.get_gcs_pipeline_deployment_path(version),
                resource_function_template="scheduler_cloud_function.py",
                resource_requirements_template="scheduler_cloud_function_requirements.txt",
                template_vars=resource.dict(),
                env_params=resource.compile_env_params,
                labels=resource.labels,
                network=resource.network,
            ),
            env=env,
            version=version,
        )

        if resource.schedule:
            pipeline_spec_path = pipeline_paths.get_gcs_pipeline_json_spec_path(version)
            body = {
                "pipeline_spec_uri": pipeline_spec_path,
                "parameter_values": resource.parameter_values,
            }  # TODO extend with execution_date(now) ?

            self.upsert_cloud_scheduler(
                function=function,
                resource=CloudSchedulerResource(
                    name=resource.pipeline_name,
                    project=resource.project,
                    location=resource.location,
                    body=body,
                    cloud_scheduler=resource.schedule,
                    service_account=resource.service_account,
                    labels=resource.labels,
                ),
                env=env,
                version=version,
            )

        else:
            Spinner().info("Deployment Manifest does not have a schedule set. Skipping Cloud Scheduler sync")

        # not possible to set alerts for failed PipelineJobs
        # since aiplatform.googleapis.com/PipelineJob
        # is not a monitored job
        # https://cloud.google.com/monitoring/api/resources
        # logging_metric_ref = f"{manifest.pipeline_name}-ml-pipeline-error"
        # gcp_resource_type = "aiplatform.googleapis.com/PipelineJob"
        # deploy.upsert_log_metric(LogMetricResource(
        #     project=manifest.project,
        #     name=logging_metric_ref,
        #     filter_= f"""
        #     resource.type="{gcp_resource_type}"
        #     AND severity >= WARNING
        #     AND resource.labels.pipeline_job_id:"{manifest.pipeline_name}"
        #     """,
        #     description=f"Log metric for {manifest.pipeline_name} vertex ai pipeline"
        # ))
        # deploy.upsert_alert_policy(
        #     logging_metric_type=logging_metric_ref,
        #     resource_type=gcp_resource_type,
        #     project=manifest.project,
        #     name=f"{manifest.pipeline_name}-ml-pipeline-alert-policy",
        #     display_name=f"{logging_metric_ref}-ml-pipeline-alert-policy",
        #     labels=manifest.labels,
        #     notification_channels=["projects/cloud-lab-304213/notificationChannels/1568320106180659521"]
        # )
