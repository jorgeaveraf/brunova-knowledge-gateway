"""Governed visual-asset inspection and safe transient rendering."""

from __future__ import annotations

import io
import logging
import re
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timedelta
from typing import Annotated, Literal
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
MAX_SOURCE_BYTES = 10 * 1024 * 1024
MAX_SOURCE_PIXELS = 50_000_000
MAX_SOURCE_DIMENSION = 16_384
MAX_DERIVED_BYTES = 10 * 1024 * 1024
MAX_DERIVED_PIXELS = 25_000_000
MAX_DERIVED_DIMENSION = 4096
DOCS_RASTER_DPI = 192
_SVG_DIMENSION = re.compile(r"^\s*([0-9]+(?:\.[0-9]+)?)")
logger = logging.getLogger(__name__)


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
    supported: Literal[True] = True
    requires_downscale: bool
    maximum_derived_width_pixels: int = MAX_DERIVED_DIMENSION
    maximum_derived_height_pixels: int = MAX_DERIVED_DIMENSION
    maximum_derived_pixels: int = MAX_DERIVED_PIXELS
    recommended_derived_width_pixels: int
    recommended_derived_height_pixels: int
    directly_insertable_in_docs: bool
    rendering_required: bool


class AssetImage(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True)

    content: bytes = Field(repr=False)
    mime_type: Literal["image/png", "image/jpeg"]
    width_pixels: int
    height_pixels: int
    source_mime_type: str | None = None
    source_width_pixels: int | None = None
    source_height_pixels: int | None = None
    transformation: Literal["none", "downscale", "rasterize", "rasterize_targeted"] = "none"


class AssetTransformationSummary(BaseModel):
    asset_ref: str
    original_mime_type: str
    original_width_pixels: int | None = None
    original_height_pixels: int | None = None
    derived_mime_type: str
    derived_width_pixels: int
    derived_height_pixels: int
    transformation: Literal["none", "downscale", "rasterize", "rasterize_targeted"]


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
    replace_method: Literal["CENTER_CROP"] = "CENTER_CROP"


GoogleDocImageOperation = Annotated[
    InsertGoogleDocImageOperation | ReplaceGoogleDocImageOperation,
    Field(discriminator="operation"),
]


class GoogleDocImageEditResult(BaseModel):
    artifact_ref: str
    source_id: str
    revision_id: str
    image_count: int
    applied_operations: int
    asset_transformations: list[AssetTransformationSummary] = Field(default_factory=list)
    verified: bool = True


@dataclass(frozen=True)
class InspectedAsset:
    detected_mime_type: str
    width: int | None
    height: int | None


