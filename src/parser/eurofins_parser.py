"""
Eurofins PDF Report Parser - Updated for actual report format.

Extracts test result data from Eurofins Microbiology Laboratories PDF reports.
Handles the specific format used by Eurofins Lancaster lab for Fisher's Popcorn.

Uses pypdf for PDF text extraction (pure Python, no compilation needed).
"""

import re
from pypdf import PdfReader
from io import BytesIO
from datetime import datetime
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Any
from pathlib import Path
import logging

logger = logging.getLogger(__name__)


@dataclass
class EurofinsReportHeader:
    """Header/metadata from Eurofins report."""
    report_number: str  # AR-25-QP-119455-01
    eurofins_sample_code: str  # 498-2025-12240272
    client_code: str  # QP0004621
    client_sample_code: str = ""  # 122325-5A
    order_code: str = ""  # 006-10547-2285931
    po_number: str = ""  # 6
    sample_description: str = ""  # |COOK-DRAIN-1 (2)|
    sample_reference: str = ""  # 122325-5A
    received_date: Optional[datetime] = None
    reported_date: Optional[datetime] = None
    registration_date: Optional[datetime] = None
    condition_upon_receipt: str = ""
    lab_name: str = "Eurofins Microbiology Laboratories (Lancaster)"


@dataclass
class EurofinsTestResult:
    """Individual test result from Eurofins report."""
    test_code: str  # UMB4D, UMPSK, UMQDQ
    test_name: str  # Enterobacteriaceae, Salmonella species, Listeria species
    parameter: str  # Enterobacteriaceae, Salmonella spp., Listeria spp.
    result: str  # "Not Detected per Sponge", "30 (est) cfu/Sponge", "< 10 cfu/Sponge"
    method: str  # AOAC 2003.01, AOAC-RI 121501, AOAC-RI 061702
    accreditation: str = ""  # ISO/IEC 17025:2017 A2LA 3329.03
    completed_date: Optional[datetime] = None
    units: str = ""  # cfu/Sponge, per Sponge
    
    @property
    def result_value(self) -> str:
        """Extract just the result value without units."""
        result = self.result
        # Remove units from result string
        result = re.sub(r'\s*(?:cfu/Sponge|per Sponge|CFU/g|MPN/g).*$', '', result, flags=re.IGNORECASE)
        return result.strip()
    
    @property
    def extracted_units(self) -> str:
        """Extract units from result string."""
        if 'cfu/Sponge' in self.result:
            return 'cfu/Sponge'
        elif 'per Sponge' in self.result:
            return 'per Sponge'
        elif 'CFU/g' in self.result:
            return 'CFU/g'
        elif 'MPN/g' in self.result:
            return 'MPN/g'
        return self.units or 'N/A'


@dataclass
class EurofinsReport:
    """Complete parsed Eurofins report."""
    header: EurofinsReportHeader
    results: List[EurofinsTestResult] = field(default_factory=list)
    raw_text: str = ""
    source_file: Optional[str] = None
    parse_errors: List[str] = field(default_factory=list)
    
    def is_valid(self) -> bool:
        """Check if report has minimum required data."""
        return bool(
            self.header.report_number and
            self.header.eurofins_sample_code
        )
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            'header': {
                'report_number': self.header.report_number,
                'eurofins_sample_code': self.header.eurofins_sample_code,
                'client_code': self.header.client_code,
                'client_sample_code': self.header.client_sample_code,
                'order_code': self.header.order_code,
                'po_number': self.header.po_number,
                'sample_description': self.header.sample_description,
                'sample_reference': self.header.sample_reference,
                'received_date': self.header.received_date.isoformat() if self.header.received_date else None,
                'reported_date': self.header.reported_date.isoformat() if self.header.reported_date else None,
                'registration_date': self.header.registration_date.isoformat() if self.header.registration_date else None,
                'condition_upon_receipt': self.header.condition_upon_receipt,
                'lab_name': self.header.lab_name,
            },
            'results': [
                {
                    'test_code': r.test_code,
                    'test_name': r.test_name,
                    'parameter': r.parameter,
                    'result': r.result,
                    'result_value': r.result_value,
                    'method': r.method,
                    'accreditation': r.accreditation,
                    'completed_date': r.completed_date.isoformat() if r.completed_date else None,
                    'units': r.extracted_units,
                }
                for r in self.results
            ],
            'source_file': self.source_file,
            'parse_errors': self.parse_errors,
        }


