from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Literal, assert_never, override

import httpx
import pytest

from src.cloud_sync import MAX_RESPONSE_BYTES, CloudError, Document, HttpxTransport, Unreachable
from src.local_config import CloudConfig
from src.result import Err, Ok, Result


@dataclass
class ResponseBody(httpx.AsyncByteStream):
    """Track consumption and closure of a remote streaming response."""

    chunks: tuple[bytes, ...]
    yielded: int = 0
    closed: bool = False

    @override
    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            self.yielded += len(chunk)
            yield chunk

    @override
    async def aclose(self) -> None:
        self.closed = True


async def send(
    transport: HttpxTransport, method: Literal["get", "post", "upload"]
) -> Result[Document, CloudError]:
    match method:
        case "get":
            return await transport.get("/large")
        case "post":
            return await transport.post("/large", {})
        case "upload":
            return await transport.upload("https://example.convex.cloud/upload", "text/plain", b"x")
    raise assert_never(method)


@pytest.mark.parametrize("method", ["get", "post", "upload"])
@pytest.mark.parametrize("status", [200, 500])
async def test_large_streams_are_stopped_and_closed_before_the_whole_body_is_read(
    method: Literal["get", "post", "upload"], status: int
) -> None:
    body = ResponseBody((b" " * 4096,) * 512)
    config = CloudConfig(url="https://example.convex.site", api_key="k")
    client = httpx.AsyncClient(
        base_url=config.url,
        transport=httpx.MockTransport(lambda _request: httpx.Response(status, stream=body)),
    )
    transport = HttpxTransport(config, client=client)

    result = await send(transport, method)

    assert isinstance(result, Err)
    assert isinstance(result.error, Unreachable)
    assert body.yielded <= 1024 * 1024 + 16 * 1024
    assert body.closed
    await transport.close()


async def test_a_json_response_at_the_byte_limit_is_accepted() -> None:
    content = b'{"x":"' + b"a" * (MAX_RESPONSE_BYTES - 8) + b'"}'
    body = ResponseBody((content,))
    config = CloudConfig(url="https://example.convex.site", api_key="k")
    client = httpx.AsyncClient(
        base_url=config.url,
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, stream=body)),
    )
    transport = HttpxTransport(config, client=client)

    result = await transport.get("/exact")

    assert result == Ok({"x": "a" * (MAX_RESPONSE_BYTES - 8)})
    assert body.closed
    await transport.close()


async def test_a_buffered_response_over_the_limit_is_refused() -> None:
    config = CloudConfig(url="https://example.convex.site", api_key="k")
    client = httpx.AsyncClient(
        base_url=config.url,
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, content=b" " * (MAX_RESPONSE_BYTES + 1))
        ),
    )
    transport = HttpxTransport(config, client=client)

    result = await transport.get("/buffered")

    assert isinstance(result, Err)
    assert isinstance(result.error, Unreachable)
    await transport.close()


async def test_unrequested_compression_is_refused_without_reading_the_body() -> None:
    body = ResponseBody((b"compressed",))
    config = CloudConfig(url="https://example.convex.site", api_key="k")

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.headers["Accept-Encoding"] == "identity"
        return httpx.Response(200, headers={"Content-Encoding": "gzip"}, stream=body)

    client = httpx.AsyncClient(base_url=config.url, transport=httpx.MockTransport(respond))
    transport = HttpxTransport(config, client=client)

    result = await transport.get("/compressed")

    assert isinstance(result, Err)
    assert isinstance(result.error, Unreachable)
    assert body.yielded == 0
    assert body.closed
    await transport.close()
