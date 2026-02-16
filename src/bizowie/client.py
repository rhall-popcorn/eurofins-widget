"""
Bizowie API client for uploading lab results.

Based on Bizowie APIv2 documentation:
- Uses api_key and secret_key for authentication
- Endpoint: POST /bz/apiv2/call/Database/record/create
- Payload: { api_key, secret_key, db_table_id, data: {...} }
"""

import requests
import json
from datetime import datetime
from typing import Dict, Any, Optional, List
from dataclasses import dataclass
import logging

logger = logging.getLogger(__name__)


@dataclass
class SwabResult:
    """
    Represents a swab test result to upload to Bizowie.
    
    EMP Swab Results table (Table 19) columns:
    - emp_sample_id: Foreign Key to EMP Swabs table (Eurofins sample code)
    - organism: Multi-Select (Salmonella, Listeria, Enterobacteriaceae, etc.)
    - method_1: Text (AOAC 2003.01, AOAC-RI 121501, etc.)
    - site_id: Foreign Key to EMP Sites table (optional)
    - result_value: Text (Not Detected, 30 (est), < 10, etc.)
    - units: Multi-Select (cfu/Sponge, per Sponge, N/A)
    - date_reported: Date
    - reviewed_by: User
    - status: Multi-Select
    """
    emp_sample_id: str  # Eurofins sample code (498-2025-12240272)
    organism: str  # Test organism (Enterobacteriaceae, Salmonella, Listeria)
    method_1: str  # Test method (AOAC 2003.01, etc.)
    result_value: str  # Result (Not Detected, 30 (est), < 10, etc.)
    units: str  # Units (cfu/Sponge, per Sponge, N/A)
    date_reported: Optional[datetime] = None
    reviewed_by: str = ""  # Leave empty for manual review assignment
    status: str = "Pending Review"
    notes: str = ""
    
    def to_bizowie_payload(self) -> Dict[str, Any]:
        """Convert to Bizowie API payload format."""
        payload = {
            'emp_sample_id': self.emp_sample_id,
            'organism': self.organism,
            'method_1': self.method_1,
            'result_value': self.result_value,
            'units': self.units,
            'status': self.status,
        }
        
        # Add date if present (try YYYY-MM-DD format for Bizowie)
        if self.date_reported:
            # Bizowie seems to use YYYY-MM-DD in its data responses
            payload['date_reported'] = self.date_reported.strftime('%Y-%m-%d')
        
        return payload


