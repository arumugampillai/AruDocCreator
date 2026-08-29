"""
One-Time Google Account Login Helper for ChatGPT & Gemini.
Launches the persistent browser profile in non-headless mode so you can log into
your Google account (arumugampillaia) once. All session cookies and credentials
will be saved permanently in ./browser_profile.
"""

import asyncio
import json
import os
import site
import sys

# Ensure site-packages are discovered
user_site = site.getusersitepackages()
if os.path.exists(user_site) and user_site not in sys.path:
    sys.path.insert(0, user_site)

appdata = os.environ.get("APPDATA")
if appdata:
    py_ver = f"Python{sys.version_info.major}{sys.version_info.minor}"
    roaming_site = os.path.join(appdata, "Python", py_ver, "site-packages")
    if os.path.exists(roaming_site) and roaming_site not in sys.path:
        sys.path.insert(0, roaming_site)

from agents.browser_automator import BrowserAutomator


async def run_login_helper():
    base_dir = os.path.dirname(os.path.abspath(__file__))
    profile_dir = os.path.abspath(os.path.join(base_dir, "browser_profile"))
    print("=" * 70)
    print("Google Account Login Helper for ChatGPT & Gemini")
    print("=" * 70)
    print(f"Target Profile Directory: {profile_dir}")
    print("\nLaunching browser in visible mode...")

    automator = BrowserAutomator({"browser": {"headless": False, "user_data_dir": profile_dir}})
    await automator.start(override_headless=False)


    print("\n[STEP 1] Opening ChatGPT (https://chatgpt.com)...")
    page1 = await automator.context.new_page()
    await page1.goto("https://chatgpt.com")

    print("[STEP 2] Opening NotebookLM (https://notebooklm.google.com/notebook/b7a81a2b-5485-493a-bbfb-cbe58808dfc3)...")
    page2 = await automator.context.new_page()
    await page2.goto("https://notebooklm.google.com/notebook/b7a81a2b-5485-493a-bbfb-cbe58808dfc3")

    print("[STEP 3] Opening Gemini (https://gemini.google.com/app)...")
    page3 = await automator.context.new_page()
    await page3.goto("https://gemini.google.com/app")

    print("\n" + "#" * 70)
    print(">>> ACTION REQUIRED IN THE OPENED BROWSER WINDOW:")
    print("1. Click 'Log in' on ChatGPT and select 'Continue with Google' (arumugampillaia).")
    print("2. Make sure you are signed into NotebookLM with your Google account.")
    print("3. Once you see the chat interface on all tabs, return here.")
    print("#" * 70)


    try:
        input("\nPress [ENTER] here in the terminal once you have completed login on both tabs: ")
    except Exception:
        await asyncio.sleep(60)

    print("\nSaving session cookies and persistent state...")
    await automator.stop()
    print("[SUCCESS] All authentication tokens are now saved in ./browser_profile.")
    print("You can now run 'python gui_app.py' or 'python orchestrator.py' without logging in again!")


if __name__ == "__main__":
    asyncio.run(run_login_helper())
