import io
import unittest

from PIL import Image

import mail_images
from mail_images import (
    ImageLimitError,
    ImageValidationError,
    RemoteImageFetcher,
    RemoteImageResponse,
    RemoteImageResponseError,
    RemoteImageSecurityError,
    validate_image,
)


def png_bytes(size=(4, 3)):
    output = io.BytesIO()
    Image.new("RGB", size, (24, 92, 168)).save(output, format="PNG")
    return output.getvalue()


class RecordingTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def __call__(
        self,
        request,
        *,
        max_bytes,
        connect_timeout,
        deadline,
    ):
        self.requests.append(request)
        if not self.responses:
            raise AssertionError("unexpected transport request")
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def public_resolver(hostname, port):
    return ("8.8.8.8",)


class ImageValidationTests(unittest.TestCase):
    def test_accepts_supported_image_and_returns_canonical_metadata(self):
        payload = png_bytes((17, 9))
        result = validate_image(payload, "image/png; charset=binary")
        self.assertEqual(result.data, payload)
        self.assertEqual(result.mime_type, "image/png")
        self.assertEqual(result.dimensions, (17, 9))
        self.assertEqual(result.frame_count, 1)

    def test_rejects_svg_malformed_and_content_type_mismatch(self):
        with self.assertRaises(ImageValidationError):
            validate_image(b"<svg xmlns='http://www.w3.org/2000/svg'></svg>")
        with self.assertRaises(ImageValidationError):
            validate_image(b"not an image")
        with self.assertRaises(ImageValidationError):
            validate_image(png_bytes(), "image/jpeg")

    def test_enforces_byte_dimension_pixel_and_frame_limits(self):
        with self.assertRaises(ImageLimitError):
            validate_image(b"x" * (mail_images.MAX_IMAGE_BYTES + 1))

        edge = io.BytesIO()
        Image.new("1", (mail_images.MAX_IMAGE_EDGE + 1, 1)).save(edge, "PNG")
        with self.assertRaises(ImageLimitError):
            validate_image(edge.getvalue())

        pixels = io.BytesIO()
        Image.new("1", (5001, 5000)).save(pixels, "PNG")
        with self.assertRaises(ImageLimitError):
            validate_image(pixels.getvalue())

        animation = io.BytesIO()
        frames = [
            Image.new("RGB", (2, 2), (index % 256, (index * 3) % 256, 80))
            for index in range(101)
        ]
        frames[0].save(
            animation,
            "GIF",
            save_all=True,
            append_images=frames[1:],
            duration=10,
            loop=0,
        )
        with self.assertRaises(ImageLimitError):
            validate_image(animation.getvalue())


