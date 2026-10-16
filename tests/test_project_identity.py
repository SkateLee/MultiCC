from __future__ import annotations

import io
import json
import os
import shutil
import unittest
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from rich.console import Console

from agents import memory, prompt, session, skill_evolution, skills, subagent, tools, ui
from agents.agent import Agent
from agents.main import _resolve_api_config
from agents.mcp_client import McpConnection, McpManager
from agents.online_skill_eval import _online_eval_root


class ProjectIdentityTests(unittest.TestCase):
    def setUp(self) -> None:
        test_directory = Path(__file__).resolve().parent
        self.root = test_directory / f"multicc-tests-{uuid.uuid4().hex}"
        self.assertTrue(self.root.resolve().is_relative_to(test_directory))
        self.root.mkdir()
        self.addCleanup(shutil.rmtree, self.root)
        self.home = self.root / "home"
        self.project = self.root / "project"
        self.home.mkdir()
        self.project.mkdir()
        previous_cwd = Path.cwd()
        self.addCleanup(os.chdir, previous_cwd)
        os.chdir(self.project)
        home_patch = patch.object(Path, "home", return_value=self.home)
        home_patch.start()
        self.addCleanup(home_patch.stop)
        skills.reset_skill_cache()
        subagent.reset_agent_cache()
        self.addCleanup(skills.reset_skill_cache)
        self.addCleanup(subagent.reset_agent_cache)

    def write_file(self, path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def test_api_aliases_and_evolution_flag(self) -> None:
        with patch.dict(os.environ, {
            "MULTICC_API_KEY": "test-key",
            "MULTICC_API_BASE": "https://provider.example/v1",
            "MULTICC_AUTO_SKILL_EVOLUTION": "off",
        }, clear=True):
            self.assertEqual(_resolve_api_config(None), ("https://provider.example/v1", "test-key", True))
            self.assertFalse(Agent._online_evolution_enabled(None))
        with patch.dict(os.environ, {"MULTICC_AUTO_SKILL_EVOLUTION": "1"}, clear=True):
            self.assertTrue(Agent._online_evolution_enabled(None))

    def test_project_and_user_skills_round_trip(self) -> None:
        for name, target, root in (("project-skill", "project", self.project), ("user-skill", "user", self.home)):
            result = skill_evolution.create_skill_file(
                name=name, description="Test skill", target=target,
                instructions="Use ${MULTICC_SKILL_DIR} for $ARGUMENTS.",
            )
            self.assertTrue(result["ok"], result)
            self.assertTrue(Path(result["file"]).is_relative_to(root / ".multicc" / "skills"))
        discovered = {skill.name: skill for skill in skills.discover_skills()}
        self.assertEqual(set(discovered), {"project-skill", "user-skill"})
        resolved = skills.resolve_skill_prompt(discovered["project-skill"], "the task")
        self.assertIn(str(self.project / ".multicc" / "skills" / "project-skill"), resolved)
        self.assertIn("the task", resolved)
        self.assertTrue((self.project / ".multicc" / "skill-evolution" / "usage.jsonl").is_file())
        self.assertTrue(_online_eval_root().is_relative_to(self.project / ".multicc"))

    def test_memory_remains_project_scoped(self) -> None:
        filename = memory.save_memory("Preference", "A test preference", "user", "Use concise answers.")
        first_directory = memory.get_memory_dir()
        self.assertTrue(first_directory.is_relative_to(self.home / ".multicc" / "projects"))
        self.assertEqual(memory.list_memories()[0].content.strip(), "Use concise answers.")
        self.assertTrue((first_directory / filename).is_file())
        another_project = self.root / "another-project"
        another_project.mkdir()
        os.chdir(another_project)
        self.assertNotEqual(memory.get_memory_dir(), first_directory)
        self.assertEqual(memory.list_memories(), [])

    def test_session_round_trip_and_folded_memory(self) -> None:
        self.assertEqual(session.SESSION_DIR.parts[-2:], (".multicc", "sessions"))
        with patch.object(session, "SESSION_DIR", self.home / ".multicc" / "sessions"):
            payload = {"openaiMessages": [{"role": "user", "content": "hello"}]}
            session.save_session("sample", payload)
            self.assertEqual(session.load_session("sample"), payload)
            session.save_folded_session_memory("sample", {"summary": "saved context"})
        folded = self.project / ".multicc" / "sessions" / "sample.folded-memory.latest.json"
        self.assertEqual(json.loads(folded.read_text(encoding="utf-8"))["summary"], "saved context")

    def test_plans_and_large_results_use_shared_user_directory(self) -> None:
        agent = object.__new__(Agent)
        agent.session_id = "sample"
        self.assertEqual(Path(agent._generate_plan_file_path()), self.home / ".multicc" / "plans" / "plan-sample.md")
        output = "large result\n" * 4000
        result = agent._persist_large_result("test_tool", output)
        stored = list((self.home / ".multicc" / "tool-results").glob("*.txt"))
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0].read_text(encoding="utf-8"), output)
        self.assertIn(str(stored[0]), result)

    def test_project_instructions_resolve_includes_and_rules(self) -> None:
        self.write_file(self.root / "MULTICC.md", "Parent project guidance.")
        self.write_file(self.project / "MULTICC.md", "Project guidance.\n@./details.md")
        self.write_file(self.project / "details.md", "Included guidance.")
        self.write_file(self.project / ".multicc" / "rules" / "style.md", "Use clear names.")
        instructions = prompt.load_project_instructions()
        self.assertLess(instructions.index("Parent project guidance."), instructions.index("Project guidance."))
        self.assertIn("Included guidance.", instructions)
        self.assertIn("Use clear names.", instructions)

    def test_settings_merge_and_custom_agent_discovery(self) -> None:
        self.write_file(self.home / ".multicc" / "settings.json", json.dumps({
            "mcpServers": {"shared": {"command": "global-command"}, "global": {"command": "global-only"}},
            "permissions": {"deny": ["run_shell"]},
        }))
        self.write_file(self.project / ".multicc" / "settings.json", json.dumps({
            "mcpServers": {"shared": {"command": "project-command"}},
        }))
        self.write_file(self.project / ".mcp.json", json.dumps({
            "mcpServers": {"shared": {"command": "local-command"}},
        }))
        configs = McpManager()._load_configs()
        self.assertEqual(configs["shared"]["command"], "local-command")
        self.assertIn("global", configs)
        with patch.object(tools, "_cached_rules", None):
            self.assertEqual(tools.check_permission("run_shell", {"command": "echo hello"})["action"], "deny")
        self.write_file(self.project / ".multicc" / "agents" / "reviewer.md",
                        "---\nname: reviewer\ndescription: Review changes\n---\nInspect the changes.")
        self.assertIn("reviewer", subagent._discover_custom_agents())

    def test_terminal_and_model_identity(self) -> None:
        output = io.StringIO()
        with patch.object(ui, "console", Console(file=output, width=90, color_system=None)):
            ui.print_welcome()
            ui.print_user_prompt()
            ui.print_goodbye()
        self.assertIn("multiCC ready", output.getvalue())
        self.assertIn("Bye from multiCC", output.getvalue())
        with patch.object(prompt, "get_git_context", return_value=""):
            built = prompt.build_system_prompt()
        self.assertIn("You are multiCC", built)
        self.assertNotIn("{{project_instructions}}", built)
        self.assertIn("multiCC", subagent.EXPLORE_PROMPT)
        self.assertIn("multiCC", subagent.GENERAL_PROMPT)


class McpIdentityTests(unittest.IsolatedAsyncioTestCase):
    async def test_initialize_announces_multicc(self) -> None:
        connection = McpConnection("sample", "unused-command")
        connection._send_request = AsyncMock(return_value={})
        connection._send_notification = Mock()
        await connection.initialize()
        args = connection._send_request.await_args.args
        self.assertEqual(args[0], "initialize")
        self.assertEqual(args[1]["clientInfo"]["name"], "multiCC")
        connection._send_notification.assert_called_once_with("notifications/initialized")


if __name__ == "__main__":
    unittest.main()
