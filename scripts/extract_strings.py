#!/usr/bin/env python3
"""Extract English product copy from the app submodules into one normalized CSV.

Sources:
  apps/android  Android string resources (res/values/*.xml: <string>, <plurals>, <string-array>)
  apps/apple    iOS/macOS String Catalogs (*.xcstrings) and en.lproj *.strings / *.stringsdict

Each row is one piece of user-visible text. Plural forms and array items get one row
each, distinguished by the `variant` column. Standard library only.

Usage: python3 scripts/extract_strings.py [--output strings.csv]
"""

import argparse
import csv
import json
import plistlib
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

SUBMODULES = {
    "android": {"path": ROOT / "apps" / "android", "repo": "https://github.com/duckduckgo/Android"},
    "apple": {"path": ROOT / "apps" / "apple", "repo": "https://github.com/duckduckgo/apple-browsers"},
}

COLUMNS = [
    "platform",           # Android | iOS | macOS | Apple (shared)
    "module",             # Gradle module or Apple target/package the string belongs to
    "key",                # String identifier used in code
    "variant",            # Plural form (plural:one), array item (item:0), substitution, device; blank if none
    "text",               # English source text
    "developer_comment",  # Translator/developer note (Android `instruction`, Apple comment)
    "context_comment",    # Android only: nearest preceding XML comment, usually a section heading
    "placeholders",       # Format specifiers found in the text, space separated
    "translatable",       # false when marked do-not-translate
    "state",              # Apple String Catalog extraction state (e.g. stale = no longer referenced in code)
    "internal_only",      # Android only: true for internal build flavors and *-internal modules
    "source_file",        # Path relative to this repo
    "line",               # Line number in source_file
    "source_url",         # Permalink to the line at the submodule's checked-out commit
]

PLACEHOLDER_RE = re.compile(
    r"%#@[A-Za-z0-9_]+@"                                   # stringsdict / xcstrings substitution
    r"|%arg\b"                                             # xcstrings substitution argument
    r"|%(?:\d+\$)?[-#+ 0,(]*\d*(?:\.\d+)?(?:ll|l|h|q|z|t|j)?[@dDuUxXoOfFeEgGcCsSaAp]"
)

TOOLS_NS = "{http://schemas.android.com/tools}"
ANDROID_SKIP_SOURCE_SETS = {"test", "androidTest", "testFixtures", "sharedTest"}
ANDROID_SKIP_FILES = {"font_certs.xml"}  # Google downloadable-font certificates, not copy


def placeholders(text):
    return " ".join(m.group(0) for m in PLACEHOLDER_RE.finditer(text.replace("%%", "")))


