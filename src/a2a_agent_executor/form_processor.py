import logging
import os
import re

from langchain.prompts import PromptTemplate
from langchain.output_parsers.json import SimpleJsonOutputParser
from llm_factory import create_llm
from typing import Dict, Any, Optional
from .database import fetch_form_by_id, fetch_workitem_by_id

logger = logging.getLogger(__name__)

model = create_llm(model="gpt-4o", streaming=True)

class CustomJsonOutputParser(SimpleJsonOutputParser):
    def parse(self, text: str) -> dict:
        # Extract JSON from markdown if present
        match = re.search(r'```json\n(.*?)\n```', text, re.DOTALL)
        if match:
            text = match.group(1)
        else:
            raise ValueError("No JSON content found within backticks.")
        
        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            raise ValueError(f"Invalid JSON: {str(e)}")
parser = CustomJsonOutputParser()

output_prompt = PromptTemplate.from_template(
"""
You are a data extraction assistant. Your task is to extract structured information from the given text and format it according to the provided form schema.

## Input Information

### Result Text (contains the data to extract):
{result_text}

### Input Text (reference information, use when available):
{input_text}

### Form HTML (for reference):
{form_html}

### Form Fields (schema definition):
{form_fields}

## Instructions

1. Analyze the "Form Fields" array to understand the structure:
   - Each field has a "key" (the field identifier to use in output)
   - Each field has a "text" (the human-readable label, may be in Korean)
   - Each field has a "type" (e.g., "text", "date", etc.)

2. Extract relevant values from the "Result Text" that match each field:
   - **Primary source**: Extract values primarily from the "Result Text"
   - **Reference source**: Use "Input Text" as additional reference information when:
     * The Result Text is incomplete or unclear
     * You need context to better understand the Result Text
     * The Input Text contains relevant details not present in Result Text
     * The Input Text provides additional context or clarification
     * The Result Text references information that can be found in Input Text
   - Match the field labels ("text") or field keys with information in both texts
   - Use the exact "key" from the field definition as the property name in the output
   - Extract and format values according to their "type" (e.g., dates should be in YYYY-MM-DD format)
   - When Input Text is available, consider it as supplementary context to enhance extraction accuracy

3. Data extraction strategy:
   - Prioritize information from Result Text
   - Cross-reference with Input Text for missing or unclear information
   - Combine relevant details from both sources when appropriate
   - Ensure the extracted data is consistent and coherent

## Important Notes
- Use the exact field "key" values from Form Fields as property names
- Extract values accurately from the Result Text as the primary source
- Use Input Text as supplementary reference information when needed
- For date fields, use ISO format (YYYY-MM-DD)
- If a value is not found in either text, you may omit the field or use null
- The output must be valid JSON enclosed in ```json code blocks

result should be in this JSON format:
{{
    "output": {{
        "<field_key>": "<extracted_value>",
        "<field_key>": "<extracted_value>",
        ...
    }}
}}
"""
)

output_chain = (
    output_prompt | model | parser
)

async def generate_output_json(task_id: str, result_text: str, input_text: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """
    Generate output JSON data for a task using LLM.
    
    This function fetches the workitem and associated form definition from the database,
    then uses an LLM to generate structured JSON output based on the result text.
    
    Args:
        task_id: The task ID to fetch workitem information
        result_text: The result text from the agent execution
        input_text: The input text from the agent execution
        
    Returns:
        Dict[str, Any]: Generated JSON output data conforming to the form schema,
                       or None if form is not found or generation fails
    """
    try:
        workitem_data = await fetch_workitem_by_id(task_id)
        
        if not workitem_data or len(workitem_data) == 0:
            logger.warning(f"Workitem not found for task_id: {task_id}")
            return None
        
        workitem = workitem_data[0]
        logger.info(f"Retrieved workitem for task {task_id}: {workitem.get('name', 'unknown')}")
        
        form_id = workitem.get('tool').replace('formHandler:', '')
        if not form_id:
            logger.info(f"No form_id found in workitem {task_id}, skipping form generation")
            return None
        
        tenant_id = workitem.get('tenant_id', None)
        form_data = await fetch_form_by_id(form_id, tenant_id)
        
        if not form_data or len(form_data) == 0:
            logger.warning(f"Form definition not found for form_id: {form_id}")
            return None
        
        form_def = form_data[0]
        form_fields = form_def.get('fields_json', {})
        form_html = form_def.get('html', '')
        logger.info(f"Retrieved form definition: {form_def.get('name', 'unknown')}")
        
        result = await output_chain.ainvoke({
            "result_text": result_text,
            "form_html": form_html,
            "form_fields": form_fields,
            "input_text": input_text
        })
        
        if not result:
            logger.error(f"Failed to generate output JSON for task {task_id}")
            return None
        
        output_json = result.get('output', {})
        
        logger.info(f"Successfully generated output JSON for task {task_id}")
        return output_json
        
    except Exception as e:
        logger.error(f"Error generating output JSON for task {task_id}: {e}", exc_info=True)
        return None