class RemoteImageFetcherTests(unittest.TestCase):
    def fetcher(self, responses, resolver=public_resolver, **kwargs):
        transport = RecordingTransport(responses)
        return (
            RemoteImageFetcher(
                resolver=resolver,
                transport=transport,
                **kwargs,
            ),
            transport,
        )

    def ok_response(self, **headers):
        values = {"Content-Type": "image/png"}
        values.update(headers)
        return RemoteImageResponse(200, values, png_bytes())

    def test_pins_resolved_ip_and_sends_no_sensitive_headers(self):
        fetcher, transport = self.fetcher([self.ok_response()])
        result = fetcher.fetch("https://images.example/path/logo.png?q=mail")
        request = transport.requests[0]
        headers = dict(request.headers)
        self.assertEqual(request.ip_address, "8.8.8.8")
        self.assertEqual(request.hostname, "images.example")
        self.assertEqual(request.port, 443)
        self.assertEqual(request.target, "/path/logo.png?q=mail")
        self.assertEqual(headers["Host"], "images.example")
        for forbidden in ("Authorization", "Cookie", "Origin", "Referer"):
            self.assertNotIn(forbidden, headers)
        self.assertEqual(result.mime_type, "image/png")
        self.assertEqual(result.final_url, "https://images.example/path/logo.png?q=mail")

    def test_rejects_unsafe_urls_before_transport(self):
        unsafe_urls = (
            "ftp://images.example/a.png",
            "https://user:secret@images.example/a.png",
            "https://images.example:444/a.png",
            "https://images.example/a.png#fragment",
            "https://images.example\\@evil.example/a.png",
        )
        for url in unsafe_urls:
            with self.subTest(url=url):
                fetcher, transport = self.fetcher([])
                with self.assertRaises(RemoteImageSecurityError):
                    fetcher.fetch(url)
                self.assertEqual(transport.requests, [])

    def test_rejects_all_non_public_address_classes(self):
        addresses = (
            "127.0.0.1",
            "10.0.0.1",
            "169.254.1.1",
            "224.0.0.1",
            "0.0.0.0",
            "192.0.2.1",
            "::1",
            "fe80::1",
            "ff02::1",
            "::",
        )
        for address in addresses:
            with self.subTest(address=address):
                fetcher, transport = self.fetcher(
                    [], resolver=lambda hostname, port, value=address: (value,)
                )
                with self.assertRaises(RemoteImageSecurityError):
                    fetcher.fetch("https://images.example/a.png")
                self.assertEqual(transport.requests, [])

    def test_allows_proxy_fake_ip_for_domain_names_only(self):
        fetcher, transport = self.fetcher(
            [self.ok_response()],
            resolver=lambda hostname, port: ("198.18.12.34",),
        )
        result = fetcher.fetch("https://images.example/a.png")
        self.assertEqual(result.mime_type, "image/png")
        self.assertEqual(transport.requests[0].ip_address, "198.18.12.34")

        fetcher, transport = self.fetcher(
            [], resolver=lambda hostname, port: ("198.18.12.34",)
        )
        with self.assertRaises(RemoteImageSecurityError):
            fetcher.fetch("https://198.18.12.34/a.png")
        self.assertEqual(transport.requests, [])

    def test_allows_redirects_but_rejects_downgrade_and_excess(self):
        fetcher, transport = self.fetcher(
            [
                RemoteImageResponse(302, {"Location": "https://cdn.example/a.png"}),
                self.ok_response(),
            ]
        )
        result = fetcher.fetch("http://images.example/a.png")
        self.assertEqual(result.redirect_count, 1)
        self.assertEqual(result.final_url, "https://cdn.example/a.png")
        self.assertEqual([item.hostname for item in transport.requests], [
            "images.example",
            "cdn.example",
        ])

        fetcher, transport = self.fetcher(
            [RemoteImageResponse(302, {"Location": "http://cdn.example/a.png"})]
        )
        with self.assertRaises(RemoteImageSecurityError):
            fetcher.fetch("https://images.example/a.png")
        self.assertEqual(len(transport.requests), 1)

        fetcher, _ = self.fetcher(
            [
                RemoteImageResponse(302, {"Location": "/2"}),
                RemoteImageResponse(302, {"Location": "/3"}),
            ],
            max_redirects=1,
        )
        with self.assertRaises(RemoteImageResponseError):
            fetcher.fetch("https://images.example/1")

    def test_rejects_missing_or_mismatched_type_and_oversized_body(self):
        cases = (
            RemoteImageResponse(200, {}, png_bytes()),
            RemoteImageResponse(200, {"Content-Type": "image/jpeg"}, png_bytes()),
            RemoteImageResponse(
                200,
                {"Content-Type": "image/png"},
                b"x" * (mail_images.MAX_IMAGE_BYTES + 1),
            ),
            RemoteImageResponse(
                200,
                {
                    "Content-Type": "image/png",
                    "Content-Length": str(mail_images.MAX_IMAGE_BYTES + 1),
                },
                png_bytes(),
            ),
        )
        for response in cases:
            with self.subTest(headers=response.headers, body_size=len(response.body)):
                fetcher, _ = self.fetcher([response])
                with self.assertRaises((ImageValidationError, ImageLimitError, RemoteImageResponseError)):
                    fetcher.fetch("https://images.example/a.png")


if __name__ == "__main__":
    unittest.main()
