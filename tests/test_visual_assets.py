import io
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PIL import Image

from app.adapters.google_workspace.errors import WorkspaceAdapterError
from app.visual_assets import (
    PNG_MIME_TYPE,
    SVG_MIME_TYPE,
    TransientAssetPublisher,
    inspect_visual_bytes,
    render_for_insertion,
    sanitize_svg,
)


def png_bytes(width=20, height=10):
    output = io.BytesIO()
    Image.new("RGB", (width, height), "red").save(output, format="PNG")
    return output.getvalue()


def test_inspect_png_uses_signature_and_dimensions():
    result = inspect_visual_bytes(png_bytes(), PNG_MIME_TYPE)
    assert (result.detected_mime_type, result.width, result.height) == (PNG_MIME_TYPE, 20, 10)


def test_declared_mime_mismatch_is_rejected():
    with pytest.raises(WorkspaceAdapterError, match="declared asset MIME"):
        inspect_visual_bytes(png_bytes(), "image/jpeg")


def test_svg_is_sanitized_and_rasterized_preserving_ratio(monkeypatch):
    svg = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 200 100"><rect width="200" height="100"/></svg>'
    monkeypatch.setitem(
        sys.modules,
        "resvg_py",
        SimpleNamespace(svg_to_bytes=lambda **_: png_bytes(100, 50)),
    )
    inspected = inspect_visual_bytes(svg, SVG_MIME_TYPE)
    rendered = render_for_insertion(svg, SVG_MIME_TYPE, width_pixels=100)
    assert (inspected.width, inspected.height) == (200, 100)
    assert rendered.mime_type == PNG_MIME_TYPE
    assert (rendered.width_pixels, rendered.height_pixels) == (100, 50)
    fitted = render_for_insertion(
        svg, SVG_MIME_TYPE, width_pixels=80, height_pixels=80
    )
    assert (fitted.width_pixels, fitted.height_pixels) == (80, 40)


def test_svg_active_and_external_content_is_rejected():
    for svg in (
        b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>',
        b'<svg xmlns="http://www.w3.org/2000/svg"><image href="https://example.com/a.png"/></svg>',
        b'<svg xmlns="http://www.w3.org/2000/svg"><style>@import url(https://example.com/x)</style></svg>',
    ):
        with pytest.raises(WorkspaceAdapterError):
            sanitize_svg(svg)


def test_svg_static_inline_css_is_allowed_but_active_css_is_rejected():
    safe = b'''<svg xmlns="http://www.w3.org/2000/svg"><style>.mark { fill: #123456; } @media (prefers-color-scheme: dark) { .mark { fill: #fff; } }</style><path class="mark" d="M0 0h1v1z"/></svg>'''
    assert b"style" in sanitize_svg(safe)

    for css in (
        "@import 'https://example.com/x.css'",
        "fill: expression(alert(1))",
        "background: javascript:alert(1)",
        r"background: u\72l(https://example.com/x.png)",
    ):
        svg = f'<svg xmlns="http://www.w3.org/2000/svg"><style>{css}</style></svg>'.encode()
        with pytest.raises(WorkspaceAdapterError):
            sanitize_svg(svg)


def test_transient_publisher_deletes_staged_object_after_use():
    blob = Mock()
    blob.generate_signed_url.return_value = "https://signed.example/asset"
    bucket = Mock()
    bucket.blob.return_value = blob
    client = Mock()
    client.bucket.return_value = bucket
    image = render_for_insertion(png_bytes(), PNG_MIME_TYPE)
    signing_credentials = Mock()
    publisher = TransientAssetPublisher(
        bucket_name="private-staging",
        prefix="assets/",
        ttl_seconds=300,
        client=client,
        signing_credentials=signing_credentials,
    )
    with publisher.signed_uri(image) as uri:
        assert uri == "https://signed.example/asset"
        blob.delete.assert_not_called()
    blob.upload_from_string.assert_called_once_with(
        image.content, content_type="image/png", if_generation_match=0
    )
    assert blob.cache_control == "private, max-age=0, no-store"
    assert blob.generate_signed_url.call_args.kwargs["credentials"] is signing_credentials
    blob.delete.assert_called_once_with(if_generation_match=blob.generation)


def test_transient_publisher_surfaces_cleanup_failure_after_success():
    blob = Mock()
    blob.generate_signed_url.return_value = "https://signed.example/asset"
    blob.delete.side_effect = RuntimeError("cleanup failed")
    bucket = Mock()
    bucket.blob.return_value = blob
    client = Mock()
    client.bucket.return_value = bucket
    publisher = TransientAssetPublisher(
        bucket_name="private-staging", prefix="assets/", ttl_seconds=300, client=client
    )

    with pytest.raises(WorkspaceAdapterError) as error, publisher.signed_uri(
        render_for_insertion(png_bytes(), PNG_MIME_TYPE)
    ):
        pass

    assert error.value.code == "visual_asset_cleanup_failed"
