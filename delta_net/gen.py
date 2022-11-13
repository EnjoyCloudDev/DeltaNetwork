import asyncio
import builtins
import collections
from collections.abc import Generator
import concurrent.futures
import datetime
import functools
from functools import singledispatch
from inspect import isawaitable
import sys
import types

from delta_net.concurrent import (
    Future,
    is_future,
    chain_future,
    future_set_exc_info,
    future_add_done_callback,
    future_set_result_unless_cancelled,
)
from delta_net.ioloop import IOLoop
from delta_net.log import app_log
from delta_net.util import TimeoutError

try:
    import contextvars
except ImportError:
    contextvars = None  # type: ignore

import typing
from typing import Union, Any, Callable, List, Type, Tuple, Awaitable, Dict, overload

if typing.TYPE_CHECKING:
    from typing import Sequence, Deque, Optional, Set, Iterable  # noqa: F401

_T = typing.TypeVar("_T")

_Yieldable = Union[
    None, Awaitable, List[Awaitable], Dict[Any, Awaitable], concurrent.futures.Future
]


class KeyReuseError(Exception):
    pass


class UnknownKeyError(Exception):
    pass


class LeakedCallbackError(Exception):
    pass


class BadYieldError(Exception):
    pass


class ReturnValueIgnoredError(Exception):
    pass


def _value_from_stopiteration(e: Union[StopIteration, "Return"]) -> Any:
    try:
        # StopIteration has a value attribute beginning in py33.
        # So does our Return class.
        return e.value
    except AttributeError:
        pass
    try:
        # Cython backports coroutine functionality by putting the value in
        # e.args[0].
        return e.args[0]
    except (AttributeError, IndexError):
        return None


def _create_future() -> Future:
    future = Future()  # type: Future
    # Fixup asyncio debug info by removing extraneous stack entries
    source_traceback = getattr(future, "_source_traceback", ())
    while source_traceback:
        # Each traceback entry is equivalent to a
        # (filename, self.lineno, self.name, self.line) tuple
        filename = source_traceback[-1][0]
        if filename == __file__:
            del source_traceback[-1]
        else:
            break
    return future


def _fake_ctx_run(f: Callable[..., _T], *args: Any, **kw: Any) -> _T:
    return f(*args, **kw)


@overload
def coroutine(
    func: Callable[..., "Generator[Any, Any, _T]"]
) -> Callable[..., "Future[_T]"]:
    ...


@overload
def coroutine(func: Callable[..., _T]) -> Callable[..., "Future[_T]"]:
    ...


def coroutine(
    func: Union[Callable[..., "Generator[Any, Any, _T]"], Callable[..., _T]]
) -> Callable[..., "Future[_T]"]:
    
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        # type: (*Any, **Any) -> Future[_T]
        # This function is type-annotated with a comment to work around
        # https://bitbucket.org/pypy/pypy/issues/2868/segfault-with-args-type-annotation-in
        future = _create_future()
        if contextvars is not None:
            ctx_run = contextvars.copy_context().run  # type: Callable
        else:
            ctx_run = _fake_ctx_run
        try:
            result = ctx_run(func, *args, **kwargs)
        except (Return, StopIteration) as e:
            result = _value_from_stopiteration(e)
        except Exception:
            future_set_exc_info(future, sys.exc_info())
            try:
                return future
            finally:
                # Avoid circular references
                future = None  # type: ignore
        else:
            if isinstance(result, Generator):
                # Inline the first iteration of Runner.run.  This lets us
                # avoid the cost of creating a Runner when the coroutine
                # never actually yields, which in turn allows us to
                # use "optional" coroutines in critical path code without
                # performance penalty for the synchronous case.
                try:
                    yielded = ctx_run(next, result)
                except (StopIteration, Return) as e:
                    future_set_result_unless_cancelled(
                        future, _value_from_stopiteration(e)
                    )
                except Exception:
                    future_set_exc_info(future, sys.exc_info())
                else:
                    # Provide strong references to Runner objects as long
                    # as their result future objects also have strong
                    # references (typically from the parent coroutine's
                    # Runner). This keeps the coroutine's Runner alive.
                    # We do this by exploiting the public API
                    # add_done_callback() instead of putting a private
                    # attribute on the Future.
                    # (GitHub issues #1769, #2229).
                    runner = Runner(ctx_run, result, future, yielded)
                    future.add_done_callback(lambda _: runner)
                yielded = None
                try:
                    return future
                finally:
                    # Subtle memory optimization: if next() raised an exception,
                    # the future's exc_info contains a traceback which
                    # includes this stack frame.  This creates a cycle,
                    # which will be collected at the next full GC but has
                    # been shown to greatly increase memory usage of
                    # benchmarks (relative to the refcount-based scheme
                    # used in the absence of cycles).  We can avoid the
                    # cycle by clearing the local variable after we return it.
                    future = None  # type: ignore
        future_set_result_unless_cancelled(future, result)
        return future

    wrapper.__wrapped__ = func  # type: ignore
    wrapper.__delta_net_coroutine__ = True  # type: ignore
    return wrapper