class EurofinsParser:
    """Parser for Eurofins Microbiology PDF reports."""
    
    # Test code patterns
    TEST_PATTERNS = [
        # (code, pattern to match test section header, organism name)
        ('UMB4D', r'UMB4D\s*-\s*Enterobacteriaceae', 'Enterobacteriaceae'),
        ('UMPSK', r'UMPSK\s*-\s*Salmonella\s+species', 'Salmonella spp.'),
        ('UMQDQ', r'UMQDQ\s*-\s*Listeria\s+species', 'Listeria spp.'),
        ('UMLM', r'UMLM\s*-\s*Listeria\s+monocytogenes', 'Listeria monocytogenes'),
        ('APC', r'Aerobic\s+Plate\s+Count', 'Aerobic Plate Count'),
        ('YM', r'Yeast\s+and\s+Mold', 'Yeast and Mold'),
    ]
    
    def __init__(self):
        self.logger = logging.getLogger(self.__class__.__name__)
    
    def parse_file(self, pdf_path: str | Path) -> EurofinsReport:
        """Parse a PDF file and extract report data."""
        pdf_path = Path(pdf_path)
        
        if not pdf_path.exists():
            raise FileNotFoundError(f"PDF file not found: {pdf_path}")
        
        if not pdf_path.suffix.lower() == '.pdf':
            raise ValueError(f"File is not a PDF: {pdf_path}")
        
        self.logger.info(f"Parsing PDF: {pdf_path}")
        
        # Extract text from PDF using pypdf
        reader = PdfReader(str(pdf_path))
        full_text = ""
        
        for page in reader.pages:
            full_text += page.extract_text() + "\n"
        
        # Parse the extracted text
        report = self._parse_text(full_text)
        report.source_file = str(pdf_path)
        report.raw_text = full_text
        
        return report
    
    def parse_bytes(self, pdf_bytes: bytes, filename: str = "unknown.pdf") -> EurofinsReport:
        """Parse PDF from bytes (for email attachments)."""
        # Use BytesIO to create a file-like object from bytes
        pdf_stream = BytesIO(pdf_bytes)
        reader = PdfReader(pdf_stream)
        full_text = ""
        
        for page in reader.pages:
            full_text += page.extract_text() + "\n"
        
        report = self._parse_text(full_text)
        report.source_file = filename
        report.raw_text = full_text
        
        return report
    
    def _parse_text(self, text: str) -> EurofinsReport:
        """Parse extracted text into structured report."""
        errors = []
        
        # Extract header information
        header = self._parse_header(text, errors)
        
        # Extract test results
        results = self._parse_results(text, errors)
        
        return EurofinsReport(
            header=header,
            results=results,
            parse_errors=errors
        )
    
    def _parse_header(self, text: str, errors: List[str]) -> EurofinsReportHeader:
        """Extract header/metadata from report text."""
        
        # Report number (AR-25-QP-119455-01)
        report_number = ""
        match = re.search(r'(AR-\d{2}-[A-Z]{2}-\d{6}-\d{2})', text)
        if match:
            report_number = match.group(1)
        else:
            errors.append("Could not extract report number")
        
        # Eurofins Sample Code (498-2025-12240272) and Client Sample Code (122325-4A)
        # Note: PDF extraction gives us labels on separate lines followed by values:
        # Client Sample Code:
        # Eurofins Sample Code:
        # 122325-4A           <- Client Sample Code value
        # 498-2025-12240273   <- Eurofins Sample Code value
        
        eurofins_sample_code = ""
        client_sample_code = ""
        
        # Try the newer format where labels and values are on separate lines
        match = re.search(
            r'Client Sample Code[:\s]*\n?'
            r'Eurofins Sample Code[:\s]*\n?'
            r'([^\n]+)\n'  # Client Sample Code value
            r'(\d{3}-\d{4}-\d{8})',  # Eurofins Sample Code value
            text
        )
        if match:
            client_sample_code = match.group(1).strip()
            eurofins_sample_code = match.group(2).strip()
        else:
            # Try alternative: labels and values on same line
            match = re.search(r'Eurofins Sample Code[:\s]*(\d{3}-\d{4}-\d{8})', text)
            if match:
                eurofins_sample_code = match.group(1)
            else:
                # Last resort: just find the pattern anywhere
                match = re.search(r'(\d{3}-\d{4}-\d{8})', text)
                if match:
                    eurofins_sample_code = match.group(1)
                else:
                    errors.append("Could not extract Eurofins sample code")
            
            # Try to get client sample code separately
            match = re.search(r'Client Sample Code[:\s]*([A-Za-z0-9-]+)', text)
            if match:
                client_sample_code = match.group(1).strip()
        
        # Client Code (QP0004621)
        client_code = ""
        match = re.search(r'Client Code[:\s]*([A-Z]{2}\d+)', text)
        if match:
            client_code = match.group(1)
        
        # Order Code (006-10547-2285931)
        order_code = ""
        match = re.search(r'Order Code[:\s]*(\d{3}-\d{5}-\d{7})', text)
        if match:
            order_code = match.group(1)
        
        # PO Number
        po_number = ""
        match = re.search(r'PO#[:\s]*(\d+)', text)
        if match:
            po_number = match.group(1)
        
        # Sample Description (|COOK-DRAIN-1 (2)|)
        sample_description = ""
        match = re.search(r'Sample Description[:\s]*([^\n]+)', text)
        if match:
            sample_description = match.group(1).strip()
        
        # Sample Reference
        sample_reference = ""
        match = re.search(r'Sample Reference[:\s]*([^\n]+)', text)
        if match:
            sample_reference = match.group(1).strip()
        
        # Dates
        received_date = self._extract_date(text, r'Received On[:\s]*')
        reported_date = self._extract_date(text, r'Reported On[:\s]*')
        registration_date = self._extract_date(text, r'Sample Registration Date[:\s]*')
        
        # Condition Upon Receipt
        condition = ""
        match = re.search(r'Condition Upon Receipt[:\s]*([^\n]+)', text)
        if match:
            condition = match.group(1).strip()
        
        return EurofinsReportHeader(
            report_number=report_number,
            eurofins_sample_code=eurofins_sample_code,
            client_code=client_code,
            client_sample_code=client_sample_code,
            order_code=order_code,
            po_number=po_number,
            sample_description=sample_description,
            sample_reference=sample_reference,
            received_date=received_date,
            reported_date=reported_date,
            registration_date=registration_date,
            condition_upon_receipt=condition,
        )
    
    def _parse_results(self, text: str, errors: List[str]) -> List[EurofinsTestResult]:
        """Extract test results from report text."""
        results = []
        
        # Split into test sections and parse each
        sections = self._split_into_test_sections(text)
        
        for section in sections:
            result = self._parse_test_section(section)
            if result:
                results.append(result)
        
        if not results:
            # Fallback: try to find individual result lines
            results = self._parse_results_fallback(text, errors)
        
        return results
    
    def _split_into_test_sections(self, text: str) -> List[tuple]:
        """Split text into individual test sections."""
        sections = []
        
        # Pattern to identify start of test sections
        section_starts = []
        for code, pattern, name in self.TEST_PATTERNS:
            for match in re.finditer(pattern, text, re.IGNORECASE):
                section_starts.append((match.start(), match.group(0), code, name))
        
        # Sort by position
        section_starts.sort(key=lambda x: x[0])
        
        # Extract each section
        for i, (start, match_text, code, name) in enumerate(section_starts):
            if i + 1 < len(section_starts):
                end = section_starts[i + 1][0]
            else:
                # End at signature or footer
                end_match = re.search(r'Respectfully Submitted|Results shown in this report', text[start:])
                if end_match:
                    end = start + end_match.start()
                else:
                    end = len(text)
            
            section_text = text[start:end]
            sections.append((code, name, section_text))
        
        return sections
    
    def _parse_test_section(self, section_info: tuple) -> Optional[EurofinsTestResult]:
        """Parse a single test section."""
        code, organism_name, text = section_info
        
        # Extract method (Reference)
        method = ""
        match = re.search(r'Reference\s*\n?([A-Z0-9-]+(?:\s*[0-9.]+)?)', text)
        if match:
            method = match.group(1).strip()
        
        # Extract accreditation
        accreditation = ""
        match = re.search(r'Accreditation\s*\n?(.+?)(?=\s*Completed)', text, re.DOTALL)
        if match:
            accreditation = ' '.join(match.group(1).split())
        
        # Extract completed date
        completed_date = None
        match = re.search(r'Completed\s*\n?(\d{2}[A-Za-z]{3}\d{4})', text)
        if match:
            completed_date = self._parse_date(match.group(1))
        
        # Extract parameter and result
        parameter = organism_name
        result = ""
        
        # Look for "Parameter Result" section then parse next line
        param_section = re.search(r'Parameter\s+Result\s*\n(.+)', text, re.DOTALL)
        if param_section:
            result_text = param_section.group(1).strip()
            # Get first line which should be the result
            first_line = result_text.split('\n')[0].strip()
            
            # Parse the line - format is "Organism Result"
            # e.g. "Enterobacteriaceae 30 (est) cfu/Sponge"
            # e.g. "Salmonella spp. Not Detected per Sponge"
            result_patterns = [
                r'(Enterobacteriaceae)\s+(.+)',
                r'(Salmonella\s+spp\.?)\s+(.+)',
                r'(Listeria\s+spp\.?)\s+(.+)',
                r'(Listeria\s+monocytogenes)\s+(.+)',
                r'([A-Za-z][A-Za-z\s.]+?)\s+((?:Not Detected|Detected|<|>|\d).+)$',
            ]
            
            for pattern in result_patterns:
                m = re.match(pattern, first_line, re.IGNORECASE)
                if m:
                    parameter = m.group(1).strip()
                    result = m.group(2).strip()
                    break
            
            if not result:
                result = first_line
        
        if not result:
            return None
        
        # Extract test name from header
        test_name_match = re.search(r'UM[A-Z0-9]+\s*-\s*([^-\n]+)', text)
        test_name = test_name_match.group(1).strip() if test_name_match else organism_name
        
        return EurofinsTestResult(
            test_code=code,
            test_name=test_name,
            parameter=parameter,
            result=result,
            method=method,
            accreditation=accreditation,
            completed_date=completed_date,
        )
    
    def _parse_results_fallback(self, text: str, errors: List[str]) -> List[EurofinsTestResult]:
        """Fallback method to extract results using simpler patterns."""
        results = []
        
        # Look for common organism/result patterns
        patterns = [
            (r'Enterobacteriaceae\s+((?:Not Detected|<|>|\d)[^\n]+)', 'UMB4D', 'Enterobacteriaceae', 'Enterobacteriaceae'),
            (r'Salmonella\s+spp\.?\s+((?:Not Detected|Detected)[^\n]+)', 'UMPSK', 'Salmonella species', 'Salmonella spp.'),
            (r'Listeria\s+spp\.?\s+((?:Not Detected|Detected)[^\n]+)', 'UMQDQ', 'Listeria species', 'Listeria spp.'),
        ]
        
        for pattern, code, test_name, parameter in patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                result = match.group(1).strip()
                
                # Try to find method
                method = ""
                if code == 'UMB4D':
                    method_match = re.search(r'AOAC\s*2003\.01', text)
                    method = method_match.group(0) if method_match else ""
                elif code == 'UMPSK':
                    method_match = re.search(r'AOAC-RI\s*121501', text)
                    method = method_match.group(0) if method_match else ""
                elif code == 'UMQDQ':
                    method_match = re.search(r'AOAC-RI\s*061702', text)
                    method = method_match.group(0) if method_match else ""
                
                results.append(EurofinsTestResult(
                    test_code=code,
                    test_name=test_name,
                    parameter=parameter,
                    result=result,
                    method=method,
                ))
        
        return results
    
    def _extract_date(self, text: str, prefix_pattern: str) -> Optional[datetime]:
        """Extract a date following a prefix pattern."""
        date_patterns = [
            r'(\d{2}[A-Za-z]{3}\d{4})',  # 05Jan2026
            r'(\d{1,2}/\d{1,2}/\d{4})',  # 1/5/2026
            r'(\d{4}-\d{2}-\d{2})',       # 2026-01-05
        ]
        
        for date_pattern in date_patterns:
            full_pattern = prefix_pattern + date_pattern
            match = re.search(full_pattern, text)
            if match:
                date_str = match.group(1)
                return self._parse_date(date_str)
        return None
    
    def _parse_date(self, date_str: str) -> Optional[datetime]:
        """Parse date string to datetime object."""
        formats = [
            '%d%b%Y',      # 05Jan2026
            '%m/%d/%Y',    # 01/05/2026
            '%Y-%m-%d',    # 2026-01-05
        ]
        
        for fmt in formats:
            try:
                return datetime.strptime(date_str, fmt)
            except ValueError:
                continue
        
        return None


def parse_eurofins_pdf(pdf_path: str | Path) -> EurofinsReport:
    """Convenience function to parse a Eurofins PDF."""
    parser = EurofinsParser()
    return parser.parse_file(pdf_path)
