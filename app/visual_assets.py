"""Governed visual-asset inspection and safe transient rendering."""

from __future__ import annotations

import io
import re
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timedelta
from typing import Annotated, Literal, Union
from uuid import uuid4

from defusedxml import ElementTree as SafeElementTree
from google.cloud import storage
from PIL import Image
from pydantic import BaseModel, ConfigDict, Field

from app.adapters.google_workspace.errors import WorkspaceAdapterError

SVG_MIME_TYPE = "image/svg+xml"
PNG_MIME_TYPE = "image/png"
JPEG_MIME_TYPE = "image/jpeg"
SUPPORTED_VISUAL_MIME_TYPES = frozenset({SVG_MIME_TYPE, PNG_MIME_TYPE, JPEG_MIME_TYPE})
MAX_ASSET_BYTES = 10 * 1024 * 1024
MAX_IMAGE_PIXELS = 25_000_000
MAX_RENDER_DIMENSION = 4096
_SVG_DIMENSION = re.compile(r"^\s*([0-9]+(?:\.[0-9]+)?)")


class VisualAssetInspection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    asset_ref: str
    name: str
    mime_type: Literal["image/svg+xml", "image/png", "image/jpeg"]
    width_pixels: int | None = None
    height_pixels: int | None = None
    aspect_ratio: float | None = None
    file_size: int = Field(ge=1)
    source_id: str
    directly_insertable_in_docs: bool
    rendering_required: bool


class AssetImage(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True)

    content: bytes = Field(repr=False)
    mime_type: Literal["image/png", "image/jpeg"]
    width_pixels: int
    height_pixels: int


class GoogleDocImageSummary(BaseModel):
    image_ref: str
    kind: Literal["inline", "positioned"]
    tab_ref: str | None = None
    width_points: float | None = None
    height_points: float | None = None
    positioned_layout: str | None = None


class InsertGoogleDocImageOperation(BaseModel):
    operation: Literal["insert_image"]
    asset_ref: str
    index: int = Field(ge=1)
    tab_ref: str | None = None
    segment_id: str = ""
    width_points: float | None = Field(default=None, ge=1, le=2000)
    height_points: float | None = Field(default=None, ge=1, le=2000)


class ReplaceGoogleDocImageOperation(BaseModel):
    operation: Literal["replace_image"]
    asset_ref: str
    image_ref: str
    replace_method: Literal["CENTER_CROP", "CENTER_INSIDE"] = "CENTER_INSIDE"


GoogleDocImageOperation = Annotated[
    Union[InsertGoogleDocImageOperation, ReplaceGoogleDocImageOperation],
    Field(discriminator="operation"),
]


class GoogleDocImageEditResult(BaseModel):
    artifact_ref: str
    source_id: str
    revision_id: str
    image_count: int
    applied_operations: int
    verified: bool = True


@dataclass(frozen=True)
class InspectedAsset:
    detected_mime_type: str
    width: int | None
    height: int | None


def inspect_visual_bytes(content: bytes, declared_mime_type: str) -> InspectedAsset:
    if not content or len(content) > MAX_ASSET_BYTES:
        raise WorkspaceAdapterError(
            "visual_asset_size_invalid",
            f"Visual assets must contain between 1 byte and {MAX_ASSET_BYTES} bytes.",
            422,
        )
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        detected = PNG_MIME_TYPE
    elif content.startswith(b"\xff\xd8\xff"):
        detected = JPEG_MIME_TYPE
    elif _looks_like_svg(content):
        detected = SVG_MIME_TYPE
    else:
        raise WorkspaceAdapterError(
            "visual_asset_mime_invalid", "The asset is not a supported PNG, JPEG, or SVG.", 422
        )
    if declared_mime_type.casefold() != detected:
        raise WorkspaceAdapterError(
            "visual_asset_mime_mismatch",
            "The declared asset MIME type does not match its content.",
            422,
        )
    if detected == SVG_MIME_TYPE:
        width, height = _safe_svg_dimensions(content)
    else:
        try:
            with Image.open(io.BytesIO(content)) as image:
                image.verify()
            with Image.open(io.BytesIO(content)) as image:
                width, height = image.size
        except Exception as error:
            raise WorkspaceAdapterError(
                "visual_asset_invalid", "The raster image could not be decoded safely.", 422
            ) from error
        _validate_pixel_bounds(width, height)
    return InspectedAsset(detected, width, height)


