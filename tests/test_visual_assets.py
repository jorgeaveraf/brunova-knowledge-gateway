import hashlib
import io
import sys
from contextlib import contextmanager
from types import SimpleNamespace
from typing import ClassVar
from unittest.mock import Mock

import pytest
from PIL import Image

from app.adapters.google_workspace.errors import WorkspaceAdapterError
from app.adapters.google_workspace.models import BinaryFileSnapshot, WorkspaceResource
from app.artifact_refs import ArtifactReferenceCodec
from app.knowledge import (
    edit_authorized_document_images,
    inspect_authorized_visual_asset,
)
from app.visual_assets import (
    JPEG_MIME_TYPE,
    PNG_MIME_TYPE,
    SVG_MIME_TYPE,
    GoogleDocImageSummary,
    InsertGoogleDocImageOperation,
    ReplaceGoogleDocImageOperation,
    TransientAssetPublisher,
    docs_points_to_render_pixels,
    inspect_visual_bytes,
    render_for_insertion,
    sanitize_svg,
)


def png_bytes(width=20, height=10):
    output = io.BytesIO()
    Image.new("RGB", (width, height), "red").save(output, format="PNG")
    return output.getvalue()


def jpeg_bytes(width=20, height=10):
    output = io.BytesIO()
    Image.new("RGB", (width, height), "blue").save(output, format="JPEG")
    return output.getvalue()


def test_inspect_png_uses_signature_and_dimensions():
    result = inspect_visual_bytes(png_bytes(), PNG_MIME_TYPE)
    assert (result.detected_mime_type, result.width, result.height) == (PNG_MIME_TYPE, 20, 10)


def test_normal_png_below_limit_is_not_reencoded_or_downscaled():
    source = png_bytes(1200, 240)
    rendered = render_for_insertion(source, PNG_MIME_TYPE)
    assert rendered.content == source
    assert rendered.transformation == "none"
    assert (rendered.width_pixels, rendered.height_pixels) == (1200, 240)


def test_hd_png_is_accepted_and_downscaled_with_aspect_ratio_preserved():
    source = png_bytes(8192, 1647)
    inspected = inspect_visual_bytes(source, PNG_MIME_TYPE)
    rendered = render_for_insertion(source, PNG_MIME_TYPE)
    assert (inspected.width, inspected.height) == (8192, 1647)
    assert rendered.transformation == "downscale"
    assert (rendered.width_pixels, rendered.height_pixels) == (4096, 824)
    assert rendered.width_pixels / rendered.height_pixels == pytest.approx(8192 / 1647, rel=0.001)


def test_hd_jpeg_uses_same_governed_downscale_and_rgb_output():
    rendered = render_for_insertion(jpeg_bytes(8192, 1647), JPEG_MIME_TYPE)
    assert rendered.transformation == "downscale"
    assert (rendered.width_pixels, rendered.height_pixels) == (4096, 824)
    with Image.open(io.BytesIO(rendered.content)) as image:
        assert image.mode == "RGB"


def test_unsafe_source_dimension_is_rejected_before_pixel_verification(monkeypatch):
    source = png_bytes(16_385, 1)
    verify = Mock(side_effect=AssertionError("verify must not run"))
    monkeypatch.setattr(Image.Image, "verify", verify)
    with pytest.raises(WorkspaceAdapterError) as error:
        inspect_visual_bytes(source, PNG_MIME_TYPE)
    assert error.value.code == "visual_asset_source_unsafe"
    verify.assert_not_called()


def test_transparent_hd_png_preserves_alpha_after_downscale():
    output = io.BytesIO()
    Image.new("RGBA", (5000, 1000), (255, 0, 0, 0)).save(output, format="PNG")
    rendered = render_for_insertion(output.getvalue(), PNG_MIME_TYPE)
    with Image.open(io.BytesIO(rendered.content)) as image:
        assert image.mode == "RGBA"
        assert image.getpixel((0, 0))[3] == 0


def test_target_aware_docs_display_uses_quality_density_below_hard_maximum():
    source = png_bytes(8192, 1647)
    rendered = render_for_insertion(
        source,
        PNG_MIME_TYPE,
        width_pixels=docs_points_to_render_pixels(300),
    )
    assert docs_points_to_render_pixels(300) == 800
    assert (rendered.width_pixels, rendered.height_pixels) == (800, 161)
    assert rendered.width_pixels < 4096


