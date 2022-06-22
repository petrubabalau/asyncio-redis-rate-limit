import asyncio
import hashlib
from functools import wraps
from types import TracebackType
from typing import (
    TYPE_CHECKING,
    Any,
    Awaitable,
    Callable,
    NamedTuple,
    Optional,
    Type,
    TypeVar,
)

from redis.asyncio import Redis
from typing_extensions import TypeAlias, final

if TYPE_CHECKING:  # pragma: no cover
    _AsyncRedis = Redis[Any]
else:
    _AsyncRedis = Redis

#: This makes our code more readable.
_Seconds: TypeAlias = int

_CoroutineFunction = TypeVar(
    '_CoroutineFunction',
    bound=Callable[..., Awaitable[Any]],
)

_RateLimiterT = TypeVar('_RateLimiterT', bound='RateLimiter')


@final
class RateLimitError(Exception):
    """We raise this error when rate limit is hit."""


@final
class RateSpec(NamedTuple):
    """
    Specifies the amount of requests can be made in the time frame in seconds.

    It is much nicier than using a custom string format like ``100/1s``.
    """

    requests: int
    seconds: _Seconds


class RateLimiter(object):
    """Implements rate limiting."""

    __slots__ = (
        '_unique_key',
        '_rate_spec',
        '_backend',
        '_cache_prefix',
        '_lock',
    )

    def __init__(
        self,
        unique_key: str,
        rate_spec: RateSpec,
        backend: _AsyncRedis,
        *,
        cache_prefix: str,
    ) -> None:
        """In the future other backends might be supported as well."""
        self._unique_key = unique_key
        self._rate_spec = rate_spec
        self._backend = backend
        self._cache_prefix = cache_prefix
        self._lock = asyncio.Lock()

    async def __aenter__(self: _RateLimiterT) -> _RateLimiterT:
        """
        Async context manager API.

        Before this object will be used, we call ``self._acquire`` to be sure
        that we can actually make any actions in this time frame.
        """
        await self._acquire()
        return self

    async def __aexit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc: Optional[BaseException],
        tb: Optional[TracebackType],
    ) -> None:
        """Do nothing. We need this to ``__aenter__`` to work."""

    # Private API:

    async def _acquire(self) -> None:
        cache_key = _make_cache_key(
            unique_key=self._unique_key,
            rate_spec=self._rate_spec,
            cache_prefix=self._cache_prefix,
        )
        pipeline = self._backend.pipeline()

        async with self._lock:
            # https://redis.io/commands/incr/#pattern-rate-limiter-1
            pipeline.incr(cache_key)  # type: ignore[unused-coroutine]
            pipeline.expire(  # type: ignore[unused-coroutine]
                cache_key,
                self._rate_spec.seconds,
                nx=True,
            )
            current_rate, _expire = await pipeline.execute()

            if current_rate > self._rate_spec.requests:
                raise RateLimitError('Rate limit is hit', current_rate)


def rate_limit(
    rate_spec: RateSpec,
    backend: _AsyncRedis,
    *,
    cache_prefix: str = 'aio-rate-limit',
) -> Callable[[_CoroutineFunction], _CoroutineFunction]:
    """
    Rate limits a function.

    Code example:

      .. code:: python

        >>> from aio_redis_rate_limit import rate_limit, RateSpec
        >>> from redis.asyncio import Redis as AsyncRedis

        >>> redis = AsyncRedis.from_url('redis://localhost:6379')

        >>> @rate_limit(
        ...    rate_spec=RateSpec(requests=1200, seconds=60),
        ...    backend=redis,
        ... )
        ... async def request() -> int:
        ...     ...   # Do something

    """
    def decorator(function: _CoroutineFunction) -> _CoroutineFunction:
        @wraps(function)
        async def factory(*args, **kwargs) -> Any:
            async with RateLimiter(
                unique_key=function.__qualname__,
                backend=backend,
                rate_spec=rate_spec,
                cache_prefix=cache_prefix,
            ):
                return await function(*args, **kwargs)
        return factory  # type: ignore[return-value]
    return decorator


def _make_cache_key(
    unique_key: str,
    rate_spec: RateSpec,
    cache_prefix: str,
) -> str:
    parts = ''.join([unique_key, str(rate_spec)])
    return cache_prefix + hashlib.md5(  # noqa: S303
        parts.encode('utf-8'),
    ).hexdigest()
