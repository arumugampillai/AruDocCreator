import os
import site
import sys
import time

user_site = site.getusersitepackages()
if os.path.exists(user_site) and user_site not in sys.path:
    sys.path.insert(0, user_site)

from agents.browser_automator import BrowserAutomator


def main():
    base_dir = os.path.dirname(os.path.abspath(__file__))
    profile_dir = os.path.abspath(os.path.join(base_dir, "browser_profile"))
    gpt_url = "https://chatgpt.com/c/6a918785-fca8-83ee-8bb6-422f629090d0"

    gpt_lib_url = "https://chatgpt.com/library/d/6a723b6fe06c819199240f5a593f7ab4"
    nb_url = "https://notebook.google.com/notebook/b7a81a2b-5485-493a-bbfb-cbe58808dfc3"
    gemini_url = "https://gemini.google.com/app"
    grok_url = "https://grok.com/project/3a4c5217-9801-44e6-bb35-60f7e17eca21"

    print("Launching Google Chrome with Remote Debugging (port 9222)...")
    BrowserAutomator.launch_chrome_with_cdp(
        port=9222,
        profile_dir=profile_dir,
        urls=[gpt_url, gpt_lib_url, nb_url, gemini_url, grok_url],
    )



    print("Chrome launched on port 9222.")


if __name__ == "__main__":
    main()
