from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - exercised on Python 3.10 only
    tomllib = None


@dataclass
class ReleaseGuardResult:
    version: str = ""
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _project_version(repo: Path) -> str:
    text = _read_text(repo / "pyproject.toml")
    if tomllib is not None:
        data = tomllib.loads(text)
        return str(data["project"]["version"])
    in_project = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped == "[project]":
            in_project = True
            continue
        if in_project and stripped.startswith("["):
            break
        if in_project:
            match = re.match(r'version\s*=\s*"([^"]+)"', stripped)
            if match:
                return match.group(1)
    raise ValueError("pyproject.toml is missing [project].version")


def _runtime_version(repo: Path) -> str:
    text = _read_text(repo / "src" / "context_kernel" / "__init__.py")
    match = re.search(r'^__version__\s*=\s*"([^"]+)"', text, re.MULTILINE)
    if not match:
        raise ValueError("src/context_kernel/__init__.py is missing __version__")
    return match.group(1)


def _npm_version(repo: Path) -> str:
    data = json.loads(_read_text(repo / "packages" / "npm" / "akernel" / "package.json"))
    return str(data["version"])


def _unreleased_bullets(changelog: str) -> list[str]:
    match = re.search(r"^## Unreleased\s*(.*?)(?=^##\s+|\Z)", changelog, re.MULTILINE | re.DOTALL)
    if not match:
        return []
    return [line.strip() for line in match.group(1).splitlines() if line.strip().startswith("- ")]


def check_release_metadata(repo: Path, *, strict_release: bool = False, tag: str = "") -> ReleaseGuardResult:
    repo = repo.resolve()
    result = ReleaseGuardResult()

    try:
        result.version = _project_version(repo)
        versions = {
            "pyproject.toml": result.version,
            "src/context_kernel/__init__.py": _runtime_version(repo),
            "packages/npm/akernel/package.json": _npm_version(repo),
        }
    except Exception as exc:  # noqa: BLE001 - this is a release guard, so surface any metadata failure.
        result.errors.append(f"Could not read release metadata: {exc}")
        return result

    mismatches = {name: version for name, version in versions.items() if version != result.version}
    if mismatches:
        rendered = ", ".join(f"{name}={version}" for name, version in mismatches.items())
        result.errors.append(f"Version mismatch: pyproject.toml={result.version}; {rendered}")

    expected_tag = f"v{result.version}"
    if tag and tag != expected_tag:
        result.errors.append(f"Tag {tag!r} does not match package version {expected_tag!r}")

    release_notes = repo / ".github" / "release-notes" / f"{expected_tag}.md"
    if not release_notes.exists():
        result.errors.append(f"Missing release notes: {release_notes.relative_to(repo)}")
    else:
        notes_text = _read_text(release_notes)
        if f"# {expected_tag}" not in notes_text:
            result.errors.append(f"Release notes must include heading '# {expected_tag}'")

    changelog_path = repo / "CHANGELOG.md"
    try:
        changelog = _read_text(changelog_path)
    except FileNotFoundError:
        result.errors.append("Missing CHANGELOG.md")
        changelog = ""

    if changelog:
        version_heading = re.compile(rf"^##\s+{re.escape(result.version)}\s+-\s+\d{{4}}-\d{{2}}-\d{{2}}\s*$", re.MULTILINE)
        if not version_heading.search(changelog):
            result.errors.append(f"CHANGELOG.md is missing '## {result.version} - YYYY-MM-DD'")

        unreleased = _unreleased_bullets(changelog)
        if unreleased and strict_release:
            result.errors.append(
                f"CHANGELOG.md still has {len(unreleased)} Unreleased item(s); move them into {result.version} before publishing"
            )
        elif unreleased:
            result.warnings.append(
                f"CHANGELOG.md has {len(unreleased)} Unreleased item(s); bump version and release notes before tagging"
            )

    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate Context Kernel release metadata.")
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--strict-release", action="store_true", help="Fail if Unreleased changelog entries remain.")
    parser.add_argument("--tag", default="", help="Expected git tag, for example v0.1.26.")
    args = parser.parse_args(argv)

    result = check_release_metadata(args.repo, strict_release=args.strict_release, tag=args.tag)
    if result.version:
        print(f"release_guard: version {result.version}")
    for warning in result.warnings:
        print(f"release_guard warning: {warning}")
    for error in result.errors:
        print(f"release_guard error: {error}", file=sys.stderr)
    if not result.ok:
        return 1
    print("release_guard: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