def test_governed_hd_inspection_reports_supported_downscale_contract():
    source = png_bytes(8192, 1647)
    codec = ArtifactReferenceCodec.for_testing()
    artifact_ref = codec.encode(source_id="workspace_validation", artifact_id="asset_file_12345")
    resource = WorkspaceResource(
        id="asset_file_12345",
        name="BKOS Wordmark HD.png",
        mime_type=PNG_MIME_TYPE,
        modified_time="2026-09-01T00:00:00Z",
        drive_id=None,
        ancestor_ids=("validation_folder_123",),
    )
    registry = Mock()
    registry.get.return_value = SimpleNamespace()
    source_policy = Mock()
    source_policy.authorize_source.return_value = object()
    workspace = Mock()
    workspace.get_resource.return_value = resource
    workspace.download_binary.return_value = BinaryFileSnapshot(
        content=source, version="7", md5_checksum="unchanged", size=len(source)
    )

    result = inspect_authorized_visual_asset(
        registry=registry,
        source_policy=source_policy,
        workspace_adapter=workspace,
        reference_codec=codec,
        source_id="workspace_validation",
        artifact_ref=artifact_ref,
    )

    assert result.supported is True
    assert result.requires_downscale is True
    assert (result.width_pixels, result.height_pixels) == (8192, 1647)
    assert (result.recommended_derived_width_pixels, result.recommended_derived_height_pixels) == (4096, 824)
    assert result.maximum_derived_width_pixels == 4096


def test_hd_insert_and_replace_are_target_aware_cleanup_staging_and_preserve_master(monkeypatch):
    source = png_bytes(8192, 1647)
    original_hash = hashlib.sha256(source).hexdigest()
    codec = ArtifactReferenceCodec.for_testing()
    document_ref = codec.encode(source_id="workspace_validation", artifact_id="document_12345")
    asset_ref = codec.encode_asset(
        source_id="workspace_validation", artifact_id="asset_file_12345", mime_type=PNG_MIME_TYPE
    )
    image_ref = codec.encode_document_image(
        source_id="workspace_validation", artifact_id="document_12345", object_id="image_12345"
    )
    document = WorkspaceResource(
        id="document_12345",
        name="HD validation",
        mime_type="application/vnd.google-apps.document",
        modified_time="2026-09-01T00:00:00Z",
        drive_id=None,
        ancestor_ids=("validation_folder_123",),
    )
    asset = WorkspaceResource(
        id="asset_file_12345",
        name="BKOS Wordmark HD.png",
        mime_type=PNG_MIME_TYPE,
        modified_time="2026-09-01T00:00:00Z",
        drive_id=None,
        ancestor_ids=("validation_folder_123",),
    )
    workspace = Mock()
    workspace.get_resource.side_effect = lambda resource_id: {
        document.id: document,
        asset.id: asset,
    }[resource_id]
    workspace.download_binary.return_value = BinaryFileSnapshot(
        content=source, version="7", md5_checksum="unchanged", size=len(source)
    )
    mutation_policy = Mock()
    mutation_policy.authorize.return_value = object()
    source_policy = Mock()
    docs = Mock()
    docs._settings = SimpleNamespace(
        workspace_asset_staging_bucket="private-staging",
        workspace_asset_staging_prefix="assets/",
        workspace_asset_url_ttl_seconds=300,
    )
    docs.inspect_structure.side_effect = [
        SimpleNamespace(
            revision_id="revision-8",
            image_count=1,
            images=[
                GoogleDocImageSummary(
                    image_ref=image_ref,
                    kind="inline",
                    width_points=300,
                    height_points=60.32,
                )
            ],
        ),
        SimpleNamespace(revision_id="revision-9", image_count=2, images=[]),
    ]
    docs.edit_images.return_value = "revision-9"

    class FakePublisher:
        active = 0
        staged: ClassVar[list] = []

        def __init__(self, **_):
            pass

        @contextmanager
        def signed_uri(self, image):
            type(self).active += 1
            type(self).staged.append(image)
            try:
                yield f"https://signed.example/{len(type(self).staged)}"
            finally:
                type(self).active -= 1

    monkeypatch.setattr("app.knowledge.TransientAssetPublisher", FakePublisher)
    monkeypatch.setattr("app.knowledge.build_keyless_signing_credentials", Mock(return_value=object()))
    result = edit_authorized_document_images(
        mutation_policy=mutation_policy,
        source_policy=source_policy,
        workspace_adapter=workspace,
        docs_adapter=docs,
        reference_codec=codec,
        source_id="workspace_validation",
        artifact_ref=document_ref,
        required_revision_id="revision-8",
        operations=[
            InsertGoogleDocImageOperation(
                operation="insert_image", asset_ref=asset_ref, index=1, width_points=300
            ),
            ReplaceGoogleDocImageOperation(
                operation="replace_image", asset_ref=asset_ref, image_ref=image_ref
            ),
        ],
        approval_reference="approval-hd-validation",
    )

    assert result.verified is True
    assert result.applied_operations == 2
    assert [item.transformation for item in result.asset_transformations] == [
        "downscale",
        "downscale",
    ]
    assert [(item.width_pixels, item.height_pixels) for item in FakePublisher.staged] == [
        (800, 161),
        (800, 161),
    ]
    assert FakePublisher.active == 0
    assert hashlib.sha256(source).hexdigest() == original_hash
    workspace.replace_binary.assert_not_called()


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
