"""Tests for the Claude Code plugin shipped in plugin/.

Catches drift between the openfoia CLI surface and what SKILL.md documents:
if a new command group is added but not mentioned in the skill, the test fails.
"""

import json
import re
import shlex
from pathlib import Path

import pytest
import typer.main
import yaml

from openfoia.cli import app

REPO_ROOT = Path(__file__).parent.parent
PLUGIN_DIR = REPO_ROOT / "plugin"
MARKETPLACE_JSON = REPO_ROOT / ".claude-plugin" / "marketplace.json"
PLUGIN_JSON = PLUGIN_DIR / ".claude-plugin" / "plugin.json"
SKILL_MD = PLUGIN_DIR / "skills" / "openfoia" / "SKILL.md"
COMMANDS_DIR = PLUGIN_DIR / "commands"


def _parse_frontmatter(markdown: str) -> dict:
    """Extract YAML frontmatter from a markdown file, or return {}."""
    if not markdown.startswith("---\n"):
        return {}
    end = markdown.find("\n---\n", 4)
    if end == -1:
        return {}
    return yaml.safe_load(markdown[4:end]) or {}


def _cli_command_names() -> set[str]:
    """Every top-level openfoia command — both @app.command and add_typer groups."""
    names = set()
    for cmd in app.registered_commands:
        if cmd.name:
            names.add(cmd.name)
        elif cmd.callback:
            names.add(cmd.callback.__name__.replace("_", "-"))
    for group in app.registered_groups:
        if group.name:
            names.add(group.name)
    return names


def _click_root():
    """The CLI as a click Group -- the real command tree, not a guess at it."""
    return typer.main.get_command(app)


def _iter_code_snippets(markdown: str):
    """Yield every fenced code block and inline code span in a markdown file.

    Only code is checked. Prose mentioning "the openfoia CLI" is not an
    invocation and must not be parsed as one.
    """
    fenced = re.findall(r"```[a-z]*\n(.*?)```", markdown, re.S)
    for block in fenced:
        yield from block.splitlines()
    stripped = re.sub(r"```[a-z]*\n.*?```", "", markdown, flags=re.S)
    yield from re.findall(r"`([^`\n]+)`", stripped)


def _iter_invocations(markdown: str):
    """Yield token lists for every `openfoia ...` command found in code."""
    for snippet in _iter_code_snippets(markdown):
        snippet = snippet.strip()
        # Drop trailing comments and shell pipelines/chains.
        snippet = snippet.split("#", 1)[0].strip()
        for part in re.split(r"&&|\|\||\||;", snippet):
            part = part.strip()
            if not part.startswith("openfoia"):
                continue
            try:
                tokens = shlex.split(part)
            except ValueError:
                continue
            if len(tokens) > 1:
                yield part, tokens[1:]


def _plugin_markdown_files():
    return [SKILL_MD, *sorted(COMMANDS_DIR.glob("*.md"))]


class Parsed:
    """One `openfoia ...` invocation resolved against the real command tree."""

    def __init__(self, error=None, cmd=None, path=(), positionals=0, flags=()):
        self.error = error
        self.cmd = cmd
        self.path = tuple(path)
        self.positionals = positionals
        self.flags = tuple(flags)

    @property
    def label(self):
        return "openfoia " + " ".join(self.path)


