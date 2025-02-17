import uuid
from unittest import mock

import globus_compute_sdk as gc
import pytest
from globus_compute_sdk import ContainerSpec
from globus_compute_sdk.errors import TaskExecutionFailed
from globus_compute_sdk.sdk.utils import get_env_details
from globus_compute_sdk.sdk.web_client import (
    FunctionRegistrationData,
    FunctionRegistrationMetadata,
)
from globus_compute_sdk.serialize import ComputeSerializer
from globus_compute_sdk.serialize.concretes import SELECTABLE_STRATEGIES


@pytest.fixture(autouse=True)
def _clear_sdk_env(monkeypatch):
    monkeypatch.delenv("FUNCX_SDK_ENVIRONMENT", raising=False)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"foo": "bar"},
        {"environment": "dev", "fx_authorizer": "blah"},
        {"asynchronous": True},
    ],
)
def test_client_warns_on_unknown_kwargs(kwargs):
    known_kwargs = [
        "funcx_home",
        "environment",
        "funcx_service_address",
        "do_version_check",
        "code_serialization_strategy",
        "data_serialization_strategy",
        "login_manager",
    ]
    unknown_kwargs = [k for k in kwargs if k not in known_kwargs]

    with pytest.warns(UserWarning) as warnings:
        _ = gc.Client(do_version_check=False, login_manager=mock.Mock(), **kwargs)

    assert len(warnings) == len(unknown_kwargs)
    for warning in warnings:
        assert any(k in str(warning.message) for k in unknown_kwargs)


@pytest.mark.parametrize("env", [None, "local", "dev", "production"])
@pytest.mark.parametrize("usage_method", ["env_var", "param"])
@pytest.mark.parametrize("explicit_params", [None, "web"])
def test_client_init_sets_addresses_by_env(
    monkeypatch, env, usage_method, explicit_params, randomstring
):
    if env in (None, "production"):
        web_uri = "https://compute.api.globus.org"
    elif env == "dev":
        web_uri = "https://api.dev.funcx.org"
    elif env == "local":
        web_uri = "http://localhost:5000"
    else:
        raise NotImplementedError

    # default kwargs: turn off external interactions
    kwargs = {
        "login_manager": mock.Mock(),
        "do_version_check": False,
    }
    # either pass the env as a param or set it in the environment
    if usage_method == "param":
        kwargs["environment"] = env
    elif usage_method == "env_var":
        if env is not None:
            monkeypatch.setenv("FUNCX_SDK_ENVIRONMENT", env)
    else:
        raise NotImplementedError

    # create the client, either with just the input env or with explicit parameters
    # for explicit params, alter the expected URI(s)
    if not explicit_params:
        client = gc.Client(**kwargs)
    elif explicit_params == "web":
        web_uri = f"http://{randomstring()}.fqdn:1234/{randomstring()}"
        client = gc.Client(funcx_service_address=web_uri, **kwargs)
    else:
        raise NotImplementedError

    # finally, confirm that the address was set correctly
    assert client.funcx_service_address == web_uri


@pytest.mark.parametrize(
    "api_data",
    [
        {"status": "Success"},  # success without result|exception
        {"status": "FAILED"},  # failure without result|exception
        "abc123",  # string, but not valid JSON
    ],
)
def test_update_task_table_on_invalid_data(api_data):
    gcc = gc.Client(do_version_check=False, login_manager=mock.Mock())

    with pytest.raises(ValueError):
        gcc._update_task_table(api_data, "task-id-foo")


def test_update_task_table_on_exception():
    api_data = {
        "status": "success",
        "exception": "foo-bar-baz",
        "completion_t": "1.1",
        "task_id": "task-id-foo",
    }
    gcc = gc.Client(do_version_check=False, login_manager=mock.Mock())

    with pytest.raises(TaskExecutionFailed) as excinfo:
        gcc._update_task_table(api_data, "task-id-foo")
    assert "foo-bar-baz" in str(excinfo.value)


def test_update_task_table_simple_object(randomstring):
    serde = ComputeSerializer()
    gcc = gc.Client(do_version_check=False, login_manager=mock.Mock())
    task_id = "some_task_id"

    payload = randomstring()
    data = {"task_id": task_id, "status": "success", "completion_t": "1.1"}
    data["result"] = serde.serialize(payload)

    st = gcc._update_task_table(data, task_id)
    assert not st["pending"]
    assert st["result"] == payload
    assert "exception" not in st