def is_coroutine_function(func: Any) -> bool:
    """Return whether *func* is a coroutine function, i.e. a function
    wrapped with `~.gen.coroutine`.

    .. versionadded:: 4.5
    """
    return getattr(func, "__delta_net_coroutine__", False)


class Return(Exception):
   
    def __init__(self, value: Any = None) -> None:
        super().__init__()
        self.value = value
        # Cython recognizes subclasses of StopIteration with a .args tuple.
        self.args = (value,)


class WaitIterator(object):
   
    _unfinished = {}  # type: Dict[Future, Union[int, str]]

    def __init__(self, *args: Future, **kwargs: Future) -> None:
        if args and kwargs:
            raise ValueError("You must provide args or kwargs, not both")

        if kwargs:
            self._unfinished = dict((f, k) for (k, f) in kwargs.items())
            futures = list(kwargs.values())  # type: Sequence[Future]
        else:
            self._unfinished = dict((f, i) for (i, f) in enumerate(args))
            futures = args

        self._finished = collections.deque()  # type: Deque[Future]
        self.current_index = None  # type: Optional[Union[str, int]]
        self.current_future = None  # type: Optional[Future]
        self._running_future = None  # type: Optional[Future]

        for future in futures:
            future_add_done_callback(future, self._done_callback)

    def done(self) -> bool:
        """Returns True if this iterator has no more results."""
        if self._finished or self._unfinished:
            return False
        # Clear the 'current' values when iteration is done.
        self.current_index = self.current_future = None
        return True

    def next(self) -> Future:
        """Returns a `.Future` that will yield the next available result.

        Note that this `.Future` will not be the same object as any of
        the inputs.
        """
        self._running_future = Future()

        if self._finished:
            return self._return_result(self._finished.popleft())

        return self._running_future

    def _done_callback(self, done: Future) -> None:
        if self._running_future and not self._running_future.done():
            self._return_result(done)
        else:
            self._finished.append(done)

    def _return_result(self, done: Future) -> Future:
        """Called set the returned future's state that of the future
        we yielded, and set the current future for the iterator.
        """
        if self._running_future is None:
            raise Exception("no future is running")
        chain_future(done, self._running_future)

        res = self._running_future
        self._running_future = None
        self.current_future = done
        self.current_index = self._unfinished.pop(done)

        return res

    def __aiter__(self) -> typing.AsyncIterator:
        return self

    def __anext__(self) -> Future:
        if self.done():
            # Lookup by name to silence pyflakes on older versions.
            raise getattr(builtins, "StopAsyncIteration")()
        return self.next()


def multi(
    children: Union[List[_Yieldable], Dict[Any, _Yieldable]],
    quiet_exceptions: "Union[Type[Exception], Tuple[Type[Exception], ...]]" = (),
) -> "Union[Future[List], Future[Dict]]":
   
    return multi_future(children, quiet_exceptions=quiet_exceptions)


Multi = multi


