import requests
import toml


def check_for_update():
    """Check GitHub releases API and print update message if newer version exists."""
    try:
        # Read current version from pyproject.toml
        with open("pyproject.toml", "r") as f:
            config = toml.load(f)
        current_version = config.get("project", {}).get("version", "0.1.0")
        
        # Query GitHub API for latest release
        api_url = "https://api.github.com/repos/Mark-Shun/scene-scout/releases/latest"
        resp = requests.get(api_url, timeout=5)
        if resp.status_code != 200:
            return
        
        data = resp.json()
        latest_version = data.get("tag_name", "").lstrip("v")
        
        if not latest_version:
            return
        
        # Compare versions
        if latest_version != current_version.lstrip("v"):
            # Use /releases/latest - GitHub redirects automatically
            latest_url = "https://github.com/Mark-Shun/scene-scout/releases/latest"
            print(f"[UPDATE] Scene Scout version: {current_version}, latest version: {latest_version}\nFind the latest release over at: {latest_url}\n\n")
    except Exception:
        pass  # Fail silently - don't block startup