def render_for_insertion(
    content: bytes,
    declared_mime_type: str,
    *,
    width_pixels: int | None = None,
    height_pixels: int | None = None,
) -> AssetImage:
    inspected = inspect_visual_bytes(content, declared_mime_type)
    target = _target_dimensions(
        inspected.width, inspected.height, width_pixels, height_pixels
    )
    if inspected.detected_mime_type == SVG_MIME_TYPE:
        try:
            sanitized = sanitize_svg(content)
            try:
                import resvg_py

                rendered = resvg_py.svg_to_bytes(
                    svg_string=sanitized.decode("utf-8"),
                    width=target[0],
                    height=target[1],
                    skip_system_fonts=True,
                    resources_dir=None,
                )
            except ImportError:
                import cairosvg

                rendered = cairosvg.svg2png(
                    bytestring=sanitized,
                    output_width=target[0],
                    output_height=target[1],
                    unsafe=False,
                )
        except WorkspaceAdapterError:
            raise
        except Exception as error:
            raise WorkspaceAdapterError(
                "visual_asset_render_failed", "The SVG could not be rendered safely.", 422
            ) from error
        return AssetImage(
            content=rendered,
            mime_type=PNG_MIME_TYPE,
            width_pixels=target[0],
            height_pixels=target[1],
        )
    try:
        with Image.open(io.BytesIO(content)) as image:
            image.load()
            if image.size != target:
                image.thumbnail(target, Image.Resampling.LANCZOS)
            output = io.BytesIO()
            output_mime = inspected.detected_mime_type
            image.save(output, format="PNG" if output_mime == PNG_MIME_TYPE else "JPEG")
            return AssetImage(
                content=output.getvalue(),
                mime_type=output_mime,
                width_pixels=image.width,
                height_pixels=image.height,
            )
    except Exception as error:
        raise WorkspaceAdapterError(
            "visual_asset_render_failed", "The image could not be prepared safely.", 422
        ) from error


def sanitize_svg(content: bytes) -> bytes:
    """Reject active/external content and return a bounded inert SVG."""

    try:
        root = SafeElementTree.fromstring(content)
    except Exception as error:
        raise WorkspaceAdapterError(
            "visual_asset_svg_invalid", "The SVG is malformed or unsafe.", 422
        ) from error
    if not root.tag.endswith("svg"):
        raise WorkspaceAdapterError(
            "visual_asset_svg_invalid", "The asset root element is not SVG.", 422
        )
    forbidden = {"script", "style", "foreignObject", "iframe", "object", "embed"}
    for element in root.iter():
        local_name = element.tag.rsplit("}", 1)[-1]
        if local_name in forbidden:
            raise WorkspaceAdapterError(
                "visual_asset_svg_unsafe", "The SVG contains active content.", 422
            )
        for attribute, value in element.attrib.items():
            name = attribute.rsplit("}", 1)[-1].casefold()
            normalized = value.strip().casefold()
            if name.startswith("on") or "url(" in normalized:
                raise WorkspaceAdapterError(
                    "visual_asset_svg_unsafe", "The SVG contains active or linked content.", 422
                )
            if name == "href" and not normalized.startswith(("#", "data:image/")):
                raise WorkspaceAdapterError(
                    "visual_asset_svg_unsafe", "The SVG contains an external reference.", 422
                )
    return SafeElementTree.tostring(root, encoding="utf-8", xml_declaration=True)


