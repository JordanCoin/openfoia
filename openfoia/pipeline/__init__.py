"""Document processing pipeline.

Stages:
1. Ingest - Accept documents from various sources
2. OCR - Extract text from scanned documents
3. Parse - Structure the extracted text
4. Extract - Pull entities and relationships
5. Link - Connect entities across documents
"""

from .extract import EntityExtractor
from .ingest import DocumentIngester
from .ocr import OCREngine

__all__ = [
    "DocumentIngester",
    "OCREngine",
    "EntityExtractor",
]
