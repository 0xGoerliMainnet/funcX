import uuid
from unittest import mock

from globus_compute_endpoint.engines.globus_compute import GlobusComputeEngine


def test_docker(tmp_path, uri="funcx/kube-endpoint:main-3.10"):
    gce = GlobusComputeEngine(address="127.0.0.1", docker_container_uri=uri)

    gce.executor.start = mock.MagicMock()
    gce.start(endpoint_id=uuid.uuid4(), run_dir=tmp_path)

    assert gce.executor.launch_cmd
    assert gce.executor.launch_cmd.startswith("docker run")
    assert uri in gce.executor.launch_cmd
    gce.executor.start.assert_called()
    # No cleanup necessary because HTEX was not started


def test_apptainer(tmp_path, uri="/tmp/kube-endpoint.sif"):
    gce = GlobusComputeEngine(address="127.0.0.1", apptainer_container_uri=uri)

    gce.executor.start = mock.MagicMock()
    gce.start(endpoint_id=uuid.uuid4(), run_dir=tmp_path)

    assert gce.executor.launch_cmd
    assert gce.executor.launch_cmd.startswith("apptainer run")
    assert uri in gce.executor.launch_cmd
    gce.executor.start.assert_called()


def test_no_container(tmp_path):
    gce = GlobusComputeEngine(address="127.0.0.1")
    original = gce.executor.launch_cmd

    gce.executor.start = mock.MagicMock()
    gce.start(endpoint_id=uuid.uuid4(), run_dir=tmp_path)

    assert gce.executor.launch_cmd == original
