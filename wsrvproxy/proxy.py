import asyncio
import logging

import aiohttp
from aiohttp import web

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
logging.basicConfig(level=logging.INFO)


localMountPoint = "/"


async def _wsforward(ws_from, ws_to):
    async for msg in ws_from:
        if msg.type == aiohttp.WSMsgType.TEXT:
            await ws_to.send_str(msg.data)
        elif msg.type == aiohttp.WSMsgType.BINARY:
            await ws_to.send_bytes(msg.data)
        elif ws_to.closed:
            await ws_to.close(code=ws_to.close_code, message=msg.extra)
        else:
            raise ValueError(f"unexpected ws message type: {msg.type}")


class Handlers:
    def __init__(self):
        self._session = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self._session and not self._session.closed:
            await self._session.close()

    async def proxy_handler(self, upstream_req):
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(auto_decompress=False)

        """
        Here, check the upstream_req URL, ensure the user is authorized, then look up the correct URL/host/port for the incoming base URL.
        raise `web.HTTPNotFound()` exception in the handler if the URL is not authorized.
        """
        userAuthorized = True
        if not userAuthorized:
            raise web.HTTPNotFound()

        if upstream_req.path_qs:
            targetBaseUrl = "http://httpbin.org:80"
        else:
            raise web.HTTPNotFound()

        if (
            upstream_req.headers.get("Connection") == "Upgrade"
            and upstream_req.headers.get("Upgrade") == "websocket"
            and upstream_req.method == "GET"
        ):  # it's a websocket upgrade request
            upstream_ws_response = web.WebSocketResponse()
            await upstream_ws_response.prepare(upstream_req)
            async with self._session.ws_connect(
                targetBaseUrl + upstream_req.path_qs,
                headers=dict(upstream_req.headers),
            ) as downstream_ws_client:

                down2up = asyncio.create_task(
                    _wsforward(downstream_ws_client, upstream_ws_response)
                )
                up2down = asyncio.create_task(
                    _wsforward(upstream_ws_response, downstream_ws_client)
                )
                await asyncio.wait(
                    (down2up, up2down), return_when=asyncio.FIRST_COMPLETED
                )
                return upstream_ws_response

        else:  # it's an HTTP request

            async with self._session.request(
                upstream_req.method,
                targetBaseUrl + upstream_req.path_qs,
                headers=upstream_req.headers,
                data=upstream_req.content,
            ) as downstream_response:
                h = downstream_response.headers.copy()

                # I was hoping this would work for both chunked and non-chunked responses, but it doesn't.
                # upstream_resp = web.StreamResponse(status=downstream_response.status, reason=downstream_response.reason, headers=h)
                # if h.get("Transfer-Encoding") == "chunked":
                #     upstream_resp.enable_chunked_encoding()
                # await upstream_resp.prepare(upstream_req)
                # async for data,_ in downstream_response.content.iter_chunks():
                #     await upstream_resp.write(data)

                if h.get("Transfer-Encoding") == "chunked":
                    upstream_resp = web.StreamResponse(
                        status=downstream_response.status,
                        reason=downstream_response.reason,
                        headers=h,
                    )
                    upstream_resp.enable_chunked_encoding()
                    await upstream_resp.prepare(upstream_req)
                    async for data, _ in downstream_response.content.iter_chunks():
                        await upstream_resp.write(data)
                    await upstream_resp.write_eof()
                    return upstream_resp
                else:
                    upstream_resp = web.Response(
                        status=downstream_response.status,
                        reason=downstream_response.reason,
                        headers=h,
                        body=await downstream_response.content.read(),
                    )

                return upstream_resp


async def main():
    async with Handlers() as handlers:
        app = web.Application()
        app.router.add_route(
            "*", localMountPoint + "{proxyPath:.*}", handlers.proxy_handler
        )
        runner = web.AppRunner(app, auto_decompress=False)
        await runner.setup()
        await web.TCPSite(runner, port=3984).start()
        while True:
            await asyncio.sleep(67)


if __name__ == "__main__":
    asyncio.run(main())
