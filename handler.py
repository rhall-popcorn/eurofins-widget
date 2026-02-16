"""
AWS Lambda handler for Eurofins lab report automation.

This function:
1. Fetches new emails from Gmail with Eurofins PDF attachments
2. Parses the PDF lab reports
3. Uploads results to Bizowie EMP Swab Results table
"""

import os
import json
import base64
import logging
from datetime import datetime, timedelta
from typing import Dict, Any, List

# Configure logging
log_level = os.environ.get('LOG_LEVEL', 'INFO')
logging.basicConfig(level=log_level)
logger = logging.getLogger(__name__)

# Import our modules
from src.gmail.client import GmailClient
from src.parser.eurofins_parser import EurofinsParser
from src.bizowie.client import BizowieClient, SwabResult


def get_config() -> Dict[str, Any]:
    """Load configuration from environment variables."""
    return {
        'gmail': {
            'credentials_json': os.environ.get('GMAIL_CREDENTIALS_JSON'),
            'token_json': os.environ.get('GMAIL_TOKEN_JSON'),
            'watch_address': os.environ.get('GMAIL_WATCH_ADDRESS', ''),
        },
        'bizowie': {
            'base_url': os.environ.get('BIZOWIE_BASE_URL', 'https://fisherspopcorn.mybizowie.com'),
            'api_key': os.environ.get('BIZOWIE_API_KEY'),
            'secret_key': os.environ.get('BIZOWIE_SECRET_KEY'),
            'database_id': int(os.environ.get('BIZOWIE_DATABASE_ID', '1')),
            'results_table_id': int(os.environ.get('BIZOWIE_RESULTS_TABLE_ID', '19')),
        },
        'processing': {
            'since_days': int(os.environ.get('SINCE_DAYS', '7')),
            'dry_run': os.environ.get('DRY_RUN', 'false').lower() == 'true',
        }
    }


def convert_report_to_results(report, bizowie_client=None, email_received_date=None) -> List[SwabResult]:
    """
    Convert parsed Eurofins report to SwabResult objects.
    
    Args:
        report: Parsed Eurofins report
        bizowie_client: Optional BizowieClient to look up EMP Sample ID from Swabs table
        email_received_date: Date the email was received (used for date_reported)
        
    Returns:
        List of SwabResult objects
    """
    results = []
    
    # Get the client sample code from the report header
    client_sample_code = report.header.client_sample_code
    
    # Look up the EMP Sample ID from Bizowie Swabs table (table 18)
    # using the client_sample_code to match the coc_ref field
    emp_sample_id = None
    if bizowie_client and client_sample_code:
        emp_sample_id = bizowie_client.lookup_emp_sample_id(client_sample_code)
    
    # Fall back to Eurofins sample code if lookup fails
    if not emp_sample_id:
        emp_sample_id = report.header.eurofins_sample_code
        logger.warning(f"Could not find EMP Sample ID for client sample code '{client_sample_code}', "
                      f"using Eurofins sample code: {emp_sample_id}")
    
    # Use email received date for date_reported, fall back to current date
    date_reported = email_received_date if email_received_date else datetime.utcnow()
    
    for test_result in report.results:
        result = SwabResult(
            emp_sample_id=emp_sample_id,
            organism=test_result.parameter,
            method_1=test_result.method,
            result_value=test_result.result_value,
            units=test_result.extracted_units,
            date_reported=date_reported,
            reviewed_by="",
            status="Pending Review",
            notes=f"Report: {report.header.report_number}"
        )
        results.append(result)
    
    return results


