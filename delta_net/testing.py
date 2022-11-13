import asyncio
from collections.abc import Generator
import functools
import inspect
import logging
import os
import re
import signal
import socket
import sys
import unittest
import warnings

from delta_net import gen
from delta_net.httpclient import AsyncHTTPClient, HTTPResponse
from delta_net.httpserver import HTTPServer
from delta_net.ioloop import IOLoop, TimeoutError
from delta_net import netutil
from delta_net.platform.asyncio import AsyncIOMainLoop
from delta_net.process import Subprocess
from delta_net.log import app_log
from delta_net.util import raise_exc_info, basestring_type
from delta_net.web import Application

import typing
from typing import Tuple, Any, Callable, Type, Dict, Union, Optional, Coroutine
from types import TracebackType

if typing.TYPE_CHECKING:
    _ExcInfoTuple = Tuple[
        Optional[Type[BaseException]], Optional[BaseException], Optional[TracebackType]
    ]


_NON_OWNED_IOLOOPS = AsyncIOMainLoop


def bind_unused_port(
    reuse_port: bool = False, address: str = "127.0.0.1"
) -> Tuple[socket.socket, int]:
    """Binds a server socket to an available port on localhost.

    Returns a tuple (socket, port).

    .. versionchanged:: 4.4
       Always binds to ``127.0.0.1`` without resolving the name
       ``localhost``.

    .. versionchanged:: 6.2
       Added optional ``address`` argument to
       override the default "127.0.0.1".
    """
    sock = netutil.bind_sockets(
        0, address, family=socket.AF_INET, reuse_port=reuse_port
    )[0]
    port = sock.getsockname()[1]
    return sock, port


def get_async_test_timeout() -> float:
    """Get the global timeout setting for async tests.

    Returns a float, the timeout in seconds.

    .. versionadded:: 3.1
    """
    env = os.environ.get("ASYNC_TEST_TIMEOUT")
    if env is not None:
        try:
            return float(env)
        except ValueError:
            pass
    return 5


class _TestMethodWrapper(object):
    """Wraps a test method to raise an error if it returns a value.

    This is mainly used to detect undecorated generators (if a test
    method yields it must use a decorator to consume the generator),
    but will also detect other kinds of return values (these are not
    necessarily errors, but we alert anyway since there is no good
    reason to return a value from a test).
    """

    def __init__(self, orig_method: Callable) -> None:
        self.orig_method = orig_method
        self.__wrapped__ = orig_method

    def __call__(self, *args: Any, **kwargs: Any) -> None:
        result = self.orig_method(*args, **kwargs)
        if isinstance(result, Generator) or inspect.iscoroutine(result):
            raise TypeError(
                "Generator and coroutine test methods should be"
                " decorated with delta_net.testing.gen_test"
            )
        elif result is not None:
            raise ValueError("Return value from test method ignored: %r" % result)

    def __getattr__(self, name: str) -> Any:
        """Proxy all unknown attributes to the original method.

        This is important for some of the decorators in the `unittest`
        module, such as `unittest.skipIf`.
        """
        return getattr(self.orig_method, name)


