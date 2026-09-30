"""Shared test setup.

The app's settings require Jira, Ollama and Groq credentials. Tests never call
those services, so placeholder values are set here before any app module reads
its settings, and mapping uses the offline keyword strategy.
"""

import os

os.environ.update(
    {
        "JIRA_URL": "http://jira.invalid",
        "JIRA_EMAIL": "test@example.com",
        "JIRA_API_TOKEN": "test-token",
        "OLLAMA_API_KEY": "test-key",
        "GROQ_API_KEY": "test-key",
        "MAPPING_TYPE": "keyword",
        "SWAGGER_URL": "",
    }
)
