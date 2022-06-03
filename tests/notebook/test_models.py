import unittest

import pytest
from mock import patch
from pydantic.error_wrappers import ValidationError

from tests.mocks import mocks
from wanna.cli.models.notebook import (
    ManagedNotebookModel,
    Network,
    NotebookDisk,
    NotebookEnvironment,
    NotebookGPU,
    NotebookModel,
)


@patch("wanna.cli.utils.gcp.gcp.MachineTypesClient", mocks.MockMachineTypesClient)
@patch("wanna.cli.utils.gcp.gcp.ImagesClient", mocks.MockImagesClient)
@patch("wanna.cli.utils.gcp.gcp.ZonesClient", mocks.MockZonesClient)
@patch("wanna.cli.utils.gcp.validators.get_credentials", mocks.mock_get_credentials)
@patch("wanna.cli.utils.gcp.gcp.get_credentials", mocks.mock_get_credentials)
@patch("wanna.cli.utils.config_loader.get_credentials", mocks.mock_get_credentials)
@patch("wanna.cli.utils.io.get_credentials", mocks.mock_get_credentials)
class TestNotebookModel(unittest.TestCase):
    def test_notebook_environment_only_container_is_set(self):
        try:
            _ = NotebookEnvironment.parse_obj(
                {
                    "docker_image_ref": "ravenclaw",
                }
            )
        except ValidationError:
            assert False, "Specifying the container image must be enough for notebook environment"

    def test_notebook_environment_container_and_vm_image_is_set(self):
        with pytest.raises(ValidationError):
            _ = NotebookEnvironment.parse_obj(
                {
                    "docker_ref": "some-defined-docker-image",
                    "vm_image": {"framework": "tf", "version": "ent-2-3-cu110"},
                }
            )

    def test_notebook_environment_vm_image(self):
        model = NotebookEnvironment.parse_obj(
            {
                "vm_image": {"framework": "tf", "version": "ent-2-3-cu110"},
            }
        )
        assert model.vm_image.framework == "tf"
        assert model.vm_image.version == "ent-2-3-cu110"

    def test_notebook_network_short_name(self):
        network_id = "network-x"
        try:
            _ = Network.parse_obj({"network_id": network_id})
        except ValidationError:
            assert False, f"network_id {network_id} raised an exception during validation"

    def test_notebook_network_full_name(self):
        network_id = "projects/gcp-project/global/networks/network-x"
        try:
            _ = Network.parse_obj({"network_id": network_id})
        except ValidationError:
            assert False, f"network_id {network_id} raised an exception during validation"

    def test_notebook_disk_valid_type(self):
        disk_type = "pd_ssd"
        try:
            _ = NotebookDisk.parse_obj({"disk_type": disk_type, "size_gb": 500})
        except ValidationError:
            assert False, f"Disk type {disk_type} raised an exception during validation"

    def test_notebook_gpu_type_invalid(self):
        gpu_type = "super_tesla"
        with pytest.raises(ValidationError):
            _ = NotebookGPU.parse_obj({"count": 1, "accelerator_type": gpu_type})

    def test_notebook_invalid_machine_type(self):
        machine_type = "expelliarmus"
        with pytest.raises(ValidationError):
            _ = NotebookModel.parse_obj(
                {
                    "name": "my-notebook",
                    "project_id": "gcp-project",
                    "zone": "europe-west4-a",
                    "machine_type": machine_type,
                }
            )


class TestManagedNotebookModel(unittest.TestCase):
    def test_kernel(self):
        kernel1 = "gcr.io/projectId/imageName1"
        kernel2 = "gcr.io/projectId/imageName2"
        with pytest.raises(ValidationError):
            _ = ManagedNotebookModel.parse_obj(
                {
                    "name": "jacek-notebook",
                    "owner": "jacek.hebda@avast.com",
                    "kernels": [kernel1, kernel2],
                }
            )
