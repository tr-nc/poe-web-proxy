"""Decode only inline images: never fetch user URLs or read user-supplied file paths."""

import base64
import binascii
import hashlib
import io
import warnings
from dataclasses import dataclass

from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel, ConfigDict, Field

MAX_IMAGE = 10 * 1024 * 1024
MAX_TOTAL = 20 * 1024 * 1024
FORMATS = {"image/png": "PNG", "image/jpeg": "JPEG", "image/webp": "WEBP", "image/gif": "GIF"}
EXTENSIONS = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp", "image/gif": "gif"}


@dataclass(frozen=True)
class Attachment:
    data: bytes
    mime: str

    @property
    def name(self):
        return hashlib.sha256(self.data).hexdigest() + "." + EXTENSIONS[self.mime]


class ImageURL(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str = Field(max_length=MAX_IMAGE * 4 // 3 + 100)
    detail: str | None = None

    def attachment(self) -> Attachment:
        if self.detail not in (None, "auto"):
            raise ValueError("Only automatic image detail is supported")
        try:
            metadata, encoded = self.url.split(",", 1)
            mime = metadata.removeprefix("data:").removesuffix(";base64")
            if mime not in FORMATS or metadata != f"data:{mime};base64":
                raise ValueError
            data = base64.b64decode(encoded, validate=True)
            if not data or len(data) > MAX_IMAGE:
                raise ValueError
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(io.BytesIO(data)) as image:
                    if image.format != FORMATS[mime] or image.width * image.height > 25000000:
                        raise ValueError
                    image.verify()
            return Attachment(data, mime)
        except (
            ValueError,
            binascii.Error,
            OSError,
            UnidentifiedImageError,
            Image.DecompressionBombWarning,
            Image.DecompressionBombError,
        ):
            raise ValueError(
                "Invalid inline image; use PNG/JPEG/WebP/GIF base64 data URLs up to 10 MiB"
            ) from None


def checked_attachments(images: list[Attachment]) -> tuple[Attachment, ...]:
    if len(images) > 4 or sum(len(image.data) for image in images) > MAX_TOTAL:
        raise ValueError("At most four new images and 20 MiB total per turn")
    # Repeated references to the same asset need only one browser upload.
    return tuple({image.name: image for image in images}.values())
