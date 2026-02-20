"""OCR engine for processing scanned documents."""

from __future__ import annotations

import asyncio
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class OCRResult:
    """Result of OCR processing."""

    text: str
    confidence: float
    page_count: int
    pages: list[dict[str, Any]]  # Per-page results
    metadata: dict[str, Any]


class OCREngine:
    """OCR engine supporting multiple backends.
    
    Backends:
    - Tesseract (local, free, good quality)
    - Google Cloud Vision (cloud, paid, excellent quality)
    - AWS Textract (cloud, paid, excellent for forms)
    """

    def __init__(
        self,
        backend: str = "tesseract",
        tesseract_cmd: str | None = None,
        google_credentials: str | None = None,
        aws_credentials: dict[str, str] | None = None,
    ):
        self.backend = backend
        self.tesseract_cmd = tesseract_cmd
        self.google_credentials = google_credentials
        self.aws_credentials = aws_credentials

    async def process_pdf(self, pdf_path: Path | str) -> OCRResult:
        """Process a PDF file through OCR."""
        pdf_path = Path(pdf_path)
        
        if self.backend == "tesseract":
            return await self._process_tesseract(pdf_path)
        elif self.backend == "google":
            return await self._process_google_vision(pdf_path)
        elif self.backend == "aws":
            return await self._process_aws_textract(pdf_path)
        else:
            raise ValueError(f"Unknown OCR backend: {self.backend}")

    async def _process_tesseract(self, pdf_path: Path) -> OCRResult:
        """Process using Tesseract OCR."""
        import pytesseract
        from pdf2image import convert_from_path
        
        if self.tesseract_cmd:
            pytesseract.pytesseract.tesseract_cmd = self.tesseract_cmd
        
        # Convert PDF to images
        def _convert():
            return convert_from_path(str(pdf_path), dpi=300)
        
        images = await asyncio.to_thread(_convert)
        
        pages = []
        all_text = []
        total_confidence = 0.0
        
        for i, image in enumerate(images):
            # Get detailed OCR data
            def _ocr():
                data = pytesseract.image_to_data(image, output_type=pytesseract.Output.DICT)
                text = pytesseract.image_to_string(image)
                return data, text
            
            data, text = await asyncio.to_thread(_ocr)
            
            # Calculate confidence for this page
            confidences = [int(c) for c in data['conf'] if c != '-1']
            page_confidence = sum(confidences) / len(confidences) if confidences else 0.0
            
            pages.append({
                "page_number": i + 1,
                "text": text,
                "confidence": page_confidence / 100.0,
                "word_count": len([w for w in data['text'] if w.strip()]),
            })
            
            all_text.append(text)
            total_confidence += page_confidence
        
        avg_confidence = (total_confidence / len(pages) / 100.0) if pages else 0.0
        
        return OCRResult(
            text="\n\n".join(all_text),
            confidence=avg_confidence,
            page_count=len(pages),
            pages=pages,
            metadata={
                "backend": "tesseract",
                "source_file": str(pdf_path),
            },
        )

    async def _process_google_vision(self, pdf_path: Path) -> OCRResult:
        """Process using Google Cloud Vision API."""
        from google.cloud import vision
        
        # Initialize client
        client = vision.ImageAnnotatorClient()
        
        # Read PDF
        with open(pdf_path, 'rb') as f:
            content = f.read()
        
        # Configure request
        input_config = vision.InputConfig(
            content=content,
            mime_type='application/pdf'
        )
        feature = vision.Feature(type_=vision.Feature.Type.DOCUMENT_TEXT_DETECTION)
        request = vision.AnnotateFileRequest(
            input_config=input_config,
            features=[feature]
        )
        
        # Process
        response = await asyncio.to_thread(
            client.batch_annotate_files,
            requests=[request]
        )
        
        pages = []
        all_text = []
        
        for page_response in response.responses[0].responses:
            text = page_response.full_text_annotation.text
            confidence = sum(
                p.confidence for p in page_response.full_text_annotation.pages
            ) / len(page_response.full_text_annotation.pages) if page_response.full_text_annotation.pages else 0.0
            
            pages.append({
                "page_number": len(pages) + 1,
                "text": text,
                "confidence": confidence,
            })
            all_text.append(text)
        
        avg_confidence = sum(p['confidence'] for p in pages) / len(pages) if pages else 0.0
        
        return OCRResult(
            text="\n\n".join(all_text),
            confidence=avg_confidence,
            page_count=len(pages),
            pages=pages,
            metadata={
                "backend": "google_vision",
                "source_file": str(pdf_path),
            },
        )

    async def _process_aws_textract(self, pdf_path: Path) -> OCRResult:
        """Process using AWS Textract."""
        import boto3
        
        textract = boto3.client(
            'textract',
            **self.aws_credentials if self.aws_credentials else {}
        )
        
        with open(pdf_path, 'rb') as f:
            content = f.read()
        
        # Start async job for multi-page PDFs
        response = await asyncio.to_thread(
            textract.start_document_text_detection,
            Document={'Bytes': content}
        )
        
        job_id = response['JobId']
        
        # Poll for completion
        while True:
            status = await asyncio.to_thread(
                textract.get_document_text_detection,
                JobId=job_id
            )
            
            if status['JobStatus'] == 'SUCCEEDED':
                break
            elif status['JobStatus'] == 'FAILED':
                raise RuntimeError(f"Textract job failed: {status.get('StatusMessage')}")
            
            await asyncio.sleep(2)
        
        # Collect results
        pages = []
        all_text = []
        
        for block in status['Blocks']:
            if block['BlockType'] == 'PAGE':
                page_num = block.get('Page', len(pages) + 1)
                if len(pages) < page_num:
                    pages.append({
                        "page_number": page_num,
                        "text": "",
                        "confidence": 0.0,
                        "lines": [],
                    })
            elif block['BlockType'] == 'LINE':
                page_idx = block.get('Page', 1) - 1
                if page_idx < len(pages):
                    pages[page_idx]['lines'].append(block['Text'])
                    pages[page_idx]['confidence'] = (
                        pages[page_idx]['confidence'] + block.get('Confidence', 0)
                    ) / 2
        
        # Compile text
        for page in pages:
            page['text'] = '\n'.join(page.get('lines', []))
            all_text.append(page['text'])
        
        avg_confidence = sum(p['confidence'] for p in pages) / len(pages) / 100.0 if pages else 0.0
        
        return OCRResult(
            text="\n\n".join(all_text),
            confidence=avg_confidence,
            page_count=len(pages),
            pages=pages,
            metadata={
                "backend": "aws_textract",
                "source_file": str(pdf_path),
                "job_id": job_id,
            },
        )


