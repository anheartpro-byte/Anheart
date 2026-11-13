"""An operator signed in, for the tests that act on the console (ANH-152).

Every action of the console takes an operator signed in on the device
(:mod:`src.console_access`). A test that is not about the sign-in gives the
console a list held in memory (:func:`held_list`, adopted without a signature:
the signature is :mod:`tests.test_operator_sync`'s subject) and signs in once.
Held in memory, the code is hashed with scrypt parameters far below Convex's, so
that a sign-in costs nothing. A list the console reads from a signed text takes
the contract's parameters only (:func:`signed_code`).
"""

from __future__ import annotations

import contextlib
import functools
import hashlib
from collections.abc import AsyncGenerator
from typing import Final, override

import httpx
from fastapi import FastAPI

from src.clock import Clock
from src.console_access import (
    HASH_BYTES,
    SCRYPT_N,
    SCRYPT_P,
    SCRYPT_R,
    ConsoleAccess,
    ListedDevice,
    ListedOperator,
    OperatorList,
    ScryptCode,
)
from src.units import UnixMillis
from src.web.deps import TOKEN_HEADER, Services

OPERATOR_ID: Final[str] = "operateur-test"
"""The account of the operator the tests sign in, as Convex would name it."""

OPERATOR_NAME: Final[str] = "Operateur Test"

CODE: Final[str] = "482915"
"""Their console code. A test value: it opens nothing anywhere."""

SALT: Final[bytes] = b"sel-de-test-1234"


def scrypt_code(code: str, salt: bytes = SALT) -> ScryptCode:
    """``code`` hashed as a list carries it, with cheap parameters."""
    n, r, p = 16, 1, 1
    digest = hashlib.scrypt(code.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=32)
    return ScryptCode(n=n, r=r, p=p, salt=salt, digest=digest)


@functools.cache
def signed_code(code: str, salt: bytes = SALT) -> ScryptCode:
    """``code`` hashed as Convex hashes it, for a list read from a signed text. Once per code."""
    digest = hashlib.scrypt(
        code.encode("utf-8"),
        salt=salt,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        maxmem=64 * 1024 * 1024,
        dklen=HASH_BYTES,
    )
    return ScryptCode(n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, salt=salt, digest=digest)


def listed(
    user_id: str = OPERATOR_ID, name: str = OPERATOR_NAME, code: str = CODE
) -> ListedOperator:
    return ListedOperator(user_id=user_id, name=name, code=scrypt_code(code))


def tablet(device_id: str, token: str, name: str = "Tablette") -> ListedDevice:
    return ListedDevice(
        device_id=device_id,
        name=name,
        token_sha256=hashlib.sha256(token.encode("utf-8")).digest(),
    )


def held_list(
    *,
    operators: tuple[ListedOperator, ...] | None = None,
    devices: tuple[ListedDevice, ...] = (),
    issued_at: int = 1_790_000_000_000,
) -> OperatorList:
    """A list of one operator (:data:`OPERATOR_ID`) and no tablet, unless told otherwise."""
    return OperatorList(
        machine_id="machine-test",
        issued_at=UnixMillis(issued_at),
        operators=(listed(),) if operators is None else operators,
        devices=devices,
    )


def open_access(clock: Clock) -> ConsoleAccess:
    """An access holding :func:`held_list`, its counts in memory only."""
    return ConsoleAccess(clock=clock, held=held_list(), lockout_path=None)


async def sign_in(
    client: httpx.AsyncClient,
    services: Services,
    headers: dict[str, str] | None = None,
) -> None:
    """Sign :data:`OPERATOR_ID` in through the API, giving the console the test list first
    when it holds none. The client keeps the cookie."""
    access = services.access
    if access is None:
        raise AssertionError("these services have no access to sign in to")
    if access.held is None:
        access.adopt(held_list())
    answer = await client.post(
        "/api/access/sign-in", json={"user_id": OPERATOR_ID, "code": CODE}, headers=headers
    )
    if answer.status_code != httpx.codes.OK:
        raise AssertionError(f"sign-in refused: {answer.status_code} {answer.text}")


class SignedTransport(httpx.AsyncBaseTransport):
    """An in-process transport that signs :data:`OPERATOR_ID` in before its requests.

    For the rigs whose ``http()`` hands out a client synchronously: the sign-in
    takes the request's device token, and its cookie goes with every request
    after. Until the console accepts one (a request without a token, say), each
    request tries again and goes as it is: the test sees what the console answers.
    """

    def __init__(self, app: FastAPI, services: Services) -> None:
        self._inner: httpx.ASGITransport = httpx.ASGITransport(app=app)
        self._services: Services = services
        self._cookie: str | None = None

    @override
    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if self._cookie is None:
            access = self._services.access
            if access is not None and access.held is None:
                access.adopt(held_list())
            token = (
                {TOKEN_HEADER: request.headers[TOKEN_HEADER]}
                if TOKEN_HEADER in request.headers
                else {}
            )
            signing = httpx.Request(
                "POST",
                request.url.copy_with(path="/api/access/sign-in", query=None),
                json={"user_id": OPERATOR_ID, "code": CODE},
                headers=token,
            )
            answer = await self._inner.handle_async_request(signing)
            _ = await answer.aread()
            if answer.status_code == httpx.codes.OK and "set-cookie" in answer.headers:
                self._cookie = answer.headers["set-cookie"].split(";", 1)[0]
        if self._cookie is not None:
            cookies = [request.headers["cookie"]] if "cookie" in request.headers else []
            request.headers["cookie"] = "; ".join([*cookies, self._cookie])
        return await self._inner.handle_async_request(request)


def signed_http(
    app: FastAPI, services: Services, headers: dict[str, str] | None = None
) -> httpx.AsyncClient:
    """A client of ``app`` in-process whose operator signs in at its first request."""
    return httpx.AsyncClient(
        transport=SignedTransport(app, services), base_url="http://testserver", headers=headers
    )


@contextlib.asynccontextmanager
async def signed_client(
    app: FastAPI,
    services: Services,
    headers: dict[str, str] | None = None,
) -> AsyncGenerator[httpx.AsyncClient]:
    """A client of ``app`` in-process, :data:`OPERATOR_ID` signed in (:func:`sign_in`)."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        await sign_in(client, services, headers)
        yield client