def git_sha(path):
    try:
        return subprocess.run(
            ["git", "-C", str(path), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
        ).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return ""


class RowBuilder:
    def __init__(self, submodule):
        self.base = SUBMODULES[submodule]["path"]
        self.repo = SUBMODULES[submodule]["repo"]
        self.sha = git_sha(self.base)

    def row(self, file, line, **fields):
        rel = file.relative_to(self.base).as_posix()
        url = f"{self.repo}/blob/{self.sha}/{rel}#L{line}" if self.sha and line else ""
        text = fields.get("text", "")
        row = {c: "" for c in COLUMNS}
        row.update(
            placeholders=placeholders(text),
            translatable="true",
            source_file=file.relative_to(ROOT).as_posix(),
            line=line or "",
            source_url=url,
        )
        row.update({k: v for k, v in fields.items() if v is not None})
        return row


def is_ignored_path(path):
    return any(part in {"build", "node_modules", ".build", "DerivedData", "Pods"} for part in path.parts)


# --- Android -----------------------------------------------------------------------------------

def android_unescape(text):
    text = text.strip()
    if len(text) >= 2 and text[0] == '"' and text[-1] == '"':
        text = text[1:-1]
    # Keep \n and \t as visible escapes so each string stays on one CSV line.
    return re.sub(r"\\(['\"@?])", r"\1", text)


def android_inner_text(el):
    """Text content including inline markup (<b>, <a>, …), as written in the resource file."""
    parts = [el.text or ""]
    for child in el:
        if child.tag is ET.Comment:
            parts.append(child.tail or "")
            continue
        parts.append(ET.tostring(child, encoding="unicode"))  # includes tail
    return android_unescape("".join(parts))


def android_comment(el):
    text = (el.text or "").strip()
    if not text or text.startswith("smartling.") or "Licensed under the Apache License" in text:
        return None
    return " ".join(text.split())


def android_line_finder(raw):
    lines = {}
    for i, line in enumerate(raw.splitlines(), 1):
        for m in re.finditer(r'<(string|plurals|string-array)\b[^>]*\bname="([^"]+)"', line):
            lines.setdefault((m.group(1), m.group(2)), i)
    return lines


def extract_android():
    rb = RowBuilder("android")
    rows = []
    for file in sorted(rb.base.glob("**/src/*/res/values/*.xml")):
        rel = file.relative_to(rb.base)
        if is_ignored_path(rel):
            continue
        module_parts = rel.parts[: rel.parts.index("src")]
        source_set = rel.parts[len(module_parts) + 1]
        if source_set in ANDROID_SKIP_SOURCE_SETS or file.name in ANDROID_SKIP_FILES:
            continue
        raw = file.read_text(encoding="utf-8")
        if not re.search(r"<(string|plurals|string-array)\b", raw):
            continue
        try:
            root = ET.fromstring(raw, parser=ET.XMLParser(target=ET.TreeBuilder(insert_comments=True)))
        except ET.ParseError as e:
            print(f"warning: skipping {file}: {e}", file=sys.stderr)
            continue

        module = "/".join(module_parts)
        internal = source_set in {"internal", "debug"} or module_parts[-1].endswith("-internal")
        file_untranslatable = file.name.startswith("donottranslate")
        lines = android_line_finder(raw)
        context = None

        for el in root:
            if el.tag is ET.Comment:
                context = android_comment(el) or context
                continue
            if el.tag not in {"string", "plurals", "string-array"}:
                continue
            name = el.get("name")
            common = dict(
                platform="Android",
                module=module,
                key=name,
                developer_comment=el.get("instruction", ""),
                context_comment=context or "",
                translatable="false" if file_untranslatable or el.get("translatable") == "false" else "true",
                internal_only="true" if internal else "false",
            )
            line = lines.get((el.tag, name))
            if el.tag == "string":
                rows.append(rb.row(file, line, text=android_inner_text(el), **common))
            else:
                items = [i for i in el if i.tag == "item"]
                for idx, item in enumerate(items):
                    variant = f"plural:{item.get('quantity')}" if el.tag == "plurals" else f"item:{idx}"
                    rows.append(rb.row(file, line, variant=variant, text=android_inner_text(item), **common))
    return rows


# --- Apple -------------------------------------------------------------------------------------

def apple_platform(rel):
    top = rel.parts[0]
    return {"iOS": "iOS", "macOS": "macOS"}.get(top, "Apple (shared)")


def apple_module(rel):
    parts = rel.parts
    for marker in ("LocalPackages", "SharedPackages"):
        if marker in parts:
            return parts[parts.index(marker) + 1]
    return parts[1] if parts[0] in {"iOS", "macOS"} and len(parts) > 2 else parts[0]


def parse_dot_strings(raw):
    """Parse a .strings file into (key, value, comment, line) tuples."""
    entries = []
    i, n, line = 0, len(raw), 1
    comment = None

    def read_quoted(pos):
        buf, pos = [], pos + 1
        while raw[pos] != '"':
            if raw[pos] == "\\":
                buf.append(raw[pos:pos + 2])
                pos += 2
            else:
                buf.append(raw[pos])
                pos += 1
        return "".join(buf), pos + 1

    pending_key = None
    key_line = None
    while i < n:
        ch = raw[i]
        if raw.startswith("/*", i):
            end = raw.index("*/", i + 2)
            comment = " ".join(raw[i + 2:end].split())
            line += raw.count("\n", i, end + 2)
            i = end + 2
        elif raw.startswith("//", i):
            end = raw.find("\n", i)
            end = n if end == -1 else end
            comment = raw[i + 2:end].strip()
            i = end
        elif ch == '"':
            start_line = line
            value, j = read_quoted(i)
            line += raw.count("\n", i, j)
            i = j
            if pending_key is None:
                pending_key, key_line = value, start_line
            else:
                entries.append((pending_key, value, comment, key_line))
                pending_key, comment = None, None
        else:
            if ch == "\n":
                line += 1
            i += 1
    return entries


def strings_unescape(text):
    return re.sub(r'\\(["\\\'])', r"\1", text)


def plist_key_lines(raw):
    """Line numbers of top-level <key> entries in a stringsdict (indented by one level)."""
    lines = {}
    for i, line in enumerate(raw.splitlines(), 1):
        m = re.match(r"^(\t| {1,4})<key>(.*)</key>\s*$", line)
        if m:
            lines.setdefault(m.group(2), i)
    return lines


def extract_dot_strings(rb, file):
    rel = file.relative_to(rb.base)
    raw = file.read_bytes()
    text = raw.decode("utf-16") if raw[:2] in (b"\xff\xfe", b"\xfe\xff") else raw.decode("utf-8-sig")
    common = dict(platform=apple_platform(rel), module=apple_module(rel))
    return [
        rb.row(file, line, key=key, text=strings_unescape(value), developer_comment=comment or "", **common)
        for key, value, comment, line in parse_dot_strings(text)
    ]


def extract_stringsdict(rb, file):
    rel = file.relative_to(rb.base)
    raw = file.read_bytes()
    data = plistlib.loads(raw)
    lines = plist_key_lines(raw.decode("utf-8"))
    common = dict(platform=apple_platform(rel), module=apple_module(rel))
    rows = []
    for key, entry in data.items():
        fmt = entry.get("NSStringLocalizedFormatKey", "")
        variables = {k: v for k, v in entry.items() if isinstance(v, dict)}
        if not variables:
            rows.append(rb.row(file, lines.get(key), key=key, text=fmt, **common))
        for var, spec in variables.items():
            for form, value in spec.items():
                if form.startswith("NSString"):
                    continue
                variant = f"plural:{form}" if len(variables) == 1 else f"{var}.plural:{form}"
                text = fmt.replace(f"%#@{var}@", value) if fmt != f"%#@{var}@" else value
                rows.append(rb.row(file, lines.get(key), key=key, variant=variant, text=text, **common))
    return rows


def xcstrings_units(node, path=()):
    """Yield (variant_path, value) for every stringUnit under an xcstrings localization node."""
    if "stringUnit" in node:
        yield path, node["stringUnit"].get("value", "")
    for kind, options in node.get("variations", {}).items():
        for option, child in options.items():
            yield from xcstrings_units(child, path + (f"{kind}:{option}",))
    for name, sub in node.get("substitutions", {}).items():
        yield from xcstrings_units(sub, path + (name,))


def extract_xcstrings(rb, file):
    rel = file.relative_to(rb.base)
    raw = file.read_text(encoding="utf-8")
    data = json.loads(raw)
    source_lang = data.get("sourceLanguage", "en")
    lines = {}
    for i, line in enumerate(raw.splitlines(), 1):
        m = re.match(r'^    ("(?:[^"\\]|\\.)*")\s*:\s*\{', line)
        if m:
            lines.setdefault(json.loads(m.group(1)), i)

    common = dict(platform=apple_platform(rel), module=apple_module(rel))
    rows = []
    for key, entry in data.get("strings", {}).items():
        if not key:
            continue
        fields = dict(
            key=key,
            developer_comment=" ".join(entry.get("comment", "").split()),
            translatable="false" if entry.get("shouldTranslate") is False else "true",
            state=entry.get("extractionState", ""),
            **common,
        )
        localization = entry.get("localizations", {}).get(source_lang)
        units = list(xcstrings_units(localization)) if localization else []
        if not units:
            # No explicit source value: the key itself is the English text.
            rows.append(rb.row(file, lines.get(key), text=key, **fields))
        for variant, value in units:
            rows.append(rb.row(file, lines.get(key), variant=".".join(variant), text=value, **fields))
    return rows


def extract_apple():
    rb = RowBuilder("apple")
    rows = []
    files = sorted(
        f for f in rb.base.rglob("*")
        if f.suffix in {".xcstrings", ".strings", ".stringsdict"} and not is_ignored_path(f.relative_to(rb.base))
    )
    for file in files:
        if file.suffix == ".xcstrings":
            rows += extract_xcstrings(rb, file)
        elif file.parent.name == "en.lproj":
            rows += extract_dot_strings(rb, file) if file.suffix == ".strings" else extract_stringsdict(rb, file)
    return rows


# --- Main --------------------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--output", default=str(ROOT / "strings.csv"))
    args = parser.parse_args()

    rows = []
    for name, extract in (("android", extract_android), ("apple", extract_apple)):
        path = SUBMODULES[name]["path"]
        if not path.is_dir() or not any(path.iterdir()):
            sys.exit(f"error: submodule apps/{name} is not checked out (run: git submodule update --init)")
        rows += extract()

    with open(args.output, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)

    counts = {}
    for r in rows:
        counts[r["platform"]] = counts.get(r["platform"], 0) + 1
    summary = ", ".join(f"{p}: {c}" for p, c in sorted(counts.items()))
    print(f"Wrote {len(rows)} rows to {args.output} ({summary})")


if __name__ == "__main__":
    main()
