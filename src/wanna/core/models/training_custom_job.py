from enum import Enum
from typing import Any, Dict, List, Literal, Optional, Union

from pydantic import BaseModel, Extra, Field, root_validator, validator
from typing_extensions import Annotated

from wanna.core.models.base_instance import BaseInstanceModel
from wanna.core.models.gcp_components import GPU, Disk


class PythonPackageModel(BaseModel, extra=Extra.forbid):
    docker_image_ref: str
    package_gcs_uri: str
    module_name: str


class ContainerModel(BaseModel, extra=Extra.forbid):
    docker_image_ref: str
    command: Optional[List[str]]


class WorkerPoolModel(BaseModel, extra=Extra.forbid):
    python_package: Optional[PythonPackageModel]
    container: Optional[ContainerModel]
    args: Optional[List[Union[str, float, int]]]
    env: Optional[Dict[str, str]]
    machine_type: str = "n1-standard-4"
    gpu: Optional[GPU]
    boot_disk: Optional[Disk]
    replica_count: int = 1

    # _machine_type = validator("machine_type", allow_reuse=True)(validators.validate_machine_type)

    @root_validator
    def one_from_python_or_container_spec_must_be_set(cls, values):  # pylint: disable=no-self-argument,no-self-use
        if values.get("python_package") and values.get("container"):
            raise ValueError("Only one of python_package or container can be set")
        if not values.get("python_package") and not values.get("container"):
            raise ValueError("At least one of python_package or container must be set")
        return values


class ReductionServerModel(BaseModel, extra=Extra.forbid):
    replica_count: int
    machine_type: str
    container_uri: str


class IntegerParameter(BaseModel, extra=Extra.forbid):
    type: Literal["integer"]
    var_name: str
    min: int
    max: int
    scale: Literal["log", "linear"] = "linear"


class DoubleParameter(BaseModel, extra=Extra.forbid):
    type: Literal["double"]
    var_name: str
    min: float
    max: float
    scale: Literal["log", "linear"] = "linear"


class CategoricalParameter(BaseModel, extra=Extra.forbid):
    type: Literal["categorical"]
    var_name: str
    values: List[str]


class DiscreteParameter(BaseModel, extra=Extra.forbid):
    type: Literal["discrete"]
    var_name: str
    scale: Literal["log", "linear"] = "linear"
    values: List[int]


HyperParamater = Annotated[
    Union[IntegerParameter, DoubleParameter, CategoricalParameter, DiscreteParameter], Field(discriminator="type")
]


class HyperparameterTuning(BaseModel):
    metrics: Dict[str, Literal["minimize", "maximize"]]
    parameters: List[HyperParamater]
    max_trial_count: int = 15
    parallel_trial_count: int = 3
    search_algorithm: Optional[Literal["grid", "random"]]


class BaseCustomJobModel(BaseInstanceModel):
    region: str
    enable_web_access: bool = False
    bucket: str
    base_output_directory: Optional[str]
    tensorboard_ref: Optional[str]
    timeout_seconds: int = 60 * 60 * 24  # 24 hours

    @root_validator(pre=False)
    def _set_base_output_directory_if_not_provided(  # pylint: disable=no-self-argument,no-self-use
        cls, values: Dict[str, Any]
    ) -> Dict[str, Any]:
        if not values.get("base_output_directory"):
            values["base_output_directory"] = f"gs://{values.get('bucket')}/jobs/{values.get('name')}/outputs"
        return values

    @root_validator(pre=False)
    def _service_account_must_be_set_when_using_tensorboard(  # pylint: disable=no-self-argument,no-self-use
        cls, values: Dict[str, Any]
    ) -> Dict[str, Any]:
        if values.get("tensorboard_ref") and not values.get("service_account"):
            raise ValueError("service_account must be set when using tensorboard in jobs")
        return values


# https://cloud.google.com/vertex-ai/docs/training/create-custom-job
class CustomJobModel(BaseCustomJobModel):
    workers: List[WorkerPoolModel]
    hp_tuning: Optional[HyperparameterTuning]

    @validator("workers", pre=False)
    def _worker_pool_must_have_same_spec(  # pylint: disable=no-self-argument,no-self-use
        cls, workers: List[WorkerPoolModel]
    ) -> List[WorkerPoolModel]:
        if workers:
            python_packages = list(filter(lambda w: w.python_package is not None, workers))
            containers = list(filter(lambda w: w.container is not None, workers))
            if len(python_packages) > 0 and len(containers) > 0:
                raise ValueError(
                    "CustomJobs must be of the same spec. " "Either just based on python_package or container"
                )

        return workers


# https://cloud.google.com/vertex-ai/docs/training/create-training-pipeline
class TrainingCustomJobModel(BaseCustomJobModel):
    worker: WorkerPoolModel
    reduction_server: Optional[ReductionServerModel]


class CustomJobType(Enum):
    CustomContainerTrainingJob = "CustomContainerTrainingJob"
    CustomPythonPackageTrainingJob = "CustomPythonPackageTrainingJob"
    CustomJob = "CustomJob"


class BaseJobManifest(BaseModel, extra=Extra.forbid, validate_assignment=True, arbitrary_types_allowed=True):
    job_type: CustomJobType
    job_payload: Dict[str, Any]
    image_refs: List[str] = []
    tensorboard: Optional[str]
    network: str


class CustomJobManifest(BaseJobManifest):
    job_config: CustomJobModel


class CustomPythonPackageTrainingJobManifest(BaseJobManifest):
    job_config: TrainingCustomJobModel


class CustomContainerTrainingJobManifest(BaseJobManifest):
    job_config: TrainingCustomJobModel


JobManifest = Union[CustomJobManifest, CustomPythonPackageTrainingJobManifest, CustomContainerTrainingJobManifest]
