from __future__ import annotations

import concurrent.futures
import json
import logging
import os
import platform
import queue
import random
import sys
import threading
import time
import typing as t
import uuid
import warnings
from collections import defaultdict

from globus_compute_sdk.sdk.hardware_report import run_hardware_report

if sys.version_info >= (3, 8):
    from concurrent.futures import InvalidStateError
else:

    class InvalidStateError(Exception):
        pass


import pika
from globus_compute_common import messagepack
from globus_compute_common.messagepack.message_types import Result
from globus_compute_sdk import __version__
from globus_compute_sdk.errors import TaskExecutionFailed
from globus_compute_sdk.sdk.asynchronous.compute_future import ComputeFuture
from globus_compute_sdk.sdk.client import Client
from globus_compute_sdk.sdk.utils import chunk_by
from globus_compute_sdk.sdk.utils.uuid_like import (
    UUID_LIKE_T,
    as_optional_uuid,
    as_uuid,
)

log = logging.getLogger(__name__)

if t.TYPE_CHECKING:
    import pika.exceptions
    from pika.channel import Channel
    from pika.frame import Method
    from pika.spec import Basic, BasicProperties


_REGISTERED_FXEXECUTORS: dict[int, t.Any] = {}


def __executor_atexit():
    threading.main_thread().join()
    to_shutdown = list(_REGISTERED_FXEXECUTORS.values())
    while to_shutdown:
        gce = to_shutdown.pop()
        gce.shutdown()


threading.Thread(target=__executor_atexit).start()


class _TaskSubmissionInfo:
    __slots__ = (
        "task_num",
        "task_group_uuid",
        "endpoint_uuid",
        "function_uuid",
        "user_endpoint_config",
        "args",
        "kwargs",
    )

    def __init__(
        self,
        *,
        task_num: int,
        task_group_id: UUID_LIKE_T | None,
        endpoint_id: UUID_LIKE_T,
        function_id: UUID_LIKE_T,
        user_endpoint_config: dict[str, t.Any] | None,
        args: tuple,
        kwargs: dict,
    ):
        self.task_num = task_num
        self.task_group_uuid = as_optional_uuid(task_group_id)
        self.function_uuid = as_uuid(function_id)
        self.endpoint_uuid = as_uuid(endpoint_id)
        self.user_endpoint_config = user_endpoint_config
        self.args = args
        self.kwargs = kwargs

    def __repr__(self):
        attrs = (
            f"task_num={self.task_num}",
            f"task_group_uuid='{self.task_group_uuid}'",
            f"endpoint_uuid='{self.endpoint_uuid}'",
            f"function_uuid='{self.function_uuid}'",
            f"user_endpoint_config={{{len(self.user_endpoint_config or {})}}}",
            f"args=[{len(self.args)}]",
            f"kwargs=[{len(self.kwargs)}]",
        )
        return f"{type(self).__name__}({'; '.join(attrs)})"