class TransientAssetPublisher:
    """Publish short-lived signed image URIs and always remove staging objects."""

    def __init__(self, *, bucket_name: str, prefix: str, ttl_seconds: int, client=None):
        self._bucket_name = bucket_name
        self._prefix = prefix.strip("/") + "/"
        self._ttl_seconds = ttl_seconds
        self._client = client

    @contextmanager
    def signed_uri(self, image: AssetImage):
        if not self._bucket_name:
            raise WorkspaceAdapterError(
                "visual_asset_staging_unavailable",
                "Transient image staging is not configured for Docs insertion.",
                503,
            )
        client = self._client or storage.Client()
        extension = "png" if image.mime_type == PNG_MIME_TYPE else "jpg"
        blob = client.bucket(self._bucket_name).blob(
            f"{self._prefix}{uuid4().hex}.{extension}"
        )
        try:
            blob.upload_from_string(image.content, content_type=image.mime_type)
            uri = blob.generate_signed_url(
                version="v4",
                expiration=timedelta(seconds=self._ttl_seconds),
                method="GET",
            )
            if len(uri) > 2048:
                raise WorkspaceAdapterError(
                    "visual_asset_uri_invalid", "The transient image URI exceeds the Docs limit.", 502
                )
            yield uri
        finally:
            body_failed = sys.exc_info()[0] is not None
            try:
                blob.delete()
            except Exception as error:
                if not body_failed:
                    raise WorkspaceAdapterError(
                        "visual_asset_cleanup_failed",
                        "The transient image was used but its staging object could not be removed.",
                        502,
                    ) from error


def _looks_like_svg(content: bytes) -> bool:
    prefix = content[:4096].lstrip()
    return prefix.startswith(b"<svg") or (prefix.startswith(b"<?xml") and b"<svg" in prefix)


def _safe_svg_dimensions(content: bytes) -> tuple[int | None, int | None]:
    sanitized = sanitize_svg(content)
    root = SafeElementTree.fromstring(sanitized)
    width = _numeric_dimension(root.attrib.get("width"))
    height = _numeric_dimension(root.attrib.get("height"))
    view_box = root.attrib.get("viewBox", "").replace(",", " ").split()
    if (width is None or height is None) and len(view_box) == 4:
        try:
            width = width or int(round(float(view_box[2])))
            height = height or int(round(float(view_box[3])))
        except ValueError:
            pass
    if width and height:
        _validate_pixel_bounds(width, height)
    return width, height


def _numeric_dimension(value: str | None) -> int | None:
    if not value:
        return None
    match = _SVG_DIMENSION.match(value)
    return max(1, int(round(float(match.group(1))))) if match else None


def _target_dimensions(
    source_width: int | None,
    source_height: int | None,
    requested_width: int | None,
    requested_height: int | None,
) -> tuple[int, int]:
    for value in (requested_width, requested_height):
        if value is not None and not 1 <= value <= MAX_RENDER_DIMENSION:
            raise WorkspaceAdapterError(
                "visual_asset_dimensions_invalid",
                f"Rendered dimensions must be between 1 and {MAX_RENDER_DIMENSION} pixels.",
                422,
            )
    width = source_width or 1024
    height = source_height or 1024
    if requested_width and requested_height:
        scale = min(requested_width / width, requested_height / height)
        width, height = max(1, round(width * scale)), max(1, round(height * scale))
    elif requested_width:
        height = max(1, round(height * requested_width / width))
        width = requested_width
    elif requested_height:
        width = max(1, round(width * requested_height / height))
        height = requested_height
    scale = min(1.0, MAX_RENDER_DIMENSION / width, MAX_RENDER_DIMENSION / height)
    result = max(1, round(width * scale)), max(1, round(height * scale))
    _validate_pixel_bounds(*result)
    return result


def _validate_pixel_bounds(width: int, height: int) -> None:
    if width < 1 or height < 1 or width > MAX_RENDER_DIMENSION or height > MAX_RENDER_DIMENSION:
        raise WorkspaceAdapterError(
            "visual_asset_dimensions_invalid", "The visual asset dimensions exceed safe limits.", 422
        )
    if width * height > MAX_IMAGE_PIXELS:
        raise WorkspaceAdapterError(
            "visual_asset_dimensions_invalid", "The visual asset exceeds the pixel limit.", 422
        )
