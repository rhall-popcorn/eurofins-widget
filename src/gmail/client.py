"""
Gmail API client for fetching Eurofins lab report emails.
"""

import base64
import pickle
import json
from datetime import datetime, timedelta
from typing import List, Optional, Dict, Any
from pathlib import Path
import logging

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

logger = logging.getLogger(__name__)

# Gmail API scopes
SCOPES = [
    'https://www.googleapis.com/auth/gmail.readonly',
    'https://www.googleapis.com/auth/gmail.modify'
]


class GmailClient:
    """Client for interacting with Gmail API to fetch Eurofins emails."""
    
    def __init__(
        self,
        credentials_path: Optional[str] = None,
        token_path: Optional[str] = None,
        credentials_json: Optional[str] = None,
        token_json: Optional[str] = None,
    ):
        """
        Initialize Gmail client.
        
        Args:
            credentials_path: Path to OAuth credentials JSON file
            token_path: Path to saved token pickle file
            credentials_json: Base64-encoded credentials JSON (for Lambda)
            token_json: Base64-encoded token JSON (for Lambda)
        """
        self.credentials_path = credentials_path
        self.token_path = token_path
        self.credentials_json = credentials_json
        self.token_json = token_json
        self.service = None
        self.creds = None
        self.logger = logging.getLogger(self.__class__.__name__)
    
    def authenticate(self) -> bool:
        """Authenticate with Gmail API."""
        try:
            # Try to load credentials from various sources
            if self.token_json:
                # Lambda environment - decode base64 token
                self.logger.info("Loading credentials from base64 token")
                token_data = json.loads(base64.b64decode(self.token_json))
                self.creds = Credentials.from_authorized_user_info(token_data, SCOPES)
            elif self.token_path and Path(self.token_path).exists():
                # Local environment - load from pickle file
                self.logger.info(f"Loading credentials from {self.token_path}")
                with open(self.token_path, 'rb') as token:
                    self.creds = pickle.load(token)
            
            # Refresh or get new credentials if needed
            if not self.creds or not self.creds.valid:
                if self.creds and self.creds.expired and self.creds.refresh_token:
                    self.logger.info("Refreshing expired credentials")
                    self.creds.refresh(Request())
                elif self.credentials_path:
                    self.logger.info("Running OAuth flow for new credentials")
                    flow = InstalledAppFlow.from_client_secrets_file(
                        self.credentials_path, SCOPES
                    )
                    self.creds = flow.run_local_server(port=0)
                    
                    # Save the credentials
                    if self.token_path:
                        with open(self.token_path, 'wb') as token:
                            pickle.dump(self.creds, token)
                else:
                    self.logger.error("No valid credentials available")
                    return False
            
            # Build the Gmail service
            self.service = build('gmail', 'v1', credentials=self.creds)
            self.logger.info("Gmail authentication successful")
            return True
            
        except Exception as e:
            self.logger.error(f"Gmail authentication failed: {e}")
            return False
    
    def search_emails(
        self,
        query: str,
        max_results: int = 100,
        since_date: Optional[datetime] = None
    ) -> List[Dict[str, Any]]:
        """
        Search for emails matching query.
        
        Args:
            query: Gmail search query
            max_results: Maximum number of results
            since_date: Only return emails after this date
            
        Returns:
            List of email metadata dicts
        """
        if not self.service:
            raise RuntimeError("Not authenticated. Call authenticate() first.")
        
        # Add date filter to query if provided
        if since_date:
            # Calculate days ago and use newer_than format
            days_ago = (datetime.now() - since_date).days
            if days_ago > 0:
                query = f"{query} newer_than:{days_ago}d"
        
        self.logger.info(f"Searching emails with query: {query}")
        
        emails = []
        page_token = None
        
        while len(emails) < max_results:
            results = self.service.users().messages().list(
                userId='me',
                q=query,
                maxResults=min(100, max_results - len(emails)),
                pageToken=page_token
            ).execute()
            
            messages = results.get('messages', [])
            if not messages:
                break
            
            emails.extend(messages)
            
            page_token = results.get('nextPageToken')
            if not page_token:
                break
        
        self.logger.info(f"Found {len(emails)} emails")
        return emails
    
    def get_email(self, message_id: str) -> Dict[str, Any]:
        """Get full email content by ID."""
        if not self.service:
            raise RuntimeError("Not authenticated. Call authenticate() first.")
        
        return self.service.users().messages().get(
            userId='me',
            id=message_id,
            format='full'
        ).execute()
    
    def get_attachments(self, message_id: str) -> List[Dict[str, Any]]:
        """
        Get all attachments from an email.
        
        Returns:
            List of dicts with 'filename', 'mime_type', and 'data' keys
        """
        if not self.service:
            raise RuntimeError("Not authenticated. Call authenticate() first.")
        
        message = self.get_email(message_id)
        attachments = []
        
        def process_parts(parts):
            for part in parts:
                if part.get('filename') and part.get('body', {}).get('attachmentId'):
                    # This is an attachment
                    attachment_id = part['body']['attachmentId']
                    attachment = self.service.users().messages().attachments().get(
                        userId='me',
                        messageId=message_id,
                        id=attachment_id
                    ).execute()
                    
                    data = base64.urlsafe_b64decode(attachment['data'])
                    
                    attachments.append({
                        'filename': part['filename'],
                        'mime_type': part.get('mimeType', 'application/octet-stream'),
                        'data': data
                    })
                
                # Recursively process nested parts
                if 'parts' in part:
                    process_parts(part['parts'])
        
        payload = message.get('payload', {})
        if 'parts' in payload:
            process_parts(payload['parts'])
        
        return attachments
    
    def get_pdf_attachments(self, message_id: str) -> List[Dict[str, Any]]:
        """Get only PDF attachments from an email."""
        attachments = self.get_attachments(message_id)
        return [
            a for a in attachments
            if a['filename'].lower().endswith('.pdf') or a['mime_type'] == 'application/pdf'
        ]
    
    def search_eurofins_emails(
        self,
        since_date: Optional[datetime] = None,
        max_results: int = 100
    ) -> List[Dict[str, Any]]:
        """
        Search specifically for Eurofins lab report emails.
        
        Args:
            since_date: Only return emails after this date
            max_results: Maximum number of results
            
        Returns:
            List of email metadata with PDFs
        """
        # Search for emails from Eurofins with PDF attachments
        # Using the specific sender domain: NoReply@ft.eurofinsus.com
        query = 'from:ft.eurofinsus.com has:attachment filename:pdf'
        
        emails = self.search_emails(query, max_results, since_date)
        
        # Filter to only emails with actual PDF attachments
        emails_with_pdfs = []
        for email in emails:
            pdfs = self.get_pdf_attachments(email['id'])
            if pdfs:
                # Get the email's internal date (when it was received)
                full_email = self.get_email(email['id'])
                internal_date_ms = int(full_email.get('internalDate', 0))
                received_date = datetime.fromtimestamp(internal_date_ms / 1000) if internal_date_ms else None
                
                emails_with_pdfs.append({
                    'id': email['id'],
                    'threadId': email.get('threadId'),
                    'pdfs': pdfs,
                    'received_date': received_date
                })
        
        self.logger.info(f"Found {len(emails_with_pdfs)} Eurofins emails with PDFs")
        return emails_with_pdfs
    
    def add_label(self, message_id: str, label_name: str) -> bool:
        """Add a label to an email (create if doesn't exist)."""
        if not self.service:
            raise RuntimeError("Not authenticated. Call authenticate() first.")
        
        try:
            # Get or create label
            labels = self.service.users().labels().list(userId='me').execute()
            label_id = None
            
            for label in labels.get('labels', []):
                if label['name'] == label_name:
                    label_id = label['id']
                    break
            
            if not label_id:
                # Create the label
                new_label = self.service.users().labels().create(
                    userId='me',
                    body={'name': label_name}
                ).execute()
                label_id = new_label['id']
            
            # Add label to message
            self.service.users().messages().modify(
                userId='me',
                id=message_id,
                body={'addLabelIds': [label_id]}
            ).execute()
            
            return True
        except Exception as e:
            self.logger.error(f"Failed to add label: {e}")
            return False
    
    def mark_as_processed(self, message_id: str) -> bool:
        """Mark an email as processed by adding a label."""
        return self.add_label(message_id, 'Eurofins-Processed')