def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """
    Main Lambda handler function.
    
    Args:
        event: Lambda event (can be CloudWatch scheduled event or manual trigger)
        context: Lambda context
        
    Returns:
        Processing summary
    """
    logger.info("Starting Eurofins lab report automation")
    logger.info(f"Event: {json.dumps(event)}")
    
    # Load configuration
    config = get_config()
    dry_run = config['processing']['dry_run']
    
    if dry_run:
        logger.info("DRY RUN MODE - No data will be uploaded to Bizowie")
    
    # Initialize summary
    summary = {
        'timestamp': datetime.utcnow().isoformat(),
        'emails_processed': 0,
        'pdfs_parsed': 0,
        'results_found': 0,
        'results_uploaded': 0,
        'results_skipped': 0,
        'errors': [],
        'dry_run': dry_run
    }
    
    try:
        # Initialize Gmail client
        gmail = GmailClient(
            credentials_json=config['gmail']['credentials_json'],
            token_json=config['gmail']['token_json']
        )
        
        if not gmail.authenticate():
            raise Exception("Gmail authentication failed")
        
        # Initialize PDF parser
        parser = EurofinsParser()
        
        # Initialize Bizowie client (needed for EMP Sample ID lookup even in dry run)
        bizowie = None
        if config['bizowie']['api_key'] and config['bizowie']['secret_key']:
            bizowie = BizowieClient(
                base_url=config['bizowie']['base_url'],
                api_key=config['bizowie']['api_key'],
                secret_key=config['bizowie']['secret_key'],
                database_id=config['bizowie']['database_id'],
                results_table_id=config['bizowie']['results_table_id']
            )
            
            if not bizowie.authenticate():
                raise Exception("Bizowie authentication failed")
        
        # Calculate date range
        since_days = config['processing']['since_days']
        since_date = datetime.utcnow() - timedelta(days=since_days)
        
        logger.info(f"Searching for emails since {since_date.isoformat()}")
        
        # Search for Eurofins emails
        emails = gmail.search_eurofins_emails(since_date=since_date)
        summary['emails_processed'] = len(emails)
        
        logger.info(f"Found {len(emails)} emails with PDF attachments")
        
        # Process each email
        all_results = []
        
        for email in emails:
            email_id = email['id']
            pdfs = email['pdfs']
            email_received_date = email.get('received_date')
            
            for pdf in pdfs:
                filename = pdf['filename']
                pdf_data = pdf['data']
                
                try:
                    logger.info(f"Parsing PDF: {filename}")
                    
                    # Parse the PDF
                    report = parser.parse_bytes(pdf_data, filename)
                    summary['pdfs_parsed'] += 1
                    
                    if report.is_valid():
                        # Convert to SwabResults (pass bizowie for EMP Sample ID lookup, email date for date_reported)
                        results = convert_report_to_results(report, bizowie, email_received_date)
                        all_results.extend(results)
                        summary['results_found'] += len(results)
                        
                        logger.info(
                            f"Parsed {filename}: {report.header.report_number} - "
                            f"{len(results)} test results"
                        )
                    else:
                        logger.warning(f"Invalid report: {filename} - {report.parse_errors}")
                        summary['errors'].append(f"Invalid report: {filename}")
                        
                except Exception as e:
                    error_msg = f"Error parsing {filename}: {str(e)}"
                    logger.error(error_msg)
                    summary['errors'].append(error_msg)
            
            # Mark email as processed (if not dry run)
            if not dry_run:
                gmail.mark_as_processed(email_id)
        
        # Upload results to Bizowie
        if all_results and not dry_run and bizowie:
            logger.info(f"Uploading {len(all_results)} results to Bizowie")
            
            upload_summary = bizowie.upload_results(all_results, skip_duplicates=True)
            summary['results_uploaded'] = upload_summary['uploaded']
            summary['results_skipped'] = upload_summary['skipped']
            summary['errors'].extend(upload_summary['errors'])
        elif all_results and dry_run:
            logger.info(f"DRY RUN: Would upload {len(all_results)} results")
            for result in all_results:
                logger.info(f"  - {result.emp_sample_id}: {result.organism} = {result.result_value}")
        
        summary['success'] = True
        
    except Exception as e:
        logger.error(f"Lambda execution failed: {e}")
        summary['success'] = False
        summary['errors'].append(str(e))
    
    logger.info(f"Processing complete: {json.dumps(summary)}")
    
    return summary


# For local testing
if __name__ == "__main__":
    # Load .env file if present
    from dotenv import load_dotenv
    load_dotenv()
    
    result = lambda_handler({}, None)
    print(json.dumps(result, indent=2))