def test_pending_tasks_always_fetched():
    should_fetch_01 = str(uuid.uuid4())
    should_fetch_02 = str(uuid.uuid4())
    no_fetch = str(uuid.uuid4())

    gcc = gc.Client(do_version_check=False, login_manager=mock.Mock())
    gcc.web_client = mock.MagicMock()
    gcc._task_status_table.update(
        {
            should_fetch_01: {"pending": True, "task_id": should_fetch_01},
            no_fetch: {"pending": False, "task_id": no_fetch},
        }
    )
    task_id_list = [no_fetch, should_fetch_01, should_fetch_02]

    # bulk avenue
    gcc.get_batch_result(task_id_list)

    args, _ = gcc.web_client.get_batch_status.call_args
    assert should_fetch_01 in args[0]
    assert should_fetch_02 in args[0]
    assert no_fetch not in args[0]

    # individual avenue
    for should_fetch, sf in (
        (True, should_fetch_01),
        (True, should_fetch_02),
        (False, no_fetch),
    ):
        gcc.web_client.get_task.reset_mock()
        gcc.get_task(sf)
        assert should_fetch is gcc.web_client.get_task.called
        if should_fetch:
            args, _ = gcc.web_client.get_task.call_args
            assert sf == args[0]


@pytest.mark.parametrize("create_result_queue", [True, False, None])
def test_batch_created_websocket_queue(create_result_queue):
    eid = str(uuid.uuid4())
    fid = str(uuid.uuid4())

    gcc = gc.Client(do_version_check=False, login_manager=mock.Mock())
    gcc.web_client = mock.MagicMock()
    if create_result_queue is None:
        batch = gcc.create_batch()
    else:
        batch = gcc.create_batch(create_websocket_queue=create_result_queue)

    batch.add(fid, (1,))
    batch.add(fid, (2,))

    gcc.batch_run(eid, batch)

    assert gcc.web_client.submit.called
    *_, submit_data = gcc.web_client.submit.call_args[0]
    assert "create_queue" in submit_data
    assert submit_data["create_queue"] is bool(create_result_queue)


@pytest.mark.parametrize(
    "strategy", [s for s in SELECTABLE_STRATEGIES if not s._for_code]
)
def test_batch_respects_serialization_strategy(strategy):
    gcc = gc.Client(
        data_serialization_strategy=strategy(),
        do_version_check=False,
        login_manager=mock.Mock(),
    )

    fn_id = str(uuid.uuid4())
    args = (1, 2, 3)
    kwargs = {"a": "b", "c": "d"}

    batch = gcc.create_batch()
    batch.add(fn_id, args, kwargs)
    tasks = batch.prepare()["tasks"]

    ser = ComputeSerializer(strategy_data=strategy())
    expected = {fn_id: [ser.pack_buffers([ser.serialize(args), ser.serialize(kwargs)])]}

    assert tasks == expected


def test_build_container(mocker, login_manager):
    mock_data = mocker.Mock()
    mock_data.data = {"container_id": "123-456"}
    login_manager.get_web_client.post = mocker.Mock(return_value=mock_data)
    gcc = gc.Client(do_version_check=False, login_manager=login_manager)
    spec = ContainerSpec(
        name="MyContainer",
        pip=[
            "matplotlib==3.5.1",
            "numpy==1.18.5",
        ],
        python_version="3.8",
        payload_url="https://github.com/funcx-faas/funcx-container-service.git",
    )

    container_id = gcc.build_container(spec)
    assert container_id == "123-456"
    login_manager.get_web_client.post.assert_called()
    calls = login_manager.get_web_client.post.call_args
    assert calls[0][0] == "/v2/containers/build"
    assert calls[1] == {"data": spec.to_json()}


def test_container_build_status(mocker, login_manager, randomstring):
    expected_status = randomstring()

    class MockData(dict):
        def __init__(self):
            self["status"] = expected_status
            self.http_status = 200

    login_manager.get_web_client.get = mocker.Mock(return_value=MockData())
    gcc = gc.Client(do_version_check=False, login_manager=login_manager)
    status = gcc.get_container_build_status("123-434")
    assert status == expected_status


def test_container_build_status_not_found(mocker, login_manager):
    class MockData(dict):
        def __init__(self):
            self.http_status = 404

    login_manager.get_web_client.get = mocker.Mock(return_value=MockData())
    gcc = gc.Client(do_version_check=False, login_manager=login_manager)

    with pytest.raises(ValueError) as excinfo:
        gcc.get_container_build_status("123-434")

    assert excinfo.value.args[0] == "Container ID 123-434 not found"


def test_container_build_status_failure(mocker, login_manager):
    class MockData(dict):
        def __init__(self):
            self.http_status = 500
            self.http_reason = "This is a reason"

    login_manager.get_web_client.get = mocker.Mock(return_value=MockData())
    gcc = gc.Client(do_version_check=False, login_manager=login_manager)

    with pytest.raises(SystemError) as excinfo:
        gcc.get_container_build_status("123-434")

    assert type(excinfo.value) is SystemError


