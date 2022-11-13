import socket
import sys

import twisted.internet.abstract  # type: ignore
import twisted.internet.asyncioreactor  # type: ignore
from twisted.internet.defer import Deferred  # type: ignore
from twisted.python import failure  # type: ignore
import twisted.names.cache  # type: ignore
import twisted.names.client  # type: ignore
import twisted.names.hosts  # type: ignore
import twisted.names.resolve  # type: ignore


from delta_net.concurrent import Future, future_set_exc_info
from delta_net.escape import utf8
from delta_net import gen
from delta_net.netutil import Resolver

import typing

if typing.TYPE_CHECKING:
    from typing import Generator, Any, List, Tuple  # noqa: F401


class TwistedResolver(Resolver):
   
    def initialize(self) -> None:
        # partial copy of twisted.names.client.createResolver, which doesn't
        # allow for a reactor to be passed in.
        self.reactor = twisted.internet.asyncioreactor.AsyncioSelectorReactor()

        host_resolver = twisted.names.hosts.Resolver("/etc/hosts")
        cache_resolver = twisted.names.cache.CacheResolver(reactor=self.reactor)
        real_resolver = twisted.names.client.Resolver(
            "/etc/resolv.conf", reactor=self.reactor
        )
        self.resolver = twisted.names.resolve.ResolverChain(
            [host_resolver, cache_resolver, real_resolver]
        )

    @gen.coroutine
    def resolve(
        self, host: str, port: int, family: int = 0
    ) -> "Generator[Any, Any, List[Tuple[int, Any]]]":
        # getHostByName doesn't accept IP addresses, so if the input
        # looks like an IP address just return it immediately.
        if twisted.internet.abstract.isIPAddress(host):
            resolved = host
            resolved_family = socket.AF_INET
        elif twisted.internet.abstract.isIPv6Address(host):
            resolved = host
            resolved_family = socket.AF_INET6
        else:
            deferred = self.resolver.getHostByName(utf8(host))
            fut = Future()  # type: Future[Any]
            deferred.addBoth(fut.set_result)
            resolved = yield fut
            if isinstance(resolved, failure.Failure):
                try:
                    resolved.raiseException()
                except twisted.names.error.DomainError as e:
                    raise IOError(e)
            elif twisted.internet.abstract.isIPAddress(resolved):
                resolved_family = socket.AF_INET
            elif twisted.internet.abstract.isIPv6Address(resolved):
                resolved_family = socket.AF_INET6
            else:
                resolved_family = socket.AF_UNSPEC
        if family != socket.AF_UNSPEC and family != resolved_family:
            raise Exception(
                "Requested socket family %d but got %d" % (family, resolved_family)
            )
        result = [(typing.cast(int, resolved_family), (resolved, port))]
        return result


def install() -> None:
    
    from twisted.internet.asyncioreactor import install

    install()


if hasattr(gen.convert_yielded, "register"):

    @gen.convert_yielded.register(Deferred)  # type: ignore
    def _(d: Deferred) -> Future:
        f = Future()  # type: Future[Any]

        def errback(failure: failure.Failure) -> None:
            try:
                failure.raiseException()
                # Should never happen, but just in case
                raise Exception("errback called without error")
            except:
                future_set_exc_info(f, sys.exc_info())

        d.addCallbacks(f.set_result, errback)
        return f