def inspect_visual_bytes(content: bytes, declared_mime_type: str) -> InspectedAsset:
    if not content or len(content) > MAX_SOURCE_BYTES:
        raise WorkspaceAdapterError(
            "visual_asset_size_invalid",
            f"Visual assets must contain between 1 byte and {MAX_SOURCE_BYTES} bytes.",
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
                _validate_source_pixel_bounds(*image.size)
                image.verify()
            with Image.open(io.BytesIO(content)) as image:
                width, height = image.size
        except WorkspaceAdapterError:
            raise
        except Exception as error:
            raise WorkspaceAdapterError(
                "visual_asset_invalid", "The raster image could not be decoded safely.", 422
            ) from error
    return InspectedAsset(detected, width, height)


def render_for_insertion(
    content: bytes,
    declared_mime_type: str,
    *,
    width_pixels: int | None = None,
    height_pixels: int | None = None,
) -> AssetImage:
    inspected = inspect_visual_bytes(content, declared_mime_type)
    target = derived_dimensions(
        inspected.width,
        inspected.height,
        width_pixels,
        height_pixels,
        allow_upscale=inspected.detected_mime_type == SVG_MIME_TYPE,
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
        _validate_derived_bytes(rendered)
        return AssetImage(
            content=rendered,
            mime_type=PNG_MIME_TYPE,
            width_pixels=target[0],
            height_pixels=target[1],
            source_mime_type=SVG_MIME_TYPE,
            source_width_pixels=inspected.width,
            source_height_pixels=inspected.height,
            transformation=(
                "rasterize_targeted"
                if width_pixels is not None or height_pixels is not None
                else "rasterize"
            ),
        )
    try:
        with Image.open(io.BytesIO(content)) as image:
            image.load()
            if image.size == target:
                return AssetImage(
                    content=content,
                    mime_type=inspected.detected_mime_type,
                    width_pixels=image.width,
                    height_pixels=image.height,
                    source_mime_type=inspected.detected_mime_type,
                    source_width_pixels=inspected.width,
                    source_height_pixels=inspected.height,
                )
            if image.size != target:
                image.thumbnail(target, Image.Resampling.LANCZOS)
            if inspected.detected_mime_type == PNG_MIME_TYPE:
                if image.mode not in {"RGBA", "LA"} and (
                    "transparency" in image.info or image.mode == "P"
                ):
                    image = image.convert("RGBA")
            elif image.mode != "RGB":
                image = image.convert("RGB")
            output = io.BytesIO()
            output_mime = inspected.detected_mime_type
            save_options = {} if output_mime == PNG_MIME_TYPE else {"quality": 92, "optimize": True}
            image.save(
                output,
                format="PNG" if output_mime == PNG_MIME_TYPE else "JPEG",
                **save_options,
            )
            derived = output.getvalue()
            _validate_derived_bytes(derived)
            return AssetImage(
                content=derived,
                mime_type=output_mime,
                width_pixels=image.width,
                height_pixels=image.height,
                source_mime_type=inspected.detected_mime_type,
                source_width_pixels=inspected.width,
                source_height_pixels=inspected.height,
                transformation="downscale",
            )
    except WorkspaceAdapterError:
        raise
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
    forbidden = {"script", "foreignObject", "iframe", "object", "embed"}
    for element in root.iter():
        local_name = element.tag.rsplit("}", 1)[-1]
        if local_name in forbidden:
            raise WorkspaceAdapterError(
                "visual_asset_svg_unsafe", "The SVG contains active content.", 422
            )
        for attribute, value in element.attrib.items():
            name = attribute.rsplit("}", 1)[-1].casefold()
            normalized = value.strip().casefold()
            if name.startswith("on") or _unsafe_css(normalized):
                raise WorkspaceAdapterError(
                    "visual_asset_svg_unsafe", "The SVG contains active or linked content.", 422
                )
            if name == "href" and not normalized.startswith("#"):
                raise WorkspaceAdapterError(
                    "visual_asset_svg_unsafe", "The SVG contains an external reference.", 422
                )
        if local_name == "style" and _unsafe_css(element.text or ""):
            raise WorkspaceAdapterError(
                "visual_asset_svg_unsafe", "The SVG contains active or linked CSS.", 422
            )
    return SafeElementTree.tostring(root, encoding="utf-8", xml_declaration=True)


def _unsafe_css(value: str) -> bool:
    normalized = re.sub(r"\s+", "", value).casefold()
    if any(rule != "media" for rule in re.findall(r"@([a-z-]+)", normalized)):
        return True
    return any(
        marker in normalized
        for marker in (
            "url(",
            "\\",
            "/*",
            "*/",
            "expression(",
            "javascript:",
            "http:",
            "https:",
            "data:",
            "//",
            "behavior:",
            "-moz-binding",
        )
    )


class TransientAssetPublisher:
    """Publish short-lived signed image URIs and always remove staging objects."""

    def __init__(
        self,
        *,
        bucket_name: str,
        prefix: str,
        ttl_seconds: int,
        client=None,
        signing_credentials=None,
    ):
        self._bucket_name = bucket_name
        self._prefix = prefix.strip("/") + "/"
        self._ttl_seconds = ttl_seconds
        self._client = client
        self._signing_credentials = signing_credentials

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
            blob.cache_control = "private, max-age=0, no-store"
            blob.upload_from_string(
                image.content,
                content_type=image.mime_type,
                if_generation_match=0,
            )
            uri = blob.generate_signed_url(
                version="v4",
                expiration=timedelta(seconds=self._ttl_seconds),
                method="GET",
                credentials=self._signing_credentials,
            )
            if len(uri) > 2048:
                raise WorkspaceAdapterError(
                    "visual_asset_uri_invalid", "The transient image URI exceeds the Docs limit.", 502
                )
            yield uri
        finally:
            body_failed = sys.exc_info()[0] is not None
            try:
                blob.delete(if_generation_match=blob.generation)
            except Exception as error:
                logger.error(
                    "visual_asset_staging_cleanup_failed",
                    extra={"bucket": self._bucket_name},
                    exc_info=error,
                )
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
            width = width or round(float(view_box[2]))
            height = height or round(float(view_box[3]))
        except ValueError:
            pass
    if width and height:
        _validate_source_pixel_bounds(width, height)
    return width, height


def _numeric_dimension(value: str | None) -> int | None:
    if not value:
        return None
    match = _SVG_DIMENSION.match(value)
    return max(1, round(float(match.group(1)))) if match else None


def derived_dimensions(
    source_width: int | None,
    source_height: int | None,
    requested_width: int | None,
    requested_height: int | None,
    *,
    allow_upscale: bool = False,
) -> tuple[int, int]:
    for value in (requested_width, requested_height):
        if value is not None and not 1 <= value <= MAX_DERIVED_DIMENSION:
            raise WorkspaceAdapterError(
                "visual_asset_dimensions_invalid",
                f"Rendered dimensions must be between 1 and {MAX_DERIVED_DIMENSION} pixels.",
                422,
            )
    width = source_width or 1024
    height = source_height or 1024
    requested_scale = float("inf")
    if requested_width:
        requested_scale = min(requested_scale, requested_width / width)
    if requested_height:
        requested_scale = min(requested_scale, requested_height / height)
    if requested_scale != float("inf"):
        scale = requested_scale if allow_upscale else min(1.0, requested_scale)
        width, height = max(1, round(width * scale)), max(1, round(height * scale))
    pixel_scale = (MAX_DERIVED_PIXELS / (width * height)) ** 0.5
    scale = min(
        1.0,
        MAX_DERIVED_DIMENSION / width,
        MAX_DERIVED_DIMENSION / height,
        pixel_scale,
    )
    result = max(1, round(width * scale)), max(1, round(height * scale))
    _validate_derived_pixel_bounds(*result)
    return result


def docs_points_to_render_pixels(points: float | None) -> int | None:
    return max(1, round(points * DOCS_RASTER_DPI / 72)) if points is not None else None


def _validate_source_pixel_bounds(width: int, height: int) -> None:
    if width < 1 or height < 1 or width > MAX_SOURCE_DIMENSION or height > MAX_SOURCE_DIMENSION:
        raise WorkspaceAdapterError(
            "visual_asset_source_unsafe",
            "The visual asset source dimensions exceed safe processing limits.",
            422,
        )
    if width * height > MAX_SOURCE_PIXELS:
        raise WorkspaceAdapterError(
            "visual_asset_source_unsafe",
            "The visual asset source exceeds the safe processing pixel limit.",
            422,
        )


def _validate_derived_pixel_bounds(width: int, height: int) -> None:
    if width < 1 or height < 1 or width > MAX_DERIVED_DIMENSION or height > MAX_DERIVED_DIMENSION:
        raise WorkspaceAdapterError(
            "visual_asset_dimensions_invalid",
            "The derived visual asset dimensions exceed safe insertion limits.",
            422,
        )
    if width * height > MAX_DERIVED_PIXELS:
        raise WorkspaceAdapterError(
            "visual_asset_dimensions_invalid",
            "The derived visual asset exceeds the insertion pixel limit.",
            422,
        )


def _validate_derived_bytes(content: bytes) -> None:
    if not content or len(content) > MAX_DERIVED_BYTES:
        raise WorkspaceAdapterError(
            "visual_asset_derived_size_invalid",
            f"The derived visual asset must contain between 1 byte and {MAX_DERIVED_BYTES} bytes.",
            422,
        )
