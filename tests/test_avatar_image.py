"""Profile pictures: what an upload becomes (PNG of at most 256 px, no metadata) and what is refused."""

from __future__ import annotations

import io
import os
import unittest

from PIL import Image

from src.utils.avatars import (
    MAX_INPUT_SIDE, MAX_UPLOAD_BYTES, OUTPUT_SIDE, ImageTooLarge, InvalidImage, UnsupportedImageType,
    check_content_type, normalise_image,
)


def encode(image: Image.Image, fmt: str, **options) -> bytes:
    out = io.BytesIO()
    image.save(out, format=fmt, **options)
    return out.getvalue()


def decode(png: bytes) -> Image.Image:
    image = Image.open(io.BytesIO(png))
    image.load()
    return image


class NormaliseImageTests(unittest.TestCase):
    def test_png_with_transparency_keeps_its_alpha(self) -> None:
        source = Image.new("RGBA", (64, 64), (255, 0, 0, 0))
        source.putpixel((10, 10), (0, 255, 0, 255))
        png, has_alpha = normalise_image(encode(source, "PNG"))
        image = decode(png)
        self.assertTrue(has_alpha)
        self.assertEqual((image.format, image.mode, image.size), ("PNG", "RGBA", (64, 64)))
        self.assertEqual(image.getpixel((10, 10)), (0, 255, 0, 255))
        self.assertEqual(image.getpixel((0, 0))[3], 0)

    def test_opaque_png_is_stored_without_an_alpha_channel(self) -> None:
        png, has_alpha = normalise_image(encode(Image.new("RGBA", (20, 20), (1, 2, 3, 255)), "PNG"))
        self.assertFalse(has_alpha)
        self.assertEqual(decode(png).mode, "RGB")

    def test_jpeg_becomes_png(self) -> None:
        png, has_alpha = normalise_image(encode(Image.new("RGB", (40, 30), (200, 10, 10)), "JPEG"))
        image = decode(png)
        self.assertFalse(has_alpha)
        self.assertEqual((image.format, image.size), ("PNG", (40, 30)))

    def test_webp_becomes_png_and_keeps_alpha(self) -> None:
        source = Image.new("RGBA", (32, 32), (0, 0, 255, 0))
        png, has_alpha = normalise_image(encode(source, "WEBP", lossless=True))
        self.assertTrue(has_alpha)
        self.assertEqual(decode(png).format, "PNG")

    def test_exif_orientation_is_applied(self) -> None:
        source = Image.new("RGB", (60, 20), (255, 255, 255))
        exif = Image.Exif()
        exif[0x0112] = 6  # rotated 90 degrees: the stored 60x20 shows as 20x60
        png, _ = normalise_image(encode(source, "JPEG", exif=exif))
        self.assertEqual(decode(png).size, (20, 60))

    def test_the_longest_side_is_cut_to_256_keeping_the_aspect_ratio(self) -> None:
        png, _ = normalise_image(encode(Image.new("RGB", (1000, 500), (9, 9, 9)), "PNG"))
        self.assertEqual(decode(png).size, (OUTPUT_SIDE, OUTPUT_SIDE // 2))
        png, _ = normalise_image(encode(Image.new("RGB", (300, 900), (9, 9, 9)), "JPEG"))
        self.assertEqual(decode(png).size[1], OUTPUT_SIDE)

    def test_a_small_image_is_not_enlarged(self) -> None:
        png, _ = normalise_image(encode(Image.new("RGB", (16, 16)), "PNG"))
        self.assertEqual(decode(png).size, (16, 16))

    def test_metadata_is_stripped(self) -> None:
        source = Image.new("RGB", (30, 30), (5, 5, 5))
        exif = Image.Exif()
        exif[0x010E] = "secret description"
        exif[0x013B] = "secret author"
        png, _ = normalise_image(encode(source, "JPEG", exif=exif))
        self.assertNotIn(b"secret", png)
        image = decode(png)
        self.assertEqual(len(image.getexif()), 0)
        self.assertEqual({key for key in image.info if key not in ("dpi",)}, set())

    def test_png_text_chunks_are_stripped(self) -> None:
        from PIL.PngImagePlugin import PngInfo
        info = PngInfo()
        info.add_text("Comment", "secret note")
        png, _ = normalise_image(encode(Image.new("RGB", (8, 8)), "PNG", pnginfo=info))
        self.assertNotIn(b"secret", png)

    def test_an_animated_webp_keeps_only_its_first_frame(self) -> None:
        frames = [Image.new("RGB", (24, 24), colour) for colour in ((255, 0, 0), (0, 0, 255))]
        data = encode(frames[0], "WEBP", save_all=True, append_images=frames[1:], lossless=True)
        png, _ = normalise_image(data)
        image = decode(png)
        self.assertEqual(getattr(image, "n_frames", 1), 1)
        self.assertGreater(image.convert("RGB").getpixel((5, 5))[0], 200)


class RefusedUploadTests(unittest.TestCase):
    def assert_invalid(self, data: bytes) -> None:
        with self.assertRaises(InvalidImage):
            normalise_image(data)

    def test_more_than_4096_pixels_on_a_side_is_refused_before_decoding(self) -> None:
        wide = encode(Image.new("1", (MAX_INPUT_SIDE + 1, 8)), "PNG")  # tiny in bytes, huge in pixels
        self.assertLess(len(wide), MAX_UPLOAD_BYTES)
        self.assert_invalid(wide)
        tall = encode(Image.new("1", (8, MAX_INPUT_SIDE + 1)), "PNG")
        self.assert_invalid(tall)

    def test_the_largest_allowed_size_is_accepted(self) -> None:
        png, _ = normalise_image(encode(Image.new("1", (MAX_INPUT_SIDE, 16)), "PNG"))
        self.assertEqual(decode(png).size[0], OUTPUT_SIDE)

    def test_svg_is_refused(self) -> None:
        self.assert_invalid(b'<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"><rect width="10" height="10"/></svg>')

    def test_gif_is_refused(self) -> None:
        self.assert_invalid(encode(Image.new("P", (10, 10)), "GIF"))

    def test_garbage_and_empty_uploads_are_refused(self) -> None:
        self.assert_invalid(b"")
        self.assert_invalid(b"not an image at all")
        self.assert_invalid(os.urandom(500))

    def test_a_truncated_png_is_refused(self) -> None:
        data = encode(Image.effect_noise((200, 200), 80).convert("RGB"), "PNG")
        self.assert_invalid(data[: len(data) // 2])

    def test_over_2_mib_is_too_large(self) -> None:
        with self.assertRaises(ImageTooLarge) as caught:
            normalise_image(b"\0" * (MAX_UPLOAD_BYTES + 1))
        self.assertEqual(caught.exception.code, "image_too_large")

    def test_exactly_2_mib_is_not_refused_for_its_size(self) -> None:
        with self.assertRaises(InvalidImage):  # garbage, but not "too large"
            normalise_image(b"\0" * MAX_UPLOAD_BYTES)

    def test_error_codes(self) -> None:
        with self.assertRaises(InvalidImage) as caught:
            normalise_image(b"nope")
        self.assertEqual(caught.exception.code, "invalid_image")


class ContentTypeTests(unittest.TestCase):
    def test_only_png_jpeg_and_webp_are_declared(self) -> None:
        for good in ("image/png", "image/jpeg", "image/webp", "IMAGE/PNG", "image/png; charset=binary"):
            check_content_type(good)
        for bad in (None, "", "image/gif", "image/svg+xml", "text/plain", "application/json", "multipart/form-data"):
            with self.subTest(declared=bad), self.assertRaises(UnsupportedImageType) as caught:
                check_content_type(bad)
            self.assertEqual(caught.exception.code, "unsupported_image_type")


if __name__ == "__main__":
    unittest.main()