class Executor(concurrent.futures.Executor):
    """
    Extend Python's |Executor|_ base class for Globus Compute's purposes.

    .. |Executor| replace:: ``Executor``
    .. _Executor: https://docs.python.org/3/library/concurrent.futures.html#executor-objects
    """  # noqa

    def __init__(
        self,
        endpoint_id: UUID_LIKE_T | None = None,
        container_id: UUID_LIKE_T | None = None,
        client: Client | None = None,
        task_group_id: UUID_LIKE_T | None = None,
        user_endpoint_config: dict[str, t.Any] | None = None,
        label: str = "",
        batch_size: int = 128,
        funcx_client: Client | None = None,
        amqp_port: int | None = None,
        **kwargs,
    ):
        """
        :param endpoint_id: id of the endpoint to which to submit tasks
        :param container_id: id of the container in which to execute tasks
        :param client: instance of Client to be used by the executor.  If not provided,
            the executor will instantiate one with default arguments.
        :param task_group_id: The Task Group to which to associate tasks.  If not set,
            one will be instantiated.
        :param user_endpoint_config: User endpoint configuration values as described
            and allowed by endpoint administrators. Must be a JSON-serializable dict
            or None.
        :param label: a label to name the executor; mainly utilized for
            logging and advanced needs with multiple executors.
        :param batch_size: the maximum number of tasks to coalesce before
            sending upstream [min: 1, default: 128]
        :param funcx_client: [DEPRECATED] alias for client.
        :param batch_interval: [DEPRECATED; unused] number of seconds to coalesce tasks
            before submitting upstream
        :param batch_enabled: [DEPRECATED; unused] whether to batch results
        :param amqp_port: Port to use when connecting to results queue. Note that the
            Compute web services only support 5671, 5672, and 443.
        """
        deprecated_kwargs = {"batch_interval", "batch_enabled"}
        for key in kwargs:
            if key in deprecated_kwargs:
                warnings.warn(
                    f"'{key}' is not utilized and will be removed in a future release",
                    DeprecationWarning,
                )
                continue
            msg = f"'{key}' is an invalid argument for {self.__class__.__name__}"
            raise TypeError(msg)

        if funcx_client:
            warnings.warn(
                "'funcx_client' is superseded by 'client'"
                " and will be removed in a future release",
                DeprecationWarning,
            )

        if not client:
            client = funcx_client if funcx_client else Client()

        self.client = client

        self.endpoint_id = endpoint_id

        # mypy ... sometimes you just ain't too bright
        self.container_id = container_id  # type: ignore[assignment]

        self._task_group_id: uuid.UUID | None = None  # help mypy out
        self.task_group_id = task_group_id

        self._amqp_port: int | None = None
        self.amqp_port = amqp_port

        self.user_endpoint_config = user_endpoint_config

        self.label = label
        self.batch_size = max(1, batch_size)

        self.task_count_submitted = 0
        self._task_counter: int = 0
        self._tasks_to_send: queue.Queue[
            tuple[ComputeFuture, _TaskSubmissionInfo] | tuple[None, None]
        ] = queue.Queue()
        self._function_registry: dict[tuple[t.Callable, str | None], str] = {}

        self._stopped = False
        self._stopped_in_error = False
        self._shutdown_lock = threading.RLock()
        self._result_watcher: _ResultWatcher | None = None

        log.debug("%r: initiated on thread: %s", self, threading.get_ident())
        self._task_submitter = threading.Thread(
            target=self._task_submitter_impl, name="TaskSubmitter", daemon=True
        )
        self._task_submitter.start()
        _REGISTERED_FXEXECUTORS[id(self)] = self

    def __repr__(self) -> str:
        name = self.__class__.__name__
        label = self.label and f"{self.label}; " or ""
        ep_id = self.endpoint_id and f"ep_id:{self.endpoint_id}; " or ""
        c_id = self.container_id and f"c_id:{self.container_id}; " or ""
        tg_id = f"tg_id:{self.task_group_id}; "
        bs = f"bs:{self.batch_size}"
        return f"{name}<{label}{ep_id}{c_id}{tg_id}{bs}>"

    @property
    def task_group_id(self):
        """
        The Task Group with which this instance is currently associated.  New tasks will
        be sent to this Task Group upstream, and the result listener will only listen
        for results for this group.

        Must be a UUID, valid uuid-like string, or None.  Set by simple assignment::

            >>> import uuid
            >>> from globus_compute_sdk import Executor
            >>> tg_id = uuid.uuid4()  # IRL: some *known* taskgroup id
            >>> gce = Executor(task_group_id=tg_id)

            # Alternatively, may use a stringified uuid:
            >>> gce = Executor(task_group_id=str(tg_id))

            # May also alter after construction:
            >>> gce.task_group_id = tg_id
            >>> gce.task_group_id = str(tg_id)

            # Internally, it is always stored as a UUID (or None):
            >>> gce.task_group_id
            UUID('11111111-2222-4444-8888-000000000000')

        This is typically used when reattaching to a previously initiated set
        of tasks.  See `reload_tasks()`_ for more information.

        If not set manually, this will be set automatically on `submit()`_, to
        a Task Group ID supplied by the services.

        [default: ``None``]
        """
        return self._task_group_id

    @task_group_id.setter
    def task_group_id(self, task_group_id: UUID_LIKE_T | None):
        self._task_group_id = as_optional_uuid(task_group_id)

    @property
    def user_endpoint_config(self) -> dict[str, t.Any] | None:
        """
        The endpoint configuration values, as described and allowed by endpoint
        administrators, that this instance is currently associated with.

        Must be a JSON-serializable dict or None. Set by simple assignment::

            >>> from globus_compute_sdk import Executor
            >>> uep_config = {"foo": "bar"}
            >>> gce = Executor(user_endpoint_config=uep_config)

            # May also alter after construction:
            >>> gce.user_endpoint_config = uep_config
        """
        return self._user_endpoint_config

    @user_endpoint_config.setter
    def user_endpoint_config(self, val: dict | None):
        if val is not None:
            if not isinstance(val, dict):
                raise TypeError(
                    f"Config must be of type dict, not {type(val).__name__}"
                )
            try:
                json.dumps(val)
            except TypeError:
                raise TypeError("Configuration must be JSON-serializable")
        self._user_endpoint_config = val

    @property
    def container_id(self) -> uuid.UUID | None:
        """
        The container id with which this Executor instance is currently associated.
        Tasks submitted after this is set will use this container.

        Must be a UUID, valid uuid-like string, or None.  Set by simple assignment::

            >>> import uuid
            >>> from globus_compute_sdk import Executor
            >>> c_id = "00000000-0000-0000-0000-000000000000"  # some known container id
            >>> c_as_uuid = uuid.UUID(c_id)
            >>> gce = Executor(container_id=c_id)

            # May also alter after construction:
            >>> gce.container_id = c_id
            >>> gce.container_id = c_as_uuid  # also accepts a UUID object

            # Internally, it is always stored as a UUID (or None):
            >>> gce.container_id
            UUID('00000000-0000-0000-0000-000000000000')

        [default: ``None``]
        """
        return self._container_id

    @container_id.setter
    def container_id(self, c_id: UUID_LIKE_T | None):
        self._container_id = as_optional_uuid(c_id)

    @property
    def amqp_port(self) -> int | None:
        """
        The port to use when connecting to the result queue. Can be one of 443, 5671,
        5672, or None. If None, the port is assigned by the Compute web services
        (typically 5671).
        """
        return self._amqp_port

    @amqp_port.setter
    def amqp_port(self, p: int | None):
        self._amqp_port = p

    def _fn_cache_key(self, fn: t.Callable):
        return fn, self.container_id

    def register_function(
        self, fn: t.Callable, function_id: str | None = None, **func_register_kwargs
    ) -> str:
        """
        Register a task function with this Executor's cache.

        All function execution submissions (i.e., ``.submit()``) communicate which
        pre-registered function to execute on the endpoint by the function's
        identifier, the ``function_id``.  This method makes the appropriate API
        call to the Globus Compute web services to first register the task function, and
        then stores the returned ``function_id`` in the Executor's cache.

        In the standard workflow, ``.submit()`` will automatically handle invoking
        this method, so the common use-case will not need to use this method.
        However, some advanced use-cases may need to fine-tune the registration
        of a function and so may manually set the registration arguments via this
        method.

        If a function has already been registered (perhaps in a previous
        iteration), the upstream API call may be avoided by specifying the known
        ``function_id``.

        If a function already exists in the Executor's cache, this method will
        raise a ValueError to help track down the errant double registration
        attempt.

        :param fn: function to be registered for remote execution
        :param function_id: if specified, associate the ``function_id`` to the
            ``fn`` immediately, short-circuiting the upstream registration call.
        :param func_register_kwargs: all other keyword arguments are passed to
            the ``Client.register_function()``.
        :returns: the function's ``function_id`` string, as returned by
            registration upstream
        """
        if self._stopped:
            raise RuntimeError(f"{self!r} is shutdown; refusing to register function")

        fn_cache_key = self._fn_cache_key(fn)
        if fn_cache_key in self._function_registry:
            msg = f"Function already registered as function id: {function_id}"
            log.error(msg)
            self.shutdown(wait=False, cancel_futures=True)
            raise ValueError(msg)

        if function_id:
            self._function_registry[fn_cache_key] = function_id
            return function_id

        log.debug("Function not registered. Registering: %s", fn)
        func_register_kwargs.pop("function", None)  # just to be sure
        reg_kwargs = {
            "function_name": fn.__name__,
            "container_uuid": self.container_id,
            "metadata": {
                "python_version": platform.python_version(),
                "sdk_version": __version__,
            },
        }
        reg_kwargs.update(func_register_kwargs)

        try:
            func_reg_id = self.client.register_function(fn, **reg_kwargs)
        except Exception:
            log.error(f"Unable to register function: {fn.__name__}")
            self.shutdown(wait=False, cancel_futures=True)
            raise
        self._function_registry[fn_cache_key] = func_reg_id
        log.debug("Function registered with id: %s", func_reg_id)
        return func_reg_id

    def submit(self, fn, *args, **kwargs):
        """
        .. _submit():

        Submit a function to be executed on the Executor's specified endpoint
        with the given arguments.

        Schedules the callable to be executed as ``fn(*args, **kwargs)`` and
        returns a ComputeFuture instance representing the execution of the
        callable.

        Example use::

            >>> def add(a: int, b: int) -> int: return a + b
            >>> gce = Executor(endpoint_id="some-ep-id")
            >>> fut = gce.submit(add, 1, 2)
            >>> fut.result()    # wait (block) until result is received from remote
            3

        :param fn: Python function to execute on endpoint
        :param args: positional arguments (if any) as required to execute
            the function
        :param kwargs: keyword arguments (if any) as required to execute
            the function
        :returns: a future object that will receive a ``.task_id`` when the
            Globus Compute Web Service acknowledges receipt, and eventually will have
            a ``.result()`` when the Globus Compute web services receive and stream it.
        """
        if self._stopped:
            err = f"{self!r} is shutdown; no new functions may be executed"
            raise RuntimeError(err)

        fn_cache_key = self._fn_cache_key(fn)
        if fn_cache_key not in self._function_registry:
            self.register_function(fn)

        fn_id = self._function_registry[fn_cache_key]
        return self.submit_to_registered_function(
            function_id=fn_id, args=args, kwargs=kwargs
        )

    def submit_to_registered_function(
        self,
        function_id: str,
        args: tuple | None = None,
        kwargs: dict | None = None,
    ):
        """
        Request an execution of an already registered function.

        This method supports use of public functions with the Executor, or
        knowledge of an already registered function.  An example use might be::

            # pre_registration.py
            from globus_compute_sdk import Executor

            def some_processor(*args, **kwargs):
                # ... function logic ...
                return ["some", "result"]

            gce = Executor()
            fn_id = gce.register_function(some_processor)
            print(f"Function registered successfully.\\nFunction ID: {fn_id}")

            # Example output:
            #
            # Function registered successfully.
            # Function ID: c407ae80-b31f-447a-9fa6-124098492057

        In this case, the function would be privately registered to you, but note that
        the function id is just a string.  One could substitute for a publicly
        available function.  For instance, ``b0a5d1a0-2b22-4381-b899-ba73321e41e0`` is
        a "well-known" uuid for the "Hello, World!" function (same as the example in
        the Globus Compute tutorial), which is publicly available::

            from globus_compute_sdk import Executor

            fn_id = "b0a5d1a0-2b22-4381-b899-ba73321e41e0"  # public; "Hello World"
            with Executor(endpoint_id="your-endpoint-id") as fxe:
                futs = [
                    fxe.submit_to_registered_function(function_id=fn_id)
                    for i in range(5)
                ]

            for f in futs:
                print(f.result())

        :param function_id: identifier (str) of registered Python function
        :param args: positional arguments (if any) as required to execute
            the function
        :param kwargs: keyword arguments (if any) as required to execute
            the function
        :returns: a future object that (eventually) will have a ``.result()``
            when the Globus Compute web services receive and stream it.
        """
        if self._stopped:
            err = f"{self!r} is shutdown; no new functions may be executed"
            raise RuntimeError(err)

        if not self.endpoint_id:
            msg = (
                "No endpoint_id set.  Did you forget to set it at construction?\n"
                "  Hint:\n\n"
                "    gce = Executor(endpoint_id=<ep_id>)\n"
                "    gce.endpoint_id = <ep_id>    # alternative"
            )
            self.shutdown(wait=False, cancel_futures=True)
            raise ValueError(msg)

        if args is None:
            args = ()
        if kwargs is None:
            kwargs = {}

        self._task_counter += 1

        task = _TaskSubmissionInfo(
            task_num=self._task_counter,  # unnecessary; maybe useful for debugging?
            task_group_id=self.task_group_id,
            endpoint_id=self.endpoint_id,
            user_endpoint_config=self.user_endpoint_config,
            function_id=function_id,
            args=args,
            kwargs=kwargs,
        )

        fut = ComputeFuture()
        self._tasks_to_send.put((fut, task))
        return fut

    def map(self, fn: t.Callable, *iterables, timeout=None, chunksize=1) -> t.Iterator:
        """
        Globus Compute does not currently implement the `.map()`_ method of the `Executor
        interface`_.  In a naive implementation, this method would merely be
        syntactic sugar for bulk use of the ``.submit()`` method.  For example::

            def map(fxexec, fn, *fn_args_kwargs):
                return [fxexec.submit(fn, *a, **kw) for a, kw in fn_args_kwargs]

        This naive implementation ignores a number of potential optimizations, so
        we have decided to look at this at a future date if there is interest.

        .. _.map(): https://docs.python.org/3/library/concurrent.futures.html#concurrent.futures.Executor.map
        .. _Executor interface: https://docs.python.org/3/library/concurrent.futures.html#executor-objects

        Raises
        ------
        RuntimeError
          if called after shutdown, otherwise, ...

        NotImplementedError
          ... always raised
        """  # noqa
        if self._stopped:
            err = f"{self!r} is shutdown; no new functions may be executed"
            raise RuntimeError(err)

        raise NotImplementedError()

    def reload_tasks(
        self, task_group_id: UUID_LIKE_T | None = None
    ) -> t.Iterable[ComputeFuture]:
        """
        .. _reload_tasks():

        Load the set of tasks associated with this Executor's Task Group from the
        web services and return a list of futures, one for each task.  This is
        nominally intended to "reattach" to a previously initiated session, based on
        the Task Group ID.

        :param task_group_id: Optionally specify a task_group_id to use. If present,
            will overwrite the Executor's task_group_id
        :returns: An iterable of futures.
        :raises ValueError: if the server response is incorrect or invalid
        :raises KeyError: the server did not return an expected response
        :raises various: the usual (unhandled) request errors (e.g., no connection;
            invalid authorization)

        Notes
        -----
        Any previous futures received from this executor will be cancelled.
        """  # noqa
        if task_group_id is not None:
            self.task_group_id = task_group_id

        if self.task_group_id is None:
            raise ValueError("must specify a task_group_id in order to reload tasks")

        # step 1: cleanup!
        if self._result_watcher:
            self._result_watcher.shutdown(wait=False, cancel_futures=True)
            self._result_watcher = None

        task_group_id = self.task_group_id  # snapshot
        assert task_group_id is not None  # mypy: we _just_ proved this

        # step 2: from server, acquire list of related task ids and make futures
        r = self.client.web_client.get_taskgroup_tasks(task_group_id)
        if r["taskgroup_id"] != str(task_group_id):
            msg = (
                "Server did not respond with requested TaskGroup Tasks.  "
                f"(Requested tasks for {task_group_id} but received "
                f"tasks for {r['taskgroup_id']}"
            )
            raise ValueError(msg)

        # step 3: create the associated set of futures
        task_ids: list[str] = [task["id"] for task in r.get("tasks", [])]
        futures: list[ComputeFuture] = []

        if task_ids:
            # Complete the futures that already have results.
            pending: list[ComputeFuture] = []
            deserialize = self.client.fx_serializer.deserialize
            chunk_size = 1024
            num_chunks = len(task_ids) // chunk_size + 1
            for chunk_no, id_chunk in enumerate(
                chunk_by(task_ids, chunk_size), start=1
            ):
                if num_chunks > 1:
                    log.debug(
                        "Large task group (%s tasks; %s chunks); retrieving chunk %s"
                        " (%s tasks)",
                        len(task_ids),
                        num_chunks,
                        chunk_no,
                        len(id_chunk),
                    )

                res = self.client.web_client.get_batch_status(id_chunk)
                for task_id, task in res.data.get("results", {}).items():
                    fut = ComputeFuture(task_id)
                    futures.append(fut)
                    completed_t = task.get("completion_t")
                    if not completed_t:
                        pending.append(fut)
                    else:
                        try:
                            if task.get("status") == "success":
                                fut.set_result(deserialize(task["result"]))
                            else:
                                exc = TaskExecutionFailed(
                                    task["exception"], completed_t
                                )
                                fut.set_exception(exc)
                        except Exception as exc:
                            funcx_err = TaskExecutionFailed(
                                "Failed to set result or exception"
                            )
                            funcx_err.__cause__ = exc
                            fut.set_exception(funcx_err)

            if pending:
                self._result_watcher = _ResultWatcher(self, port=self.amqp_port)
                self._result_watcher.watch_for_task_results(pending)
                self._result_watcher.start()
        else:
            log.warning(f"Received no tasks for Task Group ID: {task_group_id}")

        # step 5: the goods for the consumer
        return futures

    def shutdown(self, wait=True, *, cancel_futures=False):
        thread_id = threading.get_ident()
        log.debug("%r: initiating shutdown (thread: %s)", self, thread_id)
        if self._task_submitter.is_alive():
            log.debug(
                "%r: %s still alive; sending poison pill",
                self,
                self._task_submitter.name,
            )
            self._tasks_to_send.put((None, None))
            if wait and self._task_submitter.ident != thread_id:
                self._task_submitter.join(2)
                if self._task_submitter.is_alive():
                    log.warning(
                        "Task submission thread did not stop in a timely fashion; an"
                        " HTTP request may yet be outstanding.  Attempting to shutdown"
                        " Executor anyway"
                    )

        with self._shutdown_lock:
            self._stopped = True
            if self._result_watcher:
                self._result_watcher.shutdown(wait=wait, cancel_futures=cancel_futures)
                self._result_watcher = None

        if thread_id != self._task_submitter.ident and self._stopped_in_error:
            # In an unhappy path scenario, there's the potential for multiple
            # tracebacks, which means that the tracebacks will likely be
            # interwoven in the logs.  Attempt to make debugging easier for
            # that scenario by adding a slight delay on the *main* thread.
            time.sleep(0.1)
        _REGISTERED_FXEXECUTORS.pop(id(self), None)
        log.debug("%r: shutdown finished (thread: %s)", self, thread_id)

    def get_worker_hardware_details(self) -> str:
        """
        Retrieve hardware information about worker nodes for the endpoint associated
        with this Executor. For example::

            from globus_compute_sdk import Executor
            with Executor(ep_uuid) as gcx:
                print(gcx.get_worker_hardware_details())
        """

        return self.submit(run_hardware_report).result()

    def _task_submitter_impl(self) -> None:
        """
        Coalesce tasks from the interthread queue (``_tasks_to_send``), up to
        ``self.batch_size``, submit them, and then send the futures to the
        ResultWatcher.

        The main job of this method is to loop forever, forwarding task
        requests upstream and the associated futures to the ResultWatcher.

        This thread stops when it receives a poison-pill of ``(None, None)``
        in the queue.  (See ``shutdown()``.)
        """
        _tid = threading.get_ident()
        log.debug("%r: task submission thread started (%s)", self, _tid)
        to_send = self._tasks_to_send  # cache lookup

        # Alias types -- this awkward typing is all about the dict we use
        # internally to make sure we appropriately group tasks for upstream
        # submission.  For example, if the user submitted to two different
        # endpoints, we separate the tasks by the dictionary key.
        class SubmitGroup(t.NamedTuple):
            task_group_uuid: uuid.UUID | None
            endpoint_uuid: uuid.UUID
            user_endpoint_config: dict | None

            def __hash__(self):
                key = (
                    self.task_group_uuid,
                    self.endpoint_uuid,
                    json.dumps(self.user_endpoint_config, sort_keys=True),
                )
                return hash(key)

        SubmitGroupFutures = t.Dict[
            SubmitGroup,
            t.List[ComputeFuture],
        ]
        SubmitGroupTasks = t.Dict[
            SubmitGroup,
            t.List[_TaskSubmissionInfo],
        ]

        try:
            fut: ComputeFuture | None = ComputeFuture()  # just start the loop; please
            while fut is not None:
                futs: SubmitGroupFutures = defaultdict(list)
                tasks: SubmitGroupTasks = defaultdict(list)
                task_count = 0
                try:
                    log.debug("%r (tid:%s): await tasks to send upstream", self, _tid)
                    fut, task = to_send.get()  # Block; wait for first result ...
                    task_count += 1
                    bs = max(1, self.batch_size)  # May have changed while waiting
                    while task is not None:
                        assert fut is not None  # Come on mypy; contextually clear!
                        submit_group = SubmitGroup(
                            task.task_group_uuid,
                            task.endpoint_uuid,
                            task.user_endpoint_config,
                        )
                        tasks[submit_group].append(task)
                        futs[submit_group].append(fut)
                        if any(len(tl) >= bs for tl in tasks.values()):
                            break
                        fut, task = to_send.get(block=False)  # ... don't block again
                        task_count += 1
                except queue.Empty:
                    pass

                if not tasks:
                    continue  # fut and task are None; "single point of exit"

                for submit_group, task_list in tasks.items():
                    fut_list = futs[submit_group]

                    tg_uuid, ep_uuid, uep_config = submit_group
                    log.info(
                        f"Submitting tasks for Task Group {tg_uuid} to"
                        f" Endpoint {ep_uuid}: {len(task_list):,}"
                    )

                    self._submit_tasks(
                        tg_uuid, ep_uuid, uep_config, fut_list, task_list
                    )

                    to_watch = [f for f in fut_list if f.task_id and not f.done()]
                    if not to_watch:
                        continue

                    with self._shutdown_lock:
                        if self._stopped:
                            continue

                        if not (
                            self._result_watcher and self._result_watcher.is_alive()
                        ):
                            # Don't initialize the result watcher unless at least
                            # one batch has been sent
                            self._result_watcher = _ResultWatcher(
                                self, port=self.amqp_port
                            )
                            self._result_watcher.start()
                        try:
                            self._result_watcher.watch_for_task_results(to_watch)
                        except self._result_watcher.__class__.ShuttingDownError:
                            log.debug(
                                "%r (tid:%s): Waiting for previous ResultWatcher"
                                " to shutdown",
                                self,
                                _tid,
                            )
                            self._result_watcher.join()
                            self._result_watcher = _ResultWatcher(
                                self, port=self.amqp_port
                            )
                            self._result_watcher.start()
                            self._result_watcher.watch_for_task_results(to_watch)

                # important to clear futures; else a legitimately early-shutdown
                # request (e.g., __exit__()) can cancel these (finally block,
                # below) before the result comes back, even though _result_watcher
                # is already watching them.
                futs.clear()

                while task_count:
                    task_count -= 1
                    to_send.task_done()

        except Exception as exc:
            self._stopped = True
            self._stopped_in_error = True
            log.debug(
                "%r (tid:%s): task submission thread encountered error ([%s] %s)",
                self,
                _tid,
                exc.__class__.__name__,
                exc,
            )

            if self._shutdown_lock.acquire(blocking=False):
                self.shutdown(wait=False, cancel_futures=True)
                self._shutdown_lock.release()

            log.debug("%r (tid:%s): task submission thread dies", self, _tid)
            raise
        finally:
            if sys.exc_info() != (None, None, None):
                time.sleep(0.1)  # give any in-flight Futures a chance to be .put() ...
            while not self._tasks_to_send.empty():
                fut, _task = self._tasks_to_send.get()
                if fut:
                    fut.cancel()
                    fut.set_running_or_notify_cancel()
            try:
                while True:
                    self._tasks_to_send.task_done()
            except ValueError:
                pass
            log.debug("%r (tid:%s): task submission thread complete", self, _tid)

    def _submit_tasks(
        self,
        taskgroup_uuid: uuid.UUID | None,
        endpoint_uuid: uuid.UUID,
        user_endpoint_config: dict | None,
        futs: list[ComputeFuture],
        tasks: list[_TaskSubmissionInfo],
    ):
        """
        Submit a batch of tasks to the webservice, destined for self.endpoint_id.
        Upon success, update the futures with their associated task_id.

        :param endpoint_uuid: UUID of the Endpoint on which to execute this batch.
        :param taskgroup_uuid: UUID of the task group
        :param user_endpoint_config: User endpoint configuration values as described and
            allowed by endpoint administrators
        :param futs: a list of ComputeFutures; will have their task_id attribute
            set when function completes successfully.
        :param tasks: a list of tasks to submit upstream in a batch.
        """
        _tid = threading.get_ident()
        if taskgroup_uuid is None and self.task_group_id:
            taskgroup_uuid = self.task_group_id

        batch = self.client.create_batch(
            taskgroup_uuid, user_endpoint_config, create_websocket_queue=True
        )
        submitted_futs_by_fn: t.DefaultDict[str, list[ComputeFuture]] = defaultdict(
            list
        )
        for fut, task in zip(futs, tasks):
            f_uuid_str = str(task.function_uuid)
            submitted_futs_by_fn[f_uuid_str].append(fut)
            batch.add(f_uuid_str, task.args, task.kwargs)
            log.debug("Added task to Globus Compute batch: %s", task)

        log.debug("%r (%s): sending batch (task count: %s)", self, _tid, len(tasks))
        try:
            batch_response = self.client.batch_run(endpoint_uuid, batch)
            log.debug("%r (%s): received response from batch submission", self, _tid)
        except Exception as e:
            log.exception(f"Error submitting {len(tasks)} tasks to Globus Compute")
            for fut_list in submitted_futs_by_fn.values():
                for fut in fut_list:
                    fut.set_exception(e)
            raise

        try:
            received_tasks_by_fn: dict[str, list[str]] = batch_response["tasks"]
            new_tg_id: str = batch_response["task_group_id"]
        except Exception as e:
            log.exception(
                f"Server response ({batch_response}) missing an expected field"
            )
            for fut_list in submitted_futs_by_fn.values():
                for fut in fut_list:
                    fut.set_exception(e)
            raise

        if str(self.task_group_id) != new_tg_id:
            log.info(f"Updating task_group_id from {self.task_group_id} to {new_tg_id}")
            self.task_group_id = new_tg_id

        batch_count = sum(len(x) for x in received_tasks_by_fn.values())
        self.task_count_submitted += batch_count
        log.debug(
            "%r (tid:%s): Batch submitted to task_group: %s - %s (total: %s)",
            self,
            _tid,
            self.task_group_id,
            batch_count,
            self.task_count_submitted,
        )

        for fn_id, fut_list in submitted_futs_by_fn.items():
            task_uuids = received_tasks_by_fn.get(fn_id)

            if task_uuids is None:
                fut_exc = Exception(
                    f"The Globus Compute Service ignored tasks for function {fn_id}!"
                    "  This 'should not happen,' so please reach out to the Globus"
                    " Compute team if you are able to recreate this behavior."
                )
                for fut in fut_list:
                    fut.set_exception(fut_exc)
                continue

            if len(fut_list) != len(task_uuids):
                fut_exc = Exception(
                    "The Globus Compute Service only partially initiated requested"
                    f" tasks for function {fn_id}!  It is unclear which tasks it"
                    " honored, so marking all futures as failed.  Please reach out"
                    " to the Globus Compute team if you are able to recreate this"
                    " behavior."
                )
                for fut in fut_list:
                    fut.set_exception(fut_exc)
                continue

            # Happy -- expected -- path
            for fut, task_id in zip(fut_list, task_uuids):
                fut.task_id = task_id