def _parse_invocation(root, tokens):
    """Resolve tokens against the CLI, separating flags from positionals.

    Option values must not be counted as positional arguments -- in
    `records search "x" --source muckrock`, "muckrock" belongs to --source.
    """
    words = [t for t in tokens if not t.startswith("-")]
    if not words:
        return Parsed()  # e.g. `openfoia --version`

    cmd = root.get_command(None, words[0])
    if cmd is None:
        return Parsed(error=f"no such command 'openfoia {words[0]}'")

    path = [words[0]]
    if hasattr(cmd, "get_command"):
        if len(words) < 2:
            return Parsed(cmd=cmd, path=path)  # a group named on its own
        sub = cmd.get_command(None, words[1])
        if sub is None:
            return Parsed(error=f"no such subcommand 'openfoia {words[0]} {words[1]}'")
        cmd, path = sub, [words[0], words[1]]

    # Which flags consume the following token?
    takes_value = {}
    for param in cmd.params:
        wants = not getattr(param, "is_flag", False) and getattr(param, "nargs", 1) != 0
        for opt in list(param.opts) + list(param.secondary_opts):
            takes_value[opt] = wants

    rest = tokens[len(path) :]
    positionals, flags, skip = 0, [], False
    for token in rest:
        if skip:
            skip = False
            continue
        if token.startswith("-") and token != "-":
            flag = token.split("=", 1)[0]
            flags.append(flag)
            if "=" not in token and takes_value.get(flag, False):
                skip = True
        else:
            positionals += 1
    return Parsed(cmd=cmd, path=path, positionals=positionals, flags=flags)


def _iter_parsed():
    """Yield (path, raw_command, Parsed) for every invocation in the plugin."""
    root = _click_root()
    for path in _plugin_markdown_files():
        for raw, tokens in _iter_invocations(path.read_text()):
            yield path, raw, _parse_invocation(root, tokens)


class TestMarketplaceAndPluginManifests:
    def test_marketplace_json_is_valid(self):
        data = json.loads(MARKETPLACE_JSON.read_text())
        assert data["name"] == "openfoia"
        assert len(data["plugins"]) >= 1

    def test_plugin_json_is_valid(self):
        data = json.loads(PLUGIN_JSON.read_text())
        assert data["name"] == "openfoia"
        assert "version" in data
        assert "description" in data

    def test_marketplace_and_plugin_names_match(self):
        marketplace = json.loads(MARKETPLACE_JSON.read_text())
        plugin = json.loads(PLUGIN_JSON.read_text())
        marketplace_plugin = next(p for p in marketplace["plugins"] if p["name"] == plugin["name"])
        assert marketplace_plugin is not None

    def test_marketplace_source_resolves_to_plugin_dir(self):
        marketplace = json.loads(MARKETPLACE_JSON.read_text())
        source = marketplace["plugins"][0]["source"]
        resolved = (MARKETPLACE_JSON.parent.parent / source).resolve()
        assert resolved == PLUGIN_DIR.resolve()
        assert (resolved / ".claude-plugin" / "plugin.json").exists()


class TestSkillFrontmatter:
    def test_skill_md_exists(self):
        assert SKILL_MD.exists()

    def test_skill_md_has_required_frontmatter(self):
        frontmatter = _parse_frontmatter(SKILL_MD.read_text())
        assert frontmatter.get("name") == "openfoia"
        assert isinstance(frontmatter.get("description"), str)
        assert len(frontmatter["description"]) > 100, (
            "Description should be substantive — it's the primary trigger signal"
        )


class TestCommandFrontmatter:
    @pytest.fixture
    def command_files(self):
        return sorted(COMMANDS_DIR.glob("*.md"))

    def test_commands_exist(self, command_files):
        names = {f.stem for f in command_files}
        assert names == {"foia-search", "foia-investigate", "foia-graph", "foia-install"}

    def test_every_command_has_description_and_tools(self, command_files):
        for cmd_file in command_files:
            frontmatter = _parse_frontmatter(cmd_file.read_text())
            assert "description" in frontmatter, f"{cmd_file.name} missing description frontmatter"
            assert "allowed-tools" in frontmatter, (
                f"{cmd_file.name} missing allowed-tools — should scope blast radius"
            )

    def test_allowed_tools_scoped_to_openfoia(self, command_files):
        """Commands should not wildcard all tools — they should scope to openfoia."""
        for cmd_file in command_files:
            frontmatter = _parse_frontmatter(cmd_file.read_text())
            tools = frontmatter.get("allowed-tools", "")
            assert "openfoia" in tools or "open:" in tools, (
                f"{cmd_file.name} allowed-tools should reference openfoia: {tools!r}"
            )


