import logging
import re
from pathlib import Path

import requests
import toml
from packaging.version import InvalidVersion, parse

from config import load_config

logger = logging.getLogger(__name__)


def clean_release_notes(text: str) -> str:
    """Strips images, HTML artifacts, and excessive whitespace from GitHub release notes."""
    if not text:
        return "No release notes available."
    
    # Strip HTML comments (e.g., <!-- ... -->)
    text = re.sub(r'<!--.*?-->', '', text, flags=re.DOTALL)
    
    # Strip HTML image tags (e.g., <img width="1920" src="..." />)
    text = re.sub(r'<img[^>]*>', '', text, flags=re.IGNORECASE)
    
    # Strip standard Markdown images: ![alt](url)
    text = re.sub(r'!\[.*?\]\(.*?\)', '', text)
    
    # Clean up excessive empty lines left behind by stripped images
    text = re.sub(r'\n{3,}', '\n\n', text)
    
    return text.strip()


def check_for_update():
    """Check GitHub releases API and return update details if a newer version exists."""
    try:
        current_config = load_config()
        github_token = current_config.get("github_token", "")

        config_path = Path("pyproject.toml")
        if not config_path.exists():
            logger.error("pyproject.toml not found for update check.")
            return {"update_available": False}

        with config_path.open("r", encoding="utf-8") as f:
            config = toml.load(f)

        current_version = config.get("project", {}).get("version")
        if not current_version:
            logger.error("Version not found in pyproject.toml.")
            return {"update_available": False}

        api_url = "https://api.github.com/repos/Mark-Shun/scene-scout/releases/latest"
        headers = {"Accept": "application/vnd.github+json"}
        if github_token:
            headers["Authorization"] = f"token {github_token}"

        resp = requests.get(api_url, headers=headers, timeout=5)
        resp.raise_for_status()

        data = resp.json()
        latest_version = data.get("tag_name", "")
        if not latest_version:
            logger.error("GitHub release response missing tag_name.")
            return {"update_available": False}

        latest_version = latest_version.lstrip("v")
        current_version_clean = str(current_version).lstrip("v")

        try:
            current_parsed = parse(current_version_clean)
            latest_parsed = parse(latest_version)
        except InvalidVersion as exc:
            logger.error("Invalid version format during update check: %s", exc)
            return {"update_available": False}

        if latest_parsed > current_parsed:
            raw_body = data.get("body", "No release notes available.")
            
            return {
                "update_available": True,
                "current_version": current_version,
                "latest_version": latest_version,
                "url": "https://github.com/Mark-Shun/scene-scout/releases/latest",
                "notes": clean_release_notes(raw_body)
            }
            
        return {"update_available": False}

    except requests.RequestException as exc:
        logger.error("Update check failed due to network or GitHub API error: %s", exc)
        return {"update_available": False}
    except Exception:
        logger.exception("Unexpected error during update check.")
        return {"update_available": False}