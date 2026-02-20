"""Document ingestion from various sources."""

from __future__ import annotations

import asyncio
import hashlib
import mimetypes
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, BinaryIO
from uuid import uuid4

from ..models import Document, DocumentType


@dataclass
class IngestResult:
    """Result of document ingestion."""

    document_id: str
    filename: str
    file_path: str
    file_size: int
    mime_type: str
    page_count: int | None
    checksum: str
    metadata: dict[str, Any]


class DocumentIngester:
    """Ingest documents from various sources into the system."""

    def __init__(
        self,
        storage_path: Path | str,
        max_file_size_mb: int = 100,
    ):
        self.storage_path = Path(storage_path)
        self.storage_path.mkdir(parents=True, exist_ok=True)
        self.max_file_size = max_file_size_mb * 1024 * 1024

    async def ingest_file(
        self,
        file_path: Path | str,
        doc_type: DocumentType = DocumentType.FULL_RESPONSE,
        request_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> IngestResult:
        """Ingest a file from the filesystem."""
        file_path = Path(file_path)
        
        if not file_path.exists():
            raise FileNotFoundError(f"File not found: {file_path}")
        
        file_size = file_path.stat().st_size
        if file_size > self.max_file_size:
            raise ValueError(f"File too large: {file_size} bytes (max {self.max_file_size})")
        
        # Determine MIME type
        mime_type, _ = mimetypes.guess_type(str(file_path))
        mime_type = mime_type or 'application/octet-stream'
        
        # Generate document ID
        doc_id = str(uuid4())
        
        # Calculate checksum
        checksum = await self._calculate_checksum(file_path)
        
        # Copy to storage
        dest_dir = self.storage_path / doc_id[:2] / doc_id[2:4]
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest_path = dest_dir / f"{doc_id}{file_path.suffix}"
        
        await asyncio.to_thread(shutil.copy2, file_path, dest_path)
        
        # Get page count for PDFs
        page_count = None
        if mime_type == 'application/pdf':
            page_count = await self._get_pdf_page_count(dest_path)
        
        return IngestResult(
            document_id=doc_id,
            filename=file_path.name,
            file_path=str(dest_path),
            file_size=file_size,
            mime_type=mime_type,
            page_count=page_count,
            checksum=checksum,
            metadata={
                'original_path': str(file_path),
                'ingested_at': datetime.utcnow().isoformat(),
                'doc_type': doc_type.value,
                'request_id': request_id,
                **(metadata or {}),
            },
        )

    async def ingest_bytes(
        self,
        content: bytes,
        filename: str,
        doc_type: DocumentType = DocumentType.FULL_RESPONSE,
        request_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> IngestResult:
        """Ingest raw bytes (e.g., from email attachment)."""
        if len(content) > self.max_file_size:
            raise ValueError(f"Content too large: {len(content)} bytes")
        
        # Determine MIME type
        mime_type, _ = mimetypes.guess_type(filename)
        mime_type = mime_type or 'application/octet-stream'
        
        # Generate document ID
        doc_id = str(uuid4())
        
        # Calculate checksum
        checksum = hashlib.sha256(content).hexdigest()
        
        # Save to storage
        suffix = Path(filename).suffix
        dest_dir = self.storage_path / doc_id[:2] / doc_id[2:4]
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest_path = dest_dir / f"{doc_id}{suffix}"
        
        await asyncio.to_thread(dest_path.write_bytes, content)
        
        # Get page count for PDFs
        page_count = None
        if mime_type == 'application/pdf':
            page_count = await self._get_pdf_page_count(dest_path)
        
        return IngestResult(
            document_id=doc_id,
            filename=filename,
            file_path=str(dest_path),
            file_size=len(content),
            mime_type=mime_type,
            page_count=page_count,
            checksum=checksum,
            metadata={
                'ingested_at': datetime.utcnow().isoformat(),
                'doc_type': doc_type.value,
                'request_id': request_id,
                **(metadata or {}),
            },
        )

    async def ingest_email_attachment(
        self,
        email_message: Any,  # email.message.Message
        attachment_index: int,
        request_id: str | None = None,
    ) -> IngestResult:
        """Extract and ingest an attachment from an email."""
        import email
        
        # Find attachment
        attachments = []
        for part in email_message.walk():
            if part.get_content_maintype() == 'multipart':
                continue
            if part.get('Content-Disposition') is None:
                continue
            attachments.append(part)
        
        if attachment_index >= len(attachments):
            raise IndexError(f"Attachment index {attachment_index} out of range")
        
        attachment = attachments[attachment_index]
        filename = attachment.get_filename() or f"attachment_{attachment_index}"
        content = attachment.get_payload(decode=True)
        
        return await self.ingest_bytes(
            content=content,
            filename=filename,
            request_id=request_id,
            metadata={
                'source': 'email',
                'email_subject': email_message.get('Subject'),
                'email_from': email_message.get('From'),
                'email_date': email_message.get('Date'),
            },
        )

    async def ingest_directory(
        self,
        dir_path: Path | str,
        recursive: bool = True,
        file_patterns: list[str] | None = None,
        request_id: str | None = None,
    ) -> list[IngestResult]:
        """Ingest all matching files from a directory."""
        dir_path = Path(dir_path)
        
        if not dir_path.is_dir():
            raise NotADirectoryError(f"Not a directory: {dir_path}")
        
        patterns = file_patterns or ['*.pdf', '*.doc', '*.docx', '*.txt', '*.jpg', '*.png']
        
        files = []
        for pattern in patterns:
            if recursive:
                files.extend(dir_path.rglob(pattern))
            else:
                files.extend(dir_path.glob(pattern))
        
        results = []
        for file_path in files:
            try:
                result = await self.ingest_file(
                    file_path=file_path,
                    request_id=request_id,
                )
                results.append(result)
            except Exception as e:
                # Log error but continue with other files
                print(f"Error ingesting {file_path}: {e}")
        
        return results

    async def _calculate_checksum(self, file_path: Path) -> str:
        """Calculate SHA256 checksum of a file."""
        def _hash():
            sha256 = hashlib.sha256()
            with open(file_path, 'rb') as f:
                for chunk in iter(lambda: f.read(8192), b''):
                    sha256.update(chunk)
            return sha256.hexdigest()
        
        return await asyncio.to_thread(_hash)

    async def _get_pdf_page_count(self, pdf_path: Path) -> int:
        """Get the number of pages in a PDF."""
        def _count():
            from pypdf import PdfReader
            reader = PdfReader(str(pdf_path))
            return len(reader.pages)
        
        try:
            return await asyncio.to_thread(_count)
        except Exception:
            return 0
