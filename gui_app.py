import asyncio
import json
import logging
import os
import re
import site
import subprocess
import sys
import time
from datetime import datetime
from typing import Optional, Dict, Any, List

# Ensure user site-packages are discovered across all execution environments
user_site = site.getusersitepackages()
if os.path.exists(user_site) and user_site not in sys.path:
    sys.path.insert(0, user_site)

appdata = os.environ.get("APPDATA")
if appdata:
    py_ver = f"Python{sys.version_info.major}{sys.version_info.minor}"
    roaming_site = os.path.join(appdata, "Python", py_ver, "site-packages")
    if os.path.exists(roaming_site) and roaming_site not in sys.path:
        sys.path.insert(0, roaming_site)

from PySide6.QtCore import Qt, QThread, Signal, QObject, QSize
from PySide6.QtGui import QFont, QColor, QTextCursor, QIcon
from PySide6.QtWidgets import (
    QApplication,
    QMainWindow,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QGridLayout,
    QSplitter,
    QLabel,
    QLineEdit,
    QTextEdit,
    QPlainTextEdit,
    QTextBrowser,
    QPushButton,
    QComboBox,
    QCheckBox,
    QTabWidget,
    QListWidget,
    QListWidgetItem,
    QTableWidget,
    QTableWidgetItem,
    QHeaderView,
    QProgressBar,
    QFrame,
    QMessageBox,
    QStatusBar,
    QGroupBox,
    QScrollArea,
    QDialog,
    QFormLayout,
    QFileDialog,
)

from agents.qwen_controller import QwenTrafficController
from agents.browser_automator import BrowserAutomator
from agents.repo_manager import WorkspaceRepoManager


def extract_document_version(doc_content: str, fallback_v: int = 1, doc_filename: str = "") -> str:
    """Inspects document markdown headers or filename to extract authoritative version string.
    Returns formatted version string like 'v3.0.0', 'v2', etc.
    """
    import re
    if doc_content:
        # Match patterns like: Version: 3.0.0 or Version: 3 or Version: v3.0.0 or **Version**: `3.0.0`
        match = re.search(r"(?:Version|Ver|Revision)[:\s*`]+v?(\d+(?:\.\d+)*)", doc_content[:2000], re.IGNORECASE)
        if match:
            v_num = match.group(1).strip()
            return f"v{v_num}"
    if doc_filename:
        match_fn = re.search(r"[_\-\.](v\d+(?:\.\d+)*)", doc_filename, re.IGNORECASE)
        if match_fn:
            return match_fn.group(1).lower()
    return f"v{fallback_v}"


class PhaseGatekeeper:
    """Strict execution gateways between orchestration phases to guarantee data integrity."""

    @staticmethod
    def validate_phase_1_outputs(gemini_text: str, nlm_text: str, log_signal=None) -> bool:
        if len(gemini_text.strip()) < 200:
            msg = f"Phase 1 Gatekeeper Error: Gemini response is too short ({len(gemini_text.strip())} chars < 200)."
            if log_signal:
                log_signal.emit(msg, "error")
            raise ValueError(msg)
        if len(nlm_text.strip()) < 60:
            msg = f"Phase 1 Gatekeeper Error: NotebookLM response is too short ({len(nlm_text.strip())} chars < 60)."
            if log_signal:
                log_signal.emit(msg, "error")
            raise ValueError(msg)

        forbidden_badges = ["consulting your sources", "searching sources", "reading sources", "thinking..."]
        if any(badge in nlm_text.lower() for badge in forbidden_badges):
            msg = "Phase 1 Gatekeeper Error: NotebookLM returned transient status text."
            if log_signal:
                log_signal.emit(msg, "error")
            raise ValueError(msg)
        return True

    @staticmethod
    def validate_phase_2_output(chatgpt_text: str, expected_version: str = "", log_signal=None) -> bool:
        if len(chatgpt_text.strip()) < 300:
            msg = f"Phase 2 Gatekeeper Error: ChatGPT consolidated output is too short ({len(chatgpt_text.strip())} chars < 300)."
            if log_signal:
                log_signal.emit(msg, "error")
            raise ValueError(msg)
        return True


