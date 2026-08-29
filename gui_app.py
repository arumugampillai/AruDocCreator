import asyncio
import json
import logging
import os
import re
import site
import subprocess
import sys
import time
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
        max_iterations: int = 1,
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
        self.max_iterations = max_iterations
        self.is_plan_mode = is_plan_mode

        self.auto_freeze = auto_freeze
        self.fresh_session = fresh_session
        self.only_stage2 = only_stage2
        self.is_cancelled = False
        self.controller: Optional[QwenTrafficController] = None
        self.automator: Optional[BrowserAutomator] = None
        self.repo_manager: Optional[WorkspaceRepoManager] = None





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

                # STEP 0a: Update selected F-document in NotebookLM sources with version suffix
                notebook_url = self.config.get("services", {}).get("notebooklm", {}).get("url", self.automator.notebooklm_url)
                self.log_signal.emit(
                    f"Step 0a: Updating NotebookLM sources with '{versioned_doc_filename}'...",
                    "info",
                )
                self.status_signal.emit(f"Uploading {versioned_doc_filename} to NotebookLM...")
                self.progress_signal.emit(5)

                temp_source_file = os.path.join(self.repo_manager.workspace_dir, versioned_doc_filename)
                with open(temp_source_file, "w", encoding="utf-8", errors="replace") as f:
                    f.write(current_doc)

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

                # STEP 0b: Update selected F-document in ChatGPT Library/Project folder (if configured)
                gpt_lib_url = (self.chatgpt_lib_url or self.config.get("services", {}).get("chatgpt", {}).get("library_url", "")).strip()
                if gpt_lib_url:
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

                self.progress_signal.emit(10)


                target_doc_name = versioned_doc_filename

                for iter_idx in range(1, self.max_iterations + 1):
                    if self.is_cancelled:
                        break

                    self.log_signal.emit(
                        f"=== Iteration {iter_idx}/{self.max_iterations}: Dual Review (Gemini & NotebookLM) ===",
                        "info",
                    )

                    # 1. Gemini Review
                    self.status_signal.emit(f"[Iter {iter_idx}/{self.max_iterations}] Querying Gemini for critique...")
                    self.progress_signal.emit(int(10 + (iter_idx - 1) * 80 / self.max_iterations + 10 / self.max_iterations))
                    gemini_instruction = (
                        self.prompt_gemini.strip()
                        if (self.prompt_gemini and len(self.prompt_gemini.strip()) > 0)
                        else self.stage2_prompt.strip()
                    )
                    gemini_prompt = f"{gemini_instruction}\n\nDocument Under Review: {target_doc_name}"
                    self.log_signal.emit(f"[Iter {iter_idx}] Sending prompt + '{target_doc_name}' to Gemini...", "info")
                    gemini_critique = await self.automator.query_gemini(
                        gemini_prompt, new_chat=False, gemini_url=self.gemini_url
                    )

                    gemini_filename = f"{prefix}-iter{iter_idx}-gemini-critique.md"
                    self.repo_manager.save_named_file(
                        filename=gemini_filename,
                        content=gemini_critique,
                        commit_message=f"docs(gemini): iteration {iter_idx}/{self.max_iterations} review -> {gemini_filename}",
                    )
                    self.log_signal.emit(f"[Iter {iter_idx}] Gemini critique received ({len(gemini_critique)} chars).", "success")

                    # 2. NotebookLM Review (Direct chat query with prompt + document filename)
                    self.status_signal.emit(f"[Iter {iter_idx}/{self.max_iterations}] Querying NotebookLM for cross-document evaluation...")
                    self.progress_signal.emit(int(10 + (iter_idx - 1) * 80 / self.max_iterations + 35 / self.max_iterations))
                    nb_instruction = self.stage2_prompt.strip()
                    nb_prompt = f"{nb_instruction}\n\nDocument Under Review: {target_doc_name}"

                    notebook_url = self.config.get("services", {}).get("notebooklm", {}).get("url", self.automator.notebooklm_url)
                    self.log_signal.emit(f"[Iter {iter_idx}] Sending prompt + '{target_doc_name}' to NotebookLM ({notebook_url})...", "info")
                    notebooklm_critique = await self.automator.query_notebooklm(
                        prompt=nb_prompt,
                        notebook_url=notebook_url,
                    )
                    nb_filename = f"{prefix}-iter{iter_idx}-notebooklm-critique.md"
                    self.repo_manager.save_named_file(
                        filename=nb_filename,
                        content=notebooklm_critique,
                        commit_message=f"docs(notebooklm): iteration {iter_idx}/{self.max_iterations} evaluation -> {nb_filename}",
                    )
                    self.log_signal.emit(f"[Iter {iter_idx}] NotebookLM evaluation received ({len(notebooklm_critique)} chars).", "success")

                    # GATE 1 ➔ 2: Validate Phase 1 outputs
                    PhaseGatekeeper.validate_phase_1_outputs(gemini_critique, notebooklm_critique, self.log_signal)

                    # Emit combined dual critique to GUI
                    combined_critique = (
                        f"# Dual Critic Evaluation — Iteration {iter_idx}/{self.max_iterations}\n\n"
                        f"## Target Document: {target_doc_name}\n\n"
                        f"## 1. Gemini Review Critique\n{gemini_critique}\n\n"
                        f"---\n\n"
                        f"## 2. NotebookLM Cross-Document Evaluation\n{notebooklm_critique}"
                    )
                    self.critique_updated_signal.emit(combined_critique)

                    if self.is_cancelled:
                        break

                    # 3. ChatGPT Aggregator & Refiner
                    self.status_signal.emit(f"[Iter {iter_idx}/{self.max_iterations}] ChatGPT Aggregator synthesizing & refining plan...")
                    self.progress_signal.emit(int(10 + (iter_idx - 1) * 80 / self.max_iterations + 70 / self.max_iterations))
                    user_chatgpt_instruction = (
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
                    aggregator_prompt = f"""{user_chatgpt_instruction}

Document Under Review: {target_doc_name}

### 1. Gemini Review Critique (Iteration {iter_idx}/{self.max_iterations}):
---
{gemini_critique}
---

### 2. NotebookLM Cross-Document Evaluation Critique (Iteration {iter_idx}/{self.max_iterations}):
---
{notebooklm_critique}
---

### Baseline Document ({baseline_version}):
---
{current_doc}
---

Please produce the complete revised and refined implementation plan version {target_next_version} incorporating all valid points."""

                    self.log_signal.emit(f"[Iter {iter_idx}] ChatGPT Aggregator synthesizing dual feedback for '{target_doc_name}' ({target_next_version})...", "info")

                    refined_output = await self.automator.query_chatgpt(
                        aggregator_prompt, new_chat=False, chatgpt_url=self.chatgpt_url
                    )


                    # GATE 2 ➔ 3: Validate Phase 2 consolidated output
                    PhaseGatekeeper.validate_phase_2_output(refined_output, target_next_version, self.log_signal)

                    if refined_output and len(refined_output.strip()) > 10:
                        current_doc = refined_output
                        iter_out_name = (
                            f"{prefix}-final-done-by-chatgpt.md"
                            if iter_idx == self.max_iterations
                            else f"{prefix}-iter{iter_idx}-chatgpt-refined.md"
                        )
                        saved_path = self.repo_manager.save_named_file(
                            filename=iter_out_name,
                            content=current_doc,
                            commit_message=f"docs(chatgpt): iteration {iter_idx}/{self.max_iterations} refined plan -> {iter_out_name}",
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
                    f"Tri-Model Plan Refinement completed successfully across {self.max_iterations} iterations.",
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
        if hasattr(self, "combo_iterations"):
            self.config.setdefault("orchestration", {})["plan_iterations"] = self.combo_iterations.currentIndex() + 1

        # Save Active Tab & Selected Documents
        if hasattr(self, "input_tabs"):
            self.config.setdefault("ui", {})["active_tab"] = self.input_tabs.currentIndex()
        if hasattr(self, "list_docs") and self.list_docs.currentItem():
            self.config.setdefault("ui", {})["selected_doc"] = self.list_docs.currentItem().data(Qt.UserRole)
        if hasattr(self, "list_plan_docs") and self.list_plan_docs.currentItem():
            self.config.setdefault("ui", {})["selected_plan_doc"] = self.list_plan_docs.currentItem().data(Qt.UserRole)

        self._save_config()





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

        # -------------------------------------------------------------
        # TARGET URLS CONFIGURATION BAR (ChatGPT & NotebookLM)
        # -------------------------------------------------------------
        urls_bar = QHBoxLayout()
        urls_bar.setSpacing(12)

        # ChatGPT Target URL
        lbl_gpt = QLabel("ChatGPT URL:")
        lbl_gpt.setStyleSheet("color: #10a37f; font-weight: bold;")
        urls_bar.addWidget(lbl_gpt)

        default_gpt_url = self.config.get("services", {}).get("chatgpt", {}).get(
            "url", "https://chatgpt.com/c/6a918785-fca8-83ee-8bb6-422f629090d0"
        )
        self.txt_chatgpt_url = QLineEdit(default_gpt_url)
        self.txt_chatgpt_url.setPlaceholderText("https://chatgpt.com/c/<conversation_id>")
        self.txt_chatgpt_url.textChanged.connect(self._auto_save_prompts)
        urls_bar.addWidget(self.txt_chatgpt_url, 1)

        # ChatGPT Library / Project URL
        lbl_gpt_lib = QLabel("ChatGPT Lib:")
        lbl_gpt_lib.setStyleSheet("color: #10a37f; font-weight: bold;")
        urls_bar.addWidget(lbl_gpt_lib)

        default_gpt_lib_url = self.config.get("services", {}).get("chatgpt", {}).get(
            "library_url", "https://chatgpt.com/library/d/6a723b6fe06c819199240f5a593f7ab4"
        )
        self.txt_chatgpt_lib_url = QLineEdit(default_gpt_lib_url)
        self.txt_chatgpt_lib_url.setPlaceholderText("https://chatgpt.com/library/d/<folder_id>")
        self.txt_chatgpt_lib_url.textChanged.connect(self._auto_save_prompts)
        urls_bar.addWidget(self.txt_chatgpt_lib_url, 1)


        # NotebookLM Target URL
        lbl_nb = QLabel("NotebookLM Target URL:")
        lbl_nb.setStyleSheet("color: #58a6ff; font-weight: bold;")
        urls_bar.addWidget(lbl_nb)

        default_nb_url = self.config.get("services", {}).get("notebooklm", {}).get(
            "url", "https://notebook.google.com/notebook/b7a81a2b-5485-493a-bbfb-cbe58808dfc3"
        )
        self.txt_notebooklm_url = QLineEdit(default_nb_url)
        self.txt_notebooklm_url.setPlaceholderText("https://notebook.google.com/notebook/<notebook_id>")
        self.txt_notebooklm_url.textChanged.connect(self._auto_save_prompts)
        urls_bar.addWidget(self.txt_notebooklm_url, 1)

        # Gemini Target URL
        lbl_gemini = QLabel("Gemini Target URL:")
        lbl_gemini.setStyleSheet("color: #a78bfa; font-weight: bold;")
        urls_bar.addWidget(lbl_gemini)

        default_gemini_url = self.config.get("services", {}).get("gemini", {}).get(
            "url", "https://gemini.google.com/app"
        )
        self.txt_gemini_url = QLineEdit(default_gemini_url)
        self.txt_gemini_url.setPlaceholderText("https://gemini.google.com/app")
        self.txt_gemini_url.textChanged.connect(self._auto_save_prompts)
        urls_bar.addWidget(self.txt_gemini_url, 1)

        # Grok Target URL
        lbl_grok = QLabel("Grok Target URL:")
        lbl_grok.setStyleSheet("color: #f59e0b; font-weight: bold;")
        urls_bar.addWidget(lbl_grok)

        default_grok_url = self.config.get("services", {}).get("grok", {}).get(
            "url", "https://grok.com/project/3a4c5217-9801-44e6-bb35-60f7e17eca21"
        )
        self.txt_grok_url = QLineEdit(default_grok_url)
        self.txt_grok_url.setPlaceholderText("https://grok.com/project/<project_id>")
        self.txt_grok_url.textChanged.connect(self._auto_save_prompts)
        urls_bar.addWidget(self.txt_grok_url, 1)

        main_layout.addLayout(urls_bar)



        # -------------------------------------------------------------
        # MAIN HORIZONTAL SPLITTER (Left: Inputs & Prompts, Right: Outputs)
        # -------------------------------------------------------------
        splitter = QSplitter(Qt.Horizontal)

        # LEFT PANEL: Input & Doc Browser + Stage 1 & Stage 2 Prompt Boxes
        left_scroll = QScrollArea()
        left_scroll.setWidgetResizable(True)
        left_scroll.setFrameShape(QFrame.NoFrame)

        left_widget = QWidget()
        left_layout = QVBoxLayout(left_widget)
        left_layout.setContentsMargins(0, 0, 8, 0)
        # Document / Concept Selection Tabs
        self.input_tabs = QTabWidget()

        # Tab 1: Doc Review Mode (AruMLStudio docs)
        tab_docs = QWidget()

        tab_docs_layout = QVBoxLayout(tab_docs)

        lbl_filter = QLabel("Search Document Prefix (e.g. 00, 01, 08, F1, 15):")
        tab_docs_layout.addWidget(lbl_filter)

        self.txt_doc_search = QLineEdit()
        self.txt_doc_search.setPlaceholderText("Type prefix (e.g. 01, F1) or filter docs...")
        self.txt_doc_search.textChanged.connect(self._filter_docs_list)
        tab_docs_layout.addWidget(self.txt_doc_search)

        self.list_docs = QListWidget()
        self.list_docs.setFixedHeight(120)
        self.list_docs.itemClicked.connect(self._on_doc_selected)
        tab_docs_layout.addWidget(self.list_docs)

        self.lbl_selected_doc_info = QLabel("Selected: None")
        self.lbl_selected_doc_info.setStyleSheet("color: #58a6ff; font-weight: bold;")
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

        self.input_tabs.addTab(tab_docs, "AruMLStudio Docs Selection")

        # Tab 2: Custom Architecture Concept
        tab_concept = QWidget()
        tab_concept_layout = QVBoxLayout(tab_concept)

        tab_concept_layout.addWidget(QLabel("Enter Custom Architecture Requirements:"))
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

        lbl_plan_filter = QLabel("Select Implementation Plan Document (e.g. F-ENHANCEMENT_ARCHITECTURE_IMPLEMENTATION_PLAN.md):")
        tab_plan_layout.addWidget(lbl_plan_filter)

        self.txt_plan_search = QLineEdit()
        self.txt_plan_search.setPlaceholderText("Search enhancement plans (e.g. F-*, plan)...")
        self.txt_plan_search.textChanged.connect(self._filter_plan_docs_list)
        tab_plan_layout.addWidget(self.txt_plan_search)

        self.list_plan_docs = QListWidget()
        self.list_plan_docs.setFixedHeight(100)
        self.list_plan_docs.itemClicked.connect(self._on_plan_doc_selected)
        tab_plan_layout.addWidget(self.list_plan_docs)

        self.lbl_selected_plan_info = QLabel("Selected Plan: None")
        self.lbl_selected_plan_info.setStyleSheet("color: #a78bfa; font-weight: bold;")
        tab_plan_layout.addWidget(self.lbl_selected_plan_info)

        # Interaction Iterations Selector Row
        iter_row = QHBoxLayout()
        lbl_iter = QLabel("Interaction Iterations (ChatGPT ➔ Gemini/NotebookLM):")
        lbl_iter.setStyleSheet("color: #e3b341; font-weight: bold;")
        iter_row.addWidget(lbl_iter)

        self.combo_iterations = QComboBox()
        for i in range(1, 11):
            suffix = " (Default)" if i == 3 else ""
            self.combo_iterations.addItem(f"{i} Iteration{'s' if i > 1 else ''}{suffix}", i)

        saved_iters = self.config.get("orchestration", {}).get("plan_iterations", 3)
        target_idx = max(0, min(9, saved_iters - 1))
        self.combo_iterations.setCurrentIndex(target_idx)
        self.combo_iterations.currentIndexChanged.connect(self._auto_save_prompts)
        iter_row.addWidget(self.combo_iterations)

        self.btn_refresh_plan_docs = QPushButton("🔄 Refresh Files (Latest)")
        self.btn_refresh_plan_docs.setObjectName("btnSecondary")
        self.btn_refresh_plan_docs.setToolTip("Rescan docs folder and automatically select the most recently updated file")
        self.btn_refresh_plan_docs.clicked.connect(self._on_refresh_files_clicked)
        iter_row.addWidget(self.btn_refresh_plan_docs)

        iter_row.addStretch()
        tab_plan_layout.addLayout(iter_row)


        lbl_flow_info = QLabel("Tri-Model Flow: ChatGPT (Aggregator) ➔ Gemini & NotebookLM (Dual Reviewers) ➔ ChatGPT (Refined Plan) [Loops across selected iterations]")
        lbl_flow_info.setStyleSheet("color: #8b949e; font-size: 11px; font-style: italic;")
        tab_plan_layout.addWidget(lbl_flow_info)

        # Tab 3 Prompts: 4 Dedicated Boxes
        grp_plan_gpt1 = QGroupBox("1. Prompt for ChatGPT (Initial Plan / Source Preparation)")
        layout_plan_gpt1 = QVBoxLayout(grp_plan_gpt1)
        self.txt_plan_chatgpt1_prompt = QTextEdit()
        self.txt_plan_chatgpt1_prompt.setFixedHeight(75)
        saved_plan_gpt1 = self.config.get("prompts", {}).get(
            "plan_chatgpt1_prompt",
            "collect import point from this reply of my friends and what suit for this enhancement. and importantly dont miss old points. you neeed to aggregate this as per doc, then will share it with my friend until it get finized",
        )
        self.txt_plan_chatgpt1_prompt.setText(saved_plan_gpt1)
        self.txt_plan_chatgpt1_prompt.textChanged.connect(self._auto_save_prompts)
        layout_plan_gpt1.addWidget(self.txt_plan_chatgpt1_prompt)
        tab_plan_layout.addWidget(grp_plan_gpt1)

        grp_plan_gemini = QGroupBox("2. Prompt for Gemini (Implementation & Codebase Review)")
        grp_plan_gemini.setStyleSheet("QGroupBox { color: #a78bfa; border: 1px solid #3d2d5b; }")
        layout_plan_gemini = QVBoxLayout(grp_plan_gemini)
        self.txt_plan_gemini_prompt = QTextEdit()
        self.txt_plan_gemini_prompt.setFixedHeight(75)
        saved_plan_gemini = self.config.get("prompts", {}).get(
            "plan_gemini_prompt",
            "Please review this implementation plan against the actual codebase architecture. Identify missing implementation details, edge cases, class/module boundary violations, and performance bottlenecks.",
        )
        self.txt_plan_gemini_prompt.setText(saved_plan_gemini)
        self.txt_plan_gemini_prompt.textChanged.connect(self._auto_save_prompts)
        layout_plan_gemini.addWidget(self.txt_plan_gemini_prompt)
        tab_plan_layout.addWidget(grp_plan_gemini)

        grp_plan_nb = QGroupBox("3. Prompt for NotebookLM (Cross-Document Evaluation)")
        grp_plan_nb.setStyleSheet("QGroupBox { color: #58a6ff; border: 1px solid #1c3553; }")
        layout_plan_nb = QVBoxLayout(grp_plan_nb)
        self.txt_plan_notebooklm_prompt = QTextEdit()
        self.txt_plan_notebooklm_prompt.setFixedHeight(75)
        saved_plan_nb = self.config.get("prompts", {}).get(
            "plan_notebooklm_prompt",
            "review this document and review with existing architechure and find any logical loop hole and how to make it superior desighn and find flwas",
        )
        self.txt_plan_notebooklm_prompt.setText(saved_plan_nb)
        self.txt_plan_notebooklm_prompt.textChanged.connect(self._auto_save_prompts)
        layout_plan_nb.addWidget(self.txt_plan_notebooklm_prompt)
        tab_plan_layout.addWidget(grp_plan_nb)

        grp_plan_final = QGroupBox("4. Prompt for ChatGPT (Final Refinement & Aggregator)")
        grp_plan_final.setStyleSheet("QGroupBox { color: #10a37f; border: 1px solid #1a4738; }")
        layout_plan_final = QVBoxLayout(grp_plan_final)
        self.txt_plan_chatgpt_final_prompt = QTextEdit()
        self.txt_plan_chatgpt_final_prompt.setFixedHeight(75)
        saved_plan_final = self.config.get("prompts", {}).get(
            "plan_chatgpt_final_prompt",
            "we need to give this to gemini coding agent so how to ask gemini to prepare implementation plans",
        )
        self.txt_plan_chatgpt_final_prompt.setText(saved_plan_final)
        self.txt_plan_chatgpt_final_prompt.textChanged.connect(self._auto_save_prompts)
        layout_plan_final.addWidget(self.txt_plan_chatgpt_final_prompt)
        tab_plan_layout.addWidget(grp_plan_final)

        self.input_tabs.addTab(tab_plan, "Implementation Plan")
        saved_tab_idx = self.config.get("ui", {}).get("active_tab", 0)
        self.input_tabs.setCurrentIndex(max(0, min(2, saved_tab_idx)))
        self.input_tabs.currentChanged.connect(self._auto_save_prompts)
        left_layout.addWidget(self.input_tabs)




        # Action Buttons
        btn_layout = QHBoxLayout()
        self.btn_run = QPushButton("Start Full (Stage 1 ➔ Stage 2 ➔ Stage 3)")
        self.btn_run.setFixedHeight(38)
        self.btn_run.clicked.connect(self._start_orchestration)
        btn_layout.addWidget(self.btn_run)

        self.btn_run_stage2_only = QPushButton("Run Stage 2 Only (NotebookLM)")
        self.btn_run_stage2_only.setFixedHeight(38)
        self.btn_run_stage2_only.setObjectName("btnSecondary")
        self.btn_run_stage2_only.clicked.connect(self._start_stage2_only_orchestration)
        btn_layout.addWidget(self.btn_run_stage2_only)

        self.btn_stop = QPushButton("Stop")
        self.btn_stop.setFixedHeight(38)
        self.btn_stop.setObjectName("btnStop")
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self._stop_orchestration)
        btn_layout.addWidget(self.btn_stop)

        left_layout.addLayout(btn_layout)



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

        # Output Tab 3: Git History
        tab_history = QWidget()
        tab_hist_layout = QVBoxLayout(tab_history)
        self.tbl_history = QTableWidget(0, 3)
        self.tbl_history.setHorizontalHeaderLabels(["Commit Hash", "Date", "Summary"])
        self.tbl_history.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.tbl_history.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.tbl_history.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        tab_hist_layout.addWidget(self.tbl_history)
        self.output_tabs.addTab(tab_history, "Git Workspace History")

        # Output Tab 4: Execution Log
        self.txt_log = QPlainTextEdit()
        self.txt_log.setReadOnly(True)
        self.output_tabs.addTab(self.txt_log, "Live Execution Log")

        right_layout.addWidget(self.output_tabs)
        splitter.addWidget(right_widget)

        splitter.setSizes([620, 1080])

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

        saved_doc = self.config.get("ui", {}).get("selected_doc", "")
        saved_plan_doc = self.config.get("ui", {}).get("selected_plan_doc", "")

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
            with open(fpath, "r", encoding="utf-8", errors="replace") as f:
                doc_content = f.read()
        else:
            user_input = self.txt_concept.toPlainText().strip()
            if not user_input:
                QMessageBox.warning(self, "Empty Prompt", "Please enter architecture requirements.")
                return

        if is_plan_mode:
            stage1_prompt = self.txt_plan_chatgpt1_prompt.toPlainText().strip()
            prompt_gemini = self.txt_plan_gemini_prompt.toPlainText().strip()
            stage2_prompt = self.txt_plan_notebooklm_prompt.toPlainText().strip()
            stage3_prompt = self.txt_plan_chatgpt_final_prompt.toPlainText().strip()
        elif is_doc_mode:
            stage1_prompt = self.txt_doc_stage1_prompt.toPlainText().strip()
            prompt_gemini = ""
            stage2_prompt = self.txt_doc_stage2_prompt.toPlainText().strip()
            stage3_prompt = self.txt_doc_stage3_prompt.toPlainText().strip()
        else:
            stage1_prompt = self.txt_concept_stage1_prompt.toPlainText().strip()
            prompt_gemini = ""
            stage2_prompt = self.txt_concept_stage2_prompt.toPlainText().strip()
            stage3_prompt = self.txt_doc_stage3_prompt.toPlainText().strip()


        if not is_plan_mode and not stage1_prompt:
            QMessageBox.warning(self, "Empty Stage 1 Prompt", "Please enter a prompt for Stage 1 (ChatGPT).")
            return

        # Prepare updated config
        cfg = dict(self.config)
        cfg.setdefault("browser", {})["use_existing_chrome"] = self.chk_use_existing_chrome.isChecked()
        cfg.setdefault("services", {}).setdefault("chatgpt", {})["url"] = self.txt_chatgpt_url.text().strip()
        cfg.setdefault("services", {}).setdefault("notebooklm", {})["url"] = self.txt_notebooklm_url.text().strip()
        cfg.setdefault("services", {}).setdefault("gemini", {})["url"] = self.txt_gemini_url.text().strip()
        cfg.setdefault("services", {}).setdefault("grok", {})["url"] = self.txt_grok_url.text().strip()

        self.btn_run.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self.progress_bar.setValue(0)
        self.output_tabs.setCurrentIndex(3)  # Switch to execution log

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
            max_iterations=iterations,

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
        self.output_tabs.setCurrentIndex(3)  # Switch to execution log

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