class BizowieClient:
    """Client for interacting with Bizowie APIv2."""
    
    def __init__(
        self,
        base_url: str,
        api_key: str,
        secret_key: str,
        database_id: int = 1,
        results_table_id: int = 19,  # EMP Swab Results table
        swabs_table_id: int = 18,    # EMP Swabs table
    ):
        """
        Initialize Bizowie client.
        
        Args:
            base_url: Bizowie instance URL (e.g., https://fisherspopcorn.mybizowie.com)
            api_key: API key from user profile
            secret_key: Secret key from user profile
            database_id: Database ID in Bizowie (default: 1)
            results_table_id: Table ID for EMP Swab Results (default: 19)
            swabs_table_id: Table ID for EMP Swabs (default: 18)
        """
        self.base_url = base_url.rstrip('/')
        self.api_key = api_key
        self.secret_key = secret_key
        self.database_id = database_id
        self.results_table_id = results_table_id
        self.swabs_table_id = swabs_table_id
        self.session = requests.Session()
        self.session.headers['Content-Type'] = 'application/json'
        self.logger = logging.getLogger(self.__class__.__name__)
        
        # Cache for EMP Sample ID lookups
        self._emp_sample_id_cache = {}
    
    def authenticate(self) -> bool:
        """
        Test authentication by making a simple API call.
        Returns True if credentials are valid.
        """
        try:
            # Try to search for records (limit 1) to verify auth works
            response = self._api_call('Database/record/search', {
                'db_table_id': self.results_table_id,
                'limit': 1
            })
            
            if response.get('success') or 'records' in response or 'data' in response:
                self.logger.info("Bizowie authentication successful")
                return True
            
            # Check if there's an error message
            if response.get('error'):
                self.logger.error(f"Bizowie auth error: {response.get('error')}")
                return False
            
            # If we got a response without error, assume it worked
            self.logger.info("Bizowie authentication successful (response received)")
            return True
            
        except Exception as e:
            self.logger.error(f"Bizowie authentication error: {e}")
            return False
    
    def lookup_emp_sample_id(self, client_sample_code: str) -> Optional[str]:
        """
        Look up the EMP Sample ID from the Swabs table (18) using client sample code.
        
        The Swabs table has a 'coc_ref' column (Column ID 197) that matches the Client Sample Code
        from the Eurofins report. This method finds the matching record and returns
        its 'id' field to use as emp_sample_id in the Results table.
        
        Args:
            client_sample_code: The Client Sample Code from the Eurofins PDF
            
        Returns:
            The EMP Sample ID (id field) from the matching Swabs record, or None if not found
        """
        # Check cache first
        if client_sample_code in self._emp_sample_id_cache:
            return self._emp_sample_id_cache[client_sample_code]
        
        try:
            self.logger.info(f"Looking up EMP Sample ID for coc_ref: '{client_sample_code}'")
            
            # Use query as a hash with the field name as key
            response = self._api_call('Database/table_row/search', {
                'db_table_id': self.swabs_table_id,
                'query': {'coc_ref': client_sample_code}
            })
            
            # Navigate to records: response -> response -> records
            records = []
            if isinstance(response, dict):
                inner_response = response.get('response', {})
                if isinstance(inner_response, dict):
                    records = inner_response.get('records', [])
            
            # If we got records, look for matching coc_ref
            if records and len(records) > 0:
                for record in records:
                    # Data can be in 'data' or 'html' sub-dict
                    data = record.get('data') or record.get('html') or {}
                    
                    coc_ref = data.get('coc_ref', '')
                    record_id = data.get('id', '')
                    
                    if str(coc_ref).strip() == str(client_sample_code).strip():
                        self.logger.info(f"Found EMP Sample ID: {record_id} for coc_ref: {client_sample_code}")
                        self._emp_sample_id_cache[client_sample_code] = str(record_id)
                        return str(record_id)
            
            self.logger.warning(f"No matching record found in Swabs table for coc_ref: {client_sample_code}")
            return None
            
        except Exception as e:
            self.logger.error(f"Error looking up EMP Sample ID: {e}")
            return None
    
    def _api_call(self, method: str, params: Dict[str, Any] = None) -> Dict[str, Any]:
        """
        Make an API call to Bizowie.
        
        Args:
            method: API method (e.g., 'Database/record/create')
            params: Additional parameters for the call
            
        Returns:
            API response as dict
        """
        url = f'{self.base_url}/bz/apiv2/call/{method}'
        
        payload = {
            'api_key': self.api_key,
            'secret_key': self.secret_key,
        }
        
        if params:
            payload.update(params)
        
        self.logger.debug(f"API call to {method}: {json.dumps(payload, default=str)}")
        
        response = self.session.post(url, json=payload)
        
        self.logger.debug(f"Response status: {response.status_code}")
        self.logger.debug(f"Response body: {response.text[:500]}")
        
        if response.status_code != 200:
            raise Exception(f"API call failed: {response.status_code} - {response.text}")
        
        try:
            return response.json()
        except json.JSONDecodeError:
            # Some endpoints return non-JSON on success
            return {'success': True, 'raw_response': response.text}
    
    def create_result(self, result: SwabResult) -> Dict[str, Any]:
        """
        Create a new swab result record in Bizowie.
        
        Args:
            result: SwabResult object to upload
            
        Returns:
            API response dict
        """
        payload = result.to_bizowie_payload()
        
        self.logger.info(f"Creating result for sample {result.emp_sample_id}, organism {result.organism}")
        
        response = self._api_call('Database/record/create', {
            'db_table_id': self.results_table_id,
            'data': payload
        })
        
        return response
    
    def search_records(
        self,
        filters: Dict[str, Any] = None,
        limit: int = 100
    ) -> List[Dict[str, Any]]:
        """
        Search for records in the EMP Swab Results table.
        
        Args:
            filters: Field filters (e.g., {'emp_sample_id': '498-2025-12240272'})
            limit: Maximum records to return
            
        Returns:
            List of matching records
        """
        params = {
            'db_table_id': self.results_table_id,
            'limit': limit
        }
        
        if filters:
            params['filters'] = filters
        
        response = self._api_call('Database/record/search', params)
        
        # Handle different response formats
        if isinstance(response, list):
            return response
        elif 'records' in response:
            return response['records']
        elif 'data' in response:
            return response['data']
        else:
            return []
    
    def check_duplicate(self, sample_code: str, organism: str) -> bool:
        """
        Check if a result already exists for this sample/organism combination.
        
        Args:
            sample_code: Eurofins sample code
            organism: Test organism
            
        Returns:
            True if duplicate exists
        """
        try:
            records = self.search_records(
                filters={'emp_sample_id': sample_code},
                limit=100
            )
            
            for record in records:
                record_organism = record.get('organism', '')
                if organism.lower() in record_organism.lower():
                    self.logger.info(f"Duplicate found for {sample_code}/{organism}")
                    return True
            
            return False
            
        except Exception as e:
            self.logger.warning(f"Duplicate check failed: {e}")
            return False  # Assume no duplicate if check fails
    
    def upload_results(
        self,
        results: List[SwabResult],
        skip_duplicates: bool = True
    ) -> Dict[str, Any]:
        """
        Upload multiple results to Bizowie.
        
        Args:
            results: List of SwabResult objects
            skip_duplicates: Whether to skip existing records
            
        Returns:
            Summary dict with counts
        """
        summary = {
            'total': len(results),
            'uploaded': 0,
            'skipped': 0,
            'errors': []
        }
        
        for result in results:
            try:
                # Check for duplicates
                if skip_duplicates and self.check_duplicate(result.emp_sample_id, result.organism):
                    self.logger.info(f"Skipping duplicate: {result.emp_sample_id}/{result.organism}")
                    summary['skipped'] += 1
                    continue
                
                # Create the record
                self.create_result(result)
                summary['uploaded'] += 1
                
            except Exception as e:
                error_msg = f"{result.emp_sample_id}/{result.organism}: {str(e)}"
                summary['errors'].append(error_msg)
                self.logger.error(error_msg)
        
        self.logger.info(
            f"Upload complete: {summary['uploaded']} uploaded, "
            f"{summary['skipped']} skipped, {len(summary['errors'])} errors"
        )
        
        return summary
