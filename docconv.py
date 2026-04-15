import base64
import os

from dotenv import load_dotenv

try:
    from mistralai import Mistral
except ImportError:
    from mistralai.client import Mistral


load_dotenv()


def _build_client() -> Mistral:
    api_key = os.getenv("MISTRAL_API_KEY")
    if not api_key:
        raise RuntimeError("MISTRAL_API_KEY is not set in environment variables.")
    return Mistral(api_key=api_key)


def extract_markdown_from_pdf(pdf_path: str, include_image_base64: bool = False) -> list[str]:
    with open(pdf_path, "rb") as file:
        pdf_bytes = file.read()

    b64 = base64.b64encode(pdf_bytes).decode()
    client = _build_client()
    response = client.ocr.process(
        model="mistral-ocr-latest",
        document={
            "type": "document_url",
            "document_url": f"data:application/pdf;base64,{b64}",
        },
        include_image_base64=include_image_base64,
    )
    return [page.markdown for page in response.pages]


def main() -> None:
    pdf_path = os.getenv("OCR_SOURCE_PDF", "document.pdf")
    pages = extract_markdown_from_pdf(pdf_path)
    for index, page_markdown in enumerate(pages, start=1):
        print(f"\n{'=' * 20} PAGE {index} {'=' * 20}\n")
        print(page_markdown)


if __name__ == "__main__":
    main()
