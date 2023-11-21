import uuid

from globus_compute_endpoint.engines import GlobusComputeEngine


def platinfo():
    import platform
    import sys

    return platform.uname(), sys.version_info


def test_docker(tmp_path):
    gce = GlobusComputeEngine(
        address="127.0.0.1",
        label="GCE_TEST",
        docker_container_uri="funcx/kube-endpoint:main-3.10",
        container_cmd_options="--FABRICATED",
    )
    gce.start(endpoint_id=uuid.uuid4(), run_dir="/tmp")
    container_launch_cmd = gce.executor.launch_cmd
    expected = (
        "docker run --FABRICATED -v /tmp:/tmp -t "
        "funcx/kube-endpoint:main-3.10 process_worker_pool.py"
    )
    assert container_launch_cmd.startswith(expected)

    gce.shutdown()


def test_apptainer(tmp_path):
    gce = GlobusComputeEngine(
        address="127.0.0.1",
        label="GCE_TEST",
        apptainer_container_uri="APPTAINER_PATH",
        container_cmd_options="--FABRICATED",
    )
    gce.start(endpoint_id=uuid.uuid4(), run_dir="/tmp")
    container_launch_cmd = gce.executor.launch_cmd
    expected = "apptainer run --FABRICATED APPTAINER_PATH process_worker_pool.py"
    assert container_launch_cmd.startswith(expected)

    gce.shutdown()


def test_singularity(tmp_path):
    gce = GlobusComputeEngine(
        address="127.0.0.1",
        max_workers=1,
        label="GCE_TEST",
        singularity_container_uri="/home/yadunand/kube-endpoint.py3.9.sif",
        container_cmd_options="",
    )
    gce.start(endpoint_id=uuid.uuid4(), run_dir="/tmp")
    container_launch_cmd = gce.executor.launch_cmd
    expected = (
        "singularity run  /home/yadunand/kube-endpoint.py3.9.sif process_worker_pool.py"
    )
    assert container_launch_cmd.startswith(expected)

    gce.shutdown()
