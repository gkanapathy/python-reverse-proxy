from aiohttp import web
import aiohttp
import asyncio
import logging


logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


targetBaseUrl = "http://localhost:8501"
mountPoint = "/"


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
        """
        Here, check the upstream_req URL, ensure the user is authorized, then look up the correct URL/host/port for the incoming base URL.
        Incoming base URL should be something like https://app.daisi.io/daisi-uuidbase/st/*.
        The base portion (up to and including /st) should be removed and replaced with the correct base URL for the Daisi
        by setting targetBaseUrl and mountPoint
        """

        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(auto_decompress=False)

        if (
            upstream_req.headers["Connection"] == "Upgrade"
            and upstream_req.headers["Upgrade"] == "websocket"
            and upstream_req.method == "GET"
        ):  
            upstream_ws_response = web.WebSocketResponse()
            await upstream_ws_response.prepare(upstream_req)
            async with self._session.ws_connect(
                targetBaseUrl + upstream_req.path_qs,
                headers={"Cookie": upstream_req.headers["cookie"]},
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

        else:  # it's a normal request
            async with self._session.request(
                upstream_req.method,
                targetBaseUrl + upstream_req.path_qs,
                headers=upstream_req.headers,
                data=upstream_req.content,
            ) as downstream_response:
                #TODO: handle chunked responses by streaming chunks instead of buffering
                h = downstream_response.headers.copy()
                h.pop("Transfer-Encoding", None)
                upstream_resp = web.Response(
                    body=await downstream_response.read(),
                    status=downstream_response.status,
                    headers=h,
                )
                return upstream_resp


async def main():
    async with Handlers() as handlers:
        app = web.Application()
        app.router.add_route("*", mountPoint + "{proxyPath:.*}", handlers.proxy_handler)
        runner = web.AppRunner(app, auto_decompress=False)
        await runner.setup()
        await web.TCPSite(runner, port=3984).start()
        while True:
            await asyncio.sleep(67)


if __name__ == "__main__":
    asyncio.run(main())
