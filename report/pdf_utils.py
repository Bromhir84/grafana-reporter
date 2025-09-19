import io
from PIL import Image
import img2pdf
from .config import A4_WIDTH_PX, A4_HEIGHT_PX, A4_BG_COLOR

def paginate_to_a4(img: Image.Image):
    pages = []
    y_offset = 0
    while y_offset < img.height:
        page = Image.new("RGB", (A4_WIDTH_PX, A4_HEIGHT_PX), A4_BG_COLOR)
        crop = img.crop((0, y_offset, A4_WIDTH_PX, min(y_offset + A4_HEIGHT_PX, img.height)))
        page.paste(crop, (0, 0))
        buf = io.BytesIO()
        page.save(buf, format="JPEG", quality=95)
        pages.append(buf.getvalue())
        y_offset += A4_HEIGHT_PX
    return pages

def generate_pdf_from_pages(pages, output_path):
    with open(output_path, "wb") as f:
        f.write(img2pdf.convert(pages))