class _ResultWatcher(threading.Thread):
    """
    _ResultWatcher is an internal SDK class meant for consumption by the
    Executor.  It is a standard async AMQP consumer implementation
    using the Pika library that matches futures from the Executor against
    results received from the Globus Compute hosted services.

    Expected usage::

        rw = _ResultWatcher(self)  # assert isinstance(self, Executor)
        rw.start()

        # rw is its own thread; it will use the Client attached to the
        # Executor to acquire AMQP credentials, and then will open a
        # connection to the AMQP service.

        rw.watch_for_task_results(some_list_of_futures)

    When there are no more open futures and no more results, the thread
    will opportunistically shutdown; the caller must handle this scenario
    if new futures arrive, and create a new _ResultWatcher instance.

    :param funcx_executor: An Executor instance
    :param poll_period_s: [default: 0.5] how frequently to check for and
        handle events.  For example, if the thread should stop due to user
        request or if there are results to match.
    :param connect_attempt_limit: [default: 3] how many times to attempt
        connecting to the AMQP server before bailing.
    :param channel_close_window_s: [default: 10] how large a window to
        tally channel open events.
    :param channel_close_window_limit: [default: 3] how many reopen
        attempts to allow within the tally window before concluding
        there is an external error and shutting down the watcher.
    """

    class ShuttingDownError(Exception):
        pass

    def __init__(
        self,
        funcx_executor: Executor,
        poll_period_s=0.5,
        connect_attempt_limit=5,
        channel_close_window_s=10,
        channel_close_window_limit=3,
        port: int | None = None,
    ):
        super().__init__()
        self.funcx_executor = funcx_executor
        self._to_ack: list[int] = []  # outstanding amqp messages not-yet-acked
        self._time_to_check_results = threading.Event()
        self._new_futures_lock = threading.Lock()

        self._connection: pika.SelectConnection | None = None
        self._channel: Channel | None = None
        self._consumer_tag: str | None = None

        self._queue_prefix = ""

        self._open_futures: dict[str, ComputeFuture] = {}
        self._received_results: dict[str, tuple[BasicProperties, Result]] = {}

        self._open_futures_empty = threading.Event()
        self._open_futures_empty.set()
        self._time_to_stop = False

        # how often to check for work; every `poll_period_s`, the `_event_watcher`
        # method will invoke to match results to tasks, ack upstream, and see if
        # it's time to shut down the connection and thread.
        self.poll_period_s = poll_period_s
        self._connection_tries = 0  # count of connection events; reset on success

        # how many times to attempt connection before giving up and shutting
        # down the thread
        self.connect_attempt_limit = connect_attempt_limit

        self._thread_id = 0  # 0 == the parent process; will be set by .run()
        self._cancellation_reason: Exception | None = None
        self._closed = False  # precursor flag until the thread stops completely

        # list of times that channel was last closed
        self._channel_closes: list[float] = []

        # how long a time frame to keep previous channel close times
        self.channel_close_window_s = channel_close_window_s

        # how many times allowed to retry opening a channel in the above time
        # window before giving up and shutting down the thread
        self.channel_close_window_limit = channel_close_window_limit

        self.port = port

    def __repr__(self):
        return "{}<{}; pid={}; fut={:,d}; res={:,d}; qp={}>".format(
            self.__class__.__name__,
            "✓" if self._consumer_tag else "✗",
            os.getpid(),
            len(self._open_futures),
            len(self._received_results),
            self._queue_prefix if self._queue_prefix else "-",
        )

    def run(self):
        log.debug("%r AMQP thread begins", self)
        self._thread_id = threading.get_ident()

        while self._connection_tries < self.connect_attempt_limit and not (
            self._time_to_stop or self._cancellation_reason
        ):
            if self._connection or self._connection_tries:
                wait_for = random.uniform(0.5, 10)
                msg = f"%r AMQP reconnecting in {wait_for:.1f}s."
                log.debug(msg, self)
                if self._connection_tries == self.connect_attempt_limit - 1:
                    log.warning(f"{msg}  (final attempt)", self)
                time.sleep(wait_for)

            self._connection_tries += 1
            try:
                log.debug(
                    "%r Opening connection to AMQP service.  Attempt: %s (of %s)",
                    self,
                    self._connection_tries,
                    self.connect_attempt_limit,
                )
                self._connection = self._connect()
                self._event_watcher()
                self._connection.ioloop.start()  # Reminder: blocks

            except Exception:
                log.exception("%r Unhandled error; shutting down", self)

        with self._new_futures_lock:
            if self._open_futures:
                if not self._time_to_stop or self._cancellation_reason:
                    # We're not shutting down intentionally; cancel outstanding
                    # futures so consumer learns right away of problem.
                    self._time_to_stop = True  # prevent new futures from arriving
                    log.warning("Cancelling all known futures")
                    if not self._cancellation_reason:
                        self._cancellation_reason = RuntimeError(
                            "SDK thread quit unexpectedly."
                        )

                    while self._open_futures:
                        _, fut = self._open_futures.popitem()
                        fut.set_exception(self._cancellation_reason)
                        log.debug("Cancelled: %s", fut.task_id)
                    self._open_futures_empty.set()
        log.debug("%r AMQP thread complete.", self)

    def shutdown(self, wait=True, *, cancel_futures=False):
        if not self.is_alive():
            return

        self._closed = True  # No more futures will be accepted

        if threading.get_ident() == self._thread_id:
            self._stop_ioloop()

        else:
            # External thread called this method
            if cancel_futures:
                with self._new_futures_lock:
                    while self._open_futures:
                        _, fut = self._open_futures.popitem()
                        fut.cancel()
                        fut.set_running_or_notify_cancel()
                    self._open_futures_empty.set()

            # N.B. Per Python Executor documentation, the whole program can't
            # exit() until all futures are complete, regardless of the `wait`
            # argument.  Thus, the _stopper_thread is *not* daemonized, and
            # the stopping of the processing thread happens *after* there
            # are no more pending futures.
            # See: https://docs.python.org/3/library/concurrent.futures.html#concurrent.futures.Executor.shutdown # noqa
            def _stopper_thread():
                if self._open_futures:
                    self._open_futures_empty.wait()
                self._time_to_stop = True
                self.join()

            join_thread = threading.Thread(target=_stopper_thread)
            join_thread.start()
            if wait:
                join_thread.join()

    def watch_for_task_results(self, futures: list[ComputeFuture]) -> int:
        """
        Add list of ComputeFutures to internal watch list.

        Updates the thread's dictionary of futures that will be resolved when
        upstream sends associated results.  The internal dictionary is keyed
        on the ComputeFuture.task_id attribute, but this method does not verify
        that it is set -- it is up to the caller to ensure the future is
        initialized fully.  In particular, if a task_id is not set, it will
        not be added to the watch list.

        ``watch_for_task_results()`` may raise ``_ResultWatcher.ShuttingDownError``
        if, when called, the object is shutting down.  This may happen for a
        few reasons (e.g., connection errors, channel errors) -- it is the
        caller's responsibility to handle this error.

        :returns: number of tasks added to watch list
        :raises ShuttingDownError: if called while this object is shutting down
        """
        with self._new_futures_lock:
            if self._closed:
                msg = "Unable to watch futures: %s is shutting down."
                raise self.__class__.ShuttingDownError(msg % self.__class__.__name__)

            to_watch = {
                f.task_id: f
                for f in futures
                if f.task_id
                if not f.done()
                if f.task_id not in self._open_futures
            }
            self._open_futures.update(to_watch)

            if self._open_futures:  # futures as an empty list is acceptable
                self._open_futures_empty.clear()
                if self._received_results:
                    # no sense is setting the event if there are not yet
                    # any results
                    self._time_to_check_results.set()
        return len(to_watch)

    def _match_results_to_futures(self):
        """
        Match the internal ``_received_results`` and ``_open_futures`` on their
        keys, and complete the associated futures.  All matching items will,
        after processing, be forgotten (i.e., ``.pop()``).

        This method will set the _open_futures_empty event if there are no open
        futures *at the time of processing*.
        """
        deserialize = self.funcx_executor.client.fx_serializer.deserialize
        with self._new_futures_lock:
            futures_to_complete = [
                self._open_futures.pop(tid)
                for tid in self._open_futures.keys() & self._received_results.keys()
            ]
            if not self._open_futures:
                self._open_futures_empty.set()

        for fut in futures_to_complete:
            props, res = self._received_results.pop(fut.task_id)

            if res.is_error:
                fut.set_exception(
                    TaskExecutionFailed(res.data, str(props.timestamp or 0))
                )
            else:
                try:
                    fut.set_result(deserialize(res.data))
                except InvalidStateError as err:
                    log.error(
                        f"Unable to set future state ({err}) for task: {fut.task_id}"
                    )
                except Exception as exc:
                    task_exc = Exception(
                        f"Malformed or unexpected data structure. Data: {res.data}",
                    )
                    task_exc.__cause__ = exc
                    fut.set_exception(task_exc)

    def _event_watcher(self):
        """
        Check, every iteration, for the following events and act accordingly::

        - If it's time to stop the thread (``_time_to_stop``)
        - If there are any open messages to ACKnowledge to the AMQP service
        - If there are new results or futures to match (``_time_to_check_results``)

        Notably, if, at the end of processing, there are no open futures or
        results, then initiate a shutdown.

        The ``_event_watcher()` method is initiated before the eventloop starts
        (see ``.run()``), and rearms itself every iteration.
        """
        if self._time_to_stop:
            self.shutdown()
            log.debug("%r Shutdown complete", self)
            return

        try:
            if self._to_ack:
                self._to_ack.sort()  # no change in the happy path
                latest_msg_id = self._to_ack[-1]
                self._channel.basic_ack(latest_msg_id, multiple=True)
                self._to_ack.clear()
                log.debug("%r Acknowledged through message: %s", self, latest_msg_id)

            if self._time_to_check_results.is_set():
                self._time_to_check_results.clear()
                self._match_results_to_futures()
                if not (self._open_futures or self._received_results):
                    log.debug("%r Idle; no outstanding results or futures.", self)
        finally:
            self._connection.ioloop.call_later(self.poll_period_s, self._event_watcher)

    def _on_message(
        self,
        channel: Channel,
        basic_deliver: Basic.Deliver,
        props: BasicProperties,
        body: bytes,
    ):
        """
        Nominally, the kernel of this whole class -- called by the Pika library
        (ioloop) when a new Result message has arrived from upstream.  This
        method will attempt to unpack the bytes so as to minimally verify that
        the message is a valid Result object, and then store the unpacked
        object in self._received_results for later processing and set the
        ``_time_to_check_results`` event.

        If the received message is *not* a result, then immediately NACK the
        message to AMQP service -- the responsibility to handle invalid messages
        is not with this class.
        """
        msg_id: int = basic_deliver.delivery_tag
        log.debug("Received message %s: [%s] %s", msg_id, props, body)

        try:
            res = messagepack.unpack(body)
            if not isinstance(res, Result):
                raise TypeError(f"Non-Result object received ({type(res)})")

            self._received_results[str(res.task_id)] = (props, res)
            self._to_ack.append(msg_id)
            self._time_to_check_results.set()
        except Exception:
            # No sense in waiting for the RMQ default 30m timeout; let it know
            # *now* that this message failed.
            log.exception("Invalid message type queue put failed")
            channel.basic_nack(msg_id, requeue=True)

    def _stop_ioloop(self):
        """
        Gracefully stop the ioloop.

        In an effort play nice with upstream, attempt to follow the AMQP protocol
        by closing the channel and connections gracefully.  This method will
        rearm itself while the connection is still open, continually working
        toward eventually and gracefully stopping the connection, before finally
        stopping the ioloop.
        """
        if self._connection:
            self._connection.ioloop.call_later(0.1, self._stop_ioloop)
            if self._connection.is_open:
                if self._channel:
                    if self._channel.is_open:
                        self._channel.close()
                    elif self._channel.is_closed:
                        self._channel = None
                else:
                    self._connection.close()
            elif self._connection.is_closed:
                self._connection.ioloop.stop()
                self._connection = None

    def _connect(self) -> pika.SelectConnection:
        with self._new_futures_lock:
            res = self.funcx_executor.client.get_result_amqp_url()
            self._queue_prefix = res["queue_prefix"]
            connection_url = res["connection_url"]

            pika_params = pika.URLParameters(connection_url)
            if self.port is not None:
                pika_params.port = self.port
            return pika.SelectConnection(
                pika_params,
                on_close_callback=self._on_connection_closed,
                on_open_error_callback=self._on_open_failed,
                on_open_callback=self._on_connection_open,
            )

    def _on_open_failed(self, _mq_conn: pika.BaseConnection, exc: str | Exception):
        assert self._connection is not None, "Strictly called _by_ ioloop"
        count = f"[attempt {self._connection_tries} (of {self.connect_attempt_limit})]"
        pid = f"(pid: {os.getpid()})"
        exc_text = f"Failed to open connection - ({exc.__class__.__name__}) {exc}"
        msg = f"{count} {pid} {exc_text}"
        log.debug(msg)

        if not (self._connection_tries < self.connect_attempt_limit):
            log.warning(msg)
            if not isinstance(exc, Exception):
                exc = Exception(str(exc))
            self._cancellation_reason = exc
        self._connection.ioloop.stop()

    def _on_connection_closed(self, _mq_conn: pika.BaseConnection, exc: Exception):
        assert self._connection is not None, "Strictly called _by_ ioloop"
        log.debug("Connection closed: %s", exc)
        self._consumer_tag = None
        self._connection.ioloop.stop()

    def _on_connection_open(self, _mq_conn: pika.BaseConnection):
        log.debug("Connection established; creating channel")
        self._connection_tries = 0
        self._open_channel()

    def _open_channel(self):
        if self._connection.is_open:
            self._connection.channel(on_open_callback=self._on_channel_open)

    def _on_channel_open(self, channel: Channel):
        self._channel = channel

        channel.add_on_close_callback(self._on_channel_closed)
        channel.add_on_cancel_callback(self._on_consumer_cancelled)

        log.debug(
            "Channel %s opened (%s); begin consuming messages.",
            channel.channel_number,
            channel.connection.params,
        )
        self._start_consuming()

    def _on_channel_closed(self, channel: Channel, exc: Exception):
        if self._closed:
            return

        self._consumer_tag = None
        assert self._connection is not None, "Strictly called _by_ ioloop"
        # Doh!  Channel closed unexpectedly.
        now = time.monotonic()
        then = now - self.channel_close_window_s
        self._channel_closes = [cc for cc in self._channel_closes if cc > then]
        self._channel_closes.append(now)
        if len(self._channel_closes) < self.channel_close_window_limit:
            msg = f"Channel closed.  Reopening.\n  {channel}\n  ({exc})"
            log.debug(msg, exc_info=exc)
            log.warning(msg)
            self._connection.ioloop.call_later(1, self._open_channel)

        else:
            msg = (
                f"Unable to sustain channel after {len(self._channel_closes)} attempts"
                f" in {self.channel_close_window_limit} seconds. ({exc})"
            )
            log.error(msg)
            self._closed = True
            self._cancellation_reason = RuntimeError(msg)
            self.shutdown()

    def _start_consuming(self):
        self._consumer_tag = self._channel.basic_consume(
            queue=f"{self._queue_prefix}{self.funcx_executor.task_group_id}",
            on_message_callback=self._on_message,
        )

    def _on_consumer_cancelled(self, frame: Method[Basic.CancelOk]):
        log.info("Consumer cancelled remotely, shutting down: %r", frame)
        if self._channel:
            self._channel.close()
