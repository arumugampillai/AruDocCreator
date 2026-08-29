import argparse
import asyncio
import json
import logging
import os
import site
import sys

# Ensure UTF-8 output on Windows consoles
if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# Ensure user site-packages are discovered across all execution environments (PyCharm, virtualenvs, etc.)
user_site = site.getusersitepackages()
if os.path.exists(user_site) and user_site not in sys.path:
    sys.path.insert(0, user_site)

appdata = os.environ.get("APPDATA")
if appdata:
    py_ver = f"Python{sys.version_info.major}{sys.version_info.minor}"
    roaming_site = os.path.join(appdata, "Python", py_ver, "site-packages")
    if os.path.exists(roaming_site) and roaming_site not in sys.path:
        sys.path.insert(0, roaming_site)

# Optional rich formatting with standard terminal fallback
try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.prompt import Prompt, Confirm
    from rich.table import Table
    from rich.markdown import Markdown

    console = Console(force_terminal=True, legacy_windows=False)
    USE_RICH = True
except ImportError:
    USE_RICH = False
    console = None

from agents.qwen_controller import QwenTrafficController
from agents.browser_automator import BrowserAutomator
from agents.repo_manager import WorkspaceRepoManager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("orchestrator")


def print_header():
    if USE_RICH:
        console.print(
            Panel.fit(
                "[bold cyan]Iterative Software Architecture Design Orchestrator[/bold cyan]\n"
                "[italic white]Controller: Ollama (Qwen) | Automation: Playwright | Versioning: Git[/italic white]",
                border_style="cyan",
            )
        )
    else:
        print("=" * 65)
        print("Iterative Software Architecture Design Orchestrator")
        print("Controller: Ollama (Qwen) | Automation: Playwright | Versioning: Git")
        print("=" * 65)


def print_msg(text: str, style: str = ""):
    if USE_RICH:
        console.print(text)
    else:
        # Strip simple rich tags
        clean_text = text.replace("[bold]", "").replace("[/bold]", "")
        clean_text = clean_text.replace("[green]", "").replace("[/green]", "")
        clean_text = clean_text.replace("[cyan]", "").replace("[/cyan]", "")
        clean_text = clean_text.replace("[yellow]", "").replace("[/yellow]", "")
        clean_text = clean_text.replace("[red]", "").replace("[/red]", "")
        clean_text = clean_text.replace("[magenta]", "").replace("[/magenta]", "")
        clean_text = clean_text.replace("[underline]", "").replace("[/underline]", "")
        clean_text = clean_text.replace("[dim]", "").replace("[/dim]", "")
        print(clean_text)


def ask_user(prompt_text: str, default: str = "") -> str:
    if USE_RICH:
        return Prompt.ask(prompt_text, default=default)
    else:
        val = input(f"{prompt_text} [{default}]: ").strip()
        return val if val else default


def ask_choice(prompt_text: str, choices: list, default: str) -> str:
    if USE_RICH:
        return Prompt.ask(prompt_text, choices=choices, default=default)
    else:
        while True:
            val = input(f"{prompt_text} ({'/'.join(choices)}) [{default}]: ").strip()
            if not val:
                return default
            if val in choices:
                return val
            print(f"Invalid choice '{val}'. Options: {', '.join(choices)}")


def load_config(config_path: str = "config.json") -> dict:
    """Load configuration from JSON file or return defaults."""
    if not os.path.isabs(config_path):
        base_dir = os.path.dirname(os.path.abspath(__file__))
        candidate = os.path.join(base_dir, config_path)
        if os.path.exists(candidate):
            config_path = candidate
    if os.path.exists(config_path):
        with open(config_path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}