class OrchestrationWorker(QThread):


    """
    Background worker running Stage 1 (ChatGPT) and Stage 2 (NotebookLM) pipelines
    using exact user-specified prompts without any LLM alteration.
    """

    log_signal = Signal(str, str)  # message, level: info, success, warning, error
    status_signal = Signal(str)
    progress_signal = Signal(int)
    doc_updated_signal = Signal(str, str)  # doc_title, content
    critique_updated_signal = Signal(str)  # critique_text
    history_updated_signal = Signal(list)  # list of (hash, date, summary)
    finished_signal = Signal(bool, str)  # success, final_message
    decision_prompt_signal = Signal(int, dict)  # next_v, stability_dict

    def __init__(
        self,
        config: dict,
        user_input: str,
        is_doc_mode: bool,
        doc_filename: str,
        doc_content: str,
        stage1_prompt: str,
        stage2_prompt: str,
        stage3_prompt: str = "",
        prompt_gemini: str = "",
        critic_service: str = "notebooklm",
        chatgpt_url: str = "",
        chatgpt_lib_url: str = "",
        gemini_url: str = "",
        grok_url: str = "",
        claude_url: str = "",
        copilot_url: str = "",
        max_iterations: int = 1,
        plan_agent1: str = "ChatGPT",
        plan_agent2: str = "Gemini",
        plan_agent3: str = "NotebookLM",
        plan_agent4: str = "ChatGPT",
        is_plan_mode: bool = False,
        auto_freeze: bool = True,
        fresh_session: bool = False,
        only_stage2: bool = False,
    ):
        super().__init__()
        self.config = config
        self.user_input = user_input
        self.is_doc_mode = is_doc_mode
        self.doc_filename = doc_filename
        self.doc_content = doc_content
        self.stage1_prompt = stage1_prompt
        self.stage2_prompt = stage2_prompt
        self.stage3_prompt = stage3_prompt
        self.prompt_gemini = prompt_gemini
        self.critic_service = critic_service
        self.chatgpt_url = chatgpt_url
        self.chatgpt_lib_url = chatgpt_lib_url
        self.gemini_url = gemini_url
        self.grok_url = grok_url
        self.claude_url = claude_url
        self.copilot_url = copilot_url
        self.max_iterations = max_iterations
        self.plan_agent1 = plan_agent1
        self.plan_agent2 = plan_agent2
        self.plan_agent3 = plan_agent3
        self.plan_agent4 = plan_agent4
        self.is_plan_mode = is_plan_mode

        self.auto_freeze = auto_freeze
        self.fresh_session = fresh_session
        self.only_stage2 = only_stage2
        self.is_cancelled = False
        self.controller: Optional[QwenTrafficController] = None
        self.automator: Optional[BrowserAutomator] = None
        self.repo_manager: Optional[WorkspaceRepoManager] = None

    async def _query_agent(self, agent_name: str, prompt: str, target_doc_name: str = "") -> str:
        """Query any selected agent (ChatGPT, NotebookLM, Gemini, Grok, Claude, Copilot) with the given prompt."""
        agent = (agent_name or "chatgpt").lower().strip()
        if agent == "gemini":
            gemini_url = self.gemini_url or self.config.get("services", {}).get("gemini", {}).get("url", self.automator.gemini_url)
            return await self.automator.query_gemini(prompt, new_chat=False, gemini_url=gemini_url)
        elif agent == "notebooklm":
            notebook_url = self.config.get("services", {}).get("notebooklm", {}).get("url", self.automator.notebooklm_url)
            return await self.automator.query_notebooklm(prompt=prompt, notebook_url=notebook_url)
        elif agent == "grok":
            grok_url = self.grok_url or self.config.get("services", {}).get("grok", {}).get("url", self.automator.grok_url)
            return await self.automator.query_grok(prompt, new_chat=False, grok_url=grok_url)
        elif agent == "claude":
            claude_url = self.claude_url or self.config.get("services", {}).get("claude", {}).get("url", self.automator.claude_url)
            return await self.automator.query_claude(prompt, new_chat=False, claude_url=claude_url)
        elif agent == "copilot":
            copilot_url = self.copilot_url or self.config.get("services", {}).get("copilot", {}).get("url", self.automator.copilot_url)
            return await self.automator.query_copilot(prompt, new_chat=False, copilot_url=copilot_url)
        else:  # default to chatgpt
            chatgpt_url = self.chatgpt_url or self.config.get("services", {}).get("chatgpt", {}).get("url", self.automator.chatgpt_url)
            return await self.automator.query_chatgpt(prompt, new_chat=False, chatgpt_url=chatgpt_url)

    def cancel(self):
        self.is_cancelled = True

    def run(self):
        asyncio.run(self._execute_pipeline())

    async def _execute_pipeline(self):
        try:
            # 1. Subsystem setup
            self.log_signal.emit("Initializing Orchestrator subsystems...", "info")
            self.progress_signal.emit(5)
            self.status_signal.emit("Checking Ollama & Browser...")

            ollama_cfg = self.config.get("ollama", {})
            self.controller = QwenTrafficController(
                base_url=ollama_cfg.get("base_url", "http://localhost:11434"),
                model=ollama_cfg.get("model", "qwen3:4b"),
                fallback_model=ollama_cfg.get("fallback_model", "qwen2.5:3b"),
                temperature=ollama_cfg.get("temperature", 0.2),
                timeout_seconds=ollama_cfg.get("timeout_seconds", 120),
            )

            workspace_cfg = self.config.get("workspace", {})
            self.repo_manager = WorkspaceRepoManager(
                workspace_dir=workspace_cfg.get("dir", "./workspace"),
                filename_prefix=workspace_cfg.get("filename_prefix", "architecture_v"),
            )

            # If fresh session is requested and not Stage 2 only, reset workspace
            if self.fresh_session and not self.only_stage2:
                self.repo_manager.reset_workspace(archive=True)
                self.log_signal.emit("Started fresh workspace session (previous revisions archived).", "info")

            self.automator = BrowserAutomator(self.config)

            # Check Ollama status
            health = self.controller.check_health()
            if health.get("status") == "online":
                self.log_signal.emit(
                    f"Ollama online (Coordinator mode). Active model: {health.get('active_model')}", "success"
                )
            else:
                self.log_signal.emit(
                    "Ollama offline. Standard coordinator mode active.", "info"
                )

            # Launch Playwright / Connect to CDP Chrome
            self.log_signal.emit("Connecting to browser session...", "info")
            self.status_signal.emit("Connecting to browser...")
            self.progress_signal.emit(15)
            await self.automator.start()
            self.log_signal.emit("Browser session initialized.", "success")

            if self.is_cancelled:
                return

            # -------------------------------------------------------------
            # DIRECT STAGE 2 ONLY EXECUTION
            # -------------------------------------------------------------
            # -------------------------------------------------------------
            # DIRECT STAGE 2 ONLY EXECUTION
            # -------------------------------------------------------------
            if self.only_stage2:
                current_v = self.repo_manager.get_latest_version_number(self.doc_filename)
                if current_v > 0:
                    doc_title = self.repo_manager.get_filename(current_v, self.doc_filename)
                    current_doc = self.repo_manager.get_file_content(doc_title) or self.doc_content
                    latest_file_path = os.path.join(self.repo_manager.workspace_dir, doc_title)
                else:
                    current_doc = self.doc_content
                    doc_title = self.repo_manager.get_filename(1, self.doc_filename)
                    latest_file_path = self.repo_manager.save_revision(
                        version=1,
                        content=self.doc_content,
                        commit_message=f"feat: load {self.doc_filename} for Stage 2",
                        doc_filename=self.doc_filename,
                    )

                self.doc_updated_signal.emit(doc_title, current_doc)
                self.log_signal.emit(f"--- Running Stage 2 Only (NotebookLM) on '{doc_title}' ---", "info")
                self.status_signal.emit(f"Running Stage 2 critique on NotebookLM for {doc_title}...")
                self.progress_signal.emit(50)

                notebook_url = (
                    self.config.get("services", {})
                    .get("notebooklm", {})
                    .get("url", self.automator.notebooklm_url)
                )
                self.log_signal.emit(
                    f"Sending exact Stage 2 prompt + '{doc_title}' into NotebookLM ({notebook_url})...",
                    "info",
                )
                critique_feedback = await self.automator.upload_source_and_review_in_notebooklm(
                    notebook_url=notebook_url,
                    file_path=latest_file_path,
                    doc_title=doc_title,
                    doc_prefix_or_num=self.doc_filename,
                    custom_review_prompt=self.stage2_prompt,
                )
                self.log_signal.emit(
                    f"Stage 2 critique feedback received ({len(critique_feedback)} chars).", "success"
                )
                self.critique_updated_signal.emit(critique_feedback)
                self.finished_signal.emit(True, f"Stage 2 NotebookLM evaluation for {doc_title} completed successfully.")
                self.progress_signal.emit(100)
                self.status_signal.emit("Ready")
                return

            # -------------------------------------------------------------
            # PLAN MODE: Tri-Model Multi-Iteration Flow (ChatGPT Aggregator + Gemini & NotebookLM Critics)
            # -------------------------------------------------------------
            if self.is_plan_mode:
                prefix = self.doc_filename.replace(".md", "") if self.doc_filename else "F-plan"
                current_doc = self.doc_content
                self.log_signal.emit(
                    f"Starting Tri-Model Implementation Plan Orchestration on '{self.doc_filename}' ({self.max_iterations} iterations)...",
                    "info",
                )
                self.doc_updated_signal.emit(f"{prefix}-source.md", current_doc)
                self.repo_manager.save_named_file(
                    filename=f"{prefix}-source.md",
                    content=current_doc,
                    commit_message=f"docs(plan): load initial implementation plan {self.doc_filename}",
                )

                # Check and identify document version from markdown headers or filename
                version_str = extract_document_version(current_doc, fallback_v=1, doc_filename=self.doc_filename)
                base_no_ext = self.doc_filename.rsplit(".", 1)[0] if "." in self.doc_filename else self.doc_filename

                if version_str.lower() not in base_no_ext.lower() and f"_{version_str}" not in base_no_ext:
                    versioned_doc_filename = f"{base_no_ext}_{version_str}.md"
                else:
                    versioned_doc_filename = self.doc_filename

                self.log_signal.emit(
                    f"Document version identified: '{version_str}'. Prepared target filename: '{versioned_doc_filename}'",
                    "info",
                )

                # Collect only the unique agents selected across all 4 prompt boxes
                active_agents = {
                    (self.plan_agent1 or "").strip().lower(),
                    (self.plan_agent2 or "").strip().lower(),
                    (self.plan_agent3 or "").strip().lower(),
                    (self.plan_agent4 or "").strip().lower(),
                }
                active_names = ", ".join(sorted([a.title() for a in active_agents if a]))
                self.log_signal.emit(
                    f"Active pipeline agents for document synchronization: {active_names}",
                    "info",
                )

                temp_source_file = os.path.join(self.repo_manager.workspace_dir, versioned_doc_filename)
                with open(temp_source_file, "w", encoding="utf-8", errors="replace") as f:
                    f.write(current_doc)

                # STEP 0a: Update selected F-document in NotebookLM sources (ONLY if NotebookLM is selected)
                if "notebooklm" in active_agents:
                    notebook_url = self.config.get("services", {}).get("notebooklm", {}).get("url", self.automator.notebooklm_url)
                    self.log_signal.emit(
                        f"Step 0a: Updating NotebookLM sources with '{versioned_doc_filename}'...",
                        "info",
                    )
                    self.status_signal.emit(f"Uploading {versioned_doc_filename} to NotebookLM...")
                    self.progress_signal.emit(5)

                    upload_ok = await self.automator.upload_source_to_notebooklm(
                        notebook_url=notebook_url,
                        file_path=temp_source_file,
                        doc_title=versioned_doc_filename,
                    )
                    if upload_ok:
                        self.log_signal.emit(
                            f"NotebookLM updated successfully with '{versioned_doc_filename}'.",
                            "success",
                        )
                    else:
                        self.log_signal.emit(
                            f"Notice: NotebookLM source upload step completed for '{versioned_doc_filename}'.",
                            "info",
                        )

                # STEP 0b: Update selected F-document in ChatGPT Library/Project folder (ONLY if ChatGPT is selected)
                gpt_lib_url = (self.chatgpt_lib_url or self.config.get("services", {}).get("chatgpt", {}).get("library_url", "")).strip()
                if "chatgpt" in active_agents and gpt_lib_url:
                    self.log_signal.emit(
                        f"Step 0b: Updating ChatGPT library folder ({gpt_lib_url}) with '{versioned_doc_filename}'...",
                        "info",
                    )
                    self.status_signal.emit(f"Uploading {versioned_doc_filename} to ChatGPT Library...")
                    self.progress_signal.emit(8)

                    gpt_lib_ok = await self.automator.upload_source_to_chatgpt_library(
                        library_url=gpt_lib_url,
                        file_path=temp_source_file,
                        doc_title=versioned_doc_filename,
                    )
                    if gpt_lib_ok:
                        self.log_signal.emit(
                            f"ChatGPT library updated successfully with '{versioned_doc_filename}'.",
                            "success",
                        )
                    else:
                        self.log_signal.emit(
                            f"Notice: ChatGPT library update attempt completed. Proceeding to review cycles...",
                            "info",
                        )

                # STEP 0c: Update selected F-document in Grok Project files (ONLY if Grok is selected)
                grok_proj_url = (self.grok_url or self.config.get("services", {}).get("grok", {}).get("url", "")).strip()
                if "grok" in active_agents and grok_proj_url and "project" in grok_proj_url:
                    self.log_signal.emit(
                        f"Step 0c: Updating Grok Project ({grok_proj_url}) with '{versioned_doc_filename}'...",
                        "info",
                    )
                    self.status_signal.emit(f"Uploading {versioned_doc_filename} to Grok Project...")
                    self.progress_signal.emit(9)

                    grok_proj_ok = await self.automator.upload_source_to_grok_project(
                        project_url=grok_proj_url,
                        file_path=temp_source_file,
                        doc_title=versioned_doc_filename,
                    )
                    if grok_proj_ok:
                        self.log_signal.emit(
                            f"Grok Project updated successfully with '{versioned_doc_filename}'.",
                            "success",
                        )
                    else:
                        self.log_signal.emit(
                            f"Notice: Grok Project update step completed for '{versioned_doc_filename}'.",
                            "info",
                        )

                self.progress_signal.emit(10)


                target_doc_name = versioned_doc_filename

                for iter_idx in range(1, self.max_iterations + 1):
                    if self.is_cancelled:
                        break

                    self.log_signal.emit(
                        f"=== Iteration {iter_idx}/{self.max_iterations}: Dual Review ({self.plan_agent2} & {self.plan_agent3}) ===",
                        "info",
                    )

                    # 1. Reviewer 1 (self.plan_agent2)
                    self.status_signal.emit(f"[Iter {iter_idx}/{self.max_iterations}] Querying {self.plan_agent2} for critique...")
                    self.progress_signal.emit(int(10 + (iter_idx - 1) * 80 / self.max_iterations + 10 / self.max_iterations))
                    agent2_instruction = (
                        self.prompt_gemini.strip()
                        if (self.prompt_gemini and len(self.prompt_gemini.strip()) > 0)
                        else self.stage2_prompt.strip()
                    )
                    agent2_prompt = f"{agent2_instruction}\n\nDocument Under Review: {target_doc_name}"
                    self.log_signal.emit(f"[Iter {iter_idx}] Sending prompt + '{target_doc_name}' to {self.plan_agent2}...", "info")
                    agent2_critique = await self._query_agent(self.plan_agent2, agent2_prompt, target_doc_name)

                    agent2_filename = f"{prefix}-iter{iter_idx}-{self.plan_agent2.lower()}-critique.md"
                    self.repo_manager.save_named_file(
                        filename=agent2_filename,
                        content=agent2_critique,
                        commit_message=f"docs({self.plan_agent2.lower()}): iteration {iter_idx}/{self.max_iterations} review -> {agent2_filename}",
                    )
                    self.log_signal.emit(f"[Iter {iter_idx}] {self.plan_agent2} critique received ({len(agent2_critique)} chars).", "success")

                    # 2. Reviewer 2 (self.plan_agent3)
                    self.status_signal.emit(f"[Iter {iter_idx}/{self.max_iterations}] Querying {self.plan_agent3} for cross-document evaluation...")
                    self.progress_signal.emit(int(10 + (iter_idx - 1) * 80 / self.max_iterations + 35 / self.max_iterations))
                    agent3_instruction = self.stage2_prompt.strip()
                    agent3_prompt = f"{agent3_instruction}\n\nDocument Under Review: {target_doc_name}"

                    self.log_signal.emit(f"[Iter {iter_idx}] Sending prompt + '{target_doc_name}' to {self.plan_agent3}...", "info")
                    agent3_critique = await self._query_agent(self.plan_agent3, agent3_prompt, target_doc_name)

                    agent3_filename = f"{prefix}-iter{iter_idx}-{self.plan_agent3.lower()}-critique.md"
                    self.repo_manager.save_named_file(
                        filename=agent3_filename,
                        content=agent3_critique,
                        commit_message=f"docs({self.plan_agent3.lower()}): iteration {iter_idx}/{self.max_iterations} evaluation -> {agent3_filename}",
                    )
                    self.log_signal.emit(f"[Iter {iter_idx}] {self.plan_agent3} evaluation received ({len(agent3_critique)} chars).", "success")

                    # GATE 1 ➔ 2: Validate Phase 1 outputs
                    PhaseGatekeeper.validate_phase_1_outputs(agent2_critique, agent3_critique, self.log_signal)

                    # Emit combined dual critique to GUI
                    combined_critique = (
                        f"# Dual Critic Evaluation — Iteration {iter_idx}/{self.max_iterations}\n\n"
                        f"## Target Document: {target_doc_name}\n\n"
                        f"## 1. {self.plan_agent2} Review Critique\n{agent2_critique}\n\n"
                        f"---\n\n"
                        f"## 2. {self.plan_agent3} Cross-Document Evaluation\n{agent3_critique}"
                    )
                    self.critique_updated_signal.emit(combined_critique)

                    if self.is_cancelled:
                        break

                    # 3. Aggregator & Refiner (self.plan_agent4)
                    self.status_signal.emit(f"[Iter {iter_idx}/{self.max_iterations}] {self.plan_agent4} Aggregator synthesizing & refining plan...")
                    self.progress_signal.emit(int(10 + (iter_idx - 1) * 80 / self.max_iterations + 70 / self.max_iterations))
                    user_aggregator_instruction = (
                        self.stage3_prompt.strip()
                        if (self.stage3_prompt and len(self.stage3_prompt.strip()) > 0)
                        else (self.stage1_prompt.strip() if self.stage1_prompt else "collect import point from this reply of my friends and what suit for this enhancement. and importantly dont miss old points. you neeed to aggregate this as per doc, then will share it with my friend until it get finized")
                    )

                    baseline_version = extract_document_version(current_doc, f"v{iter_idx}.0.0", target_doc_name)
                    v_match = re.search(r"v?(\d+)(?:\.(\d+))?", baseline_version)
                    if v_match:
                        major = int(v_match.group(1))
                        minor = int(v_match.group(2)) if v_match.group(2) else 0
                        target_next_version = f"v{major}.{minor + 1}.0"
                    else:
                        target_next_version = f"v{iter_idx + 1}.0.0"

                    # Put exact User Prompt from App at the VERY TOP / FIRST
                    aggregator_prompt = f"""{user_aggregator_instruction}

Document Under Review: {target_doc_name}

### 1. {self.plan_agent2} Review Critique (Iteration {iter_idx}/{self.max_iterations}):
---
{agent2_critique}
---

### 2. {self.plan_agent3} Cross-Document Evaluation Critique (Iteration {iter_idx}/{self.max_iterations}):
---
{agent3_critique}
---

### Baseline Document ({baseline_version}):
---
{current_doc}
---

Please produce the complete revised and refined implementation plan version {target_next_version} incorporating all valid points."""

                    self.log_signal.emit(f"[Iter {iter_idx}] {self.plan_agent4} Aggregator synthesizing dual feedback for '{target_doc_name}' ({target_next_version})...", "info")

                    refined_output = await self._query_agent(self.plan_agent4, aggregator_prompt, target_doc_name)

                    # GATE 2 ➔ 3: Validate Phase 2 consolidated output
                    PhaseGatekeeper.validate_phase_2_output(refined_output, target_next_version, self.log_signal)

                    if refined_output and len(refined_output.strip()) > 10:
                        current_doc = refined_output
                        iter_out_name = (
                            f"{prefix}-final-done-by-{self.plan_agent4.lower()}.md"
                            if iter_idx == self.max_iterations
                            else f"{prefix}-iter{iter_idx}-{self.plan_agent4.lower()}-refined.md"
                        )
                        saved_path = self.repo_manager.save_named_file(
                            filename=iter_out_name,
                            content=current_doc,
                            commit_message=f"docs({self.plan_agent4.lower()}): iteration {iter_idx}/{self.max_iterations} refined plan -> {iter_out_name}",
                        )
                        self.doc_updated_signal.emit(iter_out_name, current_doc)
                        self.history_updated_signal.emit(self.repo_manager.get_commit_history())
                        self.log_signal.emit(
                            f"[Iter {iter_idx}/{self.max_iterations}] Refined plan saved -> {iter_out_name} ({len(current_doc)} chars).",
                            "success",
                        )

                self.progress_signal.emit(100)
                self.status_signal.emit("Ready")
                self.finished_signal.emit(
                    True,
                    f"Multi-Model Plan Refinement completed successfully across {self.max_iterations} iterations.",
                )
                return

            # -------------------------------------------------------------
            # STAGE 1: Exact User Prompt + Document -> ChatGPT
            # -------------------------------------------------------------
            version = self.repo_manager.get_latest_version_number(self.doc_filename) + 1
            v1_title = self.repo_manager.get_filename(1, self.doc_filename)

            if version == 1:
                if self.is_doc_mode:
                    self.log_signal.emit(
                        f"Stage 1 (ChatGPT): Sending exact Stage 1 prompt + '{self.doc_filename}'...", "info"
                    )
                    prompt_payload = (
                        f"{self.stage1_prompt.strip()}\n\n"
                        f"Document: {self.doc_filename}"
                    )
                    self.status_signal.emit(f"Pasting Stage 1 prompt + {self.doc_filename} into ChatGPT...")

                else:
                    self.log_signal.emit(
                        f"Stage 1 (ChatGPT): Sending exact Stage 1 prompt + custom requirements...", "info"
                    )
                    prompt_payload = (
                        f"{self.stage1_prompt.strip()}\n\n"
                        f"### Requirements:\n"
                        f"---\n"
                        f"{self.user_input}"
                    )
                    self.status_signal.emit("Sending Stage 1 prompt to ChatGPT...")

                self.progress_signal.emit(30)
                current_doc = await self.automator.query_chatgpt(
                    prompt_payload, new_chat=False, chatgpt_url=self.chatgpt_url
                )

                if not current_doc or len(current_doc.strip()) < 5:
                    self.log_signal.emit(
                        "Failed to extract document from ChatGPT or response too short.", "error"
                    )
                    self.finished_signal.emit(False, "ChatGPT extraction failed.")
                    return

                self.log_signal.emit(
                    f"ChatGPT Stage 1 response extracted successfully ({len(current_doc)} chars).", "success"
                )
                self.doc_updated_signal.emit(v1_title, current_doc)

                summary = (
                    f"Stage 1 Review of {self.doc_filename}"
                    if self.is_doc_mode
                    else "Initial architecture draft"
                )
                commit_msg = self.controller.generate_commit_message(version=1, changes_summary=summary, doc_filename=self.doc_filename)
                saved_path = self.repo_manager.save_revision(
                    version=1, content=current_doc, commit_message=commit_msg, doc_filename=self.doc_filename
                )
                self.log_signal.emit(f"Revision v1 committed to Git -> {saved_path}", "success")
                self.history_updated_signal.emit(self.repo_manager.get_commit_history())
            else:
                prev_title = self.repo_manager.get_filename(version - 1, self.doc_filename)
                self.log_signal.emit(f"Resuming from existing revision {prev_title}...", "info")
                current_doc = self.repo_manager.get_file_content(prev_title) or ""
                self.doc_updated_signal.emit(prev_title, current_doc)

            self.progress_signal.emit(50)

            # -------------------------------------------------------------
            # STAGE 2: Exact User Prompt + Extracted Doc -> NotebookLM
            # -------------------------------------------------------------
            iteration_count = 0
            while iteration_count < self.max_iterations and not self.is_cancelled:
                current_v = self.repo_manager.get_latest_version_number(self.doc_filename)
                next_v = current_v + 1
                iteration_count += 1
                curr_title = self.repo_manager.get_filename(current_v, self.doc_filename)
                
                # Determine if this iteration produces the final release spec
                is_final_iter = (iteration_count >= self.max_iterations)
                if is_final_iter:
                    next_title = self.repo_manager.get_final_filename(self.doc_filename, done_by="chatgpt")
                else:
                    next_title = self.repo_manager.get_refined_filename(next_v, self.doc_filename)

                self.log_signal.emit(
                    f"--- Stage 2: Cross-Document Evaluation for '{curr_title}' (Critic: {self.critic_service.upper()}) ---",
                    "info",
                )
                self.status_signal.emit(f"Running Stage 2 critique on {self.critic_service.upper()}...")
                self.progress_signal.emit(65)

                if self.critic_service.lower() == "notebooklm":
                    notebook_url = (
                        self.config.get("services", {})
                        .get("notebooklm", {})
                        .get("url", self.automator.notebooklm_url)
                    )
                    latest_file_path = os.path.join(self.repo_manager.workspace_dir, curr_title)
                    self.log_signal.emit(
                        f"Uploading '{curr_title}' to NotebookLM and cross-referencing ({notebook_url})...",
                        "info",
                    )
                    self.status_signal.emit(f"Uploading {curr_title} to NotebookLM & cross-referencing...")
                    critique_feedback = await self.automator.upload_source_and_review_in_notebooklm(
                        notebook_url=notebook_url,
                        file_path=latest_file_path,
                        doc_title=curr_title,
                        doc_prefix_or_num=self.doc_filename,
                        custom_review_prompt=self.stage2_prompt,
                    )
                else:
                    critique_payload = (
                        f"{self.stage2_prompt.strip()}\n\n"
                        f"### Document Under Review: {curr_title}\n"
                        f"---\n"
                        f"{current_doc}"
                    )
                    critique_feedback = await self.automator.query_gemini(critique_payload, new_chat=True)

                self.log_signal.emit(
                    f"Stage 2 critique feedback received ({len(critique_feedback)} chars).", "success"
                )
                self.critique_updated_signal.emit(critique_feedback)

                # Also save the critique itself as {prefix}-chatgpt-to-notebooklm.md
                critique_filename = self.repo_manager.get_stage2_critique_filename(self.doc_filename)
                self.repo_manager.save_named_file(
                    filename=critique_filename,
                    content=critique_feedback,
                    commit_message=f"docs(critique): cross-document review [{self.doc_filename}] -> {critique_filename}",
                )

                if self.is_cancelled:
                    break

                # Step 5: Refinement in ChatGPT (feeding Stage 3 prompt + NotebookLM critique back into ChatGPT)
                self.status_signal.emit(f"Stage 3: Refining architecture {next_title} with ChatGPT based on NotebookLM critique...")
                self.progress_signal.emit(80)
                stage3_instruction = (
                    self.stage3_prompt.strip()
                    if (self.stage3_prompt and len(self.stage3_prompt.strip()) > 0)
                    else "Please update and refine the architecture specification to incorporate all actionable recommendations and fix every identified contradiction/gap from the following cross-document critique:"
                )
                refine_prompt = (
                    f"{stage3_instruction}\n\n"
                    f"### NotebookLM Cross-Document Evaluation Feedback:\n"
                    f"---\n"
                    f"{critique_feedback}\n\n"
                    f"### Current Architecture Specification ({curr_title}):\n"
                    f"---\n"
                    f"{current_doc}"
                )
                refined_doc = await self.automator.query_chatgpt(
                    refine_prompt, new_chat=False, chatgpt_url=self.chatgpt_url
                )



                if refined_doc and len(refined_doc.strip()) > 10:
                    current_doc = refined_doc
                    self.doc_updated_signal.emit(next_title, current_doc)

                    # Save and Commit refined/final document
                    saved_path = self.repo_manager.save_named_file(
                        filename=next_title,
                        content=current_doc,
                        commit_message=f"feat(arch): finalized specification [{next_title}] addressing NotebookLM review",
                    )
                    self.log_signal.emit(f"Specification {next_title} committed to Git -> {saved_path}", "success")
                    self.history_updated_signal.emit(self.repo_manager.get_commit_history())

                    # Upload finalized revision to NotebookLM Sources
                    if self.critic_service.lower() == "notebooklm":
                        self.log_signal.emit(f"Uploading {next_title} to NotebookLM sources list...", "info")
                        self.status_signal.emit(f"Uploading {next_title} to NotebookLM sources...")
                        v_next_critique = await self.automator.upload_source_and_review_in_notebooklm(
                            notebook_url=notebook_url,
                            file_path=saved_path,
                            doc_title=next_title,
                            doc_prefix_or_num=self.doc_filename,
                            custom_review_prompt=(
                                f"Please perform a cross-document architectural evaluation of the newly refined '{next_title}' "
                                f"against all reference documents in this notebook. Confirm if all previous architectural flaws were resolved and highlight any remaining gaps."
                            ),
                        )
                        self.log_signal.emit(
                            f"NotebookLM evaluation for {next_title} received ({len(v_next_critique)} chars).",
                            "success",
                        )
                        self.critique_updated_signal.emit(v_next_critique)

            # Freeze design if requested
            if self.auto_freeze and not self.is_cancelled:
                final_v = self.repo_manager.get_latest_version_number(self.doc_filename)
                frozen_file = self.repo_manager.freeze_design(
                    final_version=final_v, doc_filename=self.doc_filename, done_by="chatgpt"
                )
                final_title = self.repo_manager.get_final_filename(self.doc_filename, done_by="chatgpt")
                self.log_signal.emit(f"Architecture Design Frozen -> {frozen_file}", "success")
                self.history_updated_signal.emit(self.repo_manager.get_commit_history())
                self.finished_signal.emit(True, f"Architecture successfully frozen and uploaded to NotebookLM: {final_title}")
            else:
                self.finished_signal.emit(True, "Pipeline execution completed.")




            self.progress_signal.emit(100)
            self.status_signal.emit("Ready")

        except Exception as e:
            self.log_signal.emit(f"Pipeline error: {e}", "error")
            self.finished_signal.emit(False, str(e))
        finally:
            if self.automator:
                await self.automator.stop()