class AsyncTestCase(unittest.TestCase):

    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.__stopped = False
        self.__running = False
        self.__failure = None  # type: Optional[_ExcInfoTuple]
        self.__stop_args = None  # type: Any
        self.__timeout = None  # type: Optional[object]

        # It's easy to forget the @gen_test decorator, but if you do
        # the test will silently be ignored because nothing will consume
        # the generator.  Replace the test method with a wrapper that will
        # make sure it's not an undecorated generator.
        setattr(self, methodName, _TestMethodWrapper(getattr(self, methodName)))

        # Not used in this class itself, but used by @gen_test
        self._test_generator = None  # type: Optional[Union[Generator, Coroutine]]

    def setUp(self) -> None:
        setup_with_context_manager(self, warnings.catch_warnings())
        warnings.filterwarnings(
            "ignore",
            message="There is no current event loop",
            category=DeprecationWarning,
            module=r"delta_net\..*",
        )
        super().setUp()
        # NOTE: this code attempts to navigate deprecation warnings introduced
        # in Python 3.10. The idea of an implicit current event loop is
        # deprecated in that version, with the intention that tests like this
        # explicitly create a new event loop and run on it. However, other
        # packages such as pytest-asyncio (as of version 0.16.0) still rely on
        # the implicit current event loop and we want to be compatible with them
        # (even when run on 3.10, but not, of course, on the future version of
        # python that removes the get/set_event_loop methods completely).
        #
        # Deprecation warnings were introduced inconsistently:
        # asyncio.get_event_loop warns, but
        # asyncio.get_event_loop_policy().get_event_loop does not. Similarly,
        # none of the set_event_loop methods warn, although comments on
        # https://bugs.python.org/issue39529 indicate that they are also
        # intended for future removal.
        #
        # Therefore, we first attempt to access the event loop with the
        # (non-warning) policy method, and if it fails, fall back to creating a
        # new event loop. We do not have effective test coverage of the
        # new event loop case; this will have to be watched when/if
        # get_event_loop is actually removed.
        self.should_close_asyncio_loop = False
        try:
            self.asyncio_loop = asyncio.get_event_loop_policy().get_event_loop()
        except Exception:
            self.asyncio_loop = asyncio.new_event_loop()
            self.should_close_asyncio_loop = True

        async def get_loop() -> IOLoop:
            return self.get_new_ioloop()

        self.io_loop = self.asyncio_loop.run_until_complete(get_loop())
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            self.io_loop.make_current()

    def tearDown(self) -> None:
        # Native coroutines tend to produce warnings if they're not
        # allowed to run to completion. It's difficult to ensure that
        # this always happens in tests, so cancel any tasks that are
        # still pending by the time we get here.
        asyncio_loop = self.io_loop.asyncio_loop  # type: ignore
        if hasattr(asyncio, "all_tasks"):  # py37
            tasks = asyncio.all_tasks(asyncio_loop)  # type: ignore
        else:
            tasks = asyncio.Task.all_tasks(asyncio_loop)
        # Tasks that are done may still appear here and may contain
        # non-cancellation exceptions, so filter them out.
        tasks = [t for t in tasks if not t.done()]  # type: ignore
        for t in tasks:
            t.cancel()
        # Allow the tasks to run and finalize themselves (which means
        # raising a CancelledError inside the coroutine). This may
        # just transform the "task was destroyed but it is pending"
        # warning into a "uncaught CancelledError" warning, but
        # catching CancelledErrors in coroutines that may leak is
        # simpler than ensuring that no coroutines leak.
        if tasks:
            done, pending = self.io_loop.run_sync(lambda: asyncio.wait(tasks))
            assert not pending
            # If any task failed with anything but a CancelledError, raise it.
            for f in done:
                try:
                    f.result()
                except asyncio.CancelledError:
                    pass

        # Clean up Subprocess, so it can be used again with a new ioloop.
        Subprocess.uninitialize()
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            self.io_loop.clear_current()
        if not isinstance(self.io_loop, _NON_OWNED_IOLOOPS):
            # Try to clean up any file descriptors left open in the ioloop.
            # This avoids leaks, especially when tests are run repeatedly
            # in the same process with autoreload (because curl does not
            # set FD_CLOEXEC on its file descriptors)
            self.io_loop.close(all_fds=True)
        if self.should_close_asyncio_loop:
            self.asyncio_loop.close()
        super().tearDown()
        # In case an exception escaped or the StackContext caught an exception
        # when there wasn't a wait() to re-raise it, do so here.
        # This is our last chance to raise an exception in a way that the
        # unittest machinery understands.
        self.__rethrow()

    def get_new_ioloop(self) -> IOLoop:
        """Returns the `.IOLoop` to use for this test.

        By default, a new `.IOLoop` is created for each test.
        Subclasses may override this method to return
        `.IOLoop.current()` if it is not appropriate to use a new
        `.IOLoop` in each tests (for example, if there are global
        singletons using the default `.IOLoop`) or if a per-test event
        loop is being provided by another system (such as
        ``pytest-asyncio``).
        """
        return IOLoop(make_current=False)

    def _handle_exception(
        self, typ: Type[Exception], value: Exception, tb: TracebackType
    ) -> bool:
        if self.__failure is None:
            self.__failure = (typ, value, tb)
        else:
            app_log.error(
                "multiple unhandled exceptions in test", exc_info=(typ, value, tb)
            )
        self.stop()
        return True

    def __rethrow(self) -> None:
        if self.__failure is not None:
            failure = self.__failure
            self.__failure = None
            raise_exc_info(failure)

    def run(
        self, result: Optional[unittest.TestResult] = None
    ) -> Optional[unittest.TestResult]:
        ret = super().run(result)
        # As a last resort, if an exception escaped super.run() and wasn't
        # re-raised in tearDown, raise it here.  This will cause the
        # unittest run to fail messily, but that's better than silently
        # ignoring an error.
        self.__rethrow()
        return ret

    def stop(self, _arg: Any = None, **kwargs: Any) -> None:
        """Stops the `.IOLoop`, causing one pending (or future) call to `wait()`
        to return.

        Keyword arguments or a single positional argument passed to `stop()` are
        saved and will be returned by `wait()`.

        .. deprecated:: 5.1

           `stop` and `wait` are deprecated; use ``@gen_test`` instead.
        """
        assert _arg is None or not kwargs
        self.__stop_args = kwargs or _arg
        if self.__running:
            self.io_loop.stop()
            self.__running = False
        self.__stopped = True

    def wait(
        self,
        condition: Optional[Callable[..., bool]] = None,
        timeout: Optional[float] = None,
    ) -> Any:
        """Runs the `.IOLoop` until stop is called or timeout has passed.

        In the event of a timeout, an exception will be thrown. The
        default timeout is 5 seconds; it may be overridden with a
        ``timeout`` keyword argument or globally with the
        ``ASYNC_TEST_TIMEOUT`` environment variable.

        If ``condition`` is not ``None``, the `.IOLoop` will be restarted
        after `stop()` until ``condition()`` returns ``True``.

        .. versionchanged:: 3.1
           Added the ``ASYNC_TEST_TIMEOUT`` environment variable.

        .. deprecated:: 5.1

           `stop` and `wait` are deprecated; use ``@gen_test`` instead.
        """
        if timeout is None:
            timeout = get_async_test_timeout()

        if not self.__stopped:
            if timeout:

                def timeout_func() -> None:
                    try:
                        raise self.failureException(
                            "Async operation timed out after %s seconds" % timeout
                        )
                    except Exception:
                        self.__failure = sys.exc_info()
                    self.stop()

                self.__timeout = self.io_loop.add_timeout(
                    self.io_loop.time() + timeout, timeout_func
                )
            while True:
                self.__running = True
                self.io_loop.start()
                if self.__failure is not None or condition is None or condition():
                    break
            if self.__timeout is not None:
                self.io_loop.remove_timeout(self.__timeout)
                self.__timeout = None
        assert self.__stopped
        self.__stopped = False
        self.__rethrow()
        result = self.__stop_args
        self.__stop_args = None
        return result