class RedactionDetector:
    """Detect and analyze redactions in documents."""

    # Common FOIA exemption patterns
    EXEMPTION_PATTERNS = {
        r'\(b\)\(1\)': 'National security',
        r'\(b\)\(2\)': 'Internal personnel rules',
        r'\(b\)\(3\)': 'Statutory exemption',
        r'\(b\)\(4\)': 'Trade secrets',
        r'\(b\)\(5\)': 'Deliberative process',
        r'\(b\)\(6\)': 'Personal privacy',
        r'\(b\)\(7\)\(A\)': 'Law enforcement - interference',
        r'\(b\)\(7\)\(C\)': 'Law enforcement - privacy',
        r'\(b\)\(7\)\(D\)': 'Law enforcement - confidential source',
        r'\(b\)\(7\)\(E\)': 'Law enforcement - techniques',
        r'\(b\)\(7\)\(F\)': 'Law enforcement - safety',
    }

    async def analyze(self, text: str, pdf_path: Path | None = None) -> dict[str, Any]:
        """Analyze a document for redactions."""
        import re
        
        exemptions_found = []
        for pattern, description in self.EXEMPTION_PATTERNS.items():
            matches = re.findall(pattern, text, re.IGNORECASE)
            if matches:
                exemptions_found.append({
                    "code": pattern.replace('\\', ''),
                    "description": description,
                    "count": len(matches),
                })
        
        # Count visual redactions if PDF available
        # This would use image analysis to detect black boxes
        visual_redaction_count = None
        if pdf_path:
            visual_redaction_count = await self._count_visual_redactions(pdf_path)
        
        return {
            "exemptions_cited": exemptions_found,
            "total_exemption_citations": sum(e['count'] for e in exemptions_found),
            "visual_redaction_count": visual_redaction_count,
        }

    async def _count_visual_redactions(self, pdf_path: Path) -> int:
        """Count visual redactions (black boxes) in a PDF.
        
        Uses image analysis to detect large black rectangular regions.
        """
        from pdf2image import convert_from_path
        import numpy as np
        
        def _analyze():
            images = convert_from_path(str(pdf_path), dpi=150)
            total_redactions = 0
            
            for image in images:
                # Convert to numpy array
                arr = np.array(image.convert('L'))  # Grayscale
                
                # Find very dark regions (potential redactions)
                dark_threshold = 30
                dark_pixels = arr < dark_threshold
                
                # Simple connected component analysis
                # In production, use cv2.findContours for proper detection
                # This is a rough approximation
                dark_ratio = np.sum(dark_pixels) / arr.size
                if dark_ratio > 0.01:  # More than 1% dark
                    # Estimate number of redactions based on dark area
                    # Rough heuristic: each redaction is ~1% of page
                    total_redactions += int(dark_ratio * 100)
            
            return total_redactions
        
        return await asyncio.to_thread(_analyze)