async def run_orchestrator(args: argparse.Namespace):
    print_header()

    # 1. Load config
    config = load_config(args.config)
    if args.headless:
        config.setdefault("browser", {})["headless"] = True

    # 2. Initialize subsystems
    ollama_cfg = config.get("ollama", {})
    controller = QwenTrafficController(
        base_url=ollama_cfg.get("base_url", "http://localhost:11434"),
        model=args.model or ollama_cfg.get("model", "qwen3:4b"),
        fallback_model=ollama_cfg.get("fallback_model", "qwen2.5:3b"),
        temperature=ollama_cfg.get("temperature", 0.2),
        timeout_seconds=ollama_cfg.get("timeout_seconds", 120),
    )

    workspace_cfg = config.get("workspace", {})
    repo_manager = WorkspaceRepoManager(
        workspace_dir=workspace_cfg.get("dir", "./workspace"),
        filename_prefix=workspace_cfg.get("filename_prefix", "architecture_v"),
    )

    automator = BrowserAutomator(config)

    # 3. Check Ollama Health
    print_msg("\n[bold yellow]Checking Ollama Traffic Controller...[/bold yellow]")
    health = controller.check_health()
    if health.get("status") == "online":
        print_msg(
            f"[green][OK] Ollama is online. Active controller model: [bold]{health.get('active_model')}[/bold][/green]"
        )
        if not health.get("model_ready"):
            print_msg(
                f"[yellow]! Warning: Model '{controller.model}' not found in Ollama. "
                f"Available: {', '.join(health.get('available_models', []))}. Using prompt fallbacks if necessary.[/yellow]"
            )
    else:
        print_msg(
            f"[yellow]! Notice: Ollama is offline ({health.get('error')}). Using built-in deterministic templates.[/yellow]"
        )

    # 4. Get User Idea or Document Prefix
    docs_dir = workspace_cfg.get("docs_dir", r"C:\Users\admin\PycharmProjects\AruMLStudio\docs")
    user_input = args.doc or args.idea

    if not user_input:
        print_msg("\n[bold]Step 1: Define Software Concept or Enter Document Prefix (e.g. 00, 01, 08, F1)[/bold]")
        user_input = ask_user(
            "[cyan]Enter software idea OR starting letters of document in docs/[/cyan]",
            default="01",
        )

    # Check if user input matches a document in docs_dir
    doc_match = controller.resolve_document(user_input, docs_dir=docs_dir)
    is_doc_review = False
    doc_filename = ""
    doc_content = ""

    if doc_match:
        doc_filepath, doc_filename, doc_content = doc_match
        is_doc_review = True
        size_kb = round(os.path.getsize(doc_filepath) / 1024, 1)
        print_msg(
            f"\n[bold green][MATCH] Found document: [underline]{doc_filename}[/underline] ({size_kb} KB)[/bold green]"
        )
        print_msg("[italic white]Direct copy-paste review mode active (Qwen will pass raw document with review instruction).[/italic white]")

    critic_service = args.critic or config.get("services", {}).get("default_critic", "gemini")

    # Reset workspace to start at v1 if --fresh is set or not explicitly resuming
    if args.fresh or not args.resume:
        if repo_manager.get_latest_version_number() > 0:
            repo_manager.reset_workspace(archive=True)
            print_msg("[cyan]Started fresh workspace session (previous revisions archived to workspace/archive/).[/cyan]")

    try:
        # 5. Launch Browser Session
        print_msg("\n[bold yellow]Launching Browser Context (Playwright)...[/bold yellow]")
        await automator.start()
        print_msg("[green][OK] Browser session initialized.[/green]")

        # -----------------------------------------------------------------
        # STEP 2 & 3: Initial Generation / Document Review via ChatGPT
        # -----------------------------------------------------------------
        version = repo_manager.get_latest_version_number() + 1

        if version == 1:
            if is_doc_review:
                print_msg(f"\n[bold green]>>> Step 2: Preparing direct review prompt for {doc_filename}...[/bold green]")
                initial_prompt = controller.format_document_review_prompt(doc_filename, doc_content, review_word="review")
                print_msg(f"[bold cyan]>>> Step 3: Pasting document into ChatGPT and requesting review...[/bold cyan]")
            else:
                print_msg(f"\n[bold green]>>> Step 2: Formulating initial architecture prompt via Qwen Controller...[/bold green]")
                initial_prompt = controller.generate_initial_chatgpt_prompt(user_input)
                print_msg(f"[bold cyan]>>> Step 3: Sending prompt to ChatGPT via Playwright...[/bold cyan]")

            current_doc = await automator.query_chatgpt(initial_prompt, new_chat=True)

            if not current_doc or len(current_doc.strip()) < 5:
                print_msg("[red]Failed to extract architecture document from ChatGPT or response too short.[/red]")
                return

            print_msg(f"\n[green][OK] ChatGPT response received ({len(current_doc)} characters).[/green]")
            preview_v1 = current_doc[:400] + ("..." if len(current_doc) > 400 else "")
            print_msg(f"[dim]{preview_v1}[/dim]\n")

            commit_summary = f"Review & critique of {doc_filename}" if is_doc_review else "Initial architecture draft from user concept."
            commit_msg = controller.generate_commit_message(version=1, changes_summary=commit_summary)
            file_saved = repo_manager.save_revision(version=1, content=current_doc, commit_message=commit_msg)
            print_msg(f"[bold green][OK] Revision v1 committed to Git and saved to [underline]{file_saved}[/underline][/bold green]")
        else:
            print_msg(f"[bold cyan]Resuming from latest existing revision v{version - 1}...[/bold cyan]")
            current_doc = repo_manager.get_file_content(f"architecture_v{version - 1}.md") or ""


        # -----------------------------------------------------------------
        # STEP 4 - 7: Iterative Critique & Refinement Loop
        # -----------------------------------------------------------------
        while True:
            current_v = repo_manager.get_latest_version_number()
            next_v = current_v + 1

            print_msg(f"\n[bold magenta]============================================================[/bold magenta]")
            print_msg(f"[bold magenta]Starting Iteration Cycle: v{current_v} -> v{next_v} (Critic: {critic_service.capitalize()})[/bold magenta]")
            print_msg(f"[bold magenta]============================================================[/bold magenta]")

            # Step 4: Critique via Gemini / NotebookLM
            if critic_service.lower() == "notebooklm":
                notebook_url = getattr(args, "notebook_url", None) or config.get("services", {}).get("notebooklm", {}).get(
                    "url", automator.notebooklm_url
                )
                latest_file_path = os.path.join(repo_manager.workspace_dir, f"architecture_v{current_v}.md")
                print_msg(f"\n[bold yellow]>>> Step 4: Uploading architecture_v{current_v}.md to NotebookLM sources...[/bold yellow]")
                print_msg(f"[bold cyan]>>> Requesting cross-document comparative review against notebook sources...[/bold cyan]")
                critique_feedback = await automator.upload_source_and_review_in_notebooklm(
                    notebook_url=notebook_url,
                    file_path=latest_file_path,
                    doc_title=f"architecture_v{current_v}.md",
                    doc_prefix_or_num=doc_filename,
                )
            else:
                print_msg(f"\n[bold yellow]>>> Step 4: Generating critique prompt via Qwen Controller...[/bold yellow]")
                critique_prompt = controller.generate_critique_prompt(current_doc)
                print_msg(f"[bold cyan]>>> Passing architecture to Gemini for flaw analysis...[/bold cyan]")
                critique_feedback = await automator.query_gemini(critique_prompt, new_chat=True)

            print_msg("\n[bold]Critique Feedback Received (Summary):[/bold]")

            preview = critique_feedback[:600] + ("..." if len(critique_feedback) > 600 else "")
            if USE_RICH:
                console.print(Panel(Markdown(preview), title=f"[bold red]Flaw Analysis ({critic_service.capitalize()})[/bold red]", border_style="red"))
            else:
                print(f"--- FLAW ANALYSIS ({critic_service.capitalize()}) ---")
                print(preview)
                print("--------------------------------------------------")

            # Step 5: Refine Architecture via ChatGPT
            print_msg(f"\n[bold yellow]>>> Step 5: Generating refinement prompt via Qwen Controller...[/bold yellow]")
            refine_prompt = controller.generate_refinement_prompt(current_doc, critique_feedback, iteration=current_v)

            print_msg(f"[bold cyan]>>> Passing feedback back to ChatGPT for refined architecture v{next_v}...[/bold cyan]")
            refined_doc = await automator.query_chatgpt(refine_prompt, new_chat=False)

            if not refined_doc or len(refined_doc.strip()) < 5:
                print_msg("[red]Failed to extract refined architecture from ChatGPT. Keeping current revision.[/red]")
                break

            current_doc = refined_doc

            # Step 6: Git Commit
            print_msg(f"\n[bold yellow]>>> Step 6: Committing revision v{next_v} to Git...[/bold yellow]")
            commit_msg = controller.generate_commit_message(version=next_v, changes_summary=f"Addressed critique from {critic_service}: {critique_feedback[:200]}")
            saved_path = repo_manager.save_revision(version=next_v, content=current_doc, commit_message=commit_msg)
            print_msg(f"[bold green][OK] Revision v{next_v} saved to [underline]{saved_path}[/underline][/bold green]")

            # Stability Analysis by Qwen
            stability = controller.evaluate_iteration_stability(current_doc, critique_feedback)
            print_msg(
                f"\n[bold blue]Controller Evaluation:[/bold blue] Stability Score: [bold]{stability.get('stability_score', 'N/A')}/10[/bold] | "
                f"Recommendation: [bold]{stability.get('recommendation', 'continue').upper()}[/bold]"
            )
            if stability.get("major_improvements"):
                print_msg(f"[green]Key Improvements:[/green] {', '.join(stability.get('major_improvements', []))}")
            if stability.get("remaining_concerns"):
                print_msg(f"[yellow]Remaining Concerns:[/yellow] {', '.join(stability.get('remaining_concerns', []))}")

            # Step 7: Terminal Prompt to Continue or Freeze
            if args.auto_freeze or (args.max_iterations and next_v >= args.max_iterations + 1):
                action = "freeze"
                print_msg(f"\n[cyan]Auto-freezing after iteration v{next_v}...[/cyan]")
            else:
                print_msg("\n[bold]Step 7: Iteration Decision[/bold]")
                action = ask_choice(
                    "[bold cyan]Choose action[/bold cyan]",
                    choices=["continue", "freeze", "switch-critic", "view-history"],
                    default="continue" if stability.get("recommendation") == "continue" else "freeze",
                )

            if action == "freeze":
                frozen_file = repo_manager.freeze_design(final_version=next_v)
                if USE_RICH:
                    console.print(
                        Panel.fit(
                            f"[bold green]Architecture Design Frozen Successfully![/bold green]\n"
                            f"Final Specification: [bold underline]{frozen_file}[/bold underline]\n"
                            f"Committed to local Git workspace repository.",
                            border_style="green",
                        )
                    )
                else:
                    print("=" * 65)
                    print(f"Architecture Design Frozen: {frozen_file}")
                    print("Committed to local Git workspace repository.")
                    print("=" * 65)
                break
            elif action == "switch-critic":
                critic_service = "notebooklm" if critic_service == "gemini" else "gemini"
                print_msg(f"[yellow]Switched critic service to: {critic_service.capitalize()}[/yellow]")
            elif action == "view-history":
                history = repo_manager.get_commit_history()
                if USE_RICH:
                    table = Table(title="Architecture Workspace Git Commit History")
                    table.add_column("Commit Hash", style="cyan")
                    table.add_column("Date", style="green")
                    table.add_column("Commit Summary", style="white")
                    for c_hash, c_date, c_sum in history:
                        table.add_row(c_hash, c_date, c_sum)
                    console.print(table)
                else:
                    print("\n--- GIT COMMIT HISTORY ---")
                    for c_hash, c_date, c_sum in history:
                        print(f"{c_hash} | {c_date} | {c_sum}")
                    print("--------------------------\n")

                cont = ask_choice("Continue with another iteration cycle?", ["yes", "no"], default="yes")
                if cont == "no":
                    repo_manager.freeze_design(final_version=next_v)
                    break

    except KeyboardInterrupt:
        print_msg("\n[yellow]Process interrupted by user.[/yellow]")
    except Exception as e:
        logger.exception(f"Error during orchestration: {e}")
        print_msg(f"[bold red]Orchestration Error: {e}[/bold red]")
    finally:
        print_msg("\n[yellow]Shutting down browser automator...[/yellow]")
        await automator.stop()
        print_msg("[green]Session finished cleanly.[/green]")


