import importlib.util
import json
import os
import unittest

from helpers import ROOT

spec = importlib.util.spec_from_file_location("generate", os.path.join(ROOT, "build", "generate.py"))
gen = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gen)


class Generate(unittest.TestCase):
    def test_committed_outputs_in_sync(self):
        self.assertEqual(gen.main(["--check"]), 0, "run: python build/generate.py")

    def test_deterministic(self):
        self.assertEqual(gen.build(), gen.build())

    def test_permissions_to_claude_tools(self):
        self.assertEqual(gen.claude_tools("read"), "Read, Grep, Glob")
        self.assertEqual(gen.claude_tools("read, run"), "Read, Grep, Glob, Bash")
        self.assertEqual(gen.claude_tools("read,run,edit"), "Read, Grep, Glob, Bash, Edit, Write")
        with self.assertRaises(SystemExit):
            gen.claude_tools("read, admin")

    def test_frontmatter_roundtrip(self):
        meta, body = gen.parse_frontmatter("---\nname: x\ndescription: a: b\n---\nbody\n")
        self.assertEqual(meta, {"name": "x", "description": "a: b"})
        self.assertEqual(body, "body\n")
        fm = gen.render_frontmatter([("name", "x"), ("description", 'say "hi": now'), ("flag", True)])
        self.assertIn('description: "say \\"hi\\": now"', fm)
        self.assertIn("flag: true", fm)
        with self.assertRaises(ValueError):
            gen.parse_frontmatter("no frontmatter")

    def test_outputs(self):
        out, stamped = gen.build()
        v = gen.version()
        plugin = json.loads(out["adapters/claude-code/.claude-plugin/plugin.json"])
        self.assertEqual(plugin["name"], "coding-plugin")
        self.assertEqual(plugin["version"], v)
        market = json.loads(out[".claude-plugin/marketplace.json"])
        source = market["plugins"][0]["source"]
        self.assertTrue(os.path.isdir(os.path.join(ROOT, source)))
        self.assertNotIn("version", market["plugins"][0])  # plugin.json is the single version source
        core = stamped["core/scripts/coding_plugin.py"]
        self.assertIn(f'__version__ = "{v}"', core)
        self.assertEqual(out["adapters/claude-code/lib/coding_plugin.py"], core)
        self.assertEqual(out["adapters/claude-code/skills/init/assets/coding_plugin.py"], core)

    def test_skills_have_no_placeholders_and_bundle_roles(self):
        out, _ = gen.build()
        for path, text in out.items():
            if path.endswith("SKILL.md"):
                with self.subTest(path=path):
                    self.assertNotIn("{{", text)
                    self.assertTrue(text.startswith("---\nname: "))
                    skill_dir = path.rsplit("/", 1)[0]
                    for ref in gen.re.findall(r"references/([a-z-]+)\.md", text):
                        self.assertIn(f"{skill_dir}/references/{ref}.md", out)
        self.assertIn("${CLAUDE_SKILL_DIR}/assets/coding_plugin.py",
                      out["adapters/claude-code/skills/init/SKILL.md"])

    def test_agents_frontmatter(self):
        out, _ = gen.build()
        text = out["adapters/claude-code/agents/reviewer.md"]
        meta, _ = gen.parse_frontmatter(text)
        self.assertEqual(meta["name"], "reviewer")
        self.assertNotIn("Edit", meta["tools"])
        meta, _ = gen.parse_frontmatter(out["adapters/claude-code/agents/test-writer.md"])
        self.assertIn("Edit", meta["tools"])


if __name__ == "__main__":
    unittest.main()