def multi_future(
    children: Union[List[_Yieldable], Dict[Any, _Yieldable]],
    quiet_exceptions: "Union[Type[Exception], Tuple[Type[Exception], ...]]" = (),
) -> "Union[Future[List], Future[Dict]]":
   
    if isinstance(children, dict):
        keys = list(children.keys())  # type: Optional[List]
        children_seq = children.values()  # type: Iterable
    else:
        keys = None
        children_seq = children
    children_futs = list(map(convert_yielded, children_seq))
    assert all(is_future(i) or isinstance(i, _NullFuture) for i in children_futs)
    unfinished_children = set(children_futs)

    future = _create_future()
    if not children_futs:
        future_set_result_unless_cancelled(future, {} if keys is not None else [])

    def callback(fut: Future) -> None:
        unfinished_children.remove(fut)
        if not unfinished_children:
            result_list = []
            for f in children_futs:
                try:
                    result_list.append(f.result())
                except Exception as e:
                    if future.done():
                        if not isinstance(e, quiet_exceptions):
                            app_log.error(
                                "Multiple exceptions in yield list", exc_info=True
                            )
                    else:
                        future_set_exc_info(future, sys.exc_info())
            if not future.done():
                if keys is not None:
                    future_set_result_unless_cancelled(
                        future, dict(zip(keys, result_list))
                    )
                else:
                    future_set_result_unless_cancelled(future, result_list)

    listening = set()  # type: Set[Future]
    for f in children_futs:
        if f not in listening:
            listening.add(f)
            future_add_done_callback(f, callback)
    return future


def maybe_future(x: Any) -> Future:
    
    if is_future(x):
        return x
    else:
        fut = _create_future()
        fut.set_result(x)
        return fut


def with_timeout(
    timeout: Union[float, datetime.timedelta],
    future: _Yieldable,
    quiet_exceptions: "Union[Type[Exception], Tuple[Type[Exception], ...]]" = (),
) -> Future:
   
    # It's tempting to optimize this by cancelling the input future on timeout
    # instead of creating a new one, but A) we can't know if we are the only
    # one waiting on the input future, so cancelling it might disrupt other
    # callers and B) concurrent futures can only be cancelled while they are
    # in the queue, so cancellation cannot reliably bound our waiting time.
    future_converted = convert_yielded(future)
    result = _create_future()
    chain_future(future_converted, result)
    io_loop = IOLoop.current()

    def error_callback(future: Future) -> None:
        try:
            future.result()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            if not isinstance(e, quiet_exceptions):
                app_log.error(
                    "Exception in Future %r after timeout", future, exc_info=True
                )

    def timeout_callback() -> None:
        if not result.done():
            result.set_exception(TimeoutError("Timeout"))
        # In case the wrapped future goes on to fail, log it.
        future_add_done_callback(future_converted, error_callback)

    timeout_handle = io_loop.add_timeout(timeout, timeout_callback)
    if isinstance(future_converted, Future):
        # We know this future will resolve on the IOLoop, so we don't
        # need the extra thread-safety of IOLoop.add_future (and we also
        # don't care about StackContext here.
        future_add_done_callback(
            future_converted, lambda future: io_loop.remove_timeout(timeout_handle)
        )
    else:
        # concurrent.futures.Futures may resolve on any thread, so we
        # need to route them back to the IOLoop.
        io_loop.add_future(
            future_converted, lambda future: io_loop.remove_timeout(timeout_handle)
        )
    return result


def sleep(duration: float) -> "Future[None]":
   
    f = _create_future()
    IOLoop.current().call_later(
        duration, lambda: future_set_result_unless_cancelled(f, None)
    )
    return f


class _NullFuture(object):
    
    def result(self) -> None:
        return None

    def done(self) -> bool:
        return True


# _null_future is used as a dummy value in the coroutine runner. It differs
# from moment in that moment always adds a delay of one IOLoop iteration
# while _null_future is processed as soon as possible.
_null_future = typing.cast(Future, _NullFuture())

