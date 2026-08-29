import os
import shutil
import subprocess
import logging
import time
from typing import List, Optional, Tuple

logger = logging.getLogger("repo_manager")


class WorkspaceRepoManager:
    """
    Manages local Git repository and versioned architecture markdown files in ./workspace.
    Uses Git CLI via subprocess.
    """

    def __init__(self, workspace_dir: str = "./workspace", filename_prefix: str = "architecture_v"):
        self.workspace_dir = os.path.abspath(workspace_dir)
        self.filename_prefix = filename_prefix
        self.frozen_filename = "architecture_frozen.md"
        self._ensure_repo()

    def _run_git(self, *args: str) -> str:
        """Execute git command in the workspace directory."""
        cmd = ["git"] + list(args)
        result = subprocess.run(
            cmd,
            cwd=self.workspace_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        if result.returncode != 0:
            logger.error(f"Git command failed: {' '.join(cmd)} -> {result.stderr.strip()}")
            raise RuntimeError(f"Git error ({result.returncode}): {result.stderr.strip()}")
        return result.stdout.strip()

    def _ensure_repo(self):
        """Ensure workspace directory and git repository exist."""
        os.makedirs(self.workspace_dir, exist_ok=True)
        git_dir = os.path.join(self.workspace_dir, ".git")

        if not os.path.exists(git_dir):
            logger.info(f"Initializing new Git repository in {self.workspace_dir}")
            self._run_git("init")
            try:
                self._run_git("config", "user.name", "Architecture Orchestrator")
                self._run_git("config", "user.email", "orchestrator@local")
            except Exception:
                pass

            # Create an initial README or .gitkeep to have a root commit
            readme_path = os.path.join(self.workspace_dir, "README.md")
            if not os.path.exists(readme_path):
                with open(readme_path, "w", encoding="utf-8") as f:
                    f.write("# Software Architecture Design Workspace\n\nManaged automatically by Orchestrator.\n")
                self._run_git("add", "README.md")
                self._run_git("commit", "-m", "chore(init): initialize architecture workspace repository")

    def reset_workspace(self, archive: bool = True):
        """
        Archive previous architecture_v*.md files to start a fresh project at v1.
        """
        if not os.path.exists(self.workspace_dir):
            return

        timestamp = time.strftime("%Y%m%d_%H%M%S")
        archive_dir = os.path.join(self.workspace_dir, "archive", timestamp)

        files_to_move = []
        for fname in os.listdir(self.workspace_dir):
            if (fname.startswith(self.filename_prefix) and fname.endswith(".md")) or fname == self.frozen_filename:
                files_to_move.append(fname)

        if not files_to_move:
            return

        if archive:
            os.makedirs(archive_dir, exist_ok=True)
            for fname in files_to_move:
                src = os.path.join(self.workspace_dir, fname)
                dst = os.path.join(archive_dir, fname)
                shutil.move(src, dst)
                try:
                    self._run_git("rm", fname)
                except Exception:
                    pass

            try:
                self._run_git("commit", "-m", f"chore(workspace): archive previous session to archive/{timestamp}")
                logger.info(f"Archived previous session files to archive/{timestamp}")
            except Exception:
                pass
        else:
            for fname in files_to_move:
                src = os.path.join(self.workspace_dir, fname)
                if os.path.exists(src):
                    os.remove(src)
                    try:
                        self._run_git("rm", fname)
                    except Exception:
                        pass
            try:
                self._run_git("commit", "-m", "chore(workspace): reset workspace files")
            except Exception:
                pass

    def get_doc_prefix(self, doc_filename: str) -> str:
        """Extract clean prefix (e.g. '01', 'F1', '08') from doc_filename."""
        if not doc_filename:
            return "custom"
        clean = doc_filename.replace(".md", "").strip()
        if "-" in clean:
            return clean.split("-")[0].strip()
        return clean[:10]

    def get_stage1_filename(self, doc_filename: str = "") -> str:
        """Stage 1: source document sent to ChatGPT -> produces initial review/spec."""
        prefix = self.get_doc_prefix(doc_filename)
        return f"{prefix}-source-to-chatgpt.md"

    def get_stage2_critique_filename(self, doc_filename: str = "") -> str:
        """Stage 2: ChatGPT output sent to NotebookLM -> produces critique."""
        prefix = self.get_doc_prefix(doc_filename)
        return f"{prefix}-chatgpt-to-notebooklm.md"

    def get_refined_filename(self, version: int = 2, doc_filename: str = "") -> str:
        """Refined revision: NotebookLM critique sent back to ChatGPT -> produces refined spec."""
        prefix = self.get_doc_prefix(doc_filename)
        if version == 2:
            return f"{prefix}-notebooklm-to-chatgpt-refined.md"
        return f"{prefix}-notebooklm-to-chatgpt-refined.v{version}.md"

    def get_final_filename(self, doc_filename: str = "", done_by: str = "chatgpt") -> str:
        """Final frozen specification: e.g. 'F1-final-done-by-chatgpt.md' or 'F1-final-done-by-notebooklm.md'."""
        prefix = self.get_doc_prefix(doc_filename)
        engine = "chatgpt" if "chatgpt" in done_by.lower() else "notebooklm"
        return f"{prefix}-final-done-by-{engine}.md"

    def get_filename(self, version: int, doc_filename: str = "") -> str:
        """Helper for versioned filenames."""
        if version <= 1:
            return self.get_stage1_filename(doc_filename)
        return self.get_refined_filename(version, doc_filename)

    def get_latest_version_number(self, doc_filename: str = "") -> int:
        """Scan workspace for files matching this doc prefix and return version count."""
        highest_v = 0
        if not os.path.exists(self.workspace_dir):
            return 0

        prefix = self.get_doc_prefix(doc_filename)
        for fname in os.listdir(self.workspace_dir):
            if fname.startswith(f"{prefix}-notebooklm-to-chatgpt-refined") and fname.endswith(".md"):
                highest_v = max(highest_v, 2)
            elif fname == f"{prefix}-source-to-chatgpt.md":
                highest_v = max(highest_v, 1)
            elif fname.startswith(f"{prefix}-interaction.v") and fname.endswith(".md"):
                try:
                    v_str = fname.replace(f"{prefix}-interaction.v", "").replace(".md", "")
                    highest_v = max(highest_v, int(v_str))
                except ValueError:
                    continue
            elif fname.startswith("architecture_v") and fname.endswith(".md"):
                try:
                    v_num = int(fname.replace("architecture_v", "").replace(".md", ""))
                    highest_v = max(highest_v, v_num)
                except ValueError:
                    continue
        return highest_v

    def get_file_content(self, filename: str) -> Optional[str]:
        """Read content of a file in workspace."""
        path = os.path.join(self.workspace_dir, filename)
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return f.read()
        return None

    def save_named_file(self, filename: str, content: str, commit_message: str) -> str:
        """Save a file by specific name and perform a Git commit."""
        filepath = os.path.join(self.workspace_dir, filename)
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(content)

        logger.info(f"Saved {filename} to {filepath}")
        try:
            self._run_git("add", filename)
            self._run_git("commit", "-m", commit_message)
            logger.info(f"Committed {filename} to git repo: '{commit_message.splitlines()[0]}'")
        except Exception as e:
            logger.error(f"Git commit failed: {e}")

        return filepath

    def save_revision(self, version: int, content: str, commit_message: str, doc_filename: str = "") -> str:
        """Save revision with descriptive filename and Git commit."""
        filename = self.get_filename(version, doc_filename)
        return self.save_named_file(filename, content, commit_message)

    def freeze_design(
        self, final_version: int, doc_filename: str = "", done_by: str = "chatgpt", commit_message: Optional[str] = None
    ) -> str:
        """
        Copy latest architecture to {prefix}-final-done-by-{engine}.md and commit to git.
        """
        source_filename = self.get_filename(final_version, doc_filename)
        source_content = self.get_file_content(source_filename)
        if source_content is None:
            source_filename = f"architecture_v{final_version}.md"
            source_content = self.get_file_content(source_filename)
            if source_content is None:
                raise FileNotFoundError(f"Source file {source_filename} not found in workspace.")

        final_filename = self.get_final_filename(doc_filename, done_by=done_by)
        frozen_path = os.path.join(self.workspace_dir, final_filename)
        with open(frozen_path, "w", encoding="utf-8") as f:
            f.write(
                f"<!-- FINAL FROZEN SPECIFICATION: Derived from {source_filename} | Done By: {done_by.upper()} -->\n\n"
                + source_content
            )

        # Also write legacy architecture_frozen.md alias for compatibility
        legacy_frozen = os.path.join(self.workspace_dir, self.frozen_filename)
        with open(legacy_frozen, "w", encoding="utf-8") as f:
            f.write(source_content)

        msg = commit_message or f"release(arch): freeze architecture design [{final_filename}] based on {source_filename}"
        try:
            self._run_git("add", final_filename)
            self._run_git("add", self.frozen_filename)
            self._run_git("commit", "-m", msg)
            logger.info(f"Committed frozen architecture to git: '{msg}'")
        except Exception as e:
            logger.error(f"Git freeze commit failed: {e}")

        return frozen_path




    def get_commit_history(self, max_count: int = 10) -> List[Tuple[str, str, str]]:
        """Return list of recent commits: (hexsha_short, date, summary)."""
        history = []
        try:
            output = self._run_git("log", f"-n{max_count}", "--pretty=format:%h|%ad|%s", "--date=short")
            for line in output.splitlines():
                if "|" in line:
                    parts = line.split("|", 2)
                    if len(parts) == 3:
                        history.append((parts[0], parts[1], parts[2]))
        except Exception as e:
            logger.warning(f"Failed to retrieve commit history: {e}")
        return history