class AgentTestWorker(QThread):
    status_signal = Signal(str)
    result_signal = Signal(bool, str)

    def __init__(self, service_key: str, target_url: str, config: dict):
        super().__init__()
        self.service_key = service_key
        self.target_url = target_url
        self.config = config

    def run(self):
        asyncio.run(self._execute_test())

    async def _execute_test(self):
        automator = BrowserAutomator(config=self.config)
        try:
            self.status_signal.emit("Connecting to browser...")
            await automator.start()
            t0 = time.time()

            if self.service_key == "chatgpt":
                self.status_signal.emit("Sending test prompt to ChatGPT...")
                res = await automator.query_chatgpt(
                    "Verification test: reply with 'OK' and confirm connection.",
                    chatgpt_url=self.target_url
                )
                dur = round(time.time() - t0, 1)
                if res and len(res.strip()) > 0:
                    preview = res.strip().replace("\n", " ")[:60]
                    self.result_signal.emit(True, f"ChatGPT Verified ({dur}s): \"{preview}\"")
                else:
                    self.result_signal.emit(False, "ChatGPT returned empty response.")

            elif self.service_key == "chatgpt_lib":
                self.status_signal.emit("Testing ChatGPT Library upload & cleanup...")
                workspace_dir = self.config.get("workspace", {}).get("dir", "./workspace")
                os.makedirs(workspace_dir, exist_ok=True)
                test_file = os.path.join(workspace_dir, "F-TEST-CHATGPT_LIB_SYNC_v1.0.md")
                with open(test_file, "w", encoding="utf-8") as f:
                    f.write("# ChatGPT Library Sync Test\n\nVerification file for library upload.")

                upload_ok = await automator.upload_source_to_chatgpt_library(
                    library_url=self.target_url,
                    file_path=test_file,
                    doc_title=os.path.basename(test_file),
                )
                dur = round(time.time() - t0, 1)
                if upload_ok:
                    self.result_signal.emit(True, f"ChatGPT Library Sync Verified ({dur}s).")
                else:
                    self.result_signal.emit(False, "ChatGPT Library upload failed or timed out.")

            elif self.service_key == "notebooklm":
                self.status_signal.emit("Testing NotebookLM sources upload & chat query...")
                workspace_dir = self.config.get("workspace", {}).get("dir", "./workspace")
                os.makedirs(workspace_dir, exist_ok=True)
                test_file = os.path.join(workspace_dir, "F-TEST-NOTEBOOKLM_SYNC_v1.0.md")
                with open(test_file, "w", encoding="utf-8") as f:
                    f.write("# NotebookLM Sync Test\n\nVerification document for source preparation.")

                upload_ok = await automator.upload_source_to_notebooklm(
                    notebook_url=self.target_url,
                    file_path=test_file,
                    doc_title=os.path.basename(test_file),
                )
                self.status_signal.emit("Querying NotebookLM chat...")
                query_res = await automator.query_notebooklm(
                    prompt="Verification test: reply with 'OK'.",
                    notebook_url=self.target_url
                )
                dur = round(time.time() - t0, 1)
                if query_res and len(query_res.strip()) > 0:
                    preview = query_res.strip().replace("\n", " ")[:60]
                    self.result_signal.emit(True, f"NotebookLM Verified ({dur}s): sources sync & chat response: \"{preview}\"")
                elif upload_ok:
                    self.result_signal.emit(True, f"NotebookLM Sources Upload Verified ({dur}s).")
                else:
                    self.result_signal.emit(False, "NotebookLM sync/query failed or timed out.")

            elif self.service_key == "gemini":
                self.status_signal.emit("Sending test prompt to Gemini...")
                res = await automator.query_gemini(
                    "Verification test: reply with 'OK' and confirm connection.",
                    gemini_url=self.target_url
                )
                dur = round(time.time() - t0, 1)
                if res and len(res.strip()) > 0:
                    preview = res.strip().replace("\n", " ")[:60]
                    self.result_signal.emit(True, f"Gemini Verified ({dur}s): \"{preview}\"")
                else:
                    self.result_signal.emit(False, "Gemini returned empty response.")

            elif self.service_key == "grok":
                self.status_signal.emit("Testing Grok Project files sync & query...")
                workspace_dir = self.config.get("workspace", {}).get("dir", "./workspace")
                os.makedirs(workspace_dir, exist_ok=True)
                test_file = os.path.join(workspace_dir, "F-TEST-GROK_SYNC_v1.0.md")
                with open(test_file, "w", encoding="utf-8") as f:
                    f.write("# Grok Sync Test\n\nVerification document for project file management.")

                doc_name = os.path.basename(test_file)
                upload_ok = await automator.upload_source_to_grok_project(
                    project_url=self.target_url,
                    file_path=test_file,
                    doc_title=doc_name,
                )
                self.status_signal.emit("Querying Grok prompt...")
                query_res = await automator.query_grok(
                    prompt="Verification test: reply with 'OK' and confirm connection.",
                    grok_url=self.target_url
                )
                await automator.delete_file_from_grok_project(self.target_url, doc_name)
                dur = round(time.time() - t0, 1)
                if query_res and len(query_res.strip()) > 0:
                    preview = query_res.strip().replace("\n", " ")[:60]
                    self.result_signal.emit(True, f"Grok Verified ({dur}s): project files upload/delete & response: \"{preview}\"")
                elif upload_ok:
                    self.result_signal.emit(True, f"Grok Project File Sync Verified ({dur}s).")
                else:
                    self.result_signal.emit(False, "Grok project test failed or timed out.")

            elif self.service_key == "claude":
                self.status_signal.emit("Sending test prompt to Claude...")
                res = await automator.query_claude(
                    "Verification test: reply with 'OK' and confirm connection.",
                    claude_url=self.target_url
                )
                dur = round(time.time() - t0, 1)
                if res and len(res.strip()) > 0:
                    preview = res.strip().replace("\n", " ")[:60]
                    self.result_signal.emit(True, f"Claude Verified ({dur}s): \"{preview}\"")
                else:
                    self.result_signal.emit(False, "Claude returned empty response.")

            elif self.service_key == "copilot":
                self.status_signal.emit("Sending test prompt to Copilot...")
                res = await automator.query_copilot(
                    "Verification test: reply with 'OK' and confirm connection.",
                    copilot_url=self.target_url
                )
                dur = round(time.time() - t0, 1)
                if res and len(res.strip()) > 0:
                    preview = res.strip().replace("\n", " ")[:60]
                    self.result_signal.emit(True, f"Copilot Verified ({dur}s): \"{preview}\"")
                else:
                    self.result_signal.emit(False, "Copilot returned empty response.")
            else:
                self.result_signal.emit(False, f"Unknown service key: {self.service_key}")
        except Exception as e:
            self.result_signal.emit(False, f"Test error: {str(e)}")
        finally:
            await automator.stop()


class BroadcastWorker(QThread):
    """
    Asynchronously broadcasts a prompt to all selected AI agents, collects structured replies with headers,
    and saves the combined markdown to the workspace.
    """
    status_signal = Signal(str)
    progress_signal = Signal(int)
    log_signal = Signal(str, str)
    agent_response_signal = Signal(str, str)  # (agent_name, response_text)
    finished_signal = Signal(bool, str, str)  # (success, combined_markdown, saved_file_path)

    def __init__(self, prompt: str, selected_agents: List[str], config: dict, user_prompt: str = "", doc_name: str = ""):
        super().__init__()
        self.prompt = prompt
        self.user_prompt = user_prompt or prompt
        self.doc_name = doc_name
        self.selected_agents = selected_agents
        self.config = config
        self.is_cancelled = False
        self.automator: Optional[BrowserAutomator] = None

    def cancel(self):
        self.is_cancelled = True

    def run(self):
        asyncio.run(self._execute_broadcast())

    async def _execute_broadcast(self):
        try:
            self.automator = BrowserAutomator(config=self.config)
            self.status_signal.emit("Connecting to browser for Multi-Agent Broadcast...")
            self.log_signal.emit("Initializing browser session for Multi-Agent Broadcast...", "info")
            self.progress_signal.emit(5)
            await self.automator.start()

            results: Dict[str, str] = {}
            total = len(self.selected_agents)
            if total == 0:
                self.finished_signal.emit(False, "No agents selected for broadcast.", "")
                return

            step_pct = 85.0 / total

            # Initialize Aggregated Markdown Document on disk immediately
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            timestamp_tag = datetime.now().strftime("%Y%m%d_%H%M%S")
            workspace_dir = self.config.get("workspace", {}).get("dir", "./workspace")
            os.makedirs(workspace_dir, exist_ok=True)
            saved_filename = f"BROADCAST_RESPONSES_{timestamp_tag}.md"
            saved_filepath = os.path.join(workspace_dir, saved_filename)

            doc_line = f"**Attached Document:** `{self.doc_name}`  \n" if (self.doc_name and self.doc_name != "None") else ""
            header_text = (
                f"# Multi-Agent Broadcast Responses\n"
                f"**Timestamp:** {now_str}  \n"
                f"**Target Agents:** {', '.join(self.selected_agents)}  \n"
                f"{doc_line}"
                f"## 📝 Broadcast Prompt\n"
                f"> {self.user_prompt.strip()}\n\n"
                f"---\n\n"
            )
            with open(saved_filepath, "w", encoding="utf-8") as f:
                f.write(header_text)
            self.log_signal.emit(f"Created broadcast responses file: {saved_filename}", "info")

            for i, agent in enumerate(self.selected_agents):
                if self.is_cancelled:
                    self.log_signal.emit("Multi-Agent Broadcast cancelled by user.", "warning")
                    break

                self.status_signal.emit(f"Querying {agent} ({i+1}/{total})...")
                self.log_signal.emit(f"[{agent}] Sending broadcast prompt...", "info")
                t0 = time.time()

                reply = ""
                try:
                    agent_lower = agent.lower()
                    if agent_lower == "chatgpt":
                        chatgpt_url = self.config.get("services", {}).get("chatgpt", {}).get("url", self.automator.chatgpt_url)
                        reply = await self.automator.query_chatgpt(self.prompt, new_chat=False, chatgpt_url=chatgpt_url)
                    elif agent_lower == "notebooklm":
                        notebook_url = self.config.get("services", {}).get("notebooklm", {}).get("url", self.automator.notebooklm_url)
                        reply = await self.automator.query_notebooklm(prompt=self.prompt, notebook_url=notebook_url)
                    elif agent_lower == "gemini":
                        gemini_url = self.config.get("services", {}).get("gemini", {}).get("url", self.automator.gemini_url)
                        reply = await self.automator.query_gemini(self.prompt, new_chat=False, gemini_url=gemini_url)
                    elif agent_lower == "grok":
                        grok_url = self.config.get("services", {}).get("grok", {}).get("url", self.automator.grok_url)
                        reply = await self.automator.query_grok(self.prompt, new_chat=False, grok_url=grok_url)
                    elif agent_lower == "claude":
                        claude_url = self.config.get("services", {}).get("claude", {}).get("url", self.automator.claude_url)
                        reply = await self.automator.query_claude(self.prompt, new_chat=False, claude_url=claude_url)
                    elif agent_lower == "copilot":
                        copilot_url = self.config.get("services", {}).get("copilot", {}).get("url", self.automator.copilot_url)
                        reply = await self.automator.query_copilot(self.prompt, new_chat=False, copilot_url=copilot_url)
                    else:
                        reply = f"Unknown agent: {agent}"
                except Exception as e_agent:
                    reply = f"Error querying {agent}: {str(e_agent)}"
                    self.log_signal.emit(f"[{agent} Error] {e_agent}", "error")

                dur = round(time.time() - t0, 1)
                results[agent] = reply

                # Incremental append of agent response to markdown file on disk immediately
                agent_chunk = f"# 🤖 {agent} Response\n\n{reply.strip()}\n\n---\n\n"
                try:
                    with open(saved_filepath, "a", encoding="utf-8") as f:
                        f.write(agent_chunk)
                except Exception as e_write:
                    self.log_signal.emit(f"Error appending {agent} response to file: {e_write}", "warning")

                # Live signal emission so UI shows the response immediately without waiting
                self.agent_response_signal.emit(agent, reply)
                self.log_signal.emit(f"[{agent}] Received response in {dur}s ({len(reply)} chars) and appended to {saved_filename}.", "success")
                self.progress_signal.emit(int(10 + (i + 1) * step_pct))

            # Read the complete incremental markdown from disk
            full_markdown = ""
            if os.path.exists(saved_filepath):
                with open(saved_filepath, "r", encoding="utf-8") as f:
                    full_markdown = f.read()

            self.log_signal.emit(f"Broadcast responses successfully saved to: {saved_filename}", "success")
            self.progress_signal.emit(100)
            self.status_signal.emit("Multi-Agent Broadcast completed successfully.")
            self.finished_signal.emit(True, full_markdown, saved_filepath)

        except Exception as e:
            self.log_signal.emit(f"Broadcast execution error: {e}", "error")
            self.finished_signal.emit(False, f"Error: {str(e)}", "")
        finally:
            if self.automator:
                await self.automator.stop()