def test_register_function():
    gcc = gc.Client(do_version_check=False, login_manager=mock.Mock())
    gcc.web_client = mock.MagicMock()

    def funk():
        return "Funky"

    metadata = {"python_version": "3.11.3", "sdk_version": "2.3.3"}
    gcc.register_function(funk, metadata=metadata)

    a, _ = gcc.web_client.register_function.call_args
    func_data = a[0]
    assert isinstance(func_data, FunctionRegistrationData)
    assert func_data.function_code is not None
    assert isinstance(func_data.metadata, FunctionRegistrationMetadata)
    assert func_data.metadata.python_version == metadata["python_version"]
    assert func_data.metadata.sdk_version == metadata["sdk_version"]


def test_register_function_no_metadata():
    gcc = gc.Client(do_version_check=False, login_manager=mock.Mock())
    gcc.web_client = mock.MagicMock()

    def funk():
        return "Funky"

    gcc.register_function(funk)

    a, _ = gcc.web_client.register_function.call_args
    func_data = a[0]
    assert isinstance(func_data, FunctionRegistrationData)
    assert func_data.metadata is None


def test_get_function():
    func_uuid_str = str(uuid.uuid4())
    gcc = gc.Client(do_version_check=False, login_manager=mock.Mock())
    gcc.web_client = mock.MagicMock()

    gcc.get_function(func_uuid_str)

    gcc.web_client.get_function.assert_called_with(func_uuid_str)


def test_delete_function():
    func_uuid_str = str(uuid.uuid4())
    gcc = gc.Client(do_version_check=False, login_manager=mock.Mock())
    gcc.web_client = mock.MagicMock()

    gcc.delete_function(func_uuid_str)

    gcc.web_client.delete_function.assert_called_with(func_uuid_str)


def test_missing_task_info(mocker, login_manager):
    tid1 = str(uuid.uuid4())
    tid1_reason = "XYZ tid1"
    tid2 = str(uuid.uuid4())

    mock_resp = {
        "response": "batch",
        "results": {
            tid1: {
                "task_id": tid1,
                "status": "failed",
                "reason": tid1_reason,
            },
            tid2: {
                "task_id": tid2,
                "status": "failed",
            },
        },
    }
    login_manager.get_web_client.get_batch_status = mocker.Mock(return_value=mock_resp)
    gcc = gc.Client(do_version_check=False, login_manager=login_manager)

    gcc.web_client.base_url = "https://a.g.org"
    res = gcc.get_batch_result([tid1, tid2])

    assert tid1 in res
    assert res[tid1]["pending"] is False
    assert res[tid1]["reason"] == tid1_reason
    assert tid2 in res
    assert res[tid2]["reason"] == "unknown"


@pytest.mark.parametrize(
    "dill_python",
    [
        [None, None],
        # Impossible dill and python versions will always warn
        ["000.dill", None],
        [None, "1.2.3"],
        ["000.dill", "1.2.3"],
    ],
)
def test_version_mismatch_from_details(
    mocker, login_manager, mock_response, dill_python
):
    worker_dill, worker_python = dill_python

    # Returned env same as client by default
    response_details = get_env_details()
    if worker_dill:
        response_details["dill_version"] = worker_dill
    if worker_python:
        response_details["python_version"] = worker_python

    tid = str(uuid.uuid4())
    stack_trace = "Some Stack Trace\n  Line 40 stack trace"
    returned_task = {
        "task_id": tid,
        "status": "success",
        "completion_t": "1677183605.212898",
        "exception": stack_trace,
        "details": {
            "os": "Linux-5.19.0-1025-aws-x86_64-with-glibc2.35",
            "python_version": response_details["python_version"],
            "dill_version": response_details["dill_version"],
            "globus_compute_sdk_version": "2.3.2",
            "task_transitions": {
                "execution-start": 1692742841.843334,
                "execution-end": 1692742846.123456,
            },
        },
    }

    mock_log = mocker.patch("globus_compute_sdk.sdk.client.logger")

    login_manager.get_web_client.get_task = mocker.Mock(
        return_value=mock_response(200, returned_task)
    )
    gcc = gc.Client(do_version_check=False, login_manager=login_manager)

    with pytest.raises(TaskExecutionFailed) as exc_info:
        gcc.get_task(tid)

    assert "Some Stack Trace" in str(exc_info)
    should_warn = worker_dill is not None or worker_python is not None
    assert mock_log.warning.called == should_warn
    if worker_dill or worker_python:
        a, *_ = mock_log.warning.call_args
        assert "Warning:  Client SDK uses" in a[0]
        if worker_dill and worker_python:
            assert f"but worker used {worker_python}/{worker_dill}" in a[0]
        if worker_python:
            assert f"but worker used {worker_python}" in a[0]
        if worker_dill:
            assert f"/{worker_dill} (OS" in a[0]
