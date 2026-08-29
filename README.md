# AruDocCreator

**Iterative Multi-Model Architecture & Implementation Plan Synthesizer**

A standalone, local Python/PySide6 application that orchestrates multi-model adversarial reviews across **ChatGPT**, **Gemini**, and **Google NotebookLM** to iteratively produce, critique, and refine software architecture specifications and implementation plans with automated Git versioning.

---

## Architecture Overview

```
                          ┌───────────────────────────┐
                          │   AruDocCreator Desktop   │
                          │        (gui_app.py)       │
                          └─────────────┬─────────────┘
                                        │
             ┌──────────────────────────┼──────────────────────────┐
             ▼                          ▼                          ▼
    ┌─────────────────┐        ┌─────────────────┐        ┌─────────────────┐
    │  Frontier Model │        │   Dual Review   │        │  Local Version  │
    │  (ChatGPT Web)  │        │  (Gemini + NLM) │        │  Control (Git)  │
    └─────────────────┘        └─────────────────┘        └─────────────────┘
```

- **Traffic Controller & Coordinator**: Local prompt coordination via Ollama / Qwen or deterministic templates.
- **Persistent Browser Automation**: Automated execution via Playwright connecting to authenticated browser sessions.
- **Git-Backed Workspace**: Every iteration is saved and committed to `./workspace/` with Conventional Commits (`architecture_v*.md`, `final-done-by-chatgpt.md`).

---

## Quick Start

### 1. Installation

```bash
cd C:\Users\admin\PycharmProjects\AruDocCreator
pip install -r requirements.txt
python -m playwright install chromium
```

### 2. Launch GUI Application

```bash
python gui_app.py
```

### 3. Launch CLI Orchestrator (Optional)

```bash
python orchestrator.py
```

---

## Project Structure

```text
AruDocCreator/
├── gui_app.py                     # Main PySide6 Desktop GUI Application
├── orchestrator.py                # Standalone CLI Orchestrator
├── open_browser_window.py         # Chrome CDP launcher helper
├── login_browser.py               # Standalone browser authentication helper
├── launch_chrome_with_debug.bat   # Windows batch launcher (Port 9222)
├── config.json                    # Application configuration & prompts
├── requirements.txt               # Standalone package dependencies
├── README.md                      # Project documentation
├── DOCUMENTATION.md               # Complete architectural guide
│
├── agents/                        # Core automation & controller modules
│   ├── __init__.py
│   ├── browser_automator.py       # Playwright browser controller
│   ├── qwen_controller.py         # Prompt engineering & Ollama coordinator
│   └── repo_manager.py            # Local Git workspace version manager
│
├── workspace/                     # Output markdown specifications & Git history
│   └── .git/
│
├── browser_profile/               # Isolated persistent Chrome user data profile
│
└── tests/
    └── test_orchestrator_components.py  # Unit tests
```