class AsyncHTTPTestCase(AsyncTestCase):

    def setUp(self) -> None:
        super().setUp()
        sock, port = bind_unused_port()
        self.__port = port

        self.http_client = self.get_http_client()
        self._app = self.get_app()
        self.http_server = self.get_http_server()
        self.http_server.add_sockets([sock])

    def get_http_client(self) -> AsyncHTTPClient:
        return AsyncHTTPClient()

    def get_http_server(self) -> HTTPServer:
        return HTTPServer(self._app, **self.get_httpserver_options())

    def get_app(self) -> Application:
        """Should be overridden by subclasses to return a
        `delta_net.web.Application` or other `.HTTPServer` callback.
        """
        raise NotImplementedError()

    def fetch(
        self, path: str, raise_error: bool = False, **kwargs: Any
    ) -> HTTPResponse:

        if path.lower().startswith(("http://", "https://")):
            url = path
        else:
            url = self.get_url(path)
        return self.io_loop.run_sync(
            lambda: self.http_client.fetch(url, raise_error=raise_error, **kwargs),
            timeout=get_async_test_timeout(),
        )

    def get_httpserver_options(self) -> Dict[str, Any]:
        """May be overridden by subclasses to return additional
        keyword arguments for the server.
        """
        return {}

    def get_http_port(self) -> int:
        """Returns the port used by the server.

        A new port is chosen for each test.
        """
        return self.__port

    def get_protocol(self) -> str:
        return "http"

    def get_url(self, path: str) -> str:
        """Returns an absolute url for the given path on the test server."""
        return "%s://127.0.0.1:%s%s" % (self.get_protocol(), self.get_http_port(), path)

    def tearDown(self) -> None:
        self.http_server.stop()
        self.io_loop.run_sync(
            self.http_server.close_all_connections, timeout=get_async_test_timeout()
        )
        self.http_client.close()
        del self.http_server
        del self._app
        super().tearDown()