moment = typing.cast(Future, _NullFuture())
moment.__doc__ = """A special object which may be yielded to allow the IOLoop to run for
one iteration.

This is not needed in normal use but it can be helpful in long-running
coroutines that are likely to yield Futures that are ready instantly.

Usage: ``yield gen.moment``

In native coroutines, the equivalent of ``yield gen.moment`` is
``await asyncio.sleep(0)``.

.. versionadded:: 4.0

.. deprecated:: 4.5
   ``yield None`` (or ``yield`` with no argument) is now equivalent to
    ``yield gen.moment``.
"""


class Runner(object):
    
    def __init__(
        self,
        ctx_run: Callable,
        gen: "Generator[_Yieldable, Any, _T]",
        result_future: "Future[_T]",
        first_yielded: _Yieldable,
    ) -> None:
        self.ctx_run = ctx_run
        self.gen = gen
        self.result_future = result_future
        self.future = _null_future  # type: Union[None, Future]
        self.running = False
        self.finished = False
        self.io_loop = IOLoop.current()
        if self.ctx_run(self.handle_yield, first_yielded):
            gen = result_future = first_yielded = None  # type: ignore
            self.ctx_run(self.run)

    def run(self) -> None:
        """Starts or resumes the generator, running until it reaches a
        yield point that is not ready.
        """
        if self.running or self.finished:
            return
        try:
            self.running = True
            while True:
                future = self.future
                if future is None:
                    raise Exception("No pending future")
                if not future.done():
                    return
                self.future = None
                try:
                    exc_info = None

                    try:
                        value = future.result()
                    except Exception:
                        exc_info = sys.exc_info()
                    future = None

                    if exc_info is not None:
                        try:
                            yielded = self.gen.throw(*exc_info)  # type: ignore
                        finally:
                            # Break up a reference to itself
                            # for faster GC on CPython.
                            exc_info = None
                    else:
                        yielded = self.gen.send(value)

                except (StopIteration, Return) as e:
                    self.finished = True
                    self.future = _null_future
                    future_set_result_unless_cancelled(
                        self.result_future, _value_from_stopiteration(e)
                    )
                    self.result_future = None  # type: ignore
                    return
                except Exception:
                    self.finished = True
                    self.future = _null_future
                    future_set_exc_info(self.result_future, sys.exc_info())
                    self.result_future = None  # type: ignore
                    return
                if not self.handle_yield(yielded):
                    return
                yielded = None
        finally:
            self.running = False

    def handle_yield(self, yielded: _Yieldable) -> bool:
        try:
            self.future = convert_yielded(yielded)
        except BadYieldError:
            self.future = Future()
            future_set_exc_info(self.future, sys.exc_info())

        if self.future is moment:
            self.io_loop.add_callback(self.ctx_run, self.run)
            return False
        elif self.future is None:
            raise Exception("no pending future")
        elif not self.future.done():

            def inner(f: Any) -> None:
                # Break a reference cycle to speed GC.
                f = None  # noqa: F841
                self.ctx_run(self.run)

            self.io_loop.add_future(self.future, inner)
            return False
        return True

    def handle_exception(
        self, typ: Type[Exception], value: Exception, tb: types.TracebackType
    ) -> bool:
        if not self.running and not self.finished:
            self.future = Future()
            future_set_exc_info(self.future, (typ, value, tb))
            self.ctx_run(self.run)
            return True
        else:
            return False


# Convert Awaitables into Futures.
try:
    _wrap_awaitable = asyncio.ensure_future
except AttributeError:
    # asyncio.ensure_future was introduced in Python 3.4.4, but
    # Debian jessie still ships with 3.4.2 so try the old name.
    _wrap_awaitable = getattr(asyncio, "async")


def convert_yielded(yielded: _Yieldable) -> Future:

    if yielded is None or yielded is moment:
        return moment
    elif yielded is _null_future:
        return _null_future
    elif isinstance(yielded, (list, dict)):
        return multi(yielded)  # type: ignore
    elif is_future(yielded):
        return typing.cast(Future, yielded)
    elif isawaitable(yielded):
        return _wrap_awaitable(yielded)  # type: ignore
    else:
        raise BadYieldError("yielded unknown object %r" % (yielded,))


convert_yielded = singledispatch(convert_yielded)
