import gzip

import httpx

from content import net


def test_gzip_responses_are_decoded_once(monkeypatch):
    body = b'{"name": "Acme"}'

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=gzip.compress(body), headers={"content-encoding": "gzip"}
        )

    monkeypatch.setattr(net, "transport", httpx.MockTransport(handler))
    monkeypatch.setattr(net, "resolver", lambda host, port: ["93.184.216.34"])
    assert net.fetch("https://wp.example.com/wp-json/").content == body
