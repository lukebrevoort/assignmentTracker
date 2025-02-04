from notion_client import Client
from datetime import datetime
import pytz
from typing import Dict, Optional
from datetime import timezone
from models.assignment import Assignment
from utils.config import NOTION_TOKEN, NOTION_DATABASE_ID, COURSE_DATABASE_ID, USER_ID
from bs4 import BeautifulSoup
import re
import logging
import backoff
from ratelimit import limits, sleep_and_retry

logger = logging.getLogger(__name__)

# NotionAPI: Handles integration between Canvas assignments and Notion database
# Manages rate limiting, error handling, and data transformation

class NotionAPI:
    """
    Manages interaction with Notion API for syncing Canvas assignments.
    Handles rate limiting, retries, and data transformation.
    """
    
    ONE_SECOND = 1
    MAX_REQUESTS_PER_SECOND = 3

    def __init__(self):
        self.notion = Client(auth=NOTION_TOKEN)
        self.database_id = NOTION_DATABASE_ID
        self.course_db_id = COURSE_DATABASE_ID
        self.course_mapping = self._get_course_mapping()

    @sleep_and_retry
    @limits(calls=MAX_REQUESTS_PER_SECOND, period=ONE_SECOND)
    @backoff.on_exception(
        backoff.expo,
        Exception,
        max_tries=5
    )
    def _make_notion_request(self, operation_type: str, **kwargs):
        """
        Rate-limited wrapper for Notion API calls with exponential backoff.
        
        Args:
            operation_type: Type of Notion operation ('query_database', 'update_page', 'create_page')
            **kwargs: Arguments passed to the Notion API call
        
        Returns:
            Response from Notion API
        
        Raises:
            ValueError: If operation_type is invalid
        """
        if operation_type == "query_database":
            return self.notion.databases.query(**kwargs)
        elif operation_type == "update_page":
            return self.notion.pages.update(**kwargs)
        elif operation_type == "create_page":
            return self.notion.pages.create(**kwargs)
        raise ValueError(f"Unknown operation type: {operation_type}")

    def _get_course_mapping(self) -> Dict[str, str]:
        """
        Maps Canvas course IDs to Notion page UUIDs from the course database.
        
        Returns:
            Dict mapping Canvas course IDs (str) to Notion page UUIDs (str)
        """
        try:
            response = self._make_notion_request(
                "query_database",
                database_id=self.course_db_id,
                page_size=100
            )
            
            mapping = {}
            
            # Debug full response
            logger.debug(f"Full response: {response}")
            
            for page in response['results']:
                try:
                    # Get page ID and properties
                    notion_uuid = page['id']
                    properties = page['properties']
                    
                    # Debug properties
                    logger.debug(f"Page {notion_uuid} properties: {properties}")
                    
                    # Access multi-select values directly
                    canvas_ids = properties['CourseID']['multi_select']
                    logger.debug(f"Canvas IDs found: {canvas_ids}")
                    
                    # Map each selected value to this page
                    for item in canvas_ids:
                        canvas_id = item['name']  # Direct access to name
                        logger.info(f"Mapping Canvas ID {canvas_id} to page {notion_uuid}")
                        mapping[str(canvas_id)] = notion_uuid
                
                except KeyError as e:
                    logger.error(f"Missing property in page {page.get('id')}: {e}")
                    continue
                except Exception as e:
                    logger.error(f"Error processing page {page.get('id')}: {e}")
                    continue
            
            logger.info(f"Final mappings: {mapping}")
            return mapping
            
        except Exception as e:
            logger.error(f"Failed to get course mapping: {e}")
            return {}
        
    def _clean_html(self, html_content: str) -> str:
        """
        Converts HTML content to plain text and truncates to Notion's 2000 char limit.
        
        Args:
            html_content: HTML string to clean
        
        Returns:
            Cleaned and truncated plain text
        """
        if not html_content:
            return ""
        try:
            # Parse HTML and get text
            soup = BeautifulSoup(html_content, 'html.parser')
            text = soup.get_text()
            
            # Clean up whitespace
            text = re.sub(r'\s+', ' ', text).strip()
            
            return text[:2000]  # Notion's limit
        except Exception as e:
            logger.warning(f"Error cleaning HTML content: {e}")
            return html_content[:2000]

    def get_assignment_page(self, assignment_id: int):
        """
        Retrieves existing assignment page from Notion by Canvas assignment ID.
        
        Args:
            assignment_id: Canvas assignment ID
        
        Returns:
            Notion page object if found, None otherwise
        """
        try:
            response = self.notion.databases.query(
                database_id=self.database_id,
                filter={
                    "property": "AssignmentID",
                    "number": {"equals": assignment_id}
                }
            )
            results = response.get('results', [])
            return results[0] if results else None
            
        except Exception as e:
            logger.error(f"Error fetching assignment {assignment_id} from Notion: {str(e)}")
            return None

    def create_or_update_assignment(self, assignment: Assignment):
        try:
            # First check if assignment already exists by ID
            existing_page = self.get_assignment_page(assignment.id)
            
            # Convert course_id to string and look up UUID
            course_id_str = str(assignment.course_id)
            course_uuid = self.course_mapping.get(course_id_str)
            
            if not course_uuid:
                logger.warning(f"No Notion UUID found for course {course_id_str}")
                return
            
            VALID_PRIORITIES = ["Low", "Medium", "High"]
    
            # Prepare base properties
            properties = {
                "Assignment Title": {"title": [{"text": {"content": str(assignment.name)}}]},
                "AssignmentID": {"number": int(assignment.id)},
                "Description": {"rich_text": [{"text": {"content": self._clean_html(assignment.description)}}]},
                "Course": {"relation": [{"id": course_uuid}]},
                "Status": {"status": {"name": str(assignment.status)}},
                "Assignment Group": {"select": {"name": assignment.group_name}} if assignment.group_name else None,
                "Group Weight": {"number": assignment.group_weight} if assignment.group_weight is not None else None,
            }

            # Only add Priority if it's a valid value
            if assignment.priority in VALID_PRIORITIES:
                properties["Priority"] = {"select": {"name": assignment.priority}}
            else:
                properties["Priority"] = {"select": {"name": "Low"}}  # Default to Low if invalid/None

            properties = {k: v for k, v in properties.items() if v is not None}
            
            
            if assignment.grade is not None:
                try:
                    properties["Grade (%)"] = {"number": float(assignment.grade)}
                except (ValueError, TypeError):
                    logger.warning(f"Invalid grade format for assignment {assignment.name}: {assignment.grade}")
                    # Also set Mark as property
                    if assignment.mark is not None:
                        try:
                            properties["Status"] = {"status": "Mark received"}
                        except (ValueError, TypeError):
                            logger.warning(f"Invalid mark format for assignment {assignment.name}: {assignment.mark}")
    
            if existing_page:
                logger.info(f"Updating existing assignment: {assignment.name}")
                # Get current status from existing page
                current_status = existing_page["properties"]["Status"]["status"]["name"]
                
                # Check for "Dont show" or "In progress" status
                if current_status == "Dont show":
                    logger.info(f"Skipping update for {assignment.name} due to 'Dont show' status")
                    return
                elif current_status == "In progress":
                    logger.info(f"Preserving 'In progress' status for {assignment.name}")
                    # Remove status from properties to preserve existing status
                    properties.pop("Status", None)
                else:
                    #only updating statis if status is not set to Don't show or In progress
                    graded = assignment.grade is not None
                    if graded:
                        properties["Status"] = {"status": {"name": "Mark received"}}
                
                # Update existing page
                self._make_notion_request(
                    "update_page",
                    page_id=existing_page["id"],
                    properties=properties
                )
            else:
                # Before creating new page, double check no duplicate exists
                double_check = self.notion.databases.query(
                    database_id=self.database_id,
                    filter={
                        "property": "AssignmentID",
                        "number": {"equals": assignment.id}
                    }
                )
                
                if double_check.get('results'):
                    logger.warning(f"Duplicate prevention: Found existing assignment with ID {assignment.id}")
                    # Recursively call update on the found page
                    return self.create_or_update_assignment(assignment)
                
                # If truly new, create page
                logger.info(f"Creating new assignment: {assignment.name}")
                self._make_notion_request(
                    "create_page",
                    parent={"database_id": self.database_id},
                    properties=properties
                )
                
        except Exception as e:
            logger.error(f"Error syncing assignment {assignment.name} to Notion: {str(e)}")
            raise