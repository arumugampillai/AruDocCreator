import os
import shutil
import tempfile
import unittest
from agents.repo_manager import WorkspaceRepoManager
from agents.qwen_controller import QwenTrafficController


class TestWorkspaceRepoManager(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.repo_manager = WorkspaceRepoManager(workspace_dir=self.test_dir)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_repo_initialization(self):
        self.assertTrue(os.path.exists(os.path.join(self.test_dir, ".git")))
        self.assertEqual(self.repo_manager.get_latest_version_number(), 0)

    def test_save_revisions_and_versioning(self):
        # Save v1
        v1_path = self.repo_manager.save_revision(
            version=1,
            content="# Architecture v1\n\nInitial draft.",
            commit_message="feat(arch): initial draft v1",
        )
        self.assertTrue(os.path.exists(v1_path))
        self.assertEqual(self.repo_manager.get_latest_version_number(), 1)

        # Save v2
        v2_path = self.repo_manager.save_revision(
            version=2,
            content="# Architecture v2\n\nRefined draft.",
            commit_message="feat(arch): revision v2",
        )
        self.assertTrue(os.path.exists(v2_path))
        self.assertEqual(self.repo_manager.get_latest_version_number(), 2)

        # Freeze design
        frozen_path = self.repo_manager.freeze_design(final_version=2)
        self.assertTrue(os.path.exists(frozen_path))
        frozen_content = self.repo_manager.get_file_content("architecture_frozen.md")
        self.assertIn("Architecture v2", frozen_content)

        # Check commit history
        history = self.repo_manager.get_commit_history()
        self.assertGreaterEqual(len(history), 3)  # init + v1 + v2 + freeze


class TestQwenControllerPromptFormatting(unittest.TestCase):
    def setUp(self):
        # Mock/Offline controller instance
        self.controller = QwenTrafficController(base_url="http://invalid-ollama-host:11434")

    def test_fallback_prompts(self):
        initial_prompt = self.controller.generate_initial_chatgpt_prompt("A distributed cache system")
        self.assertIn("distributed cache system", initial_prompt)
        self.assertIn("Principal Software Architect", initial_prompt)

        critique_prompt = self.controller.generate_critique_prompt("# Arch Doc")
        self.assertIn("Arch Doc", critique_prompt)
        self.assertIn("Senior Principal Architecture Reviewer", critique_prompt)

        refine_prompt = self.controller.generate_refinement_prompt(
            current_doc="# Arch Doc v1", critique_feedback="Fix SPOF in Redis", iteration=1
        )
        self.assertIn("Fix SPOF in Redis", refine_prompt)
        self.assertIn("Architecture Specification (v2)", refine_prompt)

    def test_resolve_document_and_review_prompt(self):
        # 1. Test with isolated temporary directory (100% self-contained)
        temp_docs = tempfile.mkdtemp()
        try:
            sample_file = os.path.join(temp_docs, "01-FINAL_ARCHITECTURE_DIAGRAM.md")
            with open(sample_file, "w", encoding="utf-8") as f:
                f.write("# Architecture Diagram\n\nFull specifications for distributed data flow.")

            match = self.controller.resolve_document("01", docs_dir=temp_docs)
            self.assertIsNotNone(match)
            filepath, filename, content = match
            self.assertEqual(filename, "01-FINAL_ARCHITECTURE_DIAGRAM.md")
            self.assertIn("Architecture Diagram", content)

            prompt = self.controller.format_document_review_prompt(filename, content, "review")
            self.assertIn("Please review the following document in detail", prompt)
            self.assertIn(filename, prompt)
        finally:
            shutil.rmtree(temp_docs, ignore_errors=True)

        # 2. Also verify against live docs_dir if present
        docs_dir = r"C:\Users\admin\PycharmProjects\AruMLStudio\docs"
        if os.path.exists(docs_dir):
            match_f1 = self.controller.resolve_document("F1", docs_dir=docs_dir)
            if match_f1:
                _, filename_f1, _ = match_f1
                self.assertTrue(filename_f1.startswith("F1-"))


if __name__ == "__main__":
    unittest.main()

