import sys
import logging
import re
from pathlib import Path

import requests
import toml
from packaging.version import InvalidVersion, parse

from config import load_config

logger = logging.getLogger(__name__)


def extract_image_url(text: str) -> str:
    """Extracts the first markdown or HTML image URL from the text."""
    md_match = re.search(r'!\[.*?\]\((.*?)\)', text)
    if md_match:
        return md_match.group(1)

    html_match = re.search(r'<img[^>]+src=["\'](.*?)["\']', text, flags=re.IGNORECASE)
    if html_match:
        return html_match.group(1)

    return ""


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
    """Check GitHub releases API and return aggregated update details if newer versions exist."""
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

        # Fetch all releases (newest first, up to 30)
        api_url = "https://api.github.com/repos/Mark-Shun/scene-scout/releases"
        headers = {"Accept": "application/vnd.github+json"}
        if github_token:
            headers["Authorization"] = f"token {github_token}"

        resp = requests.get(api_url, headers=headers, timeout=5)
        resp.raise_for_status()

        data = resp.json()
        if not data or not isinstance(data, list):
            return {"update_available": False}

        latest_release = data[0]
        latest_version = latest_release.get("tag_name", "")
        if not latest_version:
            return {"update_available": False}

        latest_version_clean = latest_version.lstrip("v")
        current_version_clean = str(current_version).lstrip("v")

        try:
            current_parsed = parse(current_version_clean)
            latest_parsed = parse(latest_version_clean)
        except InvalidVersion as exc:
            logger.error("Invalid version format during update check: %s", exc)
            return {"update_available": False}

        if latest_parsed > current_parsed:
            # Aggregate release notes for all versions newer than current
            aggregated_notes = []
            for release in data:
                rel_ver_str = release.get("tag_name", "").lstrip("v")
                try:
                    rel_parsed = parse(rel_ver_str)
                    if rel_parsed > current_parsed:
                        raw_body = release.get("body", "No release notes available.")
                        cleaned_body = clean_release_notes(raw_body)
                        aggregated_notes.append(f"## Version {release.get('tag_name')}\n{cleaned_body}")
                except InvalidVersion:
                    continue

            final_notes = "\n\n---\n\n".join(aggregated_notes)

            # Extract download links from the latest release only
            is_compiled = getattr(sys, 'frozen', False)
            download_url = ""
            is_source_zip = False

            if is_compiled:
                assets = latest_release.get("assets", [])
                for asset in assets:
                    name = asset.get("name", "").lower()
                    if sys.platform == 'win32' and name.endswith('.exe'):
                        download_url = asset.get("browser_download_url")
                        break
                    elif sys.platform == 'darwin' and (name.endswith('.dmg') or name.endswith('.app.zip')):
                        download_url = asset.get("browser_download_url")
                        break
                    elif sys.platform.startswith('linux') and name.endswith('.appimage'):
                        download_url = asset.get("browser_download_url")
                        break

            if not download_url:
                download_url = latest_release.get("zipball_url", "")
                is_source_zip = True

            # Extract image from the absolute latest release body
            latest_raw_body = latest_release.get("body", "")
            image_url = extract_image_url(latest_raw_body)
            image_bytes = None
            if image_url:
                try:
                    img_resp = requests.get(image_url, timeout=5)
                    if img_resp.status_code == 200:
                        image_bytes = img_resp.content
                except requests.RequestException as e:
                    logger.warning(f"Failed to fetch update image: {e}")

            return {
                "update_available": True,
                "current_version": current_version,
                "latest_version": latest_version_clean,
                "url": "https://github.com/Mark-Shun/scene-scout/releases/latest",
                "download_url": download_url,
                "is_source_zip": is_source_zip,
                "notes": final_notes,
                "image_bytes": image_bytes,
            }

        return {"update_available": False}

    except requests.RequestException as exc:
        logger.error("Update check failed due to network or GitHub API error: %s", exc)
        return {"update_available": False}
    except Exception:
        logger.exception("Unexpected error during update check.")
        return {"update_available": False}