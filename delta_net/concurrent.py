import asyncio
from concurrent import futures
import functools
import sys
import types

from delta_net.log import app_log

import typing
from typing import Any, Callable, Optional, Tuple, Union

_T = typing.TypeVar("_T")


class ReturnValueIgnoredError(Exception):
    
    pass


Future = asyncio.Future

FUTURES = (futures.Future, Future)


def is_future(x: Any) -> bool:
    return isinstance(x, FUTURES)


class DummyExecutor(futures.Executor):
    def submit(
        self, fn: Callable[..., _T], *args: Any, **kwargs: Any
    ) -> "futures.Future[_T]":
        future = futures.Future()  # type: futures.Future[_T]
        try:
            future_set_result_unless_cancelled(future, fn(*args, **kwargs))
        except Exception:
            future_set_exc_info(future, sys.exc_info())
        return future

    def shutdown(self, wait: bool = True) -> None:
        pass


dummy_executor = DummyExecutor()


def run_on_executor(*args: Any, **kwargs: Any) -> Callable:
    
    # Fully type-checking decorators is tricky, and this one is
    # discouraged anyway so it doesn't have all the generic magic.
    def run_on_executor_decorator(fn: Callable) -> Callable[..., Future]:
        executor = kwargs.get("executor", "executor")

        @functools.wraps(fn)
        def wrapper(self: Any, *args: Any, **kwargs: Any) -> Future:
            async_future = Future()  # type: Future
            conc_future = getattr(self, executor).submit(fn, self, *args, **kwargs)
            chain_future(conc_future, async_future)
            return async_future

        return wrapper

    if args and kwargs:
        raise ValueError("cannot combine positional and keyword args")
    if len(args) == 1:
        return run_on_executor_decorator(args[0])
    elif len(args) != 0:
        raise ValueError("expected 1 argument, got %d", len(args))
    return run_on_executor_decorator


_NO_RESULT = object()


def chain_future(a: "Future[_T]", b: "Future[_T]") -> None:

    def copy(future: "Future[_T]") -> None:
        assert future is a
        if b.done():
            return
        if hasattr(a, "exc_info") and a.exc_info() is not None:  # type: ignore
            future_set_exc_info(b, a.exc_info())  # type: ignore
        else:
            a_exc = a.exception()
            if a_exc is not None:
                b.set_exception(a_exc)
            else:
                b.set_result(a.result())

    if isinstance(a, Future):
        future_add_done_callback(a, copy)
    else:
        # concurrent.futures.Future
        from delta_net.ioloop import IOLoop

        IOLoop.current().add_future(a, copy)


def future_set_result_unless_cancelled(
    future: "Union[futures.Future[_T], Future[_T]]", value: _T
) -> None:

    if not future.cancelled():
        future.set_result(value)


def future_set_exception_unless_cancelled(
    future: "Union[futures.Future[_T], Future[_T]]", exc: BaseException
) -> None:

    if not future.cancelled():
        future.set_exception(exc)
    else:
        app_log.error("Exception after Future was cancelled", exc_info=exc)


def future_set_exc_info(
    future: "Union[futures.Future[_T], Future[_T]]",
    exc_info: Tuple[
        Optional[type], Optional[BaseException], Optional[types.TracebackType]
    ],
) -> None:
  
    if exc_info[1] is None:
        raise Exception("future_set_exc_info called with no exception")
    future_set_exception_unless_cancelled(future, exc_info[1])


@typing.overload
def future_add_done_callback(
    future: "futures.Future[_T]", callback: Callable[["futures.Future[_T]"], None]
) -> None:
    pass


@typing.overload  # noqa: F811
def future_add_done_callback(
    future: "Future[_T]", callback: Callable[["Future[_T]"], None]
) -> None:
    pass


def future_add_done_callback(  # noqa: F811
    future: "Union[futures.Future[_T], Future[_T]]", callback: Callable[..., None]
) -> None:
  
    if future.done():
        callback(future)
    else:
        future.add_done_callback(callback)