def main():
    parser = argparse.ArgumentParser(
        description="Iterative Software Architecture Design Orchestrator (Qwen + Playwright + Git)"
    )
    parser.add_argument("--idea", "-i", type=str, help="Initial software architecture idea/spec")
    parser.add_argument("--doc", "-d", type=str, help="Starting prefix or name of doc to review from docs/ (e.g. 00, 01, 08, F1)")
    parser.add_argument("--config", "-c", type=str, default="config.json", help="Path to config.json")
    parser.add_argument("--model", "-m", type=str, help="Ollama model override (default: qwen3:4b)")
    parser.add_argument("--critic", type=str, choices=["gemini", "notebooklm"], help="Critic service")
    parser.add_argument("--notebook-url", type=str, help="Target NotebookLM URL (e.g. https://notebooklm.google.com/notebook/<id>)")
    parser.add_argument("--headless", action="store_true", help="Run browser in headless mode")
    parser.add_argument("--max-iterations", type=int, default=None, help="Maximum iteration cycles before auto-freeze")
    parser.add_argument("--auto-freeze", action="store_true", help="Automatically freeze after one iteration")
    parser.add_argument("--fresh", "-f", action="store_true", default=False, help="Force fresh session starting at v1")
    parser.add_argument("--resume", "-r", action="store_true", default=False, help="Resume from latest existing revision without resetting")

    args = parser.parse_args()


    asyncio.run(run_orchestrator(args))


if __name__ == "__main__":
    main()
