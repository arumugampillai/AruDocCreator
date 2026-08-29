import json
import logging
import urllib.request
import urllib.error
from typing import Any, Dict, List, Optional

logger = logging.getLogger("qwen_controller")


class QwenTrafficController:
    """
    Local Traffic Controller utilizing Ollama (qwen3:4b or fallback models).
    Acts as coordinator/decision-maker:
    - Parses state
    - Formats structured prompts for ChatGPT and Critic (Gemini / NotebookLM)
    - Generates Git commit messages
    - Evaluates iteration stability without generating the core code/architecture itself.
    """

    def __init__(
        self,
        base_url: str = "http://localhost:11434",
        model: str = "qwen3:4b",
        fallback_model: str = "qwen2.5:3b",
        temperature: float = 0.2,
        timeout_seconds: int = 60,
    ):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.fallback_model = fallback_model
        self.temperature = temperature
        self.timeout = timeout_seconds
        self.active_model = model

    def check_health(self) -> Dict[str, Any]:
        """Check if Ollama server is accessible and verify model availability."""
        url = f"{self.base_url}/api/tags"
        try:
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=5.0) as response:
                if response.status == 200:
                    data = json.loads(response.read().decode("utf-8"))
                    models = [m.get("name", "") for m in data.get("models", [])]
                    # Check if requested model or any tag match is present
                    available = any(self.model in m for m in models)
                    if not available and self.fallback_model:
                        fallback_avail = any(self.fallback_model in m for m in models)
                        if fallback_avail:
                            logger.warning(
                                f"Model '{self.model}' not found in Ollama. Falling back to '{self.fallback_model}'."
                            )
                            self.active_model = self.fallback_model
                            available = True

                    return {
                        "status": "online",
                        "available_models": models,
                        "active_model": self.active_model,
                        "model_ready": available,
                    }
                return {"status": "error", "error": f"HTTP {response.status}"}
        except Exception as e:
            return {"status": "offline", "error": str(e)}

    def _call_ollama(
        self,
        messages: List[Dict[str, str]],
        json_format: bool = False,
        system_prompt: Optional[str] = None,
    ) -> str:
        """Call Ollama /api/chat endpoint using standard library urllib."""
        url = f"{self.base_url}/api/chat"
        payload_messages = []
        if system_prompt:
            payload_messages.append({"role": "system", "content": system_prompt})
        payload_messages.extend(messages)

        payload: Dict[str, Any] = {
            "model": self.active_model,
            "messages": payload_messages,
            "stream": False,
            "options": {
                "temperature": self.temperature,
            },
        }
        if json_format:
            payload["format"] = "json"

        data_bytes = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data_bytes,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as response:
                body = response.read().decode("utf-8")
                data = json.loads(body)
                return data.get("message", {}).get("content", "").strip()
        except urllib.error.HTTPError as e:
            err_msg = e.read().decode("utf-8") if e.fp else str(e)
            logger.error(f"Ollama HTTP error: {e.code} - {err_msg}")
            raise RuntimeError(f"Ollama returned HTTP {e.code}: {err_msg}")
        except Exception as e:
            logger.error(f"Failed to communicate with Ollama at {self.base_url}: {e}")
            raise

    @staticmethod
    def resolve_document(prefix_or_name: str, docs_dir: str = "C:\\Users\\admin\\PycharmProjects\\AruMLStudio\\docs") -> Optional[tuple[str, str, str]]:
        """
        Match a document from docs_dir starting with the given prefix (e.g. '00', '01', '08', 'F1', etc.).
        Returns (filepath, filename, content) or None.
        """
        import os

        if not os.path.exists(docs_dir):
            return None

        query = prefix_or_name.strip().lower()
        files = [f for f in os.listdir(docs_dir) if f.endswith(".md")]

        # 1. Exact match or starts with prefix followed by '-', '_', or '.'
        candidates = []
        for fname in files:
            fname_lower = fname.lower()
            if fname_lower == query or fname_lower.startswith(query + "-") or fname_lower.startswith(query + "_") or fname_lower.startswith(query + "."):
                candidates.append(fname)

        # 2. General prefix match if no strict prefix matched
        if not candidates:
            for fname in files:
                if fname.lower().startswith(query):
                    candidates.append(fname)

        if not candidates:
            return None

        # Sort to pick the closest match (shortest filename or exact prefix)
        candidates.sort(key=lambda x: (len(x), x))
        chosen = candidates[0]
        filepath = os.path.join(docs_dir, chosen)

        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()

        return filepath, chosen, content

    @staticmethod
    def format_document_review_prompt(
        filename: str,
        content: str,
        review_word: str = "review",
    ) -> str:
        """
        Fast direct copy-paste review prompt: wraps raw document with review instruction
        without requiring Ollama to summarize/analyze the file content.
        """
        return (
            f"Please {review_word} the following document in detail. "
            f"Provide an architectural critique, identify flaws/risks, and recommend concrete improvements:\n\n"
            f"### Document: {filename}\n"
            f"---\n\n"
            f"{content}"
        )

    def generate_initial_chatgpt_prompt(self, user_idea: str) -> str:
        """
        Formulate a comprehensive architecture prompt for ChatGPT based on user idea.
        Enforces clean Markdown structure, component breakdowns, and non-functional specs.
        """
        system = (
            "You are a meta-prompt engineer and software traffic controller. "
            "Your task is to take a raw software concept and produce a highly structured, "
            "authoritative prompt for an expert Software Architect AI (ChatGPT). "
            "Do NOT write the architecture document yourself. Only output the exact prompt to send to ChatGPT."
        )

        user_content = (
            f"Transform the following software idea into an exhaustive architecture prompt:\n"
            f"'''\n{user_idea}\n'''\n\n"
            "The prompt for ChatGPT must request:\n"
            "1. Executive Summary & Goals\n"
            "2. High-level Architecture & Component Topology\n"
            "3. Data Flow & Communication Protocols\n"
            "4. Technology Stack & Trade-offs\n"
            "5. Core API Schemas & Data Models\n"
            "6. Security, Scalability, and High-Availability Strategy\n"
            "7. Failure Modes, Edge Cases, and Recovery\n"
            "8. Modular Implementation Roadmap\n"
            "Instruct ChatGPT to write in complete, rigorous, and professional Markdown."
        )

        try:
            return self._call_ollama(
                messages=[{"role": "user", "content": user_content}],
                system_prompt=system,
            )
        except Exception as e:
            logger.warning(f"Ollama prompt generation fallback ({e}). Using deterministic template.")
            return (
                f"You are a Principal Software Architect. Please design a comprehensive, production-grade "
                f"software architecture for the following project specification:\n\n"
                f"### Project Idea:\n{user_idea}\n\n"
                f"Please structure your design document in detailed Markdown with the following sections:\n"
                f"1. **Executive Summary & System Goals**\n"
                f"2. **High-Level System Topology & Component Diagram** (include Mermaid diagrams where appropriate)\n"
                f"3. **Data Flow & Communication Protocols**\n"
                f"4. **Technology Stack Selection & Trade-Off Analysis**\n"
                f"5. **Core API Specifications & Data Models (JSON/Protobuf/Schemas)**\n"
                f"6. **Security, Authentication & Authorization Strategy**\n"
                f"7. **Scalability, Performance & High Availability (HA)**\n"
                f"8. **Failure Modes, Resilience & Disaster Recovery**\n"
                f"9. **Implementation Milestones & Phased Rollout Plan**\n\n"
                f"Provide a complete, production-ready document with concrete technical decisions."
            )

    def generate_critique_prompt(self, architecture_doc: str, focus_areas: Optional[List[str]] = None) -> str:
        """
        Formulate a rigorous peer-review critique prompt for Gemini / NotebookLM.
        """
        focus_str = ", ".join(focus_areas) if focus_areas else "Scalability, Concurrency, Security, Fault Tolerance, Single Points of Failure, Latency, Data Consistency"
        system = (
            "You are a controller formatting a review prompt for a senior architecture auditor. "
            "Do NOT write the critique yourself. Output the exact prompt to send to the critique AI."
        )

        user_content = (
            f"Draft a critique prompt that instructs the auditor to stress-test the attached architecture document.\n"
            f"Focus areas: {focus_str}.\n"
            f"The prompt should ask for:\n"
            f"- Critical flaws or architectural anti-patterns\n"
            f"- Specific bottleneck scenarios under load\n"
            f"- Missing security controls or failure handling\n"
            f"- Actionable, concrete recommendations for the next revision\n"
            f"Keep the prompt structured and clear."
        )

        try:
            formatted_instruction = self._call_ollama(
                messages=[{"role": "user", "content": user_content}],
                system_prompt=system,
            )
            return (
                f"{formatted_instruction}\n\n"
                f"---\n"
                f"### Architecture Document to Review:\n\n"
                f"{architecture_doc}"
            )
        except Exception:
            return (
                f"You are a Senior Principal Architecture Reviewer. Conduct an adversarial, in-depth critique "
                f"of the following software architecture specification. Focus on: {focus_str}.\n\n"
                f"Identify:\n"
                f"1. **Critical Architectural Vulnerabilities & Single Points of Failure (SPOFs)**\n"
                f"2. **Scalability & Data Synchronization Bottlenecks**\n"
                f"3. **Unaddressed Edge Cases, Latency Risks, and Concurrency Hazards**\n"
                f"4. **Security & Authorization Weaknesses**\n"
                f"5. **Prioritized Action Items for Next Revision**\n\n"
                f"---\n"
                f"### Architecture Document:\n\n{architecture_doc}"
            )

    def generate_refinement_prompt(
        self, current_doc: str, critique_feedback: str, iteration: int
    ) -> str:
        """
        Formulate a refinement prompt to feed back into ChatGPT along with the critique.
        """
        system = (
            "You are a controller coordinating an architecture refinement loop. "
            "Format the prompt for ChatGPT to update the architecture based on reviewer feedback. "
            "Do NOT write the architecture yourself."
        )

        user_content = (
            f"Format an iterative refinement prompt for ChatGPT (Revision v{iteration + 1}).\n"
            f"Instruct ChatGPT to review the critique feedback, resolve every identified flaw, "
            f"and produce the complete, updated architecture document in Markdown."
        )

        try:
            prompt_header = self._call_ollama(
                messages=[{"role": "user", "content": user_content}],
                system_prompt=system,
            )
            return (
                f"{prompt_header}\n\n"
                f"### Auditor Critique & Recommendations:\n"
                f"'''\n{critique_feedback}\n'''\n\n"
                f"### Current Architecture (v{iteration}):\n"
                f"'''\n{current_doc}\n'''\n\n"
                f"Please output the entire updated **Architecture Specification (v{iteration + 1})** "
                f"incorporating all necessary architectural improvements and fixes in clean Markdown."
            )
        except Exception:
            return (
                f"Please produce **Architecture Specification (v{iteration + 1})** by systematically addressing "
                f"the architectural critique and peer review below.\n\n"
                f"### Peer Review Feedback:\n"
                f"'''\n{critique_feedback}\n'''\n\n"
                f"### Current Specification (v{iteration}):\n"
                f"'''\n{current_doc}\n'''\n\n"
                f"Deliver the complete revised architecture specification in full Markdown with updated diagrams and schemas."
            )

    def generate_commit_message(
        self, version: int, changes_summary: str = "", doc_filename: str = ""
    ) -> str:
        """
        Generate an instant, conventional git commit message for the revision (0ms).
        """
        clean_summary = (
            changes_summary.strip().replace("\n", " ")
            if changes_summary
            else f"Architecture iteration v{version}"
        )
        if len(clean_summary) > 90:
            clean_summary = clean_summary[:87] + "..."

        prefix_tag = f" [{doc_filename}]" if doc_filename else ""
        if version == 1:
            subject = f"feat(arch): initial specification v1{prefix_tag} - {clean_summary}"
        else:
            subject = f"refactor(arch): refine specification v{version}{prefix_tag} - {clean_summary}"

        body = (
            f"Iterative architecture revision v{version}.\n"
            f"Context: {changes_summary[:300] if changes_summary else 'Pipeline automated revision'}"
        )
        return f"{subject}\n\n{body}".strip()


    def evaluate_iteration_stability(
        self, architecture_doc: str, critique_feedback: str
    ) -> Dict[str, Any]:
        """
        Evaluate if the architecture has stabilized or if severe concerns remain.
        Returns a structured dictionary with metrics and recommendations.
        """
        system = (
            "You are an architectural state evaluation controller. "
            "Analyze the latest architecture and review feedback. "
            "Return ONLY a JSON object with this exact schema:\n"
            "{\n"
            '  "stability_score": 1-10,\n'
            '  "major_improvements": ["item1", "item2"],\n'
            '  "remaining_concerns": ["item1", "item2"],\n'
            '  "recommendation": "continue" | "freeze",\n'
            '  "rationale": "short explanation"\n'
            "}"
        )

        user_content = (
            f"Critique Feedback:\n{critique_feedback[:1500]}\n\n"
            f"Architecture Summary (head):\n{architecture_doc[:1000]}"
        )

        try:
            raw = self._call_ollama(
                messages=[{"role": "user", "content": user_content}],
                system_prompt=system,
                json_format=True,
            )
            return json.loads(raw)
        except Exception:
            return {
                "stability_score": 7,
                "major_improvements": ["System components clarified", "Critique points integrated"],
                "remaining_concerns": ["Requires manual review of edge cases"],
                "recommendation": "continue",
                "rationale": "Iteration processed; user review recommended.",
            }