class TestSkillCoversEntireCliSurface:
    """The load-bearing test: catches drift when new CLI commands are added
    without corresponding updates to SKILL.md."""

    def test_every_documented_invocation_resolves_to_a_real_command(self):
        """Every `openfoia <group> <subcommand>` in the plugin must exist.

        The group-name grep below cannot catch this: `openfoia config get`
        and `openfoia entities test <name> <text>` both contain a valid group
        name while being commands that do not exist. A skill that teaches
        Claude wrong invocations burns the user's turns on errors.
        """
        failures = []
        for path, raw, parsed in _iter_parsed():
            if parsed.error:
                failures.append(f"{path.name}: {parsed.error}  --  {raw}")
                continue
            if parsed.cmd is None or hasattr(parsed.cmd, "get_command"):
                continue

            # Extra positionals are the other way docs go wrong: `config get`
            # and `entities test <name> <text>` both resolve to a real command
            # and then die on unexpected arguments.
            args = [q for q in parsed.cmd.params if getattr(q, "param_type_name", "") == "argument"]
            if any(getattr(q, "nargs", 1) == -1 for q in args):
                continue
            allowed = sum(max(getattr(q, "nargs", 1), 0) for q in args)
            if parsed.positionals > allowed:
                failures.append(
                    f"{path.name}: '{parsed.label}' takes {allowed} positional argument(s), "
                    f"example passes {parsed.positionals}  --  {raw}"
                )

        assert not failures, "Plugin docs reference commands that do not exist:\n" + "\n".join(
            failures
        )

    def test_every_documented_flag_exists_on_its_command(self):
        """Flags in the plugin docs must exist on the command they are used with.

        Catches the other half of the drift: `agency search -s ca` and
        `campaign create -f body.txt` both name real commands with flags the
        command does not accept.
        """
        failures = []
        for path, raw, parsed in _iter_parsed():
            if parsed.error or parsed.cmd is None:
                continue
            valid = {o for q in parsed.cmd.params for o in q.opts}
            valid |= {o for q in parsed.cmd.params for o in q.secondary_opts}
            valid.add("--help")
            for flag in parsed.flags:
                if flag not in valid:
                    failures.append(
                        f"{path.name}: '{parsed.label}' has no option {flag}  --  {raw}"
                    )

        assert not failures, "Plugin docs use flags that do not exist:\n" + "\n".join(failures)

    def test_every_documented_required_option_is_shown(self):
        """If a command has required options, the docs must not omit them.

        `template generate <name> -o draft.txt` looks fine and errors out --
        it omits four required options.

        Only invocations that already pass at least one flag are checked: a
        bare `openfoia request new` in prose is a reference to the command,
        not an example someone is meant to paste.
        """
        failures = []
        for path, raw, parsed in _iter_parsed():
            if parsed.error or parsed.cmd is None or not parsed.flags:
                continue
            used = set(parsed.flags)
            for param in parsed.cmd.params:
                if not getattr(param, "required", False):
                    continue
                if getattr(param, "param_type_name", "") == "argument":
                    continue  # positional; presence is covered by the arity check
                if not used & set(param.opts):
                    failures.append(
                        f"{path.name}: '{parsed.label}' requires {'/'.join(param.opts)} "
                        f"but the example omits it  --  {raw}"
                    )

        assert not failures, "Plugin docs show commands missing required options:\n" + "\n".join(
            failures
        )

    def test_skill_md_mentions_every_top_level_command(self):
        skill_text = SKILL_MD.read_text()
        cli_commands = _cli_command_names()

        missing = sorted(cmd for cmd in cli_commands if cmd not in skill_text)
        assert not missing, (
            f"SKILL.md is missing documentation for {len(missing)} CLI commands: "
            f"{missing}. When you add a command to openfoia/cli.py, also add it "
            f"to plugin/skills/openfoia/SKILL.md so Claude stays in sync."
        )