class MainWindow(QMainWindow):
    """
    Main Application Window with Stage 1 (ChatGPT) and Stage 2 (NotebookLM) Prompt Customization.
    """

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Architecture Design Orchestrator (Stage 1: ChatGPT | Stage 2: NotebookLM)")
        
        # Sizing: Increase width by 25% (1360 * 1.25 = 1700px), full height, aligned to leftmost edge
        screen = QApplication.primaryScreen()
        if screen:
            geom = screen.availableGeometry()
            new_width = min(geom.width(), int(1360 * 1.25))  # 1700px
            new_height = geom.height()  # Full screen height (excluding taskbar)
            new_x = geom.left()  # Leftmost edge (x = 0)
            new_y = geom.top()   # Top edge (y = 0)
            self.setGeometry(new_x, new_y, new_width, new_height)
        else:
            self.setGeometry(0, 0, 1700, 1050)


        self.config = self._load_config()
        self.worker: Optional[OrchestrationWorker] = None
        self.docs_dir = self.config.get(
            "workspace", {}
        ).get("docs_dir", r"C:\Users\admin\PycharmProjects\AruMLStudio\docs")


        self._init_ui()
        self._load_docs_list()
        self._refresh_git_history()
        self._check_ollama_status_async()

    def _load_config(self) -> dict:
        base_dir = os.path.dirname(os.path.abspath(__file__))
        cfg_path = os.path.join(base_dir, "config.json")
        if os.path.exists(cfg_path):
            with open(cfg_path, "r", encoding="utf-8") as f:
                return json.load(f)
        return {}

    def _save_config(self):
        try:
            base_dir = os.path.dirname(os.path.abspath(__file__))
            cfg_path = os.path.join(base_dir, "config.json")
            with open(cfg_path, "w", encoding="utf-8") as f:
                json.dump(self.config, f, indent=2)
        except Exception as e:
            print(f"Error saving config.json: {e}")


    def _auto_save_prompts(self):
        """Persist current GUI prompts and notebook configuration directly to config.json."""
        # AruMLStudio Docs Selection Prompts
        if hasattr(self, "txt_doc_stage1_prompt"):
            self.config.setdefault("prompts", {})["doc_stage1_prompt"] = self.txt_doc_stage1_prompt.toPlainText()
            self.config.setdefault("prompts", {})["stage1_prompt"] = self.txt_doc_stage1_prompt.toPlainText()
        if hasattr(self, "txt_doc_stage2_prompt"):
            self.config.setdefault("prompts", {})["doc_stage2_prompt"] = self.txt_doc_stage2_prompt.toPlainText()
            self.config.setdefault("prompts", {})["stage2_prompt"] = self.txt_doc_stage2_prompt.toPlainText()
        if hasattr(self, "txt_doc_stage3_prompt"):
            self.config.setdefault("prompts", {})["doc_stage3_prompt"] = self.txt_doc_stage3_prompt.toPlainText()
            self.config.setdefault("prompts", {})["stage3_prompt"] = self.txt_doc_stage3_prompt.toPlainText()

        # Implementation Plan Prompts
        if hasattr(self, "txt_plan_chatgpt1_prompt"):
            self.config.setdefault("prompts", {})["plan_chatgpt1_prompt"] = self.txt_plan_chatgpt1_prompt.toPlainText()
        if hasattr(self, "txt_plan_gemini_prompt"):
            self.config.setdefault("prompts", {})["plan_gemini_prompt"] = self.txt_plan_gemini_prompt.toPlainText()
        if hasattr(self, "txt_plan_notebooklm_prompt"):
            self.config.setdefault("prompts", {})["plan_notebooklm_prompt"] = self.txt_plan_notebooklm_prompt.toPlainText()
        if hasattr(self, "txt_plan_chatgpt_final_prompt"):
            self.config.setdefault("prompts", {})["plan_chatgpt_final_prompt"] = self.txt_plan_chatgpt_final_prompt.toPlainText()

        # Implementation Plan Selected Agents
        if hasattr(self, "combo_plan_agent1"):
            self.config.setdefault("agents", {})["plan_agent1"] = self.combo_plan_agent1.currentText()
        if hasattr(self, "combo_plan_agent2"):
            self.config.setdefault("agents", {})["plan_agent2"] = self.combo_plan_agent2.currentText()
        if hasattr(self, "combo_plan_agent3"):
            self.config.setdefault("agents", {})["plan_agent3"] = self.combo_plan_agent3.currentText()
        if hasattr(self, "combo_plan_agent4"):
            self.config.setdefault("agents", {})["plan_agent4"] = self.combo_plan_agent4.currentText()

        if hasattr(self, "txt_concept"):
            self.config.setdefault("prompts", {})["custom_concept"] = self.txt_concept.toPlainText()
        if hasattr(self, "txt_chatgpt_url"):
            self.config.setdefault("services", {}).setdefault("chatgpt", {})["url"] = self.txt_chatgpt_url.text().strip()
        if hasattr(self, "txt_chatgpt_lib_url"):
            self.config.setdefault("services", {}).setdefault("chatgpt", {})["library_url"] = self.txt_chatgpt_lib_url.text().strip()
        if hasattr(self, "txt_notebooklm_url"):
            self.config.setdefault("services", {}).setdefault("notebooklm", {})["url"] = self.txt_notebooklm_url.text().strip()

        if hasattr(self, "txt_gemini_url"):
            self.config.setdefault("services", {}).setdefault("gemini", {})["url"] = self.txt_gemini_url.text().strip()
        if hasattr(self, "txt_grok_url"):
            self.config.setdefault("services", {}).setdefault("grok", {})["url"] = self.txt_grok_url.text().strip()
        if hasattr(self, "txt_claude_url"):
            self.config.setdefault("services", {}).setdefault("claude", {})["url"] = self.txt_claude_url.text().strip()
        if hasattr(self, "txt_copilot_url"):
            self.config.setdefault("services", {}).setdefault("copilot", {})["url"] = self.txt_copilot_url.text().strip()

        # Multi-Agent Broadcast Prompts & Checkboxes
        if hasattr(self, "txt_broadcast_prompt"):
            self.config.setdefault("prompts", {})["broadcast_prompt"] = self.txt_broadcast_prompt.toPlainText()
        if hasattr(self, "chk_agent_chatgpt"):
            b_agents = self.config.setdefault("broadcast_agents", {})
            b_agents["chatgpt"] = self.chk_agent_chatgpt.isChecked()
            b_agents["notebooklm"] = self.chk_agent_notebooklm.isChecked()
            b_agents["gemini"] = self.chk_agent_gemini.isChecked()
            b_agents["grok"] = self.chk_agent_grok.isChecked()
            b_agents["claude"] = self.chk_agent_claude.isChecked()
            b_agents["copilot"] = self.chk_agent_copilot.isChecked()
        if hasattr(self, "chk_attach_doc_content"):
            self.config.setdefault("broadcast", {})["attach_doc"] = self.chk_attach_doc_content.isChecked()

        if hasattr(self, "combo_iterations"):
            self.config.setdefault("orchestration", {})["plan_iterations"] = self.combo_iterations.currentIndex() + 1

        # Save Active Tab & Selected Documents
        if hasattr(self, "input_tabs"):
            self.config.setdefault("ui", {})["active_tab"] = self.input_tabs.currentIndex()
        if hasattr(self, "list_docs") and self.list_docs.currentItem():
            self.config.setdefault("ui", {})["selected_doc"] = self.list_docs.currentItem().data(Qt.UserRole)
        if hasattr(self, "list_plan_docs") and self.list_plan_docs.currentItem():
            self.config.setdefault("ui", {})["selected_plan_doc"] = self.list_plan_docs.currentItem().data(Qt.UserRole)
        if hasattr(self, "list_broadcast_docs") and self.list_broadcast_docs.currentItem():
            self.config.setdefault("ui", {})["selected_broadcast_doc"] = self.list_broadcast_docs.currentItem().data(Qt.UserRole)

        self._save_config()

    def _on_agent_selection_changed(self):
        """Handle agent dropdown changes in Tab 3."""
        self._update_flow_info_label()
        self._auto_save_prompts()

    def _update_flow_info_label(self):
        """Dynamically update the flow overview label to reflect currently selected agents."""
        if not hasattr(self, "lbl_flow_info") or not hasattr(self, "combo_plan_agent1"):
            return
        a1 = self.combo_plan_agent1.currentText()
        a2 = self.combo_plan_agent2.currentText()
        a3 = self.combo_plan_agent3.currentText()
        a4 = self.combo_plan_agent4.currentText()
        iters = self.combo_iterations.currentIndex() + 1
        self.lbl_flow_info.setText(
            f"Multi-Model Flow: {a1} (Initial / Source) ➔ {a2} & {a3} (Dual Reviewers) ➔ {a4} (Refined Plan) [Loops across {iters} iteration{'s' if iters > 1 else ''}]"
        )





    def closeEvent(self, event):
        self._auto_save_prompts()
        super().closeEvent(event)


    def _init_ui(self):
        # Dark Modern Stylesheet
        self.setStyleSheet(
            """
            QMainWindow {
                background-color: #121417;
                color: #e0e6ed;
            }
            QWidget {
                background-color: #121417;
                color: #d1d7e0;
                font-family: 'Segoe UI', Arial, sans-serif;
                font-size: 13px;
            }
            QGroupBox {
                border: 1px solid #2d333b;
                border-radius: 6px;
                margin-top: 12px;
                padding-top: 12px;
                font-weight: bold;
                color: #58a6ff;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 10px;
                padding: 0 5px;
            }
            QLineEdit, QTextEdit, QPlainTextEdit, QTextBrowser, QComboBox {
                background-color: #1c2128;
                border: 1px solid #30363d;
                border-radius: 5px;
                color: #f0f6fc;
                padding: 6px;
                selection-background-color: #1f6feb;
            }
            QLineEdit:focus, QTextEdit:focus, QPlainTextEdit:focus, QTextBrowser:focus {
                border: 1px solid #58a6ff;
            }
            QPushButton {
                background-color: #238636;
                color: #ffffff;
                font-weight: bold;
                border: 1px solid #2ea043;
                border-radius: 5px;
                padding: 8px 16px;
            }
            QPushButton:hover {
                background-color: #2ea043;
            }
            QPushButton:pressed {
                background-color: #1b742e;
            }
            QPushButton#btnStop {
                background-color: #da3633;
                border: 1px solid #f85149;
            }
            QPushButton#btnStop:hover {
                background-color: #f85149;
            }
            QPushButton#btnSecondary {
                background-color: #21262d;
                border: 1px solid #30363d;
                color: #c9d1d9;
            }
            QPushButton#btnSecondary:hover {
                background-color: #30363d;
            }
            QTabWidget::pane {
                border: 1px solid #30363d;
                border-radius: 6px;
                background-color: #161b22;
            }
            QTabBar::tab {
                background-color: #1c2128;
                color: #8b949e;
                padding: 8px 16px;
                margin-right: 2px;
                border-top-left-radius: 5px;
                border-top-right-radius: 5px;
                border: 1px solid #30363d;
            }
            QTabBar::tab:selected {
                background-color: #161b22;
                color: #58a6ff;
                font-weight: bold;
                border-bottom: 2px solid #58a6ff;
            }
            QProgressBar {
                border: 1px solid #30363d;
                border-radius: 4px;
                text-align: center;
                background-color: #1c2128;
                color: #f0f6fc;
                font-size: 11px;
                height: 14px;
            }
            QProgressBar::chunk {
                background-color: #238636;
                border-radius: 3px;
            }
            QTableWidget {
                gridline-color: #2d333b;
                border: 1px solid #30363d;
                border-radius: 5px;
                background-color: #161b22;
            }
            QHeaderView::section {
                background-color: #1c2128;
                color: #8b949e;
                padding: 6px;
                border: 1px solid #30363d;
                font-weight: bold;
            }
            QCheckBox {
                color: #c9d1d9;
                spacing: 6px;
            }
            QCheckBox::indicator {
                width: 16px;
                height: 16px;
            }
            QSplitter::handle {
                background-color: #21262d;
            }
            QScrollBar:vertical {
                border: none;
                background: #161b22;
                width: 8px;
                margin: 0px;
            }
            QScrollBar::handle:vertical {
                background: #30363d;
                min-height: 20px;
                border-radius: 4px;
            }
            QScrollBar::handle:vertical:hover {
                background: #58a6ff;
            }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
                height: 0px;
            }
            QScrollBar:horizontal {
                height: 0px;
            }
            """
        )

        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)
        main_layout.setContentsMargins(14, 14, 14, 14)
        main_layout.setSpacing(10)

        # -------------------------------------------------------------
        # TOP TOOLBAR: Status, Controls, Session Mode
        # -------------------------------------------------------------
        top_bar = QHBoxLayout()
        top_bar.setSpacing(12)

        self.lbl_ollama_status = QLabel("Ollama: Coordinator")
        self.lbl_ollama_status.setStyleSheet("color: #3fb950; font-weight: bold;")
        top_bar.addWidget(self.lbl_ollama_status)

        top_bar.addWidget(QLabel("Critic Service:"))
        self.combo_critic = QComboBox()
        self.combo_critic.addItems(["NotebookLM", "Gemini", "Grok"])
        top_bar.addWidget(self.combo_critic)


        self.chk_use_existing_chrome = QCheckBox("Use Open Chrome (Port 9222)")
        self.chk_use_existing_chrome.setChecked(self.config.get("browser", {}).get("use_existing_chrome", True))
        top_bar.addWidget(self.chk_use_existing_chrome)

        self.chk_fresh_session = QCheckBox("Fresh Session (Start at v1)")
        self.chk_fresh_session.setChecked(True)
        top_bar.addWidget(self.chk_fresh_session)

        self.chk_auto_freeze = QCheckBox("Auto-Freeze")
        self.chk_auto_freeze.setChecked(True)
        top_bar.addWidget(self.chk_auto_freeze)

        top_bar.addStretch()

        self.btn_urls = QPushButton("🌐 Target URLs")
        self.btn_urls.setObjectName("btnSecondary")
        self.btn_urls.setToolTip("View, configure, and test target URLs for ChatGPT, NotebookLM, Gemini, Grok, Claude, and Copilot")
        self.btn_urls.clicked.connect(self._open_urls_dialog)
        top_bar.addWidget(self.btn_urls)

        self.btn_settings = QPushButton("⚙️ Settings")
        self.btn_settings.setObjectName("btnSecondary")
        self.btn_settings.setToolTip("Configure source documents directory and output workspace folder")
        self.btn_settings.clicked.connect(self._open_settings_dialog)
        top_bar.addWidget(self.btn_settings)

        self.btn_launch_chrome = QPushButton("Open Login Browser")
        self.btn_launch_chrome.setObjectName("btnSecondary")
        self.btn_launch_chrome.clicked.connect(self._launch_chrome_debug_clicked)
        top_bar.addWidget(self.btn_launch_chrome)

        self.btn_reset_ws = QPushButton("Reset Workspace")
        self.btn_reset_ws.setObjectName("btnSecondary")
        self.btn_reset_ws.clicked.connect(self._reset_workspace_clicked)
        top_bar.addWidget(self.btn_reset_ws)

        self.btn_open_workspace = QPushButton("Open Folder")
        self.btn_open_workspace.setObjectName("btnSecondary")
        self.btn_open_workspace.clicked.connect(self._open_workspace_folder)
        top_bar.addWidget(self.btn_open_workspace)

        main_layout.addLayout(top_bar)

        # Target URLs Data Holders (configured via 'Target URLs' dialog)
        default_gpt_url = self.config.get("services", {}).get("chatgpt", {}).get(
            "url", "https://chatgpt.com/c/6a918785-fca8-83ee-8bb6-422f629090d0"
        )
        self.txt_chatgpt_url = QLineEdit(default_gpt_url)
        self.txt_chatgpt_url.textChanged.connect(self._auto_save_prompts)

        default_gpt_lib_url = self.config.get("services", {}).get("chatgpt", {}).get(
            "library_url", "https://chatgpt.com/library/d/6a723b6fe06c819199240f5a593f7ab4"
        )
        self.txt_chatgpt_lib_url = QLineEdit(default_gpt_lib_url)
        self.txt_chatgpt_lib_url.textChanged.connect(self._auto_save_prompts)

        default_nb_url = self.config.get("services", {}).get("notebooklm", {}).get(
            "url", "https://notebook.google.com/notebook/b7a81a2b-5485-493a-bbfb-cbe58808dfc3"
        )
        self.txt_notebooklm_url = QLineEdit(default_nb_url)
        self.txt_notebooklm_url.textChanged.connect(self._auto_save_prompts)

        default_gemini_url = self.config.get("services", {}).get("gemini", {}).get(
            "url", "https://gemini.google.com/app"
        )
        self.txt_gemini_url = QLineEdit(default_gemini_url)
        self.txt_gemini_url.textChanged.connect(self._auto_save_prompts)

        default_grok_url = self.config.get("services", {}).get("grok", {}).get(
            "url", "https://grok.com/project/3a4c5217-9801-44e6-bb35-60f7e17eca21"
        )
        self.txt_grok_url = QLineEdit(default_grok_url)
        self.txt_grok_url.textChanged.connect(self._auto_save_prompts)

        default_claude_url = self.config.get("services", {}).get("claude", {}).get(
            "url", "https://claude.ai/chat/bccf1a06-ab09-483d-8681-1d6682d682f7"
        )
        self.txt_claude_url = QLineEdit(default_claude_url)
        self.txt_claude_url.textChanged.connect(self._auto_save_prompts)

        default_copilot_url = self.config.get("services", {}).get("copilot", {}).get(
            "url", "https://copilot.microsoft.com/projects/WYSvbmQqZXZMk49sA4Dr5"
        )
        self.txt_copilot_url = QLineEdit(default_copilot_url)
        self.txt_copilot_url.textChanged.connect(self._auto_save_prompts)

        # -------------------------------------------------------------
        # MAIN HORIZONTAL SPLITTER (Left: Inputs & Prompts, Right: Outputs)
        # -------------------------------------------------------------
        splitter = QSplitter(Qt.Horizontal)

        # LEFT PANEL: Input & Doc Browser + Stage 1 & Stage 2 Prompt Boxes
        left_scroll = QScrollArea()
        left_scroll.setWidgetResizable(True)
        left_scroll.setFrameShape(QFrame.NoFrame)
        left_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        left_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)

        left_widget = QWidget()
        left_layout = QVBoxLayout(left_widget)
        left_layout.setContentsMargins(0, 0, 8, 8)
        left_layout.setSpacing(10)

        # Document / Concept Selection Tabs
        self.input_tabs = QTabWidget()

        # Tab 1: Doc Review Mode (AruMLStudio docs)
        tab_docs = QWidget()
        tab_docs_layout = QVBoxLayout(tab_docs)
        tab_docs_layout.setContentsMargins(8, 8, 8, 8)
        tab_docs_layout.setSpacing(8)

        lbl_filter = QLabel("Search Document Prefix (e.g. 00, 01, 08, F1, 15):")
        lbl_filter.setWordWrap(True)
        tab_docs_layout.addWidget(lbl_filter)

        self.txt_doc_search = QLineEdit()
        self.txt_doc_search.setPlaceholderText("Type prefix (e.g. 01, F1) or filter docs...")
        self.txt_doc_search.textChanged.connect(self._filter_docs_list)
        tab_docs_layout.addWidget(self.txt_doc_search)

        self.list_docs = QListWidget()
        self.list_docs.setFixedHeight(110)
        self.list_docs.itemClicked.connect(self._on_doc_selected)
        tab_docs_layout.addWidget(self.list_docs)

        self.lbl_selected_doc_info = QLabel("Selected: None")
        self.lbl_selected_doc_info.setStyleSheet("color: #58a6ff; font-weight: bold;")
        self.lbl_selected_doc_info.setWordWrap(True)
        tab_docs_layout.addWidget(self.lbl_selected_doc_info)

        # Tab 1 Prompts: Stage 1, Stage 2, Stage 3
        grp_doc_s1 = QGroupBox("Stage 1: Prompt for ChatGPT (Initial Review & Generation)")
        layout_doc_s1 = QVBoxLayout(grp_doc_s1)
        self.txt_doc_stage1_prompt = QTextEdit()
        self.txt_doc_stage1_prompt.setFixedHeight(80)
        saved_doc_s1 = self.config.get("prompts", {}).get(
            "doc_stage1_prompt",
            self.config.get("prompts", {}).get(
                "stage1_prompt",
                "Please review the following document in detail. Provide an architectural critique, identify flaws/risks, and recommend concrete improvements.",
            ),
        )
        self.txt_doc_stage1_prompt.setText(saved_doc_s1)
        self.txt_doc_stage1_prompt.textChanged.connect(self._auto_save_prompts)
        layout_doc_s1.addWidget(self.txt_doc_stage1_prompt)
        tab_docs_layout.addWidget(grp_doc_s1)

        grp_doc_s2 = QGroupBox("Stage 2: Prompt for NotebookLM (Cross-Document Evaluation)")
        layout_doc_s2 = QVBoxLayout(grp_doc_s2)
        self.txt_doc_stage2_prompt = QTextEdit()
        self.txt_doc_stage2_prompt.setFixedHeight(80)
        saved_doc_s2 = self.config.get("prompts", {}).get(
            "doc_stage2_prompt",
            self.config.get("prompts", {}).get(
                "stage2_prompt",
                "review this document and review with existing architechure and find any logical loop hole and how to make it superior desighn and find flwas",
            ),
        )
        self.txt_doc_stage2_prompt.setText(saved_doc_s2)
        self.txt_doc_stage2_prompt.textChanged.connect(self._auto_save_prompts)
        layout_doc_s2.addWidget(self.txt_doc_stage2_prompt)
        tab_docs_layout.addWidget(grp_doc_s2)

        grp_doc_s3 = QGroupBox("Stage 3: Prompt for ChatGPT (Final Refinement)")
        layout_doc_s3 = QVBoxLayout(grp_doc_s3)
        self.txt_doc_stage3_prompt = QTextEdit()
        self.txt_doc_stage3_prompt.setFixedHeight(80)
        saved_doc_s3 = self.config.get("prompts", {}).get(
            "doc_stage3_prompt",
            self.config.get("prompts", {}).get(
                "stage3_prompt",
                "Please update and refine the architecture specification to incorporate all actionable recommendations and fix every identified contradiction/gap from the NotebookLM evaluation.",
            ),
        )
        self.txt_doc_stage3_prompt.setText(saved_doc_s3)
        self.txt_doc_stage3_prompt.textChanged.connect(self._auto_save_prompts)
        layout_doc_s3.addWidget(self.txt_doc_stage3_prompt)
        tab_docs_layout.addWidget(grp_doc_s3)

        self.input_tabs.addTab(tab_docs, "Document Review")

        # Tab 2: Custom Architecture Concept
        tab_concept = QWidget()
        tab_concept_layout = QVBoxLayout(tab_concept)
        tab_concept_layout.setContentsMargins(8, 8, 8, 8)
        tab_concept_layout.setSpacing(8)

        lbl_custom_concept = QLabel("Enter Custom Architecture Requirements:")
        lbl_custom_concept.setWordWrap(True)
        tab_concept_layout.addWidget(lbl_custom_concept)

        self.txt_concept = QTextEdit()
        self.txt_concept.setFixedHeight(110)
        self.txt_concept.setPlaceholderText(
            "Describe the software architecture requirements, components, scalability targets, and database design..."
        )
        saved_concept = self.config.get("prompts", {}).get(
            "custom_concept",
            "A high-throughput distributed event streaming platform with multi-region replication and real-time fraud detection.",
        )
        self.txt_concept.setText(saved_concept)
        self.txt_concept.textChanged.connect(self._auto_save_prompts)
        tab_concept_layout.addWidget(self.txt_concept)

        grp_concept_s1 = QGroupBox("Stage 1: Prompt for ChatGPT (Generate Architecture)")
        layout_concept_s1 = QVBoxLayout(grp_concept_s1)
        self.txt_concept_stage1_prompt = QTextEdit()
        self.txt_concept_stage1_prompt.setFixedHeight(80)
        self.txt_concept_stage1_prompt.setText(saved_doc_s1)
        self.txt_concept_stage1_prompt.textChanged.connect(self._auto_save_prompts)
        layout_concept_s1.addWidget(self.txt_concept_stage1_prompt)
        tab_concept_layout.addWidget(grp_concept_s1)

        grp_concept_s2 = QGroupBox("Stage 2: Prompt for NotebookLM (Evaluation)")
        layout_concept_s2 = QVBoxLayout(grp_concept_s2)
        self.txt_concept_stage2_prompt = QTextEdit()
        self.txt_concept_stage2_prompt.setFixedHeight(80)
        self.txt_concept_stage2_prompt.setText(saved_doc_s2)
        self.txt_concept_stage2_prompt.textChanged.connect(self._auto_save_prompts)
        layout_concept_s2.addWidget(self.txt_concept_stage2_prompt)
        tab_concept_layout.addWidget(grp_concept_s2)

        self.input_tabs.addTab(tab_concept, "Custom Architecture Concept")

        # Tab 3: Implementation Plan Review Mode (Tri-Model Flow)
        tab_plan = QWidget()
        tab_plan_layout = QVBoxLayout(tab_plan)
        tab_plan_layout.setContentsMargins(8, 8, 8, 8)
        tab_plan_layout.setSpacing(8)

        lbl_plan_filter = QLabel("Select Implementation Plan Document (e.g. F-ENHANCEMENT_ARCHITECTURE_IMPLEMENTATION_PLAN.md):")
        lbl_plan_filter.setWordWrap(True)
        tab_plan_layout.addWidget(lbl_plan_filter)

        self.txt_plan_search = QLineEdit()
        self.txt_plan_search.setPlaceholderText("Search enhancement plans (e.g. F-*, plan)...")
        self.txt_plan_search.textChanged.connect(self._filter_plan_docs_list)
        tab_plan_layout.addWidget(self.txt_plan_search)

        self.list_plan_docs = QListWidget()
        self.list_plan_docs.setFixedHeight(95)
        self.list_plan_docs.itemClicked.connect(self._on_plan_doc_selected)
        tab_plan_layout.addWidget(self.list_plan_docs)

        self.lbl_selected_plan_info = QLabel("Selected Plan: None")
        self.lbl_selected_plan_info.setStyleSheet("color: #a78bfa; font-weight: bold;")
        self.lbl_selected_plan_info.setWordWrap(True)
        tab_plan_layout.addWidget(self.lbl_selected_plan_info)

        # Interaction Iterations Selector Row
        iter_row = QHBoxLayout()
        iter_row.setSpacing(8)
        lbl_iter = QLabel("Iterations:")
        lbl_iter.setStyleSheet("color: #e3b341; font-weight: bold;")
        iter_row.addWidget(lbl_iter)

        self.combo_iterations = QComboBox()
        for i in range(1, 11):
            suffix = " (Default)" if i == 3 else ""
            self.combo_iterations.addItem(f"{i} Iteration{'s' if i > 1 else ''}{suffix}", i)

        saved_iters = self.config.get("orchestration", {}).get("plan_iterations", 3)
        target_idx = max(0, min(9, saved_iters - 1))
        self.combo_iterations.setCurrentIndex(target_idx)
        self.combo_iterations.currentIndexChanged.connect(self._on_agent_selection_changed)
        iter_row.addWidget(self.combo_iterations, 1)

        self.btn_refresh_plan_docs = QPushButton("🔄 Refresh Files")
        self.btn_refresh_plan_docs.setObjectName("btnSecondary")
        self.btn_refresh_plan_docs.setToolTip("Rescan docs folder and automatically select the most recently updated file")
        self.btn_refresh_plan_docs.clicked.connect(self._on_refresh_files_clicked)
        iter_row.addWidget(self.btn_refresh_plan_docs, 1)

        tab_plan_layout.addLayout(iter_row)

        lbl_flow_info = QLabel("Tri-Model Flow: ChatGPT (Aggregator) ➔ Gemini & NotebookLM (Dual Reviewers) ➔ ChatGPT (Refined Plan) [Loops across selected iterations]")
        lbl_flow_info.setStyleSheet("color: #8b949e; font-size: 11px; font-style: italic;")
        lbl_flow_info.setWordWrap(True)
        self.lbl_flow_info = lbl_flow_info
        tab_plan_layout.addWidget(self.lbl_flow_info)

        # Tab 3 Prompts: 4 Dedicated Boxes with Selectable Agent Dropdowns
        # Box 1: Initial Plan / Source Preparation
        grp_plan_1 = QGroupBox("1. Initial Plan / Source Preparation")
        grp_plan_1.setStyleSheet("QGroupBox { color: #10a37f; border: 1px solid #1a4738; }")
        layout_plan_1 = QVBoxLayout(grp_plan_1)
        layout_plan_1.setContentsMargins(8, 10, 8, 8)
        layout_plan_1.setSpacing(6)

        row_agent_1 = QHBoxLayout()
        row_agent_1.setSpacing(8)
        lbl_agent_1 = QLabel("Target Agent:")
        lbl_agent_1.setStyleSheet("color: #8b949e; font-size: 11px; font-weight: bold;")
        row_agent_1.addWidget(lbl_agent_1)

        self.combo_plan_agent1 = QComboBox()
        self.combo_plan_agent1.addItems(["ChatGPT", "NotebookLM", "Gemini", "Grok", "Claude", "Copilot"])
        saved_agent1 = self.config.get("agents", {}).get("plan_agent1", "ChatGPT")
        self.combo_plan_agent1.setCurrentText(saved_agent1)
        self.combo_plan_agent1.setFixedHeight(26)
        self.combo_plan_agent1.currentTextChanged.connect(self._on_agent_selection_changed)
        row_agent_1.addWidget(self.combo_plan_agent1, 1)
        row_agent_1.addStretch()
        layout_plan_1.addLayout(row_agent_1)

        self.txt_plan_chatgpt1_prompt = QTextEdit()
        self.txt_plan_chatgpt1_prompt.setFixedHeight(75)
        saved_plan_gpt1 = self.config.get("prompts", {}).get(
            "plan_chatgpt1_prompt",
            "collect import point from this reply of my friends and what suit for this enhancement. and importantly dont miss old points. you neeed to aggregate this as per doc, then will share it with my friend until it get finized",
        )
        self.txt_plan_chatgpt1_prompt.setText(saved_plan_gpt1)
        self.txt_plan_chatgpt1_prompt.textChanged.connect(self._auto_save_prompts)
        layout_plan_1.addWidget(self.txt_plan_chatgpt1_prompt)
        tab_plan_layout.addWidget(grp_plan_1)

        # Box 2: Codebase Architecture Review
        grp_plan_2 = QGroupBox("2. Codebase Architecture Review")
        grp_plan_2.setStyleSheet("QGroupBox { color: #a78bfa; border: 1px solid #3d2d5b; }")
        layout_plan_2 = QVBoxLayout(grp_plan_2)
        layout_plan_2.setContentsMargins(8, 10, 8, 8)
        layout_plan_2.setSpacing(6)

        row_agent_2 = QHBoxLayout()
        row_agent_2.setSpacing(8)
        lbl_agent_2 = QLabel("Target Agent:")
        lbl_agent_2.setStyleSheet("color: #8b949e; font-size: 11px; font-weight: bold;")
        row_agent_2.addWidget(lbl_agent_2)

        self.combo_plan_agent2 = QComboBox()
        self.combo_plan_agent2.addItems(["ChatGPT", "NotebookLM", "Gemini", "Grok", "Claude", "Copilot"])
        saved_agent2 = self.config.get("agents", {}).get("plan_agent2", "Gemini")
        self.combo_plan_agent2.setCurrentText(saved_agent2)
        self.combo_plan_agent2.setFixedHeight(26)
        self.combo_plan_agent2.currentTextChanged.connect(self._on_agent_selection_changed)
        row_agent_2.addWidget(self.combo_plan_agent2, 1)
        row_agent_2.addStretch()
        layout_plan_2.addLayout(row_agent_2)

        self.txt_plan_gemini_prompt = QTextEdit()
        self.txt_plan_gemini_prompt.setFixedHeight(75)
        saved_plan_gemini = self.config.get("prompts", {}).get(
            "plan_gemini_prompt",
            "Please review this implementation plan against the actual codebase architecture. Identify missing implementation details, edge cases, class/module boundary violations, and performance bottlenecks.",
        )
        self.txt_plan_gemini_prompt.setText(saved_plan_gemini)
        self.txt_plan_gemini_prompt.textChanged.connect(self._auto_save_prompts)
        layout_plan_2.addWidget(self.txt_plan_gemini_prompt)
        tab_plan_layout.addWidget(grp_plan_2)

        # Box 3: Cross-Document Evaluation
        grp_plan_3 = QGroupBox("3. Cross-Document Evaluation")
        grp_plan_3.setStyleSheet("QGroupBox { color: #58a6ff; border: 1px solid #1c3553; }")
        layout_plan_3 = QVBoxLayout(grp_plan_3)
        layout_plan_3.setContentsMargins(8, 10, 8, 8)
        layout_plan_3.setSpacing(6)

        row_agent_3 = QHBoxLayout()
        row_agent_3.setSpacing(8)
        lbl_agent_3 = QLabel("Target Agent:")
        lbl_agent_3.setStyleSheet("color: #8b949e; font-size: 11px; font-weight: bold;")
        row_agent_3.addWidget(lbl_agent_3)

        self.combo_plan_agent3 = QComboBox()
        self.combo_plan_agent3.addItems(["ChatGPT", "NotebookLM", "Gemini", "Grok", "Claude", "Copilot"])
        saved_agent3 = self.config.get("agents", {}).get("plan_agent3", "NotebookLM")
        self.combo_plan_agent3.setCurrentText(saved_agent3)
        self.combo_plan_agent3.setFixedHeight(26)
        self.combo_plan_agent3.currentTextChanged.connect(self._on_agent_selection_changed)
        row_agent_3.addWidget(self.combo_plan_agent3, 1)
        row_agent_3.addStretch()
        layout_plan_3.addLayout(row_agent_3)

        self.txt_plan_notebooklm_prompt = QTextEdit()
        self.txt_plan_notebooklm_prompt.setFixedHeight(75)
        saved_plan_nb = self.config.get("prompts", {}).get(
            "plan_notebooklm_prompt",
            "review this document and review with existing architechure and find any logical loop hole and how to make it superior desighn and find flwas",
        )
        self.txt_plan_notebooklm_prompt.setText(saved_plan_nb)
        self.txt_plan_notebooklm_prompt.textChanged.connect(self._auto_save_prompts)
        layout_plan_3.addWidget(self.txt_plan_notebooklm_prompt)
        tab_plan_layout.addWidget(grp_plan_3)

        # Box 4: Final Refinement & Aggregator
        grp_plan_4 = QGroupBox("4. Final Refinement & Aggregator")
        grp_plan_4.setStyleSheet("QGroupBox { color: #10a37f; border: 1px solid #1a4738; }")
        layout_plan_4 = QVBoxLayout(grp_plan_4)
        layout_plan_4.setContentsMargins(8, 10, 8, 8)
        layout_plan_4.setSpacing(6)

        row_agent_4 = QHBoxLayout()
        row_agent_4.setSpacing(8)
        lbl_agent_4 = QLabel("Target Agent:")
        lbl_agent_4.setStyleSheet("color: #8b949e; font-size: 11px; font-weight: bold;")
        row_agent_4.addWidget(lbl_agent_4)

        self.combo_plan_agent4 = QComboBox()
        self.combo_plan_agent4.addItems(["ChatGPT", "NotebookLM", "Gemini", "Grok", "Claude", "Copilot"])
        saved_agent4 = self.config.get("agents", {}).get("plan_agent4", "ChatGPT")
        self.combo_plan_agent4.setCurrentText(saved_agent4)
        self.combo_plan_agent4.setFixedHeight(26)
        self.combo_plan_agent4.currentTextChanged.connect(self._on_agent_selection_changed)
        row_agent_4.addWidget(self.combo_plan_agent4, 1)
        row_agent_4.addStretch()
        layout_plan_4.addLayout(row_agent_4)

        self.txt_plan_chatgpt_final_prompt = QTextEdit()
        self.txt_plan_chatgpt_final_prompt.setFixedHeight(75)
        saved_plan_final = self.config.get("prompts", {}).get(
            "plan_chatgpt_final_prompt",
            "we need to give this to gemini coding agent so how to ask gemini to prepare implementation plans",
        )
        self.txt_plan_chatgpt_final_prompt.setText(saved_plan_final)
        self.txt_plan_chatgpt_final_prompt.textChanged.connect(self._auto_save_prompts)
        layout_plan_4.addWidget(self.txt_plan_chatgpt_final_prompt)
        tab_plan_layout.addWidget(grp_plan_4)

        self._update_flow_info_label()

        self.input_tabs.addTab(tab_plan, "Implementation Plan")

        # -------------------------------------------------------------
        # Tab 4: Multi-Agent Prompt Broadcast
        # -------------------------------------------------------------
        tab_broadcast = QWidget()
        tab_broadcast_layout = QVBoxLayout(tab_broadcast)
        tab_broadcast_layout.setContentsMargins(8, 8, 8, 8)
        tab_broadcast_layout.setSpacing(8)

        lbl_b_desc = QLabel("Broadcast a prompt to multiple AI models simultaneously, optionally attach an F-document, and download/save aggregated replies:")
        lbl_b_desc.setStyleSheet("color: #8b949e; font-size: 11px;")
        lbl_b_desc.setWordWrap(True)
        tab_broadcast_layout.addWidget(lbl_b_desc)

        # 1. Select F-Document (Optional Attachment)
        grp_b_doc = QGroupBox("Select F-Document (Optional Reference)")
        grp_b_doc.setStyleSheet("QGroupBox { color: #d97706; font-weight: bold; border: 1px solid #78350f; }")
        layout_b_doc = QVBoxLayout(grp_b_doc)
        layout_b_doc.setContentsMargins(8, 10, 8, 8)
        layout_b_doc.setSpacing(6)

        row_doc_top = QHBoxLayout()
        row_doc_top.setSpacing(6)
        self.txt_broadcast_doc_search = QLineEdit()
        self.txt_broadcast_doc_search.setPlaceholderText("Filter F-documents...")
        self.txt_broadcast_doc_search.textChanged.connect(self._filter_broadcast_docs)
        row_doc_top.addWidget(self.txt_broadcast_doc_search, 1)

        btn_refresh_b_docs = QPushButton("🔄 Refresh")
        btn_refresh_b_docs.setFixedHeight(26)
        btn_refresh_b_docs.setObjectName("btnSecondary")
        btn_refresh_b_docs.clicked.connect(self._load_docs_list)
        row_doc_top.addWidget(btn_refresh_b_docs)
        layout_b_doc.addLayout(row_doc_top)

        self.list_broadcast_docs = QListWidget()
        self.list_broadcast_docs.setFixedHeight(95)
        self.list_broadcast_docs.itemClicked.connect(self._on_broadcast_doc_selected)
        layout_b_doc.addWidget(self.list_broadcast_docs)

        self.lbl_selected_broadcast_doc_info = QLabel("Attached Document: None (Prompt only)")
        self.lbl_selected_broadcast_doc_info.setStyleSheet("color: #8b949e; font-size: 11px;")
        self.lbl_selected_broadcast_doc_info.setWordWrap(True)
        layout_b_doc.addWidget(self.lbl_selected_broadcast_doc_info)

        tab_broadcast_layout.addWidget(grp_b_doc)

        # 2. Broadcast Prompt Box
        grp_b_prompt = QGroupBox("Prompt for All Selected Agents")
        grp_b_prompt.setStyleSheet("QGroupBox { color: #58a6ff; font-weight: bold; border: 1px solid #1c3553; }")
        layout_b_prompt = QVBoxLayout(grp_b_prompt)
        layout_b_prompt.setContentsMargins(8, 10, 8, 8)
        self.txt_broadcast_prompt = QTextEdit()
        self.txt_broadcast_prompt.setPlaceholderText("Type prompt to broadcast across all selected agents (e.g. Please analyze this architecture pattern and give pros/cons)...")
        self.txt_broadcast_prompt.setFixedHeight(105)
        saved_b_prompt = self.config.get("prompts", {}).get(
            "broadcast_prompt",
            "Please review this architectural design question. Detail strengths, edge cases, failure modes, and concrete recommendations."
        )
        self.txt_broadcast_prompt.setText(saved_b_prompt)
        self.txt_broadcast_prompt.textChanged.connect(self._auto_save_prompts)
        layout_b_prompt.addWidget(self.txt_broadcast_prompt)

        self.chk_attach_doc_content = QCheckBox("📎 Attach Selected Document Content (Prompt description first, entire document second)")
        self.chk_attach_doc_content.setChecked(self.config.get("broadcast", {}).get("attach_doc", True))
        self.chk_attach_doc_content.setStyleSheet("color: #58a6ff; font-size: 11px; font-weight: bold; margin-top: 2px;")
        self.chk_attach_doc_content.stateChanged.connect(self._auto_save_prompts)
        layout_b_prompt.addWidget(self.chk_attach_doc_content)

        tab_broadcast_layout.addWidget(grp_b_prompt)

        # 3. Selectable Agents with Checkboxes
        grp_b_agents = QGroupBox("Target AI Agents (Check to Include)")
        grp_b_agents.setStyleSheet("QGroupBox { color: #10a37f; font-weight: bold; border: 1px solid #1a4738; }")
        layout_b_agents = QVBoxLayout(grp_b_agents)
        layout_b_agents.setContentsMargins(8, 10, 8, 8)
        layout_b_agents.setSpacing(6)

        b_agents_cfg = self.config.get("broadcast_agents", {})

        grid_agents = QGridLayout()
        grid_agents.setHorizontalSpacing(15)
        grid_agents.setVerticalSpacing(8)

        self.chk_agent_chatgpt = QCheckBox("ChatGPT")
        self.chk_agent_chatgpt.setChecked(b_agents_cfg.get("chatgpt", True))
        self.chk_agent_chatgpt.stateChanged.connect(self._auto_save_prompts)
        grid_agents.addWidget(self.chk_agent_chatgpt, 0, 0)

        self.chk_agent_notebooklm = QCheckBox("NotebookLM")
        self.chk_agent_notebooklm.setChecked(b_agents_cfg.get("notebooklm", True))
        self.chk_agent_notebooklm.stateChanged.connect(self._auto_save_prompts)
        grid_agents.addWidget(self.chk_agent_notebooklm, 0, 1)

        self.chk_agent_gemini = QCheckBox("Gemini")
        self.chk_agent_gemini.setChecked(b_agents_cfg.get("gemini", True))
        self.chk_agent_gemini.stateChanged.connect(self._auto_save_prompts)
        grid_agents.addWidget(self.chk_agent_gemini, 0, 2)

        self.chk_agent_grok = QCheckBox("Grok")
        self.chk_agent_grok.setChecked(b_agents_cfg.get("grok", True))
        self.chk_agent_grok.stateChanged.connect(self._auto_save_prompts)
        grid_agents.addWidget(self.chk_agent_grok, 1, 0)

        self.chk_agent_claude = QCheckBox("Claude")
        self.chk_agent_claude.setChecked(b_agents_cfg.get("claude", True))
        self.chk_agent_claude.stateChanged.connect(self._auto_save_prompts)
        grid_agents.addWidget(self.chk_agent_claude, 1, 1)

        self.chk_agent_copilot = QCheckBox("Copilot")
        self.chk_agent_copilot.setChecked(b_agents_cfg.get("copilot", True))
        self.chk_agent_copilot.stateChanged.connect(self._auto_save_prompts)
        grid_agents.addWidget(self.chk_agent_copilot, 1, 2)

        layout_b_agents.addLayout(grid_agents)

        row_sel_helpers = QHBoxLayout()
        row_sel_helpers.setSpacing(8)
        btn_sel_all = QPushButton("Select All")
        btn_sel_all.setFixedHeight(24)
        btn_sel_all.setObjectName("btnSecondary")
        btn_sel_all.clicked.connect(lambda: self._select_all_broadcast_agents(True))
        row_sel_helpers.addWidget(btn_sel_all)

        btn_clear_all = QPushButton("Clear All")
        btn_clear_all.setFixedHeight(24)
        btn_clear_all.setObjectName("btnSecondary")
        btn_clear_all.clicked.connect(lambda: self._select_all_broadcast_agents(False))
        row_sel_helpers.addWidget(btn_clear_all)
        row_sel_helpers.addStretch()
        layout_b_agents.addLayout(row_sel_helpers)

        tab_broadcast_layout.addWidget(grp_b_agents)

        # 3. Dedicated Tab 4 Actions: Submit & Download
        row_b_actions = QVBoxLayout()
        row_b_actions.setSpacing(6)

        self.btn_broadcast_submit = QPushButton("🚀 Submit Prompt to All Selected Agents")
        self.btn_broadcast_submit.setFixedHeight(38)
        self.btn_broadcast_submit.clicked.connect(self._start_broadcast_prompt)
        row_b_actions.addWidget(self.btn_broadcast_submit)

        row_b_sub = QHBoxLayout()
        row_b_sub.setSpacing(8)
        self.btn_broadcast_download = QPushButton("💾 Download / Export Responses (.md)")
        self.btn_broadcast_download.setFixedHeight(34)
        self.btn_broadcast_download.setObjectName("btnSecondary")
        self.btn_broadcast_download.setEnabled(False)
        self.btn_broadcast_download.clicked.connect(self._download_broadcast_response)
        row_b_sub.addWidget(self.btn_broadcast_download, 3)

        self.btn_broadcast_stop = QPushButton("⏹ Stop")
        self.btn_broadcast_stop.setFixedHeight(34)
        self.btn_broadcast_stop.setObjectName("btnStop")
        self.btn_broadcast_stop.setEnabled(False)
        self.btn_broadcast_stop.clicked.connect(self._stop_broadcast)
        row_b_sub.addWidget(self.btn_broadcast_stop, 1)

        row_b_actions.addLayout(row_b_sub)
        tab_broadcast_layout.addLayout(row_b_actions)
        tab_broadcast_layout.addStretch()

        self.input_tabs.addTab(tab_broadcast, "Multi-Agent Prompt")

        saved_tab_idx = self.config.get("ui", {}).get("active_tab", 0)
        self.input_tabs.setCurrentIndex(max(0, min(3, saved_tab_idx)))
        self.input_tabs.currentChanged.connect(self._auto_save_prompts)
        left_layout.addWidget(self.input_tabs)

        # Action Buttons Layout (2-row stacked layout so nothing is cut off horizontally)
        btn_box = QVBoxLayout()
        btn_box.setSpacing(6)

        # Primary Action (Full Width)
        self.btn_run = QPushButton("🚀 Start Tri-Model Flow (Stage 1 ➔ Stage 2 ➔ Stage 3)")
        self.btn_run.setFixedHeight(38)
        self.btn_run.clicked.connect(self._start_orchestration)
        btn_box.addWidget(self.btn_run)

        # Secondary Action + Stop in one row
        sub_btn_layout = QHBoxLayout()
        sub_btn_layout.setSpacing(8)

        self.btn_run_stage2_only = QPushButton("⚡ Run Stage 2 Only (NotebookLM)")
        self.btn_run_stage2_only.setFixedHeight(36)
        self.btn_run_stage2_only.setObjectName("btnSecondary")
        self.btn_run_stage2_only.clicked.connect(self._start_stage2_only_orchestration)
        sub_btn_layout.addWidget(self.btn_run_stage2_only, 3)

        self.btn_stop = QPushButton("⏹ Stop")
        self.btn_stop.setFixedHeight(36)
        self.btn_stop.setObjectName("btnStop")
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self._stop_orchestration)
        sub_btn_layout.addWidget(self.btn_stop, 1)

        btn_box.addLayout(sub_btn_layout)
        left_layout.addLayout(btn_box)

        left_scroll.setWidget(left_widget)
        splitter.addWidget(left_scroll)

        # RIGHT PANEL: Output Tabs
        right_widget = QWidget()
        right_layout = QVBoxLayout(right_widget)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(8)

        self.output_tabs = QTabWidget()

        # Output Tab 1: Architecture Spec
        self.txt_output_doc = QTextBrowser()
        self.txt_output_doc.setOpenExternalLinks(True)
        self.output_tabs.addTab(self.txt_output_doc, "Architecture Document")

        # Output Tab 2: Critique & Flaws
        self.txt_output_critique = QTextBrowser()
        self.output_tabs.addTab(self.txt_output_critique, "Stage 2: NotebookLM Evaluation")

        # Output Tab 3: Recommendations (Live Multi-Agent Streaming)
        self.txt_output_recommendations = QTextBrowser()
        self.txt_output_recommendations.setOpenExternalLinks(True)
        self.output_tabs.addTab(self.txt_output_recommendations, "Recommendations")

        # Output Tab 4: Multi-Agent Responses
        self.txt_output_broadcast = QTextBrowser()
        self.txt_output_broadcast.setOpenExternalLinks(True)
        self.output_tabs.addTab(self.txt_output_broadcast, "Multi-Agent Responses")

        # Output Tab 5: Git History
        tab_history = QWidget()
        tab_hist_layout = QVBoxLayout(tab_history)
        self.tbl_history = QTableWidget(0, 3)
        self.tbl_history.setHorizontalHeaderLabels(["Commit Hash", "Date", "Summary"])
        self.tbl_history.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.tbl_history.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.tbl_history.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        tab_hist_layout.addWidget(self.tbl_history)
        self.output_tabs.addTab(tab_history, "Git Workspace History")

        # Output Tab 6: Execution Log
        self.txt_log = QPlainTextEdit()
        self.txt_log.setReadOnly(True)
        self.output_tabs.addTab(self.txt_log, "Live Execution Log")

        right_layout.addWidget(self.output_tabs)
        splitter.addWidget(right_widget)

        splitter.setCollapsible(0, False)
        splitter.setCollapsible(1, False)
        splitter.setSizes([750, 950])
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 1)

        main_layout.addWidget(splitter)

        # -------------------------------------------------------------
        # BOTTOM PROGRESS & STATUS BAR
        # -------------------------------------------------------------
        self.progress_bar = QProgressBar()
        self.progress_bar.setValue(0)
        self.progress_bar.setFixedHeight(16)
        main_layout.addWidget(self.progress_bar)

        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self.status_bar.showMessage("Ready")

    def _check_ollama_status_async(self):
        controller = QwenTrafficController(
            base_url=self.config.get("ollama", {}).get("base_url", "http://localhost:11434"),
            model=self.config.get("ollama", {}).get("model", "qwen3:4b"),
        )
        health = controller.check_health()
        if health.get("status") == "online":
            self.lbl_ollama_status.setText(f"Ollama: Online ({health.get('active_model')})")
            self.lbl_ollama_status.setStyleSheet("color: #3fb950; font-weight: bold;")
        else:
            self.lbl_ollama_status.setText("Ollama: Coordinator (Deterministic)")
            self.lbl_ollama_status.setStyleSheet("color: #58a6ff; font-weight: bold;")

    def _load_docs_list(self):
        self.list_docs.clear()
        self.list_plan_docs.clear()
        if hasattr(self, "list_broadcast_docs"):
            self.list_broadcast_docs.clear()
            none_item = QListWidgetItem("None (No document attached)")
            none_item.setData(Qt.UserRole, "None")
            none_item.setForeground(QColor("#8b949e"))
            self.list_broadcast_docs.addItem(none_item)

        if not os.path.exists(self.docs_dir):
            return

        files = sorted([f for f in os.listdir(self.docs_dir) if f.endswith(".md")])
        for fname in files:
            fpath = os.path.join(self.docs_dir, fname)
            size_kb = round(os.path.getsize(fpath) / 1024, 1)
            item = QListWidgetItem(f"{fname} ({size_kb} KB)")
            item.setData(Qt.UserRole, fname)
            self.list_docs.addItem(item)

            if fname.upper().startswith("F") or "plan" in fname.lower():
                plan_item = QListWidgetItem(f"{fname} ({size_kb} KB)")
                plan_item.setData(Qt.UserRole, fname)
                self.list_plan_docs.addItem(plan_item)

                if hasattr(self, "list_broadcast_docs"):
                    b_item = QListWidgetItem(f"{fname} ({size_kb} KB)")
                    b_item.setData(Qt.UserRole, fname)
                    self.list_broadcast_docs.addItem(b_item)

        # Also discover any F-documents created in ./workspace
        workspace_dir = self.config.get("workspace", {}).get("dir", "./workspace")
        if os.path.exists(workspace_dir) and hasattr(self, "list_broadcast_docs"):
            existing_b_docs = {self.list_broadcast_docs.item(i).data(Qt.UserRole) for i in range(self.list_broadcast_docs.count())}
            for wfname in sorted(os.listdir(workspace_dir)):
                if wfname.endswith(".md") and (wfname.upper().startswith("F") or "plan" in wfname.lower()) and wfname not in existing_b_docs:
                    wfpath = os.path.join(workspace_dir, wfname)
                    size_kb = round(os.path.getsize(wfpath) / 1024, 1)
                    b_item = QListWidgetItem(f"[workspace] {wfname} ({size_kb} KB)")
                    b_item.setData(Qt.UserRole, wfname)
                    self.list_broadcast_docs.addItem(b_item)

        saved_doc = self.config.get("ui", {}).get("selected_doc", "")
        saved_plan_doc = self.config.get("ui", {}).get("selected_plan_doc", "")
        saved_broadcast_doc = self.config.get("ui", {}).get("selected_broadcast_doc", "None")

        if self.list_docs.count() > 0:
            doc_found = False
            if saved_doc:
                for i in range(self.list_docs.count()):
                    item = self.list_docs.item(i)
                    if item.data(Qt.UserRole) == saved_doc:
                        self.list_docs.setCurrentRow(i)
                        self._on_doc_selected(item)
                        doc_found = True
                        break
            if not doc_found:
                self.list_docs.setCurrentRow(0)
                self._on_doc_selected(self.list_docs.item(0))

        if self.list_plan_docs.count() > 0:
            plan_found = False
            if saved_plan_doc:
                for i in range(self.list_plan_docs.count()):
                    pitem = self.list_plan_docs.item(i)
                    if pitem.data(Qt.UserRole) == saved_plan_doc:
                        self.list_plan_docs.setCurrentRow(i)
                        self._on_plan_doc_selected(pitem)
                        plan_found = True
                        break
            if not plan_found:
                for i in range(self.list_plan_docs.count()):
                    pitem = self.list_plan_docs.item(i)
                    if "enhancement_architecture_implementation_plan" in pitem.data(Qt.UserRole).lower():
                        self.list_plan_docs.setCurrentRow(i)
                        self._on_plan_doc_selected(pitem)
                        plan_found = True
                        break
            if not plan_found:
                self.list_plan_docs.setCurrentRow(0)
                self._on_plan_doc_selected(self.list_plan_docs.item(0))

        if hasattr(self, "list_broadcast_docs") and self.list_broadcast_docs.count() > 0:
            b_found = False
            if saved_broadcast_doc:
                for i in range(self.list_broadcast_docs.count()):
                    bitem = self.list_broadcast_docs.item(i)
                    if bitem.data(Qt.UserRole) == saved_broadcast_doc:
                        self.list_broadcast_docs.setCurrentRow(i)
                        self._on_broadcast_doc_selected(bitem)
                        b_found = True
                        break
            if not b_found:
                self.list_broadcast_docs.setCurrentRow(0)
                self._on_broadcast_doc_selected(self.list_broadcast_docs.item(0))

    def _filter_broadcast_docs(self, query: str):
        """Filter F-documents inside Tab 4 search box."""
        q = (query or "").strip().lower()
        matched_item = None
        for i in range(self.list_broadcast_docs.count()):
            item = self.list_broadcast_docs.item(i)
            doc_name = (item.data(Qt.UserRole) or "").lower()
            if doc_name == "none":
                item.setHidden(False)
            elif not q or q in item.text().lower() or q in doc_name:
                item.setHidden(False)
                if matched_item is None:
                    matched_item = item
            else:
                item.setHidden(True)

    def _on_broadcast_doc_selected(self, item: QListWidgetItem):
        """Handle selection of F-document in Tab 4."""
        if not item or not hasattr(self, "lbl_selected_broadcast_doc_info"):
            return
        doc_name = item.data(Qt.UserRole)
        if doc_name == "None":
            self.lbl_selected_broadcast_doc_info.setText("Attached Document: None (Prompt only)")
            self.lbl_selected_broadcast_doc_info.setStyleSheet("color: #8b949e; font-size: 11px;")
        else:
            self.lbl_selected_broadcast_doc_info.setText(f"Attached Document: {doc_name}")
            self.lbl_selected_broadcast_doc_info.setStyleSheet("color: #3fb950; font-weight: bold; font-size: 11px;")
        self._auto_save_prompts()

    def _filter_docs_list(self, text: str):
        query = text.strip().lower()
        matched_item = None
        for i in range(self.list_docs.count()):
            item = self.list_docs.item(i)
            fname = item.data(Qt.UserRole).lower()
            if not query or query in fname or fname.startswith(query):
                item.setHidden(False)
                if matched_item is None:
                    matched_item = item
            else:
                item.setHidden(True)

        if matched_item and query:
            self.list_docs.setCurrentItem(matched_item)
            self._on_doc_selected(matched_item)

    def _filter_plan_docs_list(self, text: str):
        query = text.strip().lower()
        matched_item = None
        for i in range(self.list_plan_docs.count()):
            item = self.list_plan_docs.item(i)
            fname = item.data(Qt.UserRole).lower()
            if not query or query in fname or fname.startswith(query):
                item.setHidden(False)
                if matched_item is None:
                    matched_item = item
            else:
                item.setHidden(True)

        if matched_item and query:
            self.list_plan_docs.setCurrentItem(matched_item)
            self._on_plan_doc_selected(matched_item)

    def _on_doc_selected(self, item: QListWidgetItem):
        if not item:
            return
        fname = item.data(Qt.UserRole)
        fpath = os.path.join(self.docs_dir, fname)
        if os.path.exists(fpath):
            size_kb = round(os.path.getsize(fpath) / 1024, 1)
            self.lbl_selected_doc_info.setText(f"Selected: {fname} ({size_kb} KB)")
        self._auto_save_prompts()

    def _on_plan_doc_selected(self, item: QListWidgetItem):
        if not item:
            return
        fname = item.data(Qt.UserRole)
        fpath = os.path.join(self.docs_dir, fname)
        if os.path.exists(fpath):
            size_kb = round(os.path.getsize(fpath) / 1024, 1)
            self.lbl_selected_plan_info.setText(f"Selected Plan: {fname} ({size_kb} KB)")
        self._auto_save_prompts()

    def _on_refresh_files_clicked(self):
        """Rescans docs directory, reloads list, and selects the most recently updated file."""
        self._load_docs_list()
        if not os.path.exists(self.docs_dir):
            return

        latest_file = None
        latest_mtime = -1
        try:
            for fname in os.listdir(self.docs_dir):
                if fname.lower().endswith(".md"):
                    fpath = os.path.join(self.docs_dir, fname)
                    mtime = os.path.getmtime(fpath)
                    if mtime > latest_mtime:
                        latest_mtime = mtime
                        latest_file = fname
        except Exception as e:
            logging.warning(f"Error scanning latest file: {e}")

        if latest_file:
            # Select in list_plan_docs
            for i in range(self.list_plan_docs.count()):
                item = self.list_plan_docs.item(i)
                if item.data(Qt.UserRole) == latest_file:
                    self.list_plan_docs.setCurrentItem(item)
                    self._on_plan_doc_selected(item)
                    break
            # Also select in list_docs
            for i in range(self.list_docs.count()):
                item = self.list_docs.item(i)
                if item.data(Qt.UserRole) == latest_file:
                    self.list_docs.setCurrentItem(item)
                    self._on_doc_selected(item)
                    break

            self._append_log(f"🔄 Document list refreshed. Automatically selected latest file: '{latest_file}'", "success")


    def _append_log(self, message: str, level: str = "info"):
        t_stamp = time.strftime("%H:%M:%S")
        color_map = {
            "info": "#58a6ff",
            "success": "#3fb950",
            "warning": "#d29922",
            "error": "#f85149",
        }
        color = color_map.get(level, "#c9d1d9")
        html = f"<span style='color: #8b949e;'>[{t_stamp}]</span> <span style='color: {color};'>{message}</span>"
        self.txt_log.appendHtml(html)
        self.txt_log.moveCursor(QTextCursor.End)

    def _reset_workspace_clicked(self):
        reply = QMessageBox.question(
            self,
            "Reset Workspace",
            "Are you sure you want to archive previous architecture revisions and start a fresh session at v1?",
            QMessageBox.Yes | QMessageBox.No,
        )
        if reply == QMessageBox.Yes:
            repo_mgr = WorkspaceRepoManager(
                workspace_dir=self.config.get("workspace", {}).get("dir", "./workspace")
            )
            repo_mgr.reset_workspace(archive=True)
            self._refresh_git_history()
            self.txt_output_doc.clear()
            self.txt_output_critique.clear()
            self.output_tabs.setTabText(0, "Architecture Document")
            self._append_log("Workspace archived and reset to fresh state.", "info")
            QMessageBox.information(self, "Workspace Reset", "Workspace has been archived and reset to v1.")

    def _launch_chrome_debug_clicked(self):
        """Launch visible Chrome with persistent profile for one-time login."""
        self._append_log("Launching Google Chrome browser window...", "info")
        base_dir = os.path.dirname(os.path.abspath(__file__))
        script_path = os.path.join(base_dir, "open_browser_window.py")
        subprocess.Popen([sys.executable, script_path])
        self._append_log("Google Chrome visual window launched. Please sign into Google & ChatGPT in that window.", "success")
        QMessageBox.information(
            self,
            "Browser Window Launched",
            "Google Chrome has been launched!\n\n"
            "1. Please sign in to your Google Account (for NotebookLM) and ChatGPT in the Chrome window.\n"
            "2. Once signed in, your session is saved permanently in ./browser_profile.\n"
            "3. You can then run orchestration anytime without needing to log in again!",
        )

    def _open_workspace_folder(self):
        base_dir = os.path.dirname(os.path.abspath(__file__))
        ws_rel = self.config.get("workspace", {}).get("dir", "./workspace")
        ws_dir = os.path.abspath(os.path.join(base_dir, ws_rel))
        os.makedirs(ws_dir, exist_ok=True)
        if sys.platform == "win32":
            os.startfile(ws_dir)
        elif sys.platform == "darwin":
            os.system(f"open '{ws_dir}'")
        else:
            os.system(f"xdg-open '{ws_dir}'")

    def _open_settings_dialog(self):
        """Open a modal settings dialog to view and configure source documents path and output workspace folder."""
        dialog = QDialog(self)
        dialog.setWindowTitle("Application Settings - Folders & Paths")
        dialog.setMinimumWidth(680)
        dialog.setStyleSheet(self.styleSheet())

        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(14)

        header = QLabel("<h3>⚙️ Application Folder & Directory Settings</h3>")
        header.setStyleSheet("color: #58a6ff; margin-bottom: 2px;")
        layout.addWidget(header)

        desc = QLabel("Configure the source folder for architecture documentation and the output workspace directory for generated revisions, git history, and broadcast exports:")
        desc.setStyleSheet("color: #8b949e; font-size: 12px; margin-bottom: 8px;")
        desc.setWordWrap(True)
        layout.addWidget(desc)

        form = QFormLayout()
        form.setSpacing(14)
        form.setLabelAlignment(Qt.AlignRight | Qt.AlignTop)

        # 1. Source Documents Directory
        w_source = QWidget()
        l_source = QVBoxLayout(w_source)
        l_source.setContentsMargins(0, 0, 0, 0)
        l_source.setSpacing(4)

        r_source = QHBoxLayout()
        r_source.setSpacing(8)
        txt_source_dir = QLineEdit(self.docs_dir)
        txt_source_dir.setFixedHeight(30)
        r_source.addWidget(txt_source_dir, 1)

        btn_browse_source = QPushButton("📂 Browse...")
        btn_browse_source.setObjectName("btnSecondary")
        btn_browse_source.setFixedHeight(30)
        btn_browse_source.setMinimumWidth(100)

        def on_browse_source():
            init_dir = txt_source_dir.text().strip()
            if not os.path.exists(init_dir):
                init_dir = os.getcwd()
            chosen = QFileDialog.getExistingDirectory(dialog, "Select Source Documents Directory", init_dir)
            if chosen:
                txt_source_dir.setText(os.path.normpath(chosen))

        btn_browse_source.clicked.connect(on_browse_source)
        r_source.addWidget(btn_browse_source)
        l_source.addLayout(r_source)

        lbl_source_note = QLabel("Directory containing source .md documents (populated in Document Review, Plan, and Broadcast tabs).")
        lbl_source_note.setStyleSheet("color: #8b949e; font-size: 11px;")
        l_source.addWidget(lbl_source_note)

        lbl_src_title = QLabel("Source Docs Path:")
        lbl_src_title.setStyleSheet("color: #58a6ff; font-weight: bold;")
        form.addRow(lbl_src_title, w_source)

        # 2. Output / Workspace Directory
        w_out = QWidget()
        l_out = QVBoxLayout(w_out)
        l_out.setContentsMargins(0, 0, 0, 0)
        l_out.setSpacing(4)

        r_out = QHBoxLayout()
        r_out.setSpacing(8)
        current_ws = self.config.get("workspace", {}).get("dir", "./workspace")
        txt_out_dir = QLineEdit(current_ws)
        txt_out_dir.setFixedHeight(30)
        r_out.addWidget(txt_out_dir, 1)

        btn_browse_out = QPushButton("📂 Browse...")
        btn_browse_out.setObjectName("btnSecondary")
        btn_browse_out.setFixedHeight(30)
        btn_browse_out.setMinimumWidth(100)

        def on_browse_out():
            init_dir = txt_out_dir.text().strip()
            if not os.path.isabs(init_dir):
                init_dir = os.path.abspath(init_dir)
            if not os.path.exists(init_dir):
                init_dir = os.getcwd()
            chosen = QFileDialog.getExistingDirectory(dialog, "Select Output Workspace Directory", init_dir)
            if chosen:
                txt_out_dir.setText(os.path.normpath(chosen))

        btn_browse_out.clicked.connect(on_browse_out)
        r_out.addWidget(btn_browse_out)
        l_out.addLayout(r_out)

        lbl_out_note = QLabel("Directory where generated documents, version snapshots, git history, and broadcast exports are saved.")
        lbl_out_note.setStyleSheet("color: #8b949e; font-size: 11px;")
        l_out.addWidget(lbl_out_note)

        lbl_out_title = QLabel("Output Folder Path:")
        lbl_out_title.setStyleSheet("color: #10a37f; font-weight: bold;")
        form.addRow(lbl_out_title, w_out)

        layout.addLayout(form)

        btn_box = QHBoxLayout()
        btn_box.addStretch()

        btn_save = QPushButton("Save & Apply")
        btn_save.setFixedHeight(34)
        btn_save.setMinimumWidth(110)
        btn_save.clicked.connect(dialog.accept)
        btn_box.addWidget(btn_save)

        btn_cancel = QPushButton("Cancel")
        btn_cancel.setObjectName("btnSecondary")
        btn_cancel.setFixedHeight(34)
        btn_cancel.setMinimumWidth(80)
        btn_cancel.clicked.connect(dialog.reject)
        btn_box.addWidget(btn_cancel)

        layout.addLayout(btn_box)

        if dialog.exec() == QDialog.Accepted:
            new_source = txt_source_dir.text().strip()
            new_out = txt_out_dir.text().strip()

            if new_source:
                self.docs_dir = os.path.normpath(new_source)
                self.config.setdefault("workspace", {})["docs_dir"] = self.docs_dir
            if new_out:
                self.config.setdefault("workspace", {})["dir"] = os.path.normpath(new_out)

            self._save_config()
            self._load_docs_list()
            self._refresh_git_history()
            self._append_log(f"Settings updated: Source Docs = '{self.docs_dir}', Workspace = '{new_out}'", "success")
            QMessageBox.information(
                self,
                "Settings Saved",
                f"Document paths successfully updated and applied!\n\n"
                f"Source Docs: {self.docs_dir}\n"
                f"Output Workspace: {new_out}\n\n"
                f"All document lists have been refreshed."
            )

    def _open_urls_dialog(self):
        """Open a dedicated modal dialog to view, edit, and test target service URLs."""
        dialog = QDialog(self)
        dialog.setWindowTitle("Configure Target Service URLs & Test Agents")
        dialog.setMinimumWidth(780)
        dialog.setStyleSheet(self.styleSheet())

        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(12)

        header = QLabel("<h3>🌐 Target Service URLs & Verification</h3>")
        header.setStyleSheet("color: #58a6ff; margin-bottom: 2px;")
        layout.addWidget(header)

        desc = QLabel("Configure web endpoints and project library folders, and test each agent connection, prompt handling, and document synchronization:")
        desc.setStyleSheet("color: #8b949e; font-size: 12px; margin-bottom: 6px;")
        desc.setWordWrap(True)
        layout.addWidget(desc)

        form = QFormLayout()
        form.setSpacing(10)
        form.setLabelAlignment(Qt.AlignRight | Qt.AlignTop)

        test_workers = []

        def create_url_test_row(service_key: str, default_text: str, placeholder: str, btn_text: str = "🧪 Test Agent"):
            row_widget = QWidget()
            row_layout = QVBoxLayout(row_widget)
            row_layout.setContentsMargins(0, 0, 0, 0)
            row_layout.setSpacing(4)

            top_row = QHBoxLayout()
            top_row.setSpacing(8)

            txt_edit = QLineEdit(default_text)
            txt_edit.setPlaceholderText(placeholder)
            txt_edit.setFixedHeight(30)
            top_row.addWidget(txt_edit, 1)

            btn_test = QPushButton(btn_text)
            btn_test.setObjectName("btnSecondary")
            btn_test.setFixedHeight(30)
            btn_test.setMinimumWidth(125)
            top_row.addWidget(btn_test)

            row_layout.addLayout(top_row)

            lbl_status = QLabel("")
            lbl_status.setWordWrap(True)
            lbl_status.setStyleSheet("color: #8b949e; font-size: 11px;")
            row_layout.addWidget(lbl_status)

            def on_test_clicked():
                url = txt_edit.text().strip()
                if not url:
                    lbl_status.setText("❌ URL is empty. Please enter a valid URL.")
                    lbl_status.setStyleSheet("color: #f85149; font-size: 11px; font-weight: bold;")
                    return

                btn_test.setEnabled(False)
                btn_test.setText("⏳ Testing...")
                lbl_status.setText("⏳ Connecting to browser and testing...")
                lbl_status.setStyleSheet("color: #58a6ff; font-size: 11px;")

                cfg = dict(self.config)
                cfg.setdefault("browser", {})["use_existing_chrome"] = self.chk_use_existing_chrome.isChecked()
                cfg.setdefault("services", {}).setdefault(service_key.replace("_lib", ""), {})["url"] = url

                worker = AgentTestWorker(service_key=service_key, target_url=url, config=cfg)

                def on_status(msg: str):
                    lbl_status.setText(f"⏳ {msg}")
                    self._append_log(f"[{service_key.upper()} Test] {msg}", "info")

                def on_result(ok: bool, msg: str):
                    btn_test.setEnabled(True)
                    btn_test.setText(btn_text)
                    if ok:
                        lbl_status.setText(f"✅ {msg}")
                        lbl_status.setStyleSheet("color: #3fb950; font-size: 11px; font-weight: bold;")
                        self._append_log(f"[{service_key.upper()} Verified] {msg}", "success")
                    else:
                        lbl_status.setText(f"❌ {msg}")
                        lbl_status.setStyleSheet("color: #f85149; font-size: 11px; font-weight: bold;")
                        self._append_log(f"[{service_key.upper()} Failed] {msg}", "error")

                worker.status_signal.connect(on_status)
                worker.result_signal.connect(on_result)
                worker.finished.connect(lambda: test_workers.remove(worker) if worker in test_workers else None)
                test_workers.append(worker)
                worker.start()

            btn_test.clicked.connect(on_test_clicked)
            return row_widget, txt_edit

        # 1. ChatGPT
        lbl_gpt = QLabel("ChatGPT URL:")
        lbl_gpt.setStyleSheet("color: #10a37f; font-weight: bold;")
        w_gpt, txt_gpt = create_url_test_row("chatgpt", self.txt_chatgpt_url.text(), "https://chatgpt.com/c/<conversation_id>", "🧪 Test Agent")
        form.addRow(lbl_gpt, w_gpt)

        # 2. ChatGPT Lib
        lbl_gpt_lib = QLabel("ChatGPT Lib URL:")
        lbl_gpt_lib.setStyleSheet("color: #10a37f; font-weight: bold;")
        w_gpt_lib, txt_gpt_lib = create_url_test_row("chatgpt_lib", self.txt_chatgpt_lib_url.text(), "https://chatgpt.com/library/d/<folder_id>", "🧪 Test Lib Sync")
        form.addRow(lbl_gpt_lib, w_gpt_lib)

        # 3. NotebookLM
        lbl_nb = QLabel("NotebookLM URL:")
        lbl_nb.setStyleSheet("color: #58a6ff; font-weight: bold;")
        w_nb, txt_nb = create_url_test_row("notebooklm", self.txt_notebooklm_url.text(), "https://notebook.google.com/notebook/<notebook_id>", "🧪 Test Agent & Sync")
        form.addRow(lbl_nb, w_nb)

        # 4. Gemini
        lbl_gemini = QLabel("Gemini URL:")
        lbl_gemini.setStyleSheet("color: #a78bfa; font-weight: bold;")
        w_gemini, txt_gemini = create_url_test_row("gemini", self.txt_gemini_url.text(), "https://gemini.google.com/app", "🧪 Test Agent")
        form.addRow(lbl_gemini, w_gemini)

        # 5. Grok
        lbl_grok = QLabel("Grok URL:")
        lbl_grok.setStyleSheet("color: #f59e0b; font-weight: bold;")
        w_grok, txt_grok = create_url_test_row("grok", self.txt_grok_url.text(), "https://grok.com/project/<project_id>", "🧪 Test Agent & Sync")
        form.addRow(lbl_grok, w_grok)

        # 6. Claude
        lbl_claude = QLabel("Claude URL:")
        lbl_claude.setStyleSheet("color: #d97706; font-weight: bold;")
        w_claude, txt_claude = create_url_test_row("claude", self.txt_claude_url.text(), "https://claude.ai/chat/<chat_id>", "🧪 Test Agent")
        form.addRow(lbl_claude, w_claude)

        # 7. Copilot
        lbl_copilot = QLabel("Copilot URL:")
        lbl_copilot.setStyleSheet("color: #0284c7; font-weight: bold;")
        w_copilot, txt_copilot = create_url_test_row("copilot", self.txt_copilot_url.text(), "https://copilot.microsoft.com/projects/<project_id>", "🧪 Test Agent")
        form.addRow(lbl_copilot, w_copilot)

        layout.addLayout(form)

        btn_box = QHBoxLayout()
        btn_box.addStretch()

        btn_save = QPushButton("Save & Close")
        btn_save.setFixedHeight(34)
        btn_save.setMinimumWidth(110)
        btn_save.clicked.connect(dialog.accept)
        btn_box.addWidget(btn_save)

        btn_cancel = QPushButton("Cancel")
        btn_cancel.setObjectName("btnSecondary")
        btn_cancel.setFixedHeight(34)
        btn_cancel.setMinimumWidth(80)
        btn_cancel.clicked.connect(dialog.reject)
        btn_box.addWidget(btn_cancel)

        layout.addLayout(btn_box)

        if dialog.exec() == QDialog.Accepted:
            self.txt_chatgpt_url.setText(txt_gpt.text().strip())
            self.txt_chatgpt_lib_url.setText(txt_gpt_lib.text().strip())
            self.txt_notebooklm_url.setText(txt_nb.text().strip())
            self.txt_gemini_url.setText(txt_gemini.text().strip())
            self.txt_grok_url.setText(txt_grok.text().strip())
            self.txt_claude_url.setText(txt_claude.text().strip())
            self.txt_copilot_url.setText(txt_copilot.text().strip())
            self._auto_save_prompts()
            self._append_log("Target service URLs updated and saved.", "success")


    def _refresh_git_history(self):
        repo_mgr = WorkspaceRepoManager(
            workspace_dir=self.config.get("workspace", {}).get("dir", "./workspace")
        )
        history = repo_mgr.get_commit_history(max_count=20)
        self.tbl_history.setRowCount(len(history))
        for r, (c_hash, c_date, c_sum) in enumerate(history):
            self.tbl_history.setItem(r, 0, QTableWidgetItem(c_hash))
            self.tbl_history.setItem(r, 1, QTableWidgetItem(c_date))
            self.tbl_history.setItem(r, 2, QTableWidgetItem(c_sum))

        # Check for existing latest doc
        latest_v = repo_mgr.get_latest_version_number()
        if latest_v > 0:
            doc_content = repo_mgr.get_file_content(f"architecture_v{latest_v}.md")
            if doc_content:
                self.txt_output_doc.setMarkdown(doc_content)
                self.output_tabs.setTabText(0, f"Architecture v{latest_v}")

    def _start_orchestration(self):
        current_tab = self.input_tabs.currentIndex()
        is_doc_mode = current_tab == 0
        is_concept_mode = current_tab == 1
        is_plan_mode = current_tab == 2

        doc_filename = ""
        doc_content = ""
        user_input = ""
        iterations = 1

        if is_plan_mode:
            current_item = self.list_plan_docs.currentItem()
            if not current_item:
                QMessageBox.warning(self, "No Plan Selected", "Please select a plan document from the list.")
                return
            doc_filename = current_item.data(Qt.UserRole)
            fpath = os.path.join(self.docs_dir, doc_filename)
            if not os.path.exists(fpath):
                QMessageBox.critical(self, "File Not Found", f"Cannot find plan document at: {fpath}")
                return
            with open(fpath, "r", encoding="utf-8", errors="replace") as f:
                doc_content = f.read()
            iterations = self.combo_iterations.currentIndex() + 1
        elif is_doc_mode:
            current_item = self.list_docs.currentItem()
            if not current_item:
                QMessageBox.warning(self, "No Document Selected", "Please select a document from the list.")
                return
            doc_filename = current_item.data(Qt.UserRole)
            fpath = os.path.join(self.docs_dir, doc_filename)
            if not os.path.exists(fpath):
                QMessageBox.critical(self, "File Not Found", f"Cannot find document at: {fpath}")
                return
        doc_filename = ""
        doc_content = ""

        if is_plan_mode:
            current_item = self.list_plan_docs.currentItem()
            if current_item:
                doc_filename = current_item.data(Qt.UserRole)
                fpath = os.path.join(self.docs_dir, doc_filename)
                if os.path.exists(fpath):
                    with open(fpath, "r", encoding="utf-8", errors="replace") as f:
                        doc_content = f.read()

            stage1_prompt = self.txt_plan_chatgpt1_prompt.toPlainText().strip()
            prompt_gemini = self.txt_plan_gemini_prompt.toPlainText().strip()
            stage2_prompt = self.txt_plan_notebooklm_prompt.toPlainText().strip()
            stage3_prompt = self.txt_plan_chatgpt_final_prompt.toPlainText().strip()
            iterations = self.combo_iterations.currentIndex() + 1
        elif is_doc_mode:
            current_item = self.list_docs.currentItem()
            if current_item:
                doc_filename = current_item.data(Qt.UserRole)
                fpath = os.path.join(self.docs_dir, doc_filename)
                if os.path.exists(fpath):
                    with open(fpath, "r", encoding="utf-8", errors="replace") as f:
                        doc_content = f.read()

            stage1_prompt = self.txt_doc_stage1_prompt.toPlainText().strip()
            prompt_gemini = self.txt_prompt_gemini.toPlainText().strip()
            stage2_prompt = self.txt_doc_stage2_prompt.toPlainText().strip()
            stage3_prompt = self.txt_doc_stage3_prompt.toPlainText().strip()
            iterations = 1
        else:
            user_input = self.txt_concept.toPlainText().strip()
            if not user_input:
                QMessageBox.warning(self, "Empty Concept", "Please enter a system concept in the text box.")
                return

            stage1_prompt = self.txt_concept_stage1_prompt.toPlainText().strip()
            prompt_gemini = self.txt_prompt_gemini.toPlainText().strip()
            stage2_prompt = self.txt_concept_stage2_prompt.toPlainText().strip()
            stage3_prompt = self.txt_doc_stage3_prompt.toPlainText().strip()
            iterations = 1

        if not is_plan_mode and not stage1_prompt:
            QMessageBox.warning(self, "Empty Stage 1 Prompt", "Please enter a prompt for Stage 1 (ChatGPT).")
            return

        # Save all current prompts and agent dropdown selections
        self._auto_save_prompts()

        # Prepare updated config
        cfg = dict(self.config)
        cfg.setdefault("browser", {})["use_existing_chrome"] = self.chk_use_existing_chrome.isChecked()
        cfg.setdefault("services", {}).setdefault("chatgpt", {})["url"] = self.txt_chatgpt_url.text().strip()
        cfg.setdefault("services", {}).setdefault("notebooklm", {})["url"] = self.txt_notebooklm_url.text().strip()
        cfg.setdefault("services", {}).setdefault("gemini", {})["url"] = self.txt_gemini_url.text().strip()
        cfg.setdefault("services", {}).setdefault("grok", {})["url"] = self.txt_grok_url.text().strip()
        cfg.setdefault("services", {}).setdefault("claude", {})["url"] = self.txt_claude_url.text().strip()
        cfg.setdefault("services", {}).setdefault("copilot", {})["url"] = self.txt_copilot_url.text().strip()

        self.btn_run.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self.progress_bar.setValue(0)
        self.output_tabs.setCurrentWidget(self.txt_log)  # Switch to Live Execution Log

        mode_desc = f"Tri-Model Plan Refinement ({iterations} iterations)" if is_plan_mode else "Stage 1: ChatGPT | Stage 2: NotebookLM | Stage 3: ChatGPT"
        self._append_log(f"Starting orchestration session ({mode_desc})...", "info")

        self.worker = OrchestrationWorker(
            config=cfg,
            user_input=user_input,
            is_doc_mode=is_doc_mode,
            doc_filename=doc_filename,
            doc_content=doc_content,
            stage1_prompt=stage1_prompt,
            stage2_prompt=stage2_prompt,
            stage3_prompt=stage3_prompt,
            prompt_gemini=prompt_gemini,
            critic_service=self.combo_critic.currentText().lower(),
            chatgpt_url=self.txt_chatgpt_url.text().strip(),
            chatgpt_lib_url=self.txt_chatgpt_lib_url.text().strip(),
            gemini_url=self.txt_gemini_url.text().strip(),
            grok_url=self.txt_grok_url.text().strip(),
            claude_url=self.txt_claude_url.text().strip(),
            copilot_url=self.txt_copilot_url.text().strip(),
            max_iterations=iterations,
            plan_agent1=self.combo_plan_agent1.currentText() if hasattr(self, "combo_plan_agent1") else "ChatGPT",
            plan_agent2=self.combo_plan_agent2.currentText() if hasattr(self, "combo_plan_agent2") else "Gemini",
            plan_agent3=self.combo_plan_agent3.currentText() if hasattr(self, "combo_plan_agent3") else "NotebookLM",
            plan_agent4=self.combo_plan_agent4.currentText() if hasattr(self, "combo_plan_agent4") else "ChatGPT",
            is_plan_mode=is_plan_mode,
            auto_freeze=self.chk_auto_freeze.isChecked(),
            fresh_session=self.chk_fresh_session.isChecked(),
        )

        self.worker.log_signal.connect(self._append_log)
        self.worker.status_signal.connect(self.status_bar.showMessage)
        self.worker.progress_signal.connect(self.progress_bar.setValue)
        self.worker.doc_updated_signal.connect(self._on_doc_updated)
        self.worker.critique_updated_signal.connect(self._on_critique_updated)
        self.worker.history_updated_signal.connect(self._on_history_updated)
        self.worker.finished_signal.connect(self._on_orchestration_finished)

        self.worker.start()

    def _select_all_broadcast_agents(self, select_all: bool):
        """Quick toggle to select or deselect all broadcast agent checkboxes."""
        if hasattr(self, "chk_agent_chatgpt"):
            self.chk_agent_chatgpt.setChecked(select_all)
            self.chk_agent_notebooklm.setChecked(select_all)
            self.chk_agent_gemini.setChecked(select_all)
            self.chk_agent_grok.setChecked(select_all)
            self.chk_agent_claude.setChecked(select_all)
            self.chk_agent_copilot.setChecked(select_all)
            self._auto_save_prompts()

    def _start_broadcast_prompt(self):
        """Submit the broadcast prompt to all selected AI agents in Tab 4."""
        prompt = self.txt_broadcast_prompt.toPlainText().strip()
        if not prompt:
            QMessageBox.warning(self, "Empty Prompt", "Please enter a prompt to broadcast.")
            return

        selected_agents = []
        if self.chk_agent_chatgpt.isChecked():
            selected_agents.append("ChatGPT")
        if self.chk_agent_notebooklm.isChecked():
            selected_agents.append("NotebookLM")
        if self.chk_agent_gemini.isChecked():
            selected_agents.append("Gemini")
        if self.chk_agent_grok.isChecked():
            selected_agents.append("Grok")
        if self.chk_agent_claude.isChecked():
            selected_agents.append("Claude")
        if self.chk_agent_copilot.isChecked():
            selected_agents.append("Copilot")

        if not selected_agents:
            QMessageBox.warning(self, "No Agents Selected", "Please select at least one AI agent to receive the prompt.")
            return

        # Check attached F-document and checkbox
        selected_doc_item = self.list_broadcast_docs.currentItem() if hasattr(self, "list_broadcast_docs") else None
        selected_doc_name = selected_doc_item.data(Qt.UserRole) if selected_doc_item else "None"
        should_attach = self.chk_attach_doc_content.isChecked() if hasattr(self, "chk_attach_doc_content") else True

        effective_prompt = prompt
        doc_header = ""
        if should_attach and selected_doc_name and selected_doc_name != "None":
            # Search in docs_dir or workspace
            fpath = os.path.join(self.docs_dir, selected_doc_name)
            if not os.path.exists(fpath):
                fpath = os.path.join(self.config.get("workspace", {}).get("dir", "./workspace"), selected_doc_name)

            doc_content = ""
            if os.path.exists(fpath):
                with open(fpath, "r", encoding="utf-8", errors="replace") as f:
                    doc_content = f.read().strip()

            if doc_content:
                # Prompt description in the box is the first part, second is entire selected doc
                effective_prompt = f"{prompt}\n\n---\n\n## Document: {selected_doc_name}\n\n```markdown\n{doc_content}\n```"
            else:
                effective_prompt = f"{prompt}\n\n---\n\n## Document: {selected_doc_name}"
            doc_header = selected_doc_name

        self._auto_save_prompts()

        # Update UI Controls
        self.btn_broadcast_submit.setEnabled(False)
        self.btn_broadcast_stop.setEnabled(True)
        self.btn_broadcast_download.setEnabled(False)
        self.btn_run.setEnabled(False)
        self.btn_run_stage2_only.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self.progress_bar.setValue(0)

        # Clear and prepare Recommendations & Multi-Agent Responses view
        doc_sub = f"\n**Attached Document:** `{selected_doc_name}`\n" if (should_attach and selected_doc_name != "None") else ""
        init_md = (
            f"# 🚀 Multi-Agent Recommendations & Responses\n\n"
            f"**Broadcasting to:** {', '.join(selected_agents)}{doc_sub}\n\n"
            f"**Prompt:**\n> {prompt}\n\n"
            f"---\n\n"
            f"⏳ *Broadcasting in progress... Responses will appear below live one by one as each agent finishes.*\n"
        )
        self.txt_output_recommendations.setMarkdown(init_md)
        self.txt_output_broadcast.setMarkdown(init_md)
        self.output_tabs.setCurrentWidget(self.txt_output_recommendations)  # Switch to Recommendations tab immediately

        self._append_log(f"Starting Multi-Agent Broadcast to {len(selected_agents)} agents: {', '.join(selected_agents)} (Attached Doc: {selected_doc_name if should_attach else 'None'})...", "info")

        self.last_broadcast_md = ""
        self.last_broadcast_file = ""

        self.broadcast_worker = BroadcastWorker(
            prompt=effective_prompt,
            user_prompt=prompt,
            doc_name=doc_header,
            selected_agents=selected_agents,
            config=self.config
        )
        self.broadcast_worker.status_signal.connect(self.status_bar.showMessage)
        self.broadcast_worker.progress_signal.connect(self.progress_bar.setValue)
        self.broadcast_worker.log_signal.connect(self._append_log)
        self.broadcast_worker.agent_response_signal.connect(self._on_broadcast_agent_response)
        self.broadcast_worker.finished_signal.connect(self._on_broadcast_finished)
        self.broadcast_worker.start()

    def _stop_broadcast(self):
        """Cancel the active broadcast process."""
        if hasattr(self, "broadcast_worker") and self.broadcast_worker and self.broadcast_worker.isRunning():
            self._append_log("Stopping Multi-Agent Broadcast...", "warning")
            self.broadcast_worker.cancel()
            self.btn_broadcast_stop.setEnabled(False)
            self.status_bar.showMessage("Broadcast stopping...")

    def _on_broadcast_agent_response(self, agent_name: str, response_text: str):
        """Append agent's reply live to Recommendations and Multi-Agent Responses tabs as they arrive."""
        for target_widget in [self.txt_output_recommendations, self.txt_output_broadcast]:
            current_md = target_widget.toMarkdown()
            if "⏳ *Broadcasting in progress..." in current_md:
                current_md = current_md.replace("⏳ *Broadcasting in progress... Responses will appear below live one by one as each agent finishes.*", "")
            if "*Waiting for responses...*" in current_md:
                current_md = current_md.replace("*Waiting for responses...*", "")
            current_md += f"\n\n# 🤖 {agent_name} Response\n\n{response_text.strip()}\n\n---\n"
            target_widget.setMarkdown(current_md)
            target_widget.moveCursor(QTextCursor.End)

    def _on_broadcast_finished(self, success: bool, full_markdown: str, file_path: str):
        """Handler when multi-agent broadcast completes."""
        self.btn_broadcast_submit.setEnabled(True)
        self.btn_broadcast_stop.setEnabled(False)
        self.btn_run.setEnabled(True)
        self.btn_run_stage2_only.setEnabled(True)
        self.btn_stop.setEnabled(False)

        if success:
            self.last_broadcast_md = full_markdown
            self.last_broadcast_file = file_path
            self.btn_broadcast_download.setEnabled(True)
            self.txt_output_recommendations.setMarkdown(full_markdown)
            self.txt_output_broadcast.setMarkdown(full_markdown)
            self.output_tabs.setCurrentWidget(self.txt_output_recommendations)  # Keep Recommendations active
            self._append_log(f"Multi-Agent Broadcast completed! Saved to {os.path.basename(file_path)}", "success")
            QMessageBox.information(
                self,
                "Broadcast Complete",
                f"Successfully received replies from all selected agents!\n\nSaved to:\n{file_path}\n\nYou can view full responses in the 'Recommendations' tab or click Download."
            )
        else:
            self._append_log(f"Multi-Agent Broadcast ended: {full_markdown}", "error")
            QMessageBox.warning(self, "Broadcast Incomplete", f"Broadcast finished with issues:\n{full_markdown}")

    def _download_broadcast_response(self):
        """Save/Export the aggregated responses markdown to user-chosen location."""
        if not hasattr(self, "last_broadcast_md") or not self.last_broadcast_md:
            QMessageBox.warning(self, "No Data", "No broadcast responses available to export.")
            return

        default_name = os.path.basename(self.last_broadcast_file) if hasattr(self, "last_broadcast_file") and self.last_broadcast_file else f"BROADCAST_RESPONSES_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
        save_path, _ = QFileDialog.getSaveFileName(
            self,
            "Save Multi-Agent Broadcast Responses",
            os.path.join(self.config.get("workspace", {}).get("dir", "./workspace"), default_name),
            "Markdown Files (*.md);;All Files (*)"
        )
        if save_path:
            try:
                with open(save_path, "w", encoding="utf-8") as f:
                    f.write(self.last_broadcast_md)
                self._append_log(f"Exported broadcast responses to: {save_path}", "success")
                QMessageBox.information(self, "Export Successful", f"Responses saved to:\n{save_path}")
            except Exception as e_exp:
                QMessageBox.critical(self, "Export Error", f"Failed to save file:\n{e_exp}")

    def _start_stage2_only_orchestration(self):
        """Execute Stage 2 NotebookLM review independently using the latest document or selected file."""
        current_tab = self.input_tabs.currentIndex()
        is_doc_mode = current_tab == 0
        is_plan_mode = current_tab == 2
        doc_filename = ""
        doc_content = ""

        if is_plan_mode:
            current_item = self.list_plan_docs.currentItem()
            if current_item:
                doc_filename = current_item.data(Qt.UserRole)
                fpath = os.path.join(self.docs_dir, doc_filename)
                if os.path.exists(fpath):
                    with open(fpath, "r", encoding="utf-8", errors="replace") as f:
                        doc_content = f.read()
            stage2_prompt = self.txt_plan_notebooklm_prompt.toPlainText().strip()
        elif is_doc_mode:
            current_item = self.list_docs.currentItem()
            if current_item:
                doc_filename = current_item.data(Qt.UserRole)
                fpath = os.path.join(self.docs_dir, doc_filename)
                if os.path.exists(fpath):
                    with open(fpath, "r", encoding="utf-8", errors="replace") as f:
                        doc_content = f.read()
            stage2_prompt = self.txt_doc_stage2_prompt.toPlainText().strip()
        else:
            stage2_prompt = self.txt_concept_stage2_prompt.toPlainText().strip()

        if not stage2_prompt:

            QMessageBox.warning(self, "Empty Stage 2 Prompt", "Please enter a prompt for Stage 2 (NotebookLM).")
            return

        cfg = dict(self.config)
        cfg.setdefault("browser", {})["use_existing_chrome"] = self.chk_use_existing_chrome.isChecked()
        cfg.setdefault("services", {}).setdefault("notebooklm", {})["url"] = self.txt_notebooklm_url.text().strip()

        self.btn_run.setEnabled(False)
        self.btn_run_stage2_only.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self.progress_bar.setValue(0)
        self.output_tabs.setCurrentWidget(self.txt_log)  # Switch to Live Execution Log

        self._append_log("Starting Stage 2 Only (NotebookLM Cross-Document Evaluation)...", "info")

        self.worker = OrchestrationWorker(
            config=cfg,
            user_input="",
            is_doc_mode=is_doc_mode,
            doc_filename=doc_filename,
            doc_content=doc_content,
            stage1_prompt="",
            stage2_prompt=stage2_prompt,
            critic_service="notebooklm",
            max_iterations=1,
            auto_freeze=False,
            fresh_session=False,
            only_stage2=True,
        )

        self.worker.log_signal.connect(self._append_log)
        self.worker.status_signal.connect(self.status_bar.showMessage)
        self.worker.progress_signal.connect(self.progress_bar.setValue)
        self.worker.doc_updated_signal.connect(self._on_doc_updated)
        self.worker.critique_updated_signal.connect(self._on_critique_updated)
        self.worker.history_updated_signal.connect(self._on_history_updated)
        self.worker.finished_signal.connect(self._on_orchestration_finished)

        self.worker.start()

    def _stop_orchestration(self):

        if hasattr(self, "broadcast_worker") and self.broadcast_worker and self.broadcast_worker.isRunning():
            self._stop_broadcast()
        if self.worker and self.worker.isRunning():
            self._append_log("Stopping orchestration session...", "warning")
            self.worker.cancel()
            self.btn_stop.setEnabled(False)

    def _on_doc_updated(self, title: str, content: str):
        self.output_tabs.setTabText(0, title)
        self.txt_output_doc.setMarkdown(content)

    def _on_critique_updated(self, content: str):
        self.txt_output_critique.setMarkdown(content)

    def _on_history_updated(self, history: list):
        self.tbl_history.setRowCount(len(history))
        for r, (c_hash, c_date, c_sum) in enumerate(history):
            self.tbl_history.setItem(r, 0, QTableWidgetItem(c_hash))
            self.tbl_history.setItem(r, 1, QTableWidgetItem(c_date))
            self.tbl_history.setItem(r, 2, QTableWidgetItem(c_sum))

    def _on_orchestration_finished(self, success: bool, message: str):
        self.btn_run.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.progress_bar.setValue(100 if success else 0)

        if success:
            self._append_log(f"Orchestration completed successfully! {message}", "success")
            self.output_tabs.setCurrentIndex(0)  # Show document
            QMessageBox.information(self, "Completed", f"Orchestration finished!\n\n{message}")
        else:
            self._append_log(f"Orchestration finished with error: {message}", "error")
            QMessageBox.critical(self, "Execution Error", f"Orchestration encountered an error:\n\n{message}")


def main():
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