class AsyncHTTPSTestCase(AsyncHTTPTestCase):
    """A test case that starts an HTTPS server.

    Interface is generally the same as `AsyncHTTPTestCase`.
    """

    def get_http_client(self) -> AsyncHTTPClient:
        return AsyncHTTPClient(force_instance=True, defaults=dict(validate_cert=False))

    def get_httpserver_options(self) -> Dict[str, Any]:
        return dict(ssl_options=self.get_ssl_options())

    def get_ssl_options(self) -> Dict[str, Any]:
        """May be overridden by subclasses to select SSL options.

        By default includes a self-signed testing certificate.
        """
        return AsyncHTTPSTestCase.default_ssl_options()

    @staticmethod
    def default_ssl_options() -> Dict[str, Any]:
        # Testing keys were generated with:
        # openssl req -new -keyout delta_net/test/test.key \
        #                     -out delta_net/test/test.crt -nodes -days 3650 -x509
        module_dir = os.path.dirname(__file__)
        return dict(
            certfile=os.path.join(module_dir, "test", "test.crt"),
            keyfile=os.path.join(module_dir, "test", "test.key"),
        )

    def get_protocol(self) -> str:
        return "https"


@typing.overload
def gen_test(
    *, timeout: Optional[float] = None
) -> Callable[[Callable[..., Union[Generator, "Coroutine"]]], Callable[..., None]]:
    pass


@typing.overload  # noqa: F811
def gen_test(func: Callable[..., Union[Generator, "Coroutine"]]) -> Callable[..., None]:
    pass


def gen_test(  # noqa: F811
    func: Optional[Callable[..., Union[Generator, "Coroutine"]]] = None,
    timeout: Optional[float] = None,
) -> Union[
    Callable[..., None],
    Callable[[Callable[..., Union[Generator, "Coroutine"]]], Callable[..., None]],
]:

    if timeout is None:
        timeout = get_async_test_timeout()

    def wrap(f: Callable[..., Union[Generator, "Coroutine"]]) -> Callable[..., None]:
        # Stack up several decorators to allow us to access the generator
        # object itself.  In the innermost wrapper, we capture the generator
        # and save it in an attribute of self.  Next, we run the wrapped
        # function through @gen.coroutine.  Finally, the coroutine is
        # wrapped again to make it synchronous with run_sync.
        #
        # This is a good case study arguing for either some sort of
        # extensibility in the gen decorators or cancellation support.
        @functools.wraps(f)
        def pre_coroutine(self, *args, **kwargs):
            # type: (AsyncTestCase, *Any, **Any) -> Union[Generator, Coroutine]
            # Type comments used to avoid pypy3 bug.
            result = f(self, *args, **kwargs)
            if isinstance(result, Generator) or inspect.iscoroutine(result):
                self._test_generator = result
            else:
                self._test_generator = None
            return result

        if inspect.iscoroutinefunction(f):
            coro = pre_coroutine
        else:
            coro = gen.coroutine(pre_coroutine)  # type: ignore[assignment]

        @functools.wraps(coro)
        def post_coroutine(self, *args, **kwargs):
            # type: (AsyncTestCase, *Any, **Any) -> None
            try:
                return self.io_loop.run_sync(
                    functools.partial(coro, self, *args, **kwargs), timeout=timeout
                )
            except TimeoutError as e:
                # run_sync raises an error with an unhelpful traceback.
                # If the underlying generator is still running, we can throw the
                # exception back into it so the stack trace is replaced by the
                # point where the test is stopped. The only reason the generator
                # would not be running would be if it were cancelled, which means
                # a native coroutine, so we can rely on the cr_running attribute.
                if self._test_generator is not None and getattr(
                    self._test_generator, "cr_running", True
                ):
                    self._test_generator.throw(type(e), e)
                    # In case the test contains an overly broad except
                    # clause, we may get back here.
                # Coroutine was stopped or didn't raise a useful stack trace,
                # so re-raise the original exception which is better than nothing.
                raise

        return post_coroutine

    if func is not None:
        # Used like:
        #     @gen_test
        #     def f(self):
        #         pass
        return wrap(func)
    else:
        # Used like @gen_test(timeout=10)
        return wrap


