"""Agents package for iterative software architecture design orchestration."""

import os
import site
import sys

# Ensure user site-packages and Roaming site-packages are discovered regardless of virtualenv isolation
user_site = site.getusersitepackages()
if os.path.exists(user_site) and user_site not in sys.path:
    sys.path.insert(0, user_site)

appdata = os.environ.get("APPDATA")
if appdata:
    py_ver = f"Python{sys.version_info.major}{sys.version_info.minor}"
    roaming_site = os.path.join(appdata, "Python", py_ver, "site-packages")
    if os.path.exists(roaming_site) and roaming_site not in sys.path:
        sys.path.insert(0, roaming_site)

from .qwen_controller import QwenTrafficController
from .browser_automator import BrowserAutomator
from .repo_manager import WorkspaceRepoManager

__all__ = ["QwenTrafficController", "BrowserAutomator", "WorkspaceRepoManager"]
