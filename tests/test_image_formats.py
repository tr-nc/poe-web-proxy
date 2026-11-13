import base64
import io

import pytest
from PIL import Image

from poe_web_proxy.images import ImageURL, checked_attachments


@pytest.mark.parametrize(
    "format,mime",
    [("PNG", "image/png"), ("JPEG", "image/jpeg"), ("WEBP", "image/webp"), ("GIF", "image/gif")],
)
def test_supported_images_and_duplicate_asset_references(format, mime):
    output = io.BytesIO()
    Image.new("RGB", (20, 20), "blue").save(output, format=format)
    image = ImageURL(url=f"data:{mime};base64," + base64.b64encode(output.getvalue()).decode())
    attachment = image.attachment()
    assert attachment.mime == mime
    assert checked_attachments([attachment, attachment]) == (attachment,)
    with pytest.raises(ValueError, match="four"):
        checked_attachments([attachment] * 5)


def test_mime_spoofing_rejected():
    output = io.BytesIO()
    Image.new("RGB", (20, 20)).save(output, format="JPEG")
    with pytest.raises(ValueError, match="Invalid inline image"):
        ImageURL(
            url="data:image/png;base64," + base64.b64encode(output.getvalue()).decode()
        ).attachment()