# Without this attribute, nosetests will try to run gen_test as a test
# anywhere it is imported.
gen_test.__test__ = False  # type: ignore


class ExpectLog(logging.Filter):

    def __init__(
        self,
        logger: Union[logging.Logger, basestring_type],
        regex: str,
        required: bool = True,
        level: Optional[int] = None,
    ) -> None:


        if isinstance(logger, basestring_type):
            logger = logging.getLogger(logger)
        self.logger = logger
        self.regex = re.compile(regex)
        self.required = required
        self.matched = False
        self.logged_stack = False
        self.level = level
        self.orig_level = None  # type: Optional[int]

    def filter(self, record: logging.LogRecord) -> bool:
        if record.exc_info:
            self.logged_stack = True
        message = record.getMessage()
        if self.regex.match(message):
            if self.level is not None and record.levelno != self.level:
                app_log.warning(
                    "Got expected log message %r at unexpected level (%s vs %s)"
                    % (message, logging.getLevelName(self.level), record.levelname)
                )
                return True
            self.matched = True
            return False
        return True

    def __enter__(self) -> "ExpectLog":
        if self.level is not None and self.level < self.logger.getEffectiveLevel():
            self.orig_level = self.logger.level
            self.logger.setLevel(self.level)
        self.logger.addFilter(self)
        return self

    def __exit__(
        self,
        typ: "Optional[Type[BaseException]]",
        value: Optional[BaseException],
        tb: Optional[TracebackType],
    ) -> None:
        if self.orig_level is not None:
            self.logger.setLevel(self.orig_level)
        self.logger.removeFilter(self)
        if not typ and self.required and not self.matched:
            raise Exception("did not get expected log message")


# From https://nedbatchelder.com/blog/201508/using_context_managers_in_test_setup.html
def setup_with_context_manager(testcase: unittest.TestCase, cm: Any) -> Any:
    """Use a contextmanager to setUp a test case."""
    val = cm.__enter__()
    testcase.addCleanup(cm.__exit__, None, None, None)
    return val


def main(**kwargs: Any) -> None:

    from delta_net.options import define, options, parse_command_line

    define(
        "exception_on_interrupt",
        type=bool,
        default=True,
        help=(
            "If true (default), ctrl-c raises a KeyboardInterrupt "
            "exception.  This prints a stack trace but cannot interrupt "
            "certain operations.  If false, the process is more reliably "
            "killed, but does not print a stack trace."
        ),
    )

    # support the same options as unittest's command-line interface
    define("verbose", type=bool)
    define("quiet", type=bool)
    define("failfast", type=bool)
    define("catch", type=bool)
    define("buffer", type=bool)

    argv = [sys.argv[0]] + parse_command_line(sys.argv)

    if not options.exception_on_interrupt:
        signal.signal(signal.SIGINT, signal.SIG_DFL)

    if options.verbose is not None:
        kwargs["verbosity"] = 2
    if options.quiet is not None:
        kwargs["verbosity"] = 0
    if options.failfast is not None:
        kwargs["failfast"] = True
    if options.catch is not None:
        kwargs["catchbreak"] = True
    if options.buffer is not None:
        kwargs["buffer"] = True

    if __name__ == "__main__" and len(argv) == 1:
        print("No tests specified", file=sys.stderr)
        sys.exit(1)
    # In order to be able to run tests by their fully-qualified name
    # on the command line without importing all tests here,
    # module must be set to None.  Python 3.2's unittest.main ignores
    # defaultTest if no module is given (it tries to do its own
    # test discovery, which is incompatible with auto2to3), so don't
    # set module if we're not asking for a specific test.
    if len(argv) > 1:
        unittest.main(module=None, argv=argv, **kwargs)  # type: ignore
    else:
        unittest.main(defaultTest="all", argv=argv, **kwargs)


if __name__ == "__main__":
    main()
